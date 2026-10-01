# blocksync

Copy large files (VM images, LVM snapshots etc.) between Linux servers.

Features:

- Encryption using TLS-PSK (requires Python 3.13+)
- Compression using Zstandard (will use [backports.zstd](https://pypi.org/project/backports.zstd/) for Python 3.13)
- Incremental - only blocks with differing hash are transferred

## Usage

```shell
# Create pre-shared key file
openssl rand -base64 512 > pre-shared-key

# Source side
blocksync.py serve --port 9001 --psk-file pre-shared-key --src-file /dev/vg0/vm_example_root

# Target side
blocksync.py retrieve --host source.example.com --port 9001 --psk-file pre-shared-key --target-file /dev/vg0/vm_example_root
```
