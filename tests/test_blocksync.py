"""
Tests of serve() and retrieve() running in-process over localhost.

API expected by these tests:

- `async def serve(*, src_file: Path, port: int, psk: str | None, ready: Callable[[int], object] | None = None) -> None`
  - `psk=None` means `--insecure`
  - `port=0` chooses a random port; `ready` is called with the actual port once listening
  - returns after one successful transfer
- `async def retrieve(*, host: str, port: int, psk: str | None, target_file: Path, block_size: int = ...) -> RetrieveStats`
- `RetrieveStats` with attributes `unchanged_blocks`, `raw_blocks`, `compressed_blocks`
"""

from asyncio import FIRST_COMPLETED, Future, StreamReader, StreamWriter, Task, create_task, get_running_loop, open_connection, run, timeout, wait
from collections.abc import Callable, Coroutine
from contextlib import suppress
from hashlib import sha3_256
from importlib import import_module
from json import loads
from pathlib import Path
from random import Random
from struct import pack, unpack
from types import ModuleType
from typing import Any

from pytest import mark, raises

from blocksync import RetrieveStats, retrieve, serve

BLOCK_SIZE = 4096
TIMEOUT = 10

# 51 blocks (more than retrieve sends ahead), the last one is partial
SOURCE = Random(1).randbytes(50 * BLOCK_SIZE + 1000)
SOURCE_BLOCKS = 51


def load_zstd() -> ModuleType | None:
    for name in ('compression.zstd', 'backports.zstd'):
        try:
            return import_module(name)
        except ImportError:
            pass
    return None


zstd = load_zstd()


def run_with_timeout[T](coro: Coroutine[Any, Any, T]) -> T:
    async def with_timeout() -> T:
        async with timeout(TIMEOUT):
            return await coro

    return run(with_timeout())


async def start_server(src_file: Path, psk: str | None) -> tuple[Task[None], int]:
    port_future: Future[int] = get_running_loop().create_future()
    server_task = create_task(serve(src_file=src_file, port=0, psk=psk, ready=port_future.set_result))
    await wait([port_future, server_task], return_when=FIRST_COMPLETED)
    if server_task.done():
        server_task.result()
        raise AssertionError('serve() returned before listening')
    return server_task, port_future.result()


async def transfer(src_file: Path, target_file: Path, psk: str | None = 'secret') -> RetrieveStats:
    server_task, port = await start_server(src_file, psk)
    try:
        stats = await retrieve(host='127.0.0.1', port=port, psk=psk, target_file=target_file, block_size=BLOCK_SIZE)
        # serve exits after one successful transfer
        await server_task
    finally:
        server_task.cancel()
    return stats


def changed(data: bytes, offset: int) -> bytes:
    return data[:offset] + bytes([data[offset] ^ 0xFF]) + data[offset + 1 :]


@mark.parametrize(
    ('make_target', 'expected_transferred'),
    [
        (None, SOURCE_BLOCKS),
        (lambda s: b'', SOURCE_BLOCKS),
        (lambda s: s, 0),
        (lambda s: changed(s, 20 * BLOCK_SIZE + 7), 1),
        (lambda s: changed(s, len(s) - 1), 1),
        (lambda s: s[: 30 * BLOCK_SIZE + 100], 21),
        (lambda s: s + Random(2).randbytes(5000), 0),
    ],
    ids=['missing', 'empty', 'identical', 'byte-changed-in-middle', 'last-partial-block-changed', 'shorter', 'longer'],
)
def test_transfer(tmp_path: Path, make_target: Callable[[bytes], bytes] | None, expected_transferred: int) -> None:
    src, target = tmp_path / 'src', tmp_path / 'target'
    src.write_bytes(SOURCE)
    if make_target:
        target.write_bytes(make_target(SOURCE))

    stats = run_with_timeout(transfer(src, target))

    assert target.read_bytes() == SOURCE
    assert stats.raw_blocks + stats.compressed_blocks == expected_transferred
    assert stats.unchanged_blocks == SOURCE_BLOCKS - expected_transferred


def test_insecure_transfer(tmp_path: Path) -> None:
    src, target = tmp_path / 'src', tmp_path / 'target'
    src.write_bytes(SOURCE)
    target.write_bytes(changed(SOURCE, 7))

    stats = run_with_timeout(transfer(src, target, psk=None))

    assert target.read_bytes() == SOURCE
    assert stats.raw_blocks + stats.compressed_blocks == 1


