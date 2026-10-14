#!/usr/bin/env python3
"""Copy large files (VM images, LVM snapshots etc.) between Linux servers.

See README.md for usage and protocol description.
"""

from argparse import ArgumentParser
from collections.abc import Sequence
from pathlib import Path

DEFAULT_PORT = 9001


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog='blocksync.py', description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)

    serve = subparsers.add_parser('serve', help='serve source file to clients')
    serve.add_argument('--port', type=int, default=DEFAULT_PORT)
    serve.add_argument('--psk-file', type=Path, required=True)
    serve.add_argument('--src-file', type=Path, required=True)

    retrieve = subparsers.add_parser('retrieve', help='retrieve file from server')
    retrieve.add_argument('--host', required=True)
    retrieve.add_argument('--port', type=int, default=DEFAULT_PORT)
    retrieve.add_argument('--psk-file', type=Path, required=True)
    retrieve.add_argument('--target-file', type=Path, required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise SystemExit(f'Command {args.command!r} is not implemented yet')


if __name__ == '__main__':
    raise SystemExit(main())
