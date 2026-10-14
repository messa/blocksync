from pathlib import Path

from pytest import CaptureFixture, raises

from blocksync import DEFAULT_PORT, build_parser, main


def test_parse_serve_command_from_readme() -> None:
    args = build_parser().parse_args(
        [
            'serve',
            '--port', '9002',
            '--psk-file', 'pre-shared-key',
            '--src-file', '/dev/vg0/vm_example_root',
        ]
    )  # fmt: skip
    assert args.command == 'serve'
    assert args.port == 9002
    assert args.psk_file == Path('pre-shared-key')
    assert args.src_file == Path('/dev/vg0/vm_example_root')


def test_parse_retrieve_command_uses_default_port() -> None:
    args = build_parser().parse_args(
        [
            'retrieve',
            '--host', 'source.example.com',
            '--psk-file', 'pre-shared-key',
            '--target-file', '/dev/vg0/vm_example_root',
        ]
    )  # fmt: skip
    assert args.command == 'retrieve'
    assert args.host == 'source.example.com'
    assert args.port == DEFAULT_PORT
    assert args.target_file == Path('/dev/vg0/vm_example_root')


def test_main_requires_command(capsys: CaptureFixture[str]) -> None:
    with raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2
    assert 'required: command' in capsys.readouterr().err
