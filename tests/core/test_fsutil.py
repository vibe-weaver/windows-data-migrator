import os
import stat
import sys
from pathlib import Path

import pytest

from dmig.core import fsutil
from dmig.core.models import EntryKind


def test_long_path_adds_prefix_for_absolute_path():
    assert fsutil.long_path(r"C:\a\b") == "\\\\?\\C:\\a\\b"


def test_long_path_is_idempotent():
    once = fsutil.long_path(r"C:\a\b")
    assert fsutil.long_path(once) == once


def test_long_path_leaves_unc_alone():
    assert fsutil.long_path(r"\\server\share\a") == r"\\server\share\a"


def test_strip_long_prefix_removes_both_forms():
    assert fsutil.strip_long_prefix("\\\\?\\C:\\a") == "C:\\a"
    assert fsutil.strip_long_prefix("\\??\\C:\\a") == "C:\\a"
    assert fsutil.strip_long_prefix("C:\\a") == "C:\\a"


def test_norm_target_handles_junction_form():
    assert fsutil.norm_target("\\??\\D:\\myapp") == "D:\\myapp"


def test_is_inside_respects_path_boundary():
    assert fsutil.is_inside(r"C:\a\b", r"C:\a") is True
    assert fsutil.is_inside(r"C:\a", r"C:\a") is True
    # C:\ab 不能被 C:\a 匹配
    assert fsutil.is_inside(r"C:\ab", r"C:\a") is False
    assert fsutil.is_inside(r"C:\other", r"C:\a") is False


def test_is_inside_is_case_insensitive():
    assert fsutil.is_inside(r"c:\A\B", r"C:\a") is True


def test_rel_to_strips_long_prefix_on_both_sides():
    assert fsutil.rel_to("\\\\?\\C:\\a\\b\\c.txt", "\\\\?\\C:\\a\\b") == "c.txt"
    assert fsutil.rel_to(r"C:\a\b\c.txt", r"C:\a") == r"b\c.txt"


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_reparse_kind_returns_none_for_plain_file(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("x", encoding="utf-8")
    assert fsutil.reparse_kind(os.lstat(f)) is None


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_reparse_kind_detects_junction(tmp_path):
    target = tmp_path / "store"
    target.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前环境无符号链接权限：{exc}")
    st = os.lstat(link)
    if not (st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        pytest.skip("当前文件系统不支持重解析点")
    kind = fsutil.reparse_kind(st)
    assert kind in (EntryKind.JUNCTION, EntryKind.SYMLINK_DIR)


def test_drive_of(tmp_path):
    drive = fsutil.drive_of(str(tmp_path))
    assert len(drive) == 2 and drive.endswith(":")


def test_volume_free_bytes_is_positive(tmp_path):
    assert fsutil.volume_free_bytes(str(tmp_path)) > 0


def test_volume_free_bytes_works_for_missing_path(tmp_path):
    r"""目标路径整条链都不存在时也必须算出剩余空间。

    否则 disk_usage 抛 FileNotFoundError 被吞掉后变成「剩余 0 字节」，
    前置校验就会误报空间不足、直接拒绝迁移——迁到任意新的嵌套目录都会踩到。
    """
    missing = tmp_path / "no" / "such" / "nested" / "dir"
    assert fsutil.volume_free_bytes(str(missing)) > 0


def test_existing_ancestor_walks_up_to_nearest_existing(tmp_path):
    missing = tmp_path / "a" / "b" / "c"
    assert fsutil.existing_ancestor(str(missing)) == str(tmp_path)
    assert fsutil.existing_ancestor(str(tmp_path)) == str(tmp_path)
