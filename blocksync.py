#!/usr/bin/env python3
"""Copy large files (VM images, LVM snapshots etc.) between Linux servers.

See README.md for usage and protocol description.
"""

import os
from argparse import ArgumentParser, ArgumentTypeError, Namespace, RawDescriptionHelpFormatter
from asyncio import (
    FIRST_COMPLETED,
    AbstractEventLoop,
    Event,
    Future,
    Queue,
    Semaphore,
    StreamReader,
    StreamWriter,
    Task,
    TaskGroup,
    create_task,
    current_task,
    gather,
    get_running_loop,
    open_connection,
    run,
    start_server,
    timeout,
    wait,
)
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha3_256, sha256
from importlib import import_module
from json import dumps, loads
from logging import INFO, basicConfig, getLogger
from pathlib import Path
from secrets import token_urlsafe
from shlex import quote
from socket import AF_INET, AF_INET6, create_server, getfqdn, has_dualstack_ipv6, socket
from ssl import PROTOCOL_TLS_CLIENT, PROTOCOL_TLS_SERVER, SSLContext, TLSVersion
from stat import S_ISREG
from struct import pack, unpack
from time import monotonic
from types import ModuleType

logger = getLogger('blocksync')

DEFAULT_BLOCK_SIZE = 1024 * 1024
MAX_BLOCK_SIZE = 64 * 1024 * 1024

GREETING = b'blocksync/1\n'
PSK_IDENTITY = 'blocksync'
HASH_SIZE = 32

CODE_UNCHANGED = 0
CODE_RAW = 1
CODE_ZSTD = 2
CODE_ERROR = 3

HANDSHAKE_TIMEOUT = 30
IDLE_TIMEOUT = 5 * 60
PROGRESS_INTERVAL = 5

# Threads are used for everything that releases the GIL: disk I/O, hashing and (de)compression.
WORKER_COUNT = min(32, (os.process_cpu_count() or 1) + 4)

# How many blocks can be "in flight" on one connection, i.e. requested by `retr` and not completely processed yet.
# This is what allows the blocks to be processed in parallel, and it also bounds the memory used for the block data.
MAX_PIPELINED_BLOCKS = 64
MAX_PIPELINED_BYTES = 256 * 1024 * 1024

# Size of the StreamReader buffer; the default 64 kB would mean too many small reads.
STREAM_BUFFER_SIZE = 1024 * 1024


def load_zstd() -> ModuleType | None:
    """
    Import the Zstandard module: from the standard library (Python 3.14+) or the backports.zstd package.

    Returns None if neither is available; compression is then not used.
    """
    for name in ('compression.zstd', 'backports.zstd'):
        try:
            return import_module(name)
        except ImportError:
            pass
    return None


zstd = load_zstd()


class BlocksyncError(Exception):
    """Error that is reported to the user without a traceback."""


class ProtocolError(BlocksyncError):
    """
    The other side sent something that is not allowed by the protocol.
    """


@dataclass
class RetrieveStats:
    """
    Result of retrieve(): how many blocks were unchanged and how many were transferred.
    """

    unchanged_blocks: int = 0
    raw_blocks: int = 0
    compressed_blocks: int = 0
    received_bytes: int = 0  # size of the block data as transferred over the network (i.e. after compression)

    @property
    def transferred_blocks(self) -> int:
        return self.raw_blocks + self.compressed_blocks


# Things shared by serve and retrieve


def tls_key(psk: str | bytes) -> bytes:
    """
    Convert the pre-shared key as given by the user to the actual TLS key.
    """
    stripped = (psk.encode() if isinstance(psk, str) else psk).strip()
    if not stripped:
        raise BlocksyncError('The pre-shared key is empty')
    return sha256(stripped).digest()


def server_ssl_context(psk: str | bytes) -> SSLContext:
    """
    TLS 1.3 context for `serve` that accepts only clients knowing the pre-shared key.
    """
    key = tls_key(psk)
    context = SSLContext(PROTOCOL_TLS_SERVER)
    context.minimum_version = TLSVersion.TLSv1_3
    # No certificate is loaded, so a handshake cannot succeed without the pre-shared key.
    context.set_psk_server_callback(lambda identity: key)
    return context


