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
    - `length` (int32)
    - `data` (of given `length`) - JSON-encoded object
      - `"size"`: size of file in bytes
- `retr`
  - Retrieve source file block, if its hash differs
  - Params:
    - `offset` (int64)
    - `length` (int32)
    - `hash` (binary, 32 bytes) - SHA3-256 
  - Response:
    - `code` (1 byte)
      - `\x00`: hash matches
      - `\x01`: returning raw block data
      - `\x02`: returning Zstandard-compressed block data 
    - `length` (int32)
    - `data` (of given `length`)

### `retrieve`

Connect to the server. Receive `blocksync/1\n`.
Retrieve metadata (`meta`).

Read the target file by blocks (parallelized - compute hashes in threads). For each block send a `retr` command to the server.
