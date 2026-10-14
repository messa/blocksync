# blocksync

Copy large files (VM images, LVM snapshots etc.) between Linux servers.

Features:

- Encryption and authentication using TLS-PSK
- Compression using Zstandard
- Incremental - only blocks with differing hash are transferred
- Python standard library only - no libraries or venv required
- Single `.py` file - trivial to copy or deploy wherever you need

Python version support:

- TLS-PSK support needs Python 3.13+
- Zstandard support needs Python 3.14+, or 3.13 with [backports.zstd](https://pypi.org/project/backports.zstd/)
- Python versions 3.12 and older are not supported
  - Use `uv run --python 3.14 blocksync.py ...` to run this script with managed Python

## Usage

Simplest use - let `serve` choose a random port and generate a random pre-shared key:

```shell
# Source side
blocksync.py serve --src-file /dev/vg0/vm_example_root
```

`serve` prints a ready-to-use `retrieve` command, for example:

```shell
# Target side
blocksync.py retrieve \
    --host source.example.com \
    --port 40123 \
    --psk 'z8kQ...' \
    --target-file /dev/vg0/vm_example_root
```

With an explicit port and a pre-shared key file:

```shell
# Create pre-shared key file and copy it to both sides
openssl rand -base64 512 > pre-shared-key

# Source side
blocksync.py serve \
    --port 9001 \
    --psk-file pre-shared-key \
    --src-file /dev/vg0/vm_example_root

# Target side
blocksync.py retrieve \
    --host source.example.com \
    --port 9001 \
    --psk-file pre-shared-key \
    --target-file /dev/vg0/vm_example_root
```

## Low-level details

### Pre-shared key

The key is given by `--psk VALUE` or `--psk-file PATH`.
Leading and trailing whitespace is stripped; the actual TLS key is SHA-256 of the remaining bytes.
So `--psk VALUE` and `--psk-file` with a file containing `VALUE` are equivalent.

TLS 1.3 is used, with PSK identity `blocksync`.

If you specify `--insecure`, TLS-PSK is not used and no pre-shared key is needed.
`--insecure` must be used on both sides.
Use `--insecure` only over secure connections, e.g. VPN.

Options `--psk`, `--psk-file` and `--insecure` are mutually exclusive.

### `serve`

Listens on the given TCP port (all interfaces, IPv4 and IPv6).
Incoming connections are wrapped in TLS-PSK using the pre-shared key.

If you don't provide `--port`, a random unused TCP port is chosen.

If you don't provide `--psk`, `--psk-file` or `--insecure`, a random key is generated.

On start, `serve` prints an example `retrieve` command to stdout.
It contains the host name (FQDN) of the server, the port, the key option (including the generated key value) and the `--src-file` path as `--target-file`.
Logs go to stderr.

`serve` is single-purpose: it exits after one successful transfer (`done` command).
Failed connections (wrong key, dropped connection, ...) are logged and `serve` keeps waiting, so `retrieve` can be simply run again.

#### Protocol

After the client connects (and the TLS-PSK handshake succeeds, unless `--insecure` is used):

1. Server sends `blocksync/1\n`
2. Client sends commands and server responds to them in the same order.
   Client does not need to wait for a response before sending the next command (pipelining).

Each command is 4 ASCII bytes followed by its params.
All integers are unsigned big-endian.

Commands:

- `meta`
  - Retrieve source file metadata
  - Params: none
  - Response:
    - `length` (uint32): size of `data` in bytes
    - `data` (of given `length`): UTF-8 JSON-encoded object
      - `"size"`: size of the source file in bytes
- `retr`
  - Retrieve source file block, if its hash differs
  - Params:
    - `offset` (uint64): offset of the block in the file (in bytes)
    - `length` (uint32): length of the block (in bytes), at most 64 MiB
    - `hash` (32 bytes): SHA3-256 of the target block contents
  - Response:
    - `code` (1 byte)
      - `\x00`: hash matches, no data need to be returned
      - `\x01`: hash mismatch, returning raw block data
      - `\x02`: hash mismatch, returning Zstandard-compressed block data
    - `length` (uint32): size of `data` in bytes
    - `data` (of given `length`): raw or zstd-compressed contents of the block, or nothing if the hash matches
  - Server returns compressed data only if it is smaller than raw data.
  - The block must not extend beyond the end of the source file; otherwise the server closes the connection.
- `done`
  - Transfer is completed
  - Params: none
  - Response:
    - `ok` (1 byte): the value `b'1'`
    - Both sides close the TCP connection.

If running under Python 3.13 and `backports.zstd` is not present, Zstandard compression is not used.

### `retrieve`

Options `--host`, `--port` and one of `--psk`, `--psk-file` or `--insecure` are required.

1. Connect to the server and receive `blocksync/1\n`.
2. Retrieve metadata (`meta`).
3. Check the target:
   - Block device must be at least as large as the source file, otherwise `retrieve` ends with an error before writing anything.
   - Regular file is created if it does not exist; it is never truncated before the transfer.
4. Read target blocks (default block size 1 MiB, see `--block-size`), compute their hashes and send them to the server (`retr`).
   A limited number of `retr` commands is sent ahead without waiting for responses.
5. Receive responses and write changed blocks to the target in place.
6. Send `done`, wait for the response, `fsync` the target.
   A regular file is truncated to the source file size.

Blocks are read and hashed using a thread pool so that hashing does not become a bottleneck.
The bottleneck should be disk read I/O or network speed.

Progress is printed to stderr.

If running under Python 3.13 and `backports.zstd` is not present, receiving a `retr` response with `code` `\x02` ends with an error.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```shell
uv sync                    # create .venv with dev tools
uv run ruff check          # lint
uv run ruff format         # format
uv run mypy                # type check
uv run pytest              # tests
```
