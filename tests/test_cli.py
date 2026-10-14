from asyncio import StreamReader, create_subprocess_exec, run, timeout
from asyncio.subprocess import PIPE
from collections.abc import Callable
from pathlib import Path
from random import Random
from shlex import split
from sys import executable

from pytest import mark, raises

from blocksync import build_parser, main

SCRIPT = Path(__file__).parent.parent / 'blocksync.py'
TIMEOUT = 30


def test_serve_needs_only_src_file() -> None:
    args = build_parser().parse_args(['serve', '--src-file', '/dev/vg0/vm_example_root'])
    assert args.command == 'serve'
    assert args.src_file == Path('/dev/vg0/vm_example_root')
    assert args.psk is None
    assert args.psk_file is None
    assert not args.insecure


@mark.parametrize(
    'argv',
    [
        ['retrieve', '--host', 'h', '--psk', 'k', '--target-file', 't'],
        ['retrieve', '--host', 'h', '--port', '9001', '--target-file', 't'],
        ['serve', '--src-file', 's', '--psk', 'k', '--psk-file', 'f'],
        ['serve', '--src-file', 's', '--psk', 'k', '--insecure'],
        ['retrieve', '--host', 'h', '--port', '9001', '--psk-file', 'f', '--insecure', '--target-file', 't'],
    ],
    ids=['retrieve-without-port', 'retrieve-without-key', 'psk-and-psk-file', 'psk-and-insecure', 'psk-file-and-insecure'],
)
def test_invalid_options(argv: list[str]) -> None:
    with raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


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