def test_zero_blocks_are_compressed_if_zstd_is_available(tmp_path: Path) -> None:
    src, target = tmp_path / 'src', tmp_path / 'target'
    src.write_bytes(bytes(10 * BLOCK_SIZE))

    stats = run_with_timeout(transfer(src, target))

    assert target.read_bytes() == bytes(10 * BLOCK_SIZE)
    if zstd:
        assert (stats.compressed_blocks, stats.raw_blocks) == (10, 0)
    else:
        assert (stats.compressed_blocks, stats.raw_blocks) == (0, 10)


def test_wrong_key_fails_and_server_keeps_waiting(tmp_path: Path) -> None:
    src, target = tmp_path / 'src', tmp_path / 'target'
    src.write_bytes(SOURCE)

    async def scenario() -> None:
        server_task, port = await start_server(src, 'right')
        try:
            with raises((OSError, EOFError)):
                await retrieve(host='127.0.0.1', port=port, psk='wrong', target_file=target, block_size=BLOCK_SIZE)
            await retrieve(host='127.0.0.1', port=port, psk='right', target_file=target, block_size=BLOCK_SIZE)
            await server_task
        finally:
            server_task.cancel()

    run_with_timeout(scenario())
    assert target.read_bytes() == SOURCE


# Raw protocol tests - check the wire format as documented in README.md,
# so that the same mistake on both sides (e.g. byte order) is not hidden.


async def connect(port: int) -> tuple[StreamReader, StreamWriter]:
    reader, writer = await open_connection('127.0.0.1', port)
    assert await reader.readexactly(12) == b'blocksync/1\n'
    return reader, writer


def retr(offset: int, length: int, hash: bytes) -> bytes:
    return b'retr' + pack('>QI', offset, length) + hash


async def read_retr_response(reader: StreamReader) -> tuple[int, bytes]:
    code, length = unpack('>BI', await reader.readexactly(5))
    return code, await reader.readexactly(length)


def test_protocol_pipelined_commands(tmp_path: Path) -> None:
    random_block = Random(3).randbytes(BLOCK_SIZE)
    zero_block = bytes(BLOCK_SIZE)
    src = tmp_path / 'src'
    src.write_bytes(random_block + zero_block)

    async def scenario() -> None:
        server_task, port = await start_server(src, psk=None)
        try:
            reader, writer = await connect(port)
            writer.write(b'meta')
            writer.write(retr(0, BLOCK_SIZE, sha3_256(random_block).digest()))
            writer.write(retr(0, BLOCK_SIZE, bytes(32)))
            writer.write(retr(BLOCK_SIZE, BLOCK_SIZE, bytes(32)))
            writer.write(b'done')
            await writer.drain()

            (length,) = unpack('>I', await reader.readexactly(4))
            assert loads(await reader.readexactly(length))['size'] == 2 * BLOCK_SIZE

            assert await read_retr_response(reader) == (0, b'')
            assert await read_retr_response(reader) == (1, random_block)
            code, data = await read_retr_response(reader)
            if zstd:
                assert code == 2
                assert zstd.decompress(data) == zero_block
            else:
                assert (code, data) == (1, zero_block)

            assert await reader.readexactly(1) == b'1'
            writer.close()
            await server_task
        finally:
            server_task.cancel()

    run_with_timeout(scenario())


def test_protocol_retr_beyond_end_closes_connection_and_server_keeps_waiting(tmp_path: Path) -> None:
    src, target = tmp_path / 'src', tmp_path / 'target'
    src.write_bytes(SOURCE)

    async def scenario() -> None:
        server_task, port = await start_server(src, psk=None)
        try:
            reader, writer = await connect(port)
            writer.write(retr(len(SOURCE) - 10, BLOCK_SIZE, bytes(32)))
            await writer.drain()
            with suppress(ConnectionResetError):
                assert await reader.read() == b''
            writer.close()

            await retrieve(host='127.0.0.1', port=port, psk=None, target_file=target, block_size=BLOCK_SIZE)
            await server_task
        finally:
            server_task.cancel()

    run_with_timeout(scenario())
    assert target.read_bytes() == SOURCE
