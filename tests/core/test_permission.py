import os
import sys

import pytest

from dmig.core import permission

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
