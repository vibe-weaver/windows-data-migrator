import glob
import os
import sys
import tempfile

import pytest

from dmig.core import permission, reparse

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def test_is_elevated_returns_bool():
    assert isinstance(permission.is_elevated(), bool)


def test_current_user_sid_looks_like_sid():
    sid = permission.current_user_sid()
    assert sid.startswith("S-1-")


def test_owner_sid_of_temp_dir(tmp_path):
    sid = permission.owner_sid(tmp_path)
    assert sid is None or sid.startswith("S-1-")


def test_is_owner_true_for_own_temp_dir(tmp_path):
    assert permission.is_owner(tmp_path) is True


def test_backup_acl_returns_sddl(tmp_path):
    sddl = permission.backup_acl(tmp_path)
    assert sddl and sddl.startswith("D:") or sddl.startswith("O:")


def test_restore_acl_roundtrip(tmp_path):
    before = permission.backup_acl(tmp_path)
    assert permission.restore_acl(tmp_path, before) is True
    assert permission.backup_acl(tmp_path) is not None


def test_take_ownership_is_skipped_without_elevation(tmp_path):
    if permission.is_elevated():
        pytest.skip("已提权，跳过未提权路径断言")
    assert permission.take_ownership(tmp_path) is False


def test_owner_sid_of_missing_path_is_none(tmp_path):
    assert permission.owner_sid(tmp_path / "nope") is None


def test_can_create_symlink_matches_reality(tmp_path):
    """探测结论必须与真实 API 行为一致，不能自己说自己对。

    本机实测：未提权且开发者模式关闭时，带不带 SYMLINK_UNPRIVILEGED_FLAG
    都失败于 WinError 1314，连用 FSCTL_SET_REPARSE_POINT 手工造也被拒。
    """
    capable = permission.can_create_symlink()
    target = tmp_path / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        reparse.create_symlink(link, target, is_dir=False)
    except OSError as exc:
        assert capable is False, f"实测建不起来，探测却说能：{exc}"
        assert exc.winerror == 1314
        return
    assert capable is True, "实测建链接成功，探测却说不行"
    assert os.readlink(link)


def test_can_create_symlink_is_cached(monkeypatch):
    """结果按进程缓存：每次调用都建临时目录试探，计划页每次重扫都要白等。"""
    calls = {"n": 0}
    real_mkdtemp = tempfile.mkdtemp

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(permission, "_SYMLINK_CAPABLE", None)
    monkeypatch.setattr(permission.tempfile, "mkdtemp", counting)
    first = permission.can_create_symlink()
    second = permission.can_create_symlink()
    assert first is second
    assert calls["n"] == 1


def test_can_create_symlink_leaves_no_probe_dir(monkeypatch):
    """探测用的临时目录要清干净，别在 %TEMP% 里留垃圾。"""
    pattern = os.path.join(tempfile.gettempdir(), "dmig-symlink-probe-*")
    before = set(glob.glob(pattern))
    monkeypatch.setattr(permission, "_SYMLINK_CAPABLE", None)
    permission.can_create_symlink()
    assert set(glob.glob(pattern)) == before