def client_ssl_context(psk: str | bytes) -> SSLContext:
    """
    TLS 1.3 context for `retrieve` that accepts only a server knowing the pre-shared key.
    """
    key = tls_key(psk)
    context = SSLContext(PROTOCOL_TLS_CLIENT)
    context.minimum_version = TLSVersion.TLSv1_3
    # The server is authenticated by the pre-shared key, there is no host name to check.
    # But verify_mode must stay CERT_REQUIRED: no CA certificates are loaded, so any server
    # that tries to use a certificate instead of the pre-shared key is rejected.
    context.check_hostname = False
    context.set_psk_client_callback(lambda hint: (PSK_IDENTITY, key))
    return context


def read_block(fd: int, offset: int, length: int) -> bytes:
    """
    Read block from the file. Returns less than `length` bytes only if the file ends there.
    """
    data = os.pread(fd, length, offset)
    if len(data) == length or not data:
        return data
    chunks = [data]
    done = len(data)
    while done < length:
        data = os.pread(fd, length - done, offset + done)
        if not data:
            break
        chunks.append(data)
        done += len(data)
    return b''.join(chunks)


def write_block(fd: int, offset: int, data: bytes) -> None:
    """
    Write all the data to the file at the given offset.
    """
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        view = view[written:]
        offset += written


def file_size(fd: int) -> int:
    """
    Size of regular file or block device (st_size is zero for block devices).
    """
    return os.lseek(fd, 0, os.SEEK_END)


async def read_exactly(reader: StreamReader, length: int) -> bytes:
    """
    Like reader.readexactly(), but fails if no data are received for IDLE_TIMEOUT seconds.

    The timeout applies to each chunk of data, not to the whole read,
    so that a large block can be received over a slow connection.
    """
    chunks = []
    remaining = length
    while remaining:
        try:
            async with timeout(IDLE_TIMEOUT):
                chunk = await reader.read(remaining)
        except TimeoutError:
            raise BlocksyncError(f'No data received for {IDLE_TIMEOUT} seconds') from None
        if not chunk:
            raise EOFError('Connection closed by the other side')
        chunks.append(chunk)
        remaining -= len(chunk)
    return chunks[0] if len(chunks) == 1 else b''.join(chunks)


async def close_stream(writer: StreamWriter, *, graceful: bool) -> None:
    """
    Close the connection.

    graceful=True is for a finished transfer: buffered data are sent and the TLS session is shut down properly
    (waiting at most 10 seconds). graceful=False is for errors: the connection is dropped immediately.
    """
    if not graceful:
        writer.transport.abort()
        return
    writer.close()
    with suppress(OSError, TimeoutError):
        async with timeout(10):
            await writer.wait_closed()


def format_address(address: object) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f'[{address[0]}]:{address[1]}' if ':' in str(address[0]) else f'{address[0]}:{address[1]}'
    return str(address)


def mib(size: int) -> str:
    return f'{size / 2**20:.1f} MiB'


# Serve


@dataclass
class Response:
    """
    Response of the server to one command, ready to be sent.
    """

    chunks: tuple[bytes, ...]
    budget: int = 0  # how many bytes to return to the ByteBudget after the response is sent
    final: bool = False  # close the connection after sending this response
    transfer_completed: bool = False


