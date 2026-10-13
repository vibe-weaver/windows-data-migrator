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


def test_long_path_normalizes_forward_slashes():
    r"""加了 \\?\ 前缀 Win32 就不再解析路径，混合分隔符实测直接 FileNotFoundError。"""
    assert fsutil.long_path("C:/a/b") == "\\\\?\\C:\\a\\b"
    assert fsutil.long_path("C:/a/b/") == "\\\\?\\C:\\a\\b\\"


def test_long_path_forward_slash_unc_stays_unc():
    assert fsutil.long_path("//server/share/a") == "\\\\server\\share\\a"


def test_long_path_forward_slash_form_actually_opens(tmp_path):
    """端到端：正斜杠形式的路径经 long_path 之后必须真能打开文件。"""
    target = tmp_path / "sub" / "f.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    posix = str(tmp_path).replace("\\", "/") + "/sub/f.txt"
    with open(fsutil.long_path(posix), "rb") as handle:
        assert handle.read() == b"x"


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


def test_is_excluded_matches_when_the_exclude_carries_a_long_prefix(tmp_path):
    """P2 回归：排除项带 `\\?\` 前缀时也必须匹配。

    界面「跳过无权限目录」勾选框固化的排除项来自 plan.permission_issues，
    扫描器见过的都是带前缀形式。原来 is_excluded 只剥候选路径、不剥排除项，
    按分量比较全部落空——勾了等于没勾，迁移照样 permission_denied。
    """
    denied = tmp_path / "src" / "denied"
    denied.mkdir(parents=True)
    prefixed = "\\\\?\\" + str(denied)

    assert fsutil.is_excluded(denied, [prefixed]) is True
    assert fsutil.is_excluded(denied / "child", [prefixed]) is True
    assert fsutil.is_excluded(tmp_path / "src" / "ok", [prefixed]) is False


def test_is_excluded_matches_when_both_sides_carry_a_long_prefix(tmp_path):
    """两侧都带前缀也要匹配（扫描器 scandir 给出的 entry.path 就带前缀）。"""
    denied = tmp_path / "src" / "denied"
    denied.mkdir(parents=True)
    prefixed = "\\\\?\\" + str(denied)
    assert fsutil.is_excluded(prefixed, [prefixed]) is True
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


# --- 卷信息：盘类型与文件系统（C3） -------------------------------

def test_volume_root_of_local_path(tmp_path):
    drive = os.path.splitdrive(str(tmp_path))[0]
    assert fsutil.volume_root(str(tmp_path)) == drive + "\\"


def test_volume_root_strips_long_prefix():
    r"""GetDriveTypeW / GetVolumeInformationW 都不认 \\?\ 前缀，必须先剥掉。"""
    assert fsutil.volume_root("\\\\?\\C:\\a\\b") == "C:\\"


def test_volume_root_of_unc_share():
    assert fsutil.volume_root("\\\\server\\share\\a\\b") == "\\\\server\\share\\"


def test_volume_root_of_missing_deep_path(tmp_path):
    """目标常常是还没建的深层目录；卷根是纯字符串运算，照样算得出来。"""
    missing = tmp_path / "no" / "such" / "nested" / "dir"
    assert fsutil.volume_root(str(missing)) == fsutil.volume_root(str(tmp_path))


def test_volume_root_of_empty_string():
    assert fsutil.volume_root("") == ""


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_volume_info_reports_a_fixed_volume(tmp_path):
    kind, fs = fsutil.volume_info(str(tmp_path))
    assert kind == fsutil.DRIVE_FIXED
    assert fs.upper() in fsutil.REPARSE_FILESYSTEMS
    assert fsutil.supports_reparse_points(str(tmp_path))


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_volume_info_accepts_a_long_prefixed_path(tmp_path):
    assert fsutil.volume_info(fsutil.long_path(str(tmp_path))) == \
        fsutil.volume_info(str(tmp_path))


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_volume_info_is_cached_per_volume_root(tmp_path, monkeypatch):
    """网络卷上这两个调用会阻塞到超时，而选择页每敲一个字符就跑一遍前置校验。"""
    fsutil.clear_volume_cache()
    calls: list[str] = []

    def fake_query(root):
        calls.append(root)
        return (fsutil.DRIVE_FIXED, "NTFS")

    monkeypatch.setattr(fsutil, "_query_volume_info", fake_query)
    try:
        assert fsutil.volume_info(str(tmp_path / "a")) == (fsutil.DRIVE_FIXED, "NTFS")
        assert fsutil.volume_info(str(tmp_path / "b")) == (fsutil.DRIVE_FIXED, "NTFS")
        assert calls == [fsutil.volume_root(str(tmp_path))]
    finally:
        fsutil.clear_volume_cache()


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_volume_info_caches_a_failed_lookup_too(tmp_path, monkeypatch):
    """断开的映射盘每次都要阻塞到超时，失败结果同样得缓存住。"""
    fsutil.clear_volume_cache()
    calls: list[str] = []

    def fake_query(root):
        calls.append(root)
        return (fsutil.DRIVE_UNKNOWN, "")

    monkeypatch.setattr(fsutil, "_query_volume_info", fake_query)
    try:
        assert fsutil.drive_type(str(tmp_path)) == fsutil.DRIVE_UNKNOWN
        assert fsutil.volume_filesystem(str(tmp_path)) == ""
        assert len(calls) == 1
    finally:
        fsutil.clear_volume_cache()


def test_supports_reparse_points_is_permissive_when_unknown(monkeypatch):
    """问不出文件系统名（网络卷、无权限、盘没挂上）时放行，不能把能跑的迁移拦死。"""
    monkeypatch.setattr(fsutil, "volume_info",
                        lambda p: (fsutil.DRIVE_REMOTE, ""))
    assert fsutil.supports_reparse_points("\\\\server\\share\\a") is True


