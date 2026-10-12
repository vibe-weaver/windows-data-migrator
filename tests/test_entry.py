import sys

import pytest

from dmig import entry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def test_is_cli_invocation_for_subcommands():
    assert entry.is_cli_invocation(["app.py", "scan", "--source", "x"]) is True
    assert entry.is_cli_invocation(["app.py", "migrate", "--yes"]) is True
    assert entry.is_cli_invocation(["app.py", "resume", "s.json"]) is True
    assert entry.is_cli_invocation(["app.py", "rollback", "s.json"]) is True


def test_discard_is_a_cli_subcommand():
    """discard 有完整实现，README 与 cli 自己的报错文案都在教用户敲它。

    入口的 CLI_COMMANDS 漏了它时，请求会被当成「没带子命令」转去开图形界面：
    迁移中途失败的用户照着自己工具给的建议操作，得到的是一个窗口。
    """
    assert entry.is_cli_invocation(["app.py", "discard", "s.json", "--yes"]) is True


def test_unknown_command_detects_typos():
    """拼错的子命令要被认出来，不能当成「无子命令」去开图形界面。"""
    assert entry.unknown_command(["app.py", "migrat", "--yes"]) == "migrat"
    assert entry.unknown_command(["app.py", "scan", "--source", "x"]) is None
    assert entry.unknown_command(["app.py"]) is None
    assert entry.unknown_command(["app.py", "--elevated", "--session", "abc"]) is None


def test_unknown_command_ignores_session_value():
    """--session 的值长得像子命令，不能被当成拼错。"""
    assert entry.unknown_command(["app.py", "--session", "migrate"]) is None


def test_main_reports_unknown_subcommand_instead_of_opening_gui(monkeypatch, capsys):
    import dmig.ui.main_window as window_mod

    def _boom(argv):
        raise AssertionError("未知子命令不该去开图形界面")

    monkeypatch.setattr(window_mod, "run_gui", _boom)
    assert entry.main(["app.py", "migrat", "--yes"]) != 0
    out = capsys.readouterr().out
    assert "migrat" in out
    assert "scan" in out, "应当列出可用子命令"


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


class _FakeStream:
    """够用的假流：能记住是否 tty、能否 reconfigure。"""

    def __init__(self, tty: bool):
        self._tty = tty
        self.reconfigured = None

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs) -> None:
        self.reconfigured = kwargs


def test_force_utf8_stdio_only_touches_redirected_streams():
    """被重定向的流切 UTF-8；交互式控制台保持原编码。

    中文控制台本身是 cp936，强行 UTF-8 会让中文变乱码，所以不能无脑切。
    """
    pipe = _FakeStream(tty=False)
    console = _FakeStream(tty=True)
    entry.force_utf8_stdio([pipe, console])
    assert pipe.reconfigured is not None
    assert pipe.reconfigured.get("encoding") == "utf-8"
    assert console.reconfigured is None


def test_force_utf8_stdio_tolerates_none_and_broken_streams():
    """GUI 模式下 stdout 是 None；某些流也不支持 reconfigure——都不能抛。"""
    class _NoReconfigure:
        def isatty(self):
            return False

    entry.force_utf8_stdio([None, _NoReconfigure()])


def test_force_utf8_stdio_tolerates_stream_without_isatty():
    class _Bare:
        def __init__(self):
            self.reconfigured = None

        def isatty(self):
            raise OSError("boom")

        def reconfigure(self, **kwargs):
            self.reconfigured = kwargs

    bare = _Bare()
    entry.force_utf8_stdio([bare])
    assert bare.reconfigured is not None


def test_main_dispatches_to_gui(monkeypatch):
    import dmig.ui.main_window as window_mod
    captured = {}
    monkeypatch.setattr(window_mod, "run_gui",
                        lambda argv: captured.setdefault("argv", argv) and 7)
    assert entry.main(["app.py", "--elevated", "--session", "abc"]) == 7
    assert captured["argv"] == ["app.py", "--elevated", "--session", "abc"]
