from asyncio import StreamReader, create_subprocess_exec, run, timeout
from asyncio.subprocess import PIPE
from collections.abc import Callable
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from random import Random
from shlex import split
from sys import executable

from pytest import mark, raises

from blocksync import build_parser

SCRIPT = Path(__file__).parent.parent / 'blocksync.py'
TIMEOUT = 30


def test_serve_needs_only_src_file() -> None:
    args = build_parser().parse_args(['serve', '--src-file', '/dev/vg0/vm_example_root'])
    assert args.command == 'serve'
    assert args.src_file == Path('/dev/vg0/vm_example_root')
    assert args.psk is None
    assert args.psk_file is None
    assert not args.insecure


RETRIEVE = ['retrieve', '--host', 'h', '--psk', 'k', '--target-file', 't']


def test_missing_or_conflicting_options() -> None:
    assert 'required: --port' in usage_error(RETRIEVE)
    without_key = ['retrieve', '--host', 'h', '--port', '9001', '--target-file', 't']
    assert 'one of the arguments --psk --psk-file --insecure is required' in usage_error(without_key)
    assert 'not allowed with' in usage_error(['serve', '--src-file', 's', '--psk', 'k', '--psk-file', 'f'])
    assert 'not allowed with' in usage_error(['serve', '--src-file', 's', '--psk', 'k', '--insecure'])
    assert 'not allowed with' in usage_error([*RETRIEVE, '--port', '9001', '--insecure'])


def test_invalid_port() -> None:
    assert 'argument --port' in usage_error(['serve', '--src-file', 's', '--port', '0'])
    assert 'argument --port' in usage_error([*RETRIEVE, '--port', '0'])
    assert 'argument --port' in usage_error([*RETRIEVE, '--port', '65536'])
    assert 'argument --port' in usage_error([*RETRIEVE, '--port', 'abc'])


def test_invalid_block_size() -> None:
    assert 'argument --block-size' in usage_error([*RETRIEVE, '--port', '9001', '--block-size', '0'])
    assert 'argument --block-size' in usage_error([*RETRIEVE, '--port', '9001', '--block-size', str(64 * 1024 * 1024 + 1)])
    assert 'argument --block-size' in usage_error([*RETRIEVE, '--port', '9001', '--block-size', '1M'])


def usage_error(argv: list[str]) -> str:
    """
    Parse argv, check it fails with a usage error (exit code 2) and return the error message
    (the last line of stderr, without the usage line).
    """
    stderr = StringIO()
    with redirect_stderr(stderr), raises(SystemExit) as exc_info:
        build_parser().parse_args(argv)
    assert exc_info.value.code == 2
    message = stderr.getvalue().splitlines()[-1]
    assert ' error: ' in message
    return message


@mark.parametrize('port', [1, 65535])
def test_port_limits_are_accepted(port: int) -> None:
    args = build_parser().parse_args(['serve', '--src-file', 's', '--port', str(port)])
    assert args.port == port
    args = build_parser().parse_args(['retrieve', '--host', 'h', '--port', str(port), '--psk', 'k', '--target-file', 't'])
    assert args.port == port


@mark.parametrize('block_size', [1, 64 * 1024 * 1024])
def test_block_size_limits_are_accepted(block_size: int) -> None:
    argv = ['retrieve', '--host', 'h', '--port', '9001', '--psk', 'k', '--target-file', 't', '--block-size', str(block_size)]
    args = build_parser().parse_args(argv)
    assert args.block_size == block_size


def test_block_size_defaults_to_1_mib() -> None:
    args = build_parser().parse_args(['retrieve', '--host', 'h', '--port', '9001', '--psk', 'k', '--target-file', 't'])
    assert args.block_size == 1024 * 1024


def test_serve_prints_working_retrieve_command_with_generated_key(tmp_path: Path) -> None:
    source = Random(0).randbytes(300_000)
    (tmp_path / 'src').write_bytes(source)

    retrieve_args = run(serve_and_retrieve(tmp_path, serve_args=[], edit_retrieve_args=lambda args: args))

    assert int(option_value(retrieve_args, '--port')) > 0
    assert option_value(retrieve_args, '--psk')
    assert (tmp_path / 'target').read_bytes() == source


def test_psk_file_and_psk_with_surrounding_whitespace_are_equivalent(tmp_path: Path) -> None:
    source = Random(0).randbytes(300_000)
    (tmp_path / 'src').write_bytes(source)
    (tmp_path / 'psk').write_text('abc\n')

    def use_psk_option(args: list[str]) -> list[str]:
        assert option_value(args, '--psk-file')
        i = args.index('--psk-file')
        return [*args[:i], '--psk', ' abc ', *args[i + 2 :]]

    run(serve_and_retrieve(tmp_path, serve_args=['--psk-file', str(tmp_path / 'psk')], edit_retrieve_args=use_psk_option))

    assert (tmp_path / 'target').read_bytes() == source


async def serve_and_retrieve(tmp_path: Path, serve_args: list[str], edit_retrieve_args: Callable[[list[str]], list[str]]) -> list[str]:
    """
    Run `serve` on tmp_path/src, run the retrieve command it prints (with --host and --target-file replaced)
    and check that both processes succeed. Returns the printed retrieve command arguments.
    """
    serve = await create_subprocess_exec(
        executable, str(SCRIPT), 'serve', '--src-file', str(tmp_path / 'src'), *serve_args,
        stdout=PIPE,
    )  # fmt: skip
    try:
        async with timeout(TIMEOUT):
            assert serve.stdout
            printed_args = await read_retrieve_command(serve.stdout)
            assert option_value(printed_args, '--target-file') == str(tmp_path / 'src')
            retrieve_args = replace_option(printed_args, '--host', '127.0.0.1')
            retrieve_args = replace_option(retrieve_args, '--target-file', str(tmp_path / 'target'))
            retrieve_args = edit_retrieve_args(retrieve_args)
            retrieve = await create_subprocess_exec(executable, str(SCRIPT), 'retrieve', *retrieve_args)
            assert await retrieve.wait() == 0
            # serve exits after one successful transfer
            assert await serve.wait() == 0
            return printed_args
    finally:
        if serve.returncode is None:
            serve.kill()
            await serve.wait()


async def read_retrieve_command(stdout: StreamReader) -> list[str]:
    """
    Read stdout until a complete `... retrieve ...` command (possibly continued with backslashes)
    is printed and return its arguments after `retrieve`.
    """
    command = ''
    while True:
        line = (await stdout.readline()).decode()
        assert line, f'serve stdout closed without printing retrieve command: {command!r}'
        if not command and 'retrieve' not in line:
            continue
        command += line
        if not line.rstrip('\n').endswith('\\'):
            break
    words = split(command.replace('\\\n', ' '))
    return words[words.index('retrieve') + 1 :]


def option_value(args: list[str], name: str) -> str:
    assert name in args, f'{name} not in {args}'
    return args[args.index(name) + 1]


def replace_option(args: list[str], name: str, value: str) -> list[str]:
    option_value(args, name)
    i = args.index(name)
    return [*args[: i + 1], value, *args[i + 2 :]]
