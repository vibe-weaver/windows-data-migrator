import os
import sys

import pytest

from dmig.ui import shell

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def test_build_relaunch_argv_in_development(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    argv = shell.build_relaunch_argv("20261009-120000-ab12")
    assert argv[0] == sys.executable
    assert argv[1].endswith("app.py")
    assert argv[2:] == ["--elevated", "--session", "20261009-120000-ab12"]


def test_build_relaunch_argv_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"E:\dist\数据迁移工具.exe")
    assert shell.build_relaunch_argv("sid") == [
        r"E:\dist\数据迁移工具.exe", "--elevated", "--session", "sid"]


def test_relaunch_elevated_delegates_to_core(monkeypatch):
    from dmig.core import permission
    captured = {}

    def _fake(argv):
        captured["argv"] = argv
        return True

    monkeypatch.setattr(permission, "relaunch_elevated", _fake)
    assert shell.relaunch_elevated("sid") is True
    assert captured["argv"][-1] == "sid"
    assert captured["argv"][-3:] == ["--elevated", "--session", "sid"]


def test_parse_process_label_single():
    assert shell.parse_process_label("chrome.exe (1234)") == ("chrome.exe", 1234)


def test_parse_process_label_takes_first_of_many():
    assert shell.parse_process_label("a.exe (1), b.exe (2)") == ("a.exe", 1)


def test_parse_process_label_without_pid():
    assert shell.parse_process_label("unknown") == ("unknown", None)


def test_parse_process_label_empty():
    assert shell.parse_process_label("") == ("", None)


def test_process_image_path_of_current_process():
    path = shell.process_image_path(os.getpid())
    assert path
    assert os.path.basename(path).lower().startswith("python")


def test_process_image_path_returns_str_for_unknown_pid():
    assert isinstance(shell.process_image_path(999999), str)
