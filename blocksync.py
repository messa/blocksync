#!/usr/bin/env python3
"""Copy large files (VM images, LVM snapshots etc.) between Linux servers.

See README.md for usage and protocol description.
"""

from argparse import ArgumentParser, ArgumentTypeError, RawDescriptionHelpFormatter
from collections.abc import Sequence
from pathlib import Path

DEFAULT_BLOCK_SIZE = 1024 * 1024
MAX_BLOCK_SIZE = 64 * 1024 * 1024


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog='blocksync.py', description=__doc__, formatter_class=RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest='command', required=True)

    serve = subparsers.add_parser('serve', help='serve source file to clients')
    serve.add_argument('--port', type=port_number, help='TCP port to listen on (default: random unused port)')
    add_key_options(serve, required=False)
    serve.add_argument('--src-file', type=Path, required=True, help='source file or block device')

    retrieve = subparsers.add_parser('retrieve', help='retrieve file from server')
    retrieve.add_argument('--host', required=True, help='server host name or address')
    retrieve.add_argument('--port', type=port_number, required=True, help='server TCP port')
    add_key_options(retrieve, required=True)
    retrieve.add_argument('--target-file', type=Path, required=True, help='target file or block device')
    retrieve.add_argument(
        '--block-size',
        type=block_size,
        default=DEFAULT_BLOCK_SIZE,
        help=f'block size in bytes (default: {DEFAULT_BLOCK_SIZE}, max: {MAX_BLOCK_SIZE})',
    )

    return parser


def add_key_options(parser: ArgumentParser, *, required: bool) -> None:
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise SystemExit(f'Command {args.command!r} is not implemented yet')


if __name__ == '__main__':
    raise SystemExit(main())