class ByteBudget:
    """
    Limits the total size of blocks being processed. Something like a semaphore counting bytes.

    A request larger than the whole limit is allowed when nothing else is being processed.
    Only one task may call acquire().
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.waiter: Future[None] | None = None

    async def acquire(self, size: int) -> None:
        while self.used and self.used + size > self.limit:
            self.waiter = get_running_loop().create_future()
            await self.waiter
        self.used += size

    def release(self, size: int) -> None:
        self.used -= size
        if self.waiter and not self.waiter.done():
            self.waiter.set_result(None)


def process_retr(fd: int, offset: int, length: int, dst_hash: bytes, use_zstd: bool) -> Response:
    """
    Compute the response to a `retr` command. Runs in a worker thread.
    """
    try:
        data = read_block(fd, offset, length)
        if len(data) != length:
            raise OSError(f'short read: {len(data)} bytes instead of {length}')
    except OSError as e:
        logger.error('Failed to read source block at offset %d: %s', offset, e)
        message = f'Failed to read block at offset {offset}: {e}'.encode()
        return Response((pack('>BI', CODE_ERROR, len(message)), message), budget=length, final=True)
    if sha3_256(data).digest() == dst_hash:
        return Response((pack('>BI', CODE_UNCHANGED, 0),), budget=length)
    if use_zstd and zstd:
        compressed: bytes = zstd.compress(data)
        if len(compressed) < length:
            return Response((pack('>BI', CODE_ZSTD, len(compressed)), compressed), budget=length)
    return Response((pack('>BI', CODE_RAW, length), data), budget=length)


class ServerConnection:
    """
    One client connection of `serve`.

    Commands are read by one task and responses are written by another one. Between them is a queue
    of futures of the responses, in the order of the commands. The `retr` commands are processed
    in worker threads, so several blocks are read, hashed and compressed at the same time,
    while the responses are still sent in the right order.
    """

    def __init__(self, reader: StreamReader, writer: StreamWriter, src_fd: int, src_size: int, pool: ThreadPoolExecutor) -> None:
        self.reader = reader
        self.writer = writer
        self.src_fd = src_fd
        self.src_size = src_size
        self.pool = pool
        self.loop = get_running_loop()
        self.responses: Queue[Future[Response]] = Queue(MAX_PIPELINED_BLOCKS)
        self.budget = ByteBudget(MAX_PIPELINED_BYTES)
        self.use_zstd = zstd is not None
        self.last_activity = monotonic()

    async def run(self) -> bool:
        """
        Returns True if the transfer was completed (`done` command), False if the connection was closed for an error.
        """
        read_task = create_task(self.read_commands())
        write_task = create_task(self.write_responses())
        try:
            await wait([read_task, write_task], return_when=FIRST_COMPLETED)
            if read_task.done():
                # raises if reading failed; otherwise all commands were read and the rest of responses needs to be sent
                read_task.result()
            return await write_task
        finally:
            read_task.cancel()
            write_task.cancel()
            await gather(read_task, write_task, return_exceptions=True)
            while not self.responses.empty():
                self.responses.get_nowait().cancel()

    async def read_commands(self) -> None:
        """
        Read commands and put (futures of) their responses to the queue, until `done` is received.

        Raises ProtocolError for an invalid command; the connection is then closed without sending anything more.
        `retr` commands are only validated here, the work is done by process_retr() in a worker thread.
        """
        while True:
            command = await self.read_command()
            if command == b'meta':
                data = dumps({'size': self.src_size}).encode()
                await self.respond(Response((pack('>I', len(data)), data)))
            elif command == b'nozs':
                self.use_zstd = False
                await self.respond(Response((b'1',)))
            elif command == b'retr':
                offset, length, dst_hash = unpack(f'>QI{HASH_SIZE}s', await read_exactly(self.reader, 12 + HASH_SIZE))
                if not 0 < length <= MAX_BLOCK_SIZE:
                    raise ProtocolError(f'retr with invalid length {length}')
                if offset + length > self.src_size:
                    raise ProtocolError(f'retr beyond the end of the source file (offset {offset}, length {length})')
                # Backpressure: stop reading commands when too much data is waiting to be processed and sent
                await self.budget.acquire(length)
                await self.responses.put(self.loop.run_in_executor(self.pool, process_retr, self.src_fd, offset, length, dst_hash, self.use_zstd))
            elif command == b'done':
                await self.respond(Response((b'1',), final=True, transfer_completed=True))
                return
            else:
                raise ProtocolError(f'unknown command {command!r}')

    async def read_command(self) -> bytes:
        """
        Read the 4 bytes of a command name. Fails if no command is received for IDLE_TIMEOUT seconds.
        """
        while True:
            try:
                async with timeout(IDLE_TIMEOUT):
                    # Cancelling readexactly() does not lose any data
                    return await self.reader.readexactly(4)
            except TimeoutError:
                # The client cannot be blamed if it is waiting for us (or for the network) with its send window full
                if monotonic() - self.last_activity >= IDLE_TIMEOUT:
                    raise BlocksyncError(f'No command received for {IDLE_TIMEOUT} seconds') from None

    async def respond(self, response: Response) -> None:
        """
        Enqueue a response that is already known (i.e. not computed in a worker thread).
        """
        future: Future[Response] = self.loop.create_future()
        future.set_result(response)
        await self.responses.put(future)

    async def write_responses(self) -> bool:
        """
        Send responses in the order of the commands, waiting for each one to be computed.

        Ends after a final response (`done` or a read error) and returns True if it was `done`.
        """
        while True:
            response = await (await self.responses.get())
            for chunk in response.chunks:
                self.writer.write(chunk)
            await self.writer.drain()
            self.budget.release(response.budget)
            self.last_activity = monotonic()
            if response.final:
                return response.transfer_completed


class Server:
    """
    State of `serve` shared by all connections: the source file, the thread pool and whether the transfer was completed.
    """

    def __init__(self, src_fd: int, src_size: int, ssl_context: SSLContext | None, pool: ThreadPoolExecutor) -> None:
        self.src_fd = src_fd
        self.src_size = src_size
        self.ssl_context = ssl_context
        self.pool = pool
        self.tasks: set[Task[object]] = set()
        self.completed = Event()

    async def handle_connection(self, reader: StreamReader, writer: StreamWriter) -> None:
        """
        Handle one client connection from accept to close. Called by asyncio as a separate task for each connection.

        A failed connection is only logged, the server keeps running. A connection that ends with `done` sets `self.completed`,
        which makes serve() return.
        """
        task = current_task()
        assert task
        self.tasks.add(task)
        peer = format_address(writer.get_extra_info('peername'))
        graceful = False
        try:
            logger.info('Connection from %s', peer)
            if self.ssl_context:
                # TLS is started here and not by start_server() so that failed handshakes can be logged
                await writer.start_tls(self.ssl_context, ssl_handshake_timeout=HANDSHAKE_TIMEOUT)
            writer.write(GREETING)
            graceful = await ServerConnection(reader, writer, self.src_fd, self.src_size, self.pool).run()
            if graceful:
                # Make sure the response to `done` is delivered before the server exits
                await close_stream(writer, graceful=True)
                logger.info('Transfer completed by %s', peer)
                self.completed.set()
        except (BlocksyncError, OSError, EOFError, TimeoutError) as e:
            logger.warning('Connection from %s failed: %s', peer, e or type(e).__name__)
        finally:
            self.tasks.discard(task)
            if not graceful:
                await close_stream(writer, graceful=False)


def create_listening_socket(bind: str | None, port: int) -> socket:
    """
    Create the listening socket of `serve`.

    Without `bind`, one dual-stack socket accepts both IPv4 and IPv6 connections,
    so that port 0 (random port) gives the same port number for both protocols.
    """
    if bind is None:
        if has_dualstack_ipv6():
            return create_server(('::', port), family=AF_INET6, dualstack_ipv6=True)
        return create_server(('0.0.0.0', port), family=AF_INET)
    return create_server((bind, port), family=AF_INET6 if ':' in bind else AF_INET)


async def serve(
    *,
    src_file: Path,
    port: int,
    psk: str | bytes | None,
    bind: str | None = None,
    on_listening: Callable[[int], object] | None = None,
) -> None:
    """
    Serve the source file until one client completes the transfer.

    psk=None means no TLS (--insecure). port=0 means random port; on_listening is called with the actual port.
    """
    ssl_context = server_ssl_context(psk) if psk is not None else None
    src_fd = os.open(src_file, os.O_RDONLY)
    try:
        src_size = file_size(src_fd)
        with ThreadPoolExecutor(WORKER_COUNT, thread_name_prefix='blocksync') as pool:
            server = Server(src_fd, src_size, ssl_context, pool)
            listener = await start_server(server.handle_connection, sock=create_listening_socket(bind, port), limit=STREAM_BUFFER_SIZE)
            try:
                actual_port: int = listener.sockets[0].getsockname()[1]
                logger.info('Serving %s (%s) on port %d', src_file, mib(src_size), actual_port)
                if on_listening:
                    on_listening(actual_port)
                await server.completed.wait()
            finally:
                listener.close()
                tasks = list(server.tasks)
                for task in tasks:
                    task.cancel()
                await gather(*tasks, return_exceptions=True)
    finally:
        os.close(src_fd)


# Retrieve


def open_dst(dst_file: Path, src_size: int) -> int:
    """
    Open the destination for reading and writing and return the file descriptor.

    A regular file is created if it does not exist and it is never truncated here.
    Anything else (block device) must be at least as large as the source; it is checked before anything is written.
    """
    fd = os.open(dst_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            dst_size = file_size(fd)
            if dst_size < src_size:
                raise BlocksyncError(f'Destination {dst_file} is too small: {dst_size} bytes, the source has {src_size} bytes')
    except BaseException:
        os.close(fd)
        raise
    return fd


def finish_dst(fd: int, src_size: int) -> None:
    """
    Finish the destination after all blocks were written: cut off a regular file longer than the source and fsync.
    """
    if S_ISREG(os.fstat(fd).st_mode):
        os.ftruncate(fd, src_size)
    os.fsync(fd)


def read_and_hash_block(fd: int, offset: int, length: int) -> bytes:
    """
    Return SHA3-256 of the destination block. Runs in a worker thread.

    If the destination ends before the end of the block, the hash is computed from the data that is there;
    it then does not match the source block and the block is transferred.
    """
    return sha3_256(read_block(fd, offset, length)).digest()


def store_block(fd: int, offset: int, length: int, data: bytes, compressed: bool) -> None:
    """
    Decompress the received block (if compressed), check its length and write it to the destination. Runs in a worker thread.
    """
    if compressed:
        assert zstd
        data = zstd.decompress(data)
    if len(data) != length:
        raise ProtocolError(f'Received {len(data)} bytes for block at offset {offset}, expected {length}')
    write_block(fd, offset, data)


def first_leaf_exception(group: BaseExceptionGroup) -> BaseException:
    """
    Return the first exception of an exception group that is not a group itself.
    """
    e: BaseException = group
    while isinstance(e, BaseExceptionGroup):
        e = e.exceptions[0]
    return e


class Retrieval:
    """
    The main part of `retrieve`: synchronization of all blocks over one connection.

    It is a pipeline:

    1. Destination blocks are read and hashed in worker threads, several blocks ahead.
    2. `retr` commands are sent in the order of the blocks; at most `window` blocks may be in flight.
    3. Responses are received (in the same order) and changed blocks are decompressed and written in worker threads.
    """

    def __init__(
        self,
        reader: StreamReader,
        writer: StreamWriter,
        dst_fd: int,
        src_size: int,
        block_size: int,
        pool: ThreadPoolExecutor,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.dst_fd = dst_fd
        self.src_size = src_size
        self.block_size = block_size
        self.pool = pool
        self.loop: AbstractEventLoop = get_running_loop()
        self.block_count = -(-src_size // block_size)
        window = max(1, min(MAX_PIPELINED_BLOCKS, MAX_PIPELINED_BYTES // block_size))
        self.window = Semaphore(window)
        self.in_flight: Queue[tuple[int, int]] = Queue()  # (offset, length) of blocks requested from the server
        self.stats = RetrieveStats()
        self.start_time = self.last_progress_time = monotonic()
        self.done_bytes = 0

    def blocks(self) -> Iterator[tuple[int, int]]:
        """
        Offsets and lengths of all blocks. Only the last block may be shorter than the block size.
        """
        for offset in range(0, self.src_size, self.block_size):
            yield offset, min(self.block_size, self.src_size - offset)

    async def run(self) -> RetrieveStats:
        """
        Synchronize all blocks. On return, all changed blocks are written to the destination (but not synced yet).
        """
        try:
            async with TaskGroup() as group:
                group.create_task(self.send_requests())
                group.create_task(self.receive_responses())
        except BaseExceptionGroup as group_error:
            # Report the original error, not the group
            raise first_leaf_exception(group_error) from None
        return self.stats

    async def send_requests(self) -> None:
        """
        Hash destination blocks in worker threads and send `retr` commands in the order of the blocks.

        Hashing runs up to 2 * WORKER_COUNT blocks ahead of sending. Sending waits while the window is full,
        i.e. while too many blocks are waiting for a response or for being written.
        """
        hashing: Queue[tuple[int, int, Future[bytes]]] = Queue(2 * WORKER_COUNT)

        async def start_hashing() -> None:
            for offset, length in self.blocks():
                await hashing.put((offset, length, self.loop.run_in_executor(self.pool, read_and_hash_block, self.dst_fd, offset, length)))

        hashing_task = create_task(start_hashing())
        try:
            for _ in range(self.block_count):
                offset, length, hash_future = await hashing.get()
                block_hash = await hash_future
                await self.window.acquire()
                self.in_flight.put_nowait((offset, length))
                self.writer.write(b'retr' + pack('>QI', offset, length) + block_hash)
                await self.writer.drain()
            await hashing_task
        finally:
            hashing_task.cancel()
            while not hashing.empty():
                hashing.get_nowait()[2].cancel()

    async def receive_responses(self) -> None:
        """
        Receive the response to each `retr` command and start writing of changed blocks in worker threads.

        Returns after all blocks are written.
        """
        async with TaskGroup() as writes:
            for _ in range(self.block_count):
                # The idle timeout is not running while there is nothing to wait for
                offset, length = await self.in_flight.get()
                code, data_length = unpack('>BI', await read_exactly(self.reader, 5))
                if code == CODE_UNCHANGED:
                    if data_length:
                        raise ProtocolError('Received data for an unchanged block')
                    self.stats.unchanged_blocks += 1
                    self.block_done(length)
                elif code == CODE_RAW:
                    if data_length != length:
                        raise ProtocolError(f'Received {data_length} bytes for block at offset {offset}, expected {length}')
                    data = await read_exactly(self.reader, data_length)
                    writes.create_task(self.store(offset, length, data, compressed=False))
                elif code == CODE_ZSTD:
                    if not zstd:
                        raise ProtocolError('Received compressed block, but Zstandard is not available')
                    if data_length >= length:
                        raise ProtocolError(f'Received {data_length} compressed bytes for block of {length} bytes')
                    data = await read_exactly(self.reader, data_length)
                    writes.create_task(self.store(offset, length, data, compressed=True))
                elif code == CODE_ERROR:
                    if data_length > 65536:
                        raise ProtocolError('Received too long error message')
                    message = (await read_exactly(self.reader, data_length)).decode(errors='replace')
                    raise BlocksyncError(f'Server error: {message}')
                else:
                    raise ProtocolError(f'Received unknown response code {code}')

    async def store(self, offset: int, length: int, data: bytes, compressed: bool) -> None:
        """
        Write one received block (in a worker thread) and update the stats.
        """
        await self.loop.run_in_executor(self.pool, store_block, self.dst_fd, offset, length, data, compressed)
        if compressed:
            self.stats.compressed_blocks += 1
        else:
            self.stats.raw_blocks += 1
        self.stats.received_bytes += len(data)
        self.block_done(length)

    def block_done(self, length: int) -> None:
        """
        Called when a block is completely processed: frees its place in the window and logs progress from time to time.
        """
        self.window.release()
        self.done_bytes += length
        now = monotonic()
        if now - self.last_progress_time >= PROGRESS_INTERVAL:
            self.last_progress_time = now
            logger.info(
                '%5.1f %% - %s of %s, %d blocks transferred (%s), %s/s',
                100 * self.done_bytes / self.src_size,
                mib(self.done_bytes),
                mib(self.src_size),
                self.stats.transferred_blocks,
                mib(self.stats.received_bytes),
                mib(int(self.done_bytes / (now - self.start_time))),
            )


async def retrieve(
    *,
    host: str,
    port: int,
    psk: str | bytes | None,
    dst_file: Path,
    block_size: int = DEFAULT_BLOCK_SIZE,
) -> RetrieveStats:
    """
    Make the destination file identical to the file served by serve() at the given host and port.

    psk=None means no TLS (--insecure).
    """
    start_time = monotonic()
    if psk is None:
        reader, writer = await open_connection(host, port, limit=STREAM_BUFFER_SIZE)
    else:
        reader, writer = await open_connection(
            host, port, limit=STREAM_BUFFER_SIZE, ssl=client_ssl_context(psk), ssl_handshake_timeout=HANDSHAKE_TIMEOUT
        )
    graceful = False
    try:
        if await read_exactly(reader, len(GREETING)) != GREETING:
            raise ProtocolError('The server is not blocksync or it uses an unsupported protocol version')
        writer.write(b'meta')
        if not zstd:
            writer.write(b'nozs')
        (meta_length,) = unpack('>I', await read_exactly(reader, 4))
        src_size = loads(await read_exactly(reader, meta_length))['size']
        if not isinstance(src_size, int) or src_size < 0:
            raise ProtocolError(f'Received invalid source file size: {src_size!r}')
        if not zstd and await read_exactly(reader, 1) != b'1':
            raise ProtocolError('Unexpected response to nozs')
        logger.info('Source file size: %s', mib(src_size))

        dst_fd = open_dst(dst_file, src_size)
        try:
            with ThreadPoolExecutor(WORKER_COUNT, thread_name_prefix='blocksync') as pool:
                stats = await Retrieval(reader, writer, dst_fd, src_size, block_size, pool).run()
                await get_running_loop().run_in_executor(pool, finish_dst, dst_fd, src_size)
        finally:
            os.close(dst_fd)

        writer.write(b'done')
        if await read_exactly(reader, 1) != b'1':
            raise ProtocolError('Unexpected response to done')
        graceful = True
    finally:
        await close_stream(writer, graceful=graceful)
    logger.info(
        'Done in %.1f s: %d blocks unchanged, %d blocks transferred (%d compressed, %s received)',
        monotonic() - start_time,
        stats.unchanged_blocks,
        stats.transferred_blocks,
        stats.compressed_blocks,
        mib(stats.received_bytes),
    )
    return stats


# Command line


def build_parser() -> ArgumentParser:
    """
    Command line parser with the `serve` and `retrieve` subcommands. See README.md for the options.
    """
    parser = ArgumentParser(prog='blocksync.py', description=__doc__, formatter_class=RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest='command', required=True)

    serve = subparsers.add_parser('serve', help='serve source file to clients')
    serve.add_argument('--port', type=port_number, help='TCP port to listen on (default: random unused port)')
    serve.add_argument('--bind', metavar='ADDRESS', help='IP address to listen on (default: all interfaces, IPv4 and IPv6)')
    add_key_options(serve, required=False)
    serve.add_argument('--src-file', type=Path, required=True, help='source file or block device')

    retrieve = subparsers.add_parser('retrieve', help='retrieve file from server')
    retrieve.add_argument('--host', required=True, help='server host name or address')
    retrieve.add_argument('--port', type=port_number, required=True, help='server TCP port')
    add_key_options(retrieve, required=True)
    retrieve.add_argument('--dst-file', type=Path, required=True, help='destination file or block device')
    retrieve.add_argument(
        '--block-size',
        type=block_size,
        default=DEFAULT_BLOCK_SIZE,
        help=f'block size in bytes (default: {DEFAULT_BLOCK_SIZE}, max: {MAX_BLOCK_SIZE})',
    )

    return parser


def add_key_options(parser: ArgumentParser, *, required: bool) -> None:
    """
    Add the mutually exclusive options --psk, --psk-file and --insecure.
    """
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument('--psk', metavar='VALUE', help='pre-shared key value')
    group.add_argument('--psk-file', metavar='PATH', type=Path, help='file containing the pre-shared key')
    group.add_argument('--insecure', action='store_true', help='do not use TLS-PSK (use only over secure connections, e.g. VPN)')


def port_number(value: str) -> int:
    """
    Argparse `type=` converter for `--port`.

    Converts the option value to int and checks it is a valid TCP port (1-65535).
    Doing the check here means an invalid port is reported as a normal usage error
    (with exit code 2) when the arguments are parsed, not as a confusing error from
    the socket layer later.

    Port 0 is rejected on purpose: for `serve`, a random port is chosen by omitting
    `--port`; for `retrieve`, connecting to port 0 makes no sense.
    """
    port = int(value)
    if not 0 < port < 65536:
        raise ArgumentTypeError(f'port must be between 1 and 65535: {value}')
    return port


def block_size(value: str) -> int:
    """
    Argparse `type=` converter for `retrieve --block-size`.

    Converts the option value to int and checks it is between 1 byte and MAX_BLOCK_SIZE.
    The upper limit comes from the protocol: the `retr` command allows blocks of
    at most 64 MiB (see README.md), so a larger block size could never work.
    A zero or negative block size would make no progress through the file.
    Checking it here reports the problem as a usage error before any connection is made.
    """
    size = int(value)
    if not 0 < size <= MAX_BLOCK_SIZE:
        raise ArgumentTypeError(f'block size must be between 1 and {MAX_BLOCK_SIZE}: {value}')
    return size


def psk_from_args(args: Namespace) -> str | bytes | None:
    """
    Returns None if neither --psk nor --psk-file was given.
    """
    if args.psk is not None:
        return str(args.psk)
    if args.psk_file is not None:
        return Path(args.psk_file).read_bytes()
    return None


async def serve_command(args: Namespace) -> None:
    """
    The `serve` subcommand: run serve() and print a ready-to-use `retrieve` command to stdout once listening.

    If no key option is given, a random pre-shared key is generated (and printed as a part of the command).
    """
    psk = psk_from_args(args)
    if psk is not None:
        tls_key(psk)  # fail early if the key is empty
    if args.psk_file is not None:
        key_option = f'--psk-file {quote(str(args.psk_file))}'
    elif args.insecure:
        key_option = '--insecure'
    else:
        if psk is None:
            psk = token_urlsafe(32)
        key_option = f'--psk {quote(str(psk))}'

    def print_retrieve_command(port: int) -> None:
        lines = [
            f'{quote(Path(__file__).name)} retrieve',
            f'--host {quote(getfqdn())}',
            f'--port {port}',
            key_option,
            f'--dst-file {quote(str(args.src_file))}',
        ]
        print('Run this on the destination side:\n', flush=True)
        print(' \\\n    '.join(lines), end='\n\n', flush=True)

    await serve(src_file=args.src_file, port=args.port or 0, psk=psk, bind=args.bind, on_listening=print_retrieve_command)


async def retrieve_command(args: Namespace) -> None:
    """
    The `retrieve` subcommand.
    """
    await retrieve(host=args.host, port=args.port, psk=psk_from_args(args), dst_file=args.dst_file, block_size=args.block_size)


def main(argv: Sequence[str] | None = None) -> int:
    """
    Command line entry point. Returns the exit code: 0 for success, 1 for error (which is logged to stderr).

    Invalid options end with SystemExit(2) raised by argparse.
    """
    args = build_parser().parse_args(argv)
    basicConfig(level=INFO, format='%(asctime)s %(levelname)s: %(message)s')
    try:
        run(serve_command(args) if args.command == 'serve' else retrieve_command(args))
    except KeyboardInterrupt:
        logger.error('Interrupted')
        return 1
    except (BlocksyncError, OSError, EOFError) as e:
        logger.error('%s', e or type(e).__name__)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
