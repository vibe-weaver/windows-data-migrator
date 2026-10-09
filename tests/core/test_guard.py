import os
import sys
from pathlib import Path

import pytest

from dmig.core import guard

ALLOWANCE = 512 * 1024 * 1024
MARGIN = 1.05


def _codes(issues):
    return {i.code for i in issues}


def test_source_must_exist(tmp_path):
    issues = guard.check(tmp_path / "nope", tmp_path / "dst")
    assert "source_missing" in _codes(issues)
    assert guard.has_error(issues)


def test_source_must_be_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    assert "source_not_dir" in _codes(guard.check(f, tmp_path / "dst"))


def test_source_must_not_equal_target(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    assert "same_path" in _codes(guard.check(src, src))


def test_target_must_not_be_inside_source(tmp_path):
    src = tmp_path / "a"
    (src / "inner").mkdir(parents=True)
    assert "target_inside_source" in _codes(guard.check(src, src / "inner"))


def test_source_must_not_be_inside_target(tmp_path):
    dst = tmp_path / "b"
    (dst / "inner").mkdir(parents=True)
    assert "source_inside_target" in _codes(guard.check(dst / "inner", dst))


def test_source_must_not_be_drive_root():
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    drive = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep
    assert "source_is_root" in _codes(guard.check(drive, r"E:\anywhere"))


def test_fails_when_target_is_inside_its_own_drive_subpath(tmp_path):
    """目标在源子树的兄弟目录里是允许的，不应报错。"""
    src = tmp_path / "a"
    src.mkdir()
    dst = tmp_path / "b"
    issues = guard.check(src, dst)
    assert not guard.has_error(issues)


def test_not_enough_space_is_error(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    (src / "f.bin").write_text("data", encoding="utf-8")
    huge = guard.volume_free_bytes(str(tmp_path)) * 10
    assert "insufficient_space" in _codes(guard.check(src, tmp_path / "b", required_bytes=huge))


def test_enough_space_passes(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    assert not guard.has_error(guard.check(src, tmp_path / "b", required_bytes=1024))


def test_space_check_works_when_target_chain_missing(tmp_path):
    r"""目标是全新的嵌套路径时不能误判空间不足。

    目标整条链都不存在时，若 disk_usage 直接抛错被吞成 0，就会报
    「目标盘剩余 0 MB」并把迁移拒掉——这是真实场景里最常见的目标形态。
    """
    src = tmp_path / "a"
    src.mkdir()
    (src / "f.bin").write_text("data", encoding="utf-8")
    deep_target = tmp_path / "no" / "such" / "nested" / "target"
    assert not guard.has_error(guard.check(src, deep_target, required_bytes=1024))


def test_non_empty_target_is_warning_only(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    dst = tmp_path / "b"
    dst.mkdir()
    (dst / "existing.txt").write_text("x", encoding="utf-8")
    issues = guard.check(src, dst)
    assert "target_not_empty" in _codes(issues)
    assert not guard.has_error(issues)
    assert next(i for i in issues if i.code == "target_not_empty").severity == "warning"


def test_source_being_reparse_point_is_error(tmp_path):
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(str(real), str(link), target_is_directory=True)
    except OSError:
        pytest.skip("无符号链接权限")
    assert "source_is_reparse" in _codes(guard.check(link, tmp_path / "dst"))
