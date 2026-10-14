# blocksync

Copy large files (VM images, LVM snapshots etc.) between Linux servers.

Features:

- Encryption using TLS-PSK
- Compression using Zstandard (will use  for Python 3.13)
- Incremental - only blocks with differing hash are transferred
- Python standard library only - no libraries or venv required
- Single `.py` file - trivial to copy or deploy wherever you need

Python version support:

- TLS-PSK support needs Python 3.13+
- Zstandard support needs Python 3.14+, or 3.13 with [backports.zstd](https://pypi.org/project/backports.zstd/)
- Python versions 3.12 and less are not supported
  - Use `uvx --python 3.14` to run this script with managed Python

## Usage

```shell
# Create pre-shared key file
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

### `serve`

Listens on given TCP port. Incoming connections are wrapped in TLS-PSK using the pre-shared key.

#### Protocol

After client connects and TLS-PSK is successfully opened:

1. Server sends `blocksync/1\n`
2. Client sends command and server responds to it. Commands may be queued in the TCP connection.

Commands:

- `meta`
  - Retrieve source file metadata
  - Params: none
  - Response:
    - `length` (uint32): size of the next field in bytes
    - `data` (of given `length`) - JSON-encoded object
      - `"size"`: size of file in bytes
- `retr`
  - Retrieve source file block, if its hash differs
  - Params:
    - `offset` (uint64): offset of the block in the file (in bytes)
    - `length` (uint32): length of the block (in bytes)
    - `hash` (binary, 32 bytes) - SHA3-256 
  - Response:
    - `code` (1 byte)
      - `\x00`: hash matches, no data need to be returned
      - `\x01`: hash mismatch, returning raw block data
      - `\x02`: hash mismatch, returning Zstandard-compressed block data 
    - `length` (uint32): size of `data` in bytes
    - `data` (of given `length`): raw or zstd-compressed contents of the block, or nothing if the hash matches
- `done`
  - Transfer is completed
  - Params: none
  - Response:
    - `ok` (1 byte): the value `b'1'`
    - Both sides close the TCP connection.

### `retrieve`

Connect to the server.
Receive `blocksync/1\n`.
Retrieve metadata (`meta`).
Read destination file/device blocks, compute hashes, send them to server (`retr`), receive source file/device data and save them.

Block hashes are computed using a threadpool so that hashing does not become bottleneck.
The bottleneck should be disk read I/O or network speed.


Read the target file by blocks (parallelized - compute hashes in threads). For each block send a `retr` command to the server.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```shell
uv sync                    # create .venv with dev tools
uv run ruff check          # lint
uv run ruff format         # format
uv run mypy                # type check
uv run pytest              # tests
```
