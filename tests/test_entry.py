import sys

import pytest

from dmig import entry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def test_is_cli_invocation_for_subcommands():
    assert entry.is_cli_invocation(["app.py", "scan", "--source", "x"]) is True
    assert entry.is_cli_invocation(["app.py", "migrate", "--yes"]) is True
    assert entry.is_cli_invocation(["app.py", "resume", "s.json"]) is True
    assert entry.is_cli_invocation(["app.py", "rollback", "s.json"]) is True


def test_is_cli_invocation_for_gui():
    assert entry.is_cli_invocation(["app.py"]) is False
    assert entry.is_cli_invocation(["app.py", "--elevated",
                                    "--session", "abc"]) is False


def test_help_flags_go_to_cli_not_gui():
    """`app.py --help` 必须打印用法，而不是开图形界面并阻塞。"""
    assert entry.is_cli_invocation(["app.py", "--help"]) is True
    assert entry.is_cli_invocation(["app.py", "-h"]) is True
    assert entry.is_cli_invocation(["app.py", "--elevated", "--help"]) is True


def test_is_cli_invocation_skips_session_value():
    # --session 后面跟的会话 id 不能被当成子命令
    assert entry.is_cli_invocation(["app.py", "--session", "migrate"]) is False


def test_parse_launch_args_reads_flags():
    assert entry.parse_launch_args(["app.py", "--elevated", "--session", "abc"]) == {
        "elevated": True, "session": "abc"}


def test_parse_launch_args_defaults():
    assert entry.parse_launch_args(["app.py"]) == {"elevated": False, "session": None}


def test_parse_launch_args_tolerates_dangling_session_flag():
    assert entry.parse_launch_args(["app.py", "--session"]) == {
        "elevated": False, "session": None}


def test_main_dispatches_to_cli(monkeypatch):
    import dmig.cli as cli_mod
    captured = {}
    monkeypatch.setattr(cli_mod, "main",
                        lambda argv: captured.setdefault("argv", argv) and 0)
    assert entry.main(["app.py", "scan", "--source", "x"]) == 0
    assert captured["argv"] == ["scan", "--source", "x"]


def test_main_dispatches_to_gui(monkeypatch):
    import dmig.ui.main_window as window_mod
    captured = {}
    monkeypatch.setattr(window_mod, "run_gui",
                        lambda argv: captured.setdefault("argv", argv) and 7)
    assert entry.main(["app.py", "--elevated", "--session", "abc"]) == 7
    assert captured["argv"] == ["app.py", "--elevated", "--session", "abc"]