def test_supports_reparse_points_rejects_fat_volumes(monkeypatch):
    monkeypatch.setattr(fsutil, "volume_info",
                        lambda p: (fsutil.DRIVE_REMOVABLE, "exFAT"))
    assert fsutil.supports_reparse_points("E:\\data") is False
    monkeypatch.setattr(fsutil, "volume_info",
                        lambda p: (fsutil.DRIVE_FIXED, "ReFS"))
    assert fsutil.supports_reparse_points("C:\\data") is True


def test_every_unstable_drive_type_has_display_text():
    """guard 直接 DRIVE_TYPE_TEXT[kind] 取值，漏一个就是 KeyError。"""
    assert set(fsutil.DRIVE_TYPE_TEXT) >= fsutil.UNSTABLE_DRIVE_TYPES
    assert fsutil.DRIVE_FIXED not in fsutil.UNSTABLE_DRIVE_TYPES


# --- 复制保不住的属性（C9） -------------------------------------------

FSCTL_SET_SPARSE = 0x000900C4


def _mark_sparse(path) -> None:
    """用 FSCTL_SET_SPARSE 把文件真正标成稀疏，让 is_sparse 有真东西可测。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    fd = os.open(str(path), os.O_RDWR)
    try:
        returned = wintypes.DWORD(0)
        if not k.DeviceIoControl(msvcrt.get_osfhandle(fd), FSCTL_SET_SPARSE,
                                 None, 0, None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_is_sparse_reads_the_file_attribute(tmp_path):
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"x" * 4096)
    assert fsutil.is_sparse(os.lstat(plain)) is False
    _mark_sparse(plain)
    assert fsutil.is_sparse(os.lstat(plain)) is True


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_is_hardlinked(tmp_path):
    first = tmp_path / "a.txt"
    first.write_text("x", encoding="utf-8")
    assert fsutil.is_hardlinked(os.lstat(first)) is False
    os.link(str(first), str(tmp_path / "b.txt"))
    assert fsutil.is_hardlinked(os.lstat(first)) is True


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_has_alternate_streams_detects_a_zone_identifier(tmp_path):
    """下载文件的 Zone.Identifier 是最常见的 ADS。

    探测只回答「有没有」；复制时内核整文件复制会把它一并带过去，
    所以这里不涉及「会不会保留」——那条由 test_copier 里的逐字节比对负责。
    """
    if not fsutil.supports_reparse_points(str(tmp_path)):
        pytest.skip("当前卷不支持 ADS")
    plain = tmp_path / "downloaded.zip"
    plain.write_bytes(b"data")
    assert fsutil.has_alternate_streams(str(plain)) is False

    with open(f"{plain}:Zone.Identifier", "w", encoding="utf-8") as handle:
        handle.write("[ZoneTransfer]\r\nZoneId=3\r\n")
    assert fsutil.has_alternate_streams(str(plain)) is True
    # 带 \\?\ 前缀的路径也不能让探测失灵
    assert fsutil.has_alternate_streams(fsutil.long_path(str(plain))) is True


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_has_alternate_streams_on_a_missing_file_is_false(tmp_path):
    """查不出来时按「没有」处理，不能让一个读不到的条目打断整趟扫描。"""
    assert fsutil.has_alternate_streams(str(tmp_path / "nope")) is False


# --- D6：剩余空间按卷缓存 ---------------------------------------------

def test_cached_free_bytes_is_reused_within_the_ttl(tmp_path, monkeypatch):
    """选择页每敲一个字符就刷一次剩余空间，不能每次都去问文件系统。"""
    fsutil.clear_free_cache()
    calls: list[str] = []

    def fake(path):
        calls.append(str(path))
        return 1234

    monkeypatch.setattr(fsutil, "volume_free_bytes", fake)
    try:
        assert fsutil.cached_free_bytes(str(tmp_path)) == 1234
        assert fsutil.cached_free_bytes(str(tmp_path / "sub" / "deeper")) == 1234
        assert len(calls) == 1, "同一个卷根应当只查一次"
    finally:
        fsutil.clear_free_cache()


def test_cached_free_bytes_reports_unavailable_as_minus_one(tmp_path, monkeypatch):
    """查不到要和「真的只剩 0 字节」分开，否则界面会把「不知道」显示成 0 B。"""
    fsutil.clear_free_cache()

    def boom(path):
        raise OSError("盘不见了")

    monkeypatch.setattr(fsutil, "volume_free_bytes", boom)
    try:
        assert fsutil.cached_free_bytes(str(tmp_path)) == -1
    finally:
        fsutil.clear_free_cache()


def test_cached_free_bytes_caches_the_failure_too(tmp_path, monkeypatch):
    """断开的映射盘不能每个按键都再阻塞一次，失败结果也要缓存。"""
    fsutil.clear_free_cache()
    calls: list[int] = []

    def boom(path):
        calls.append(1)
        raise OSError("盘不见了")

    monkeypatch.setattr(fsutil, "volume_free_bytes", boom)
    try:
        fsutil.cached_free_bytes(str(tmp_path))
        fsutil.cached_free_bytes(str(tmp_path))
        assert len(calls) == 1
    finally:
        fsutil.clear_free_cache()


def test_cached_free_bytes_expires(tmp_path, monkeypatch):
    fsutil.clear_free_cache()
    calls: list[int] = []

    def fake(path):
        calls.append(1)
        return 1

    monkeypatch.setattr(fsutil, "volume_free_bytes", fake)
    try:
        fsutil.cached_free_bytes(str(tmp_path), ttl=0.0)
        fsutil.cached_free_bytes(str(tmp_path), ttl=0.0)
        assert len(calls) == 2
    finally:
        fsutil.clear_free_cache()
