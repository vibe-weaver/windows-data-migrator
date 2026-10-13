"""路径、卷与重解析点的基础工具。"""
from __future__ import annotations

import ctypes
import os
import stat
import time

from .models import EntryKind

LONG_PREFIX = "\\\\?\\"
NT_PREFIX = "\\??\\"

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
IO_REPARSE_TAG_DEDUP = 0x80000013


def long_path(p) -> str:
    r"""给绝对路径加 \\?\ 前缀；UNC 与已带前缀的路径原样返回。

    正斜杠必须先换成反斜杠：`\\?\` 前缀会让 Win32 **停止路径解析**，
    实测 `\\?\C:/a/b` 直接报 FileNotFoundError，同一条路径写成
    `\\?\C:\a\b` 才能打开。拖拽、命令行参数、`pathlib.PurePath.as_posix()`
    都可能给出正斜杠形式。
    """
    s = str(p)
    if s.startswith(LONG_PREFIX) or s.startswith(NT_PREFIX):
        return s
    if "/" in s:
        s = s.replace("/", "\\")
    if s.startswith("\\\\"):
        return s
    if len(s) >= 2 and s[1] == ":":
        return LONG_PREFIX + s
    return s


def strip_long_prefix(s: str) -> str:
    for prefix in (LONG_PREFIX, NT_PREFIX):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def norm_target(raw: str) -> str:
    """把 os.readlink 的返回值规范化成普通绝对/相对路径。"""
    return strip_long_prefix(raw)


def reparse_kind(st: os.stat_result) -> EntryKind | None:
    """按重解析点标签分类；非重解析点返回 None。"""
    attrs = getattr(st, "st_file_attributes", 0)
    if not (attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        return None
    is_dir = bool(attrs & stat.FILE_ATTRIBUTE_DIRECTORY)
    tag = getattr(st, "st_reparse_tag", 0)
    if tag == IO_REPARSE_TAG_MOUNT_POINT:
        return EntryKind.JUNCTION
    if tag == IO_REPARSE_TAG_SYMLINK:
        return EntryKind.SYMLINK_DIR if is_dir else EntryKind.SYMLINK_FILE
    return EntryKind.UNKNOWN_REPARSE


LINK_KINDS = frozenset({EntryKind.JUNCTION, EntryKind.SYMLINK_DIR,
                        EntryKind.SYMLINK_FILE})


def is_link_kind(kind: EntryKind | None) -> bool:
    """只有链接类重解析点才「重建链接本身、不碰内容」。

    其余重解析点（云占位符、AppExecLink、Dedup…）都带有真实数据语义，
    必须当内容处理，否则它们只存在于备份里，删备份即丢数据。
    """
    return kind in LINK_KINDS


# 校验器比对时间戳用的容差：FAT/exFAT 的 mtime 只有 2 秒精度，
# copystat 写回目标时会被截断，不留容差会把整棵目标树误判为不一致。
# 注意这个容差**不能**用在复制端的「是否已复制」判定上，见 same_content_stat。
MTIME_TOLERANCE_SECONDS = 2


def same_content_stat(src_st: os.stat_result, dst_st: os.stat_result) -> bool:
    """大小相同且 mtime **精确相等** —— 目标端这份与我们复制过去的那份没有差别。

    这是「能否跳过」的第二道关，第一道是会话记账（`Session.completed_rels`）。
    时间戳必须精确比较、不能像校验器那样留容差：实测同一系统时钟节拍（约 15ms）
    内写入的两个同大小文件 st_mtime_ns 完全相同，容差判定区分不出来。
    copystat 以 ns 精度写回、NTFS 存 100ns 刻度，我们自己复制的文件必然精确相等
    （实测差值 0）；FAT/exFAT 只有 2 秒精度，续传时会重复制一遍，慢但安全。
    """
    return (int(src_st.st_size) == int(dst_st.st_size)
            and src_st.st_mtime_ns == dst_st.st_mtime_ns)


def is_inside(child, parent) -> bool:
    """child 是否等于 parent 或位于 parent 子树内（大小写不敏感、按分量比较）。"""
    c = os.path.normcase(os.path.normpath(str(child)))
    p = os.path.normcase(os.path.normpath(str(parent)))
    if c == p:
        return True
    if not p.endswith(os.sep):
        p += os.sep
    return c.startswith(p)


def rel_to(path, root) -> str:
    r"""计算相对路径前先剥离 \\?\ 前缀。

    ntpath 会把 \\?\C:\a 的驱动器解析成 \\?\C:，不剥前缀会导致 relpath 结果错误。
    """
    return os.path.relpath(strip_long_prefix(str(path)), strip_long_prefix(str(root)))


def is_excluded(path, excludes) -> bool:
    r"""该路径是否落在任一排除项之下（含相等）。

    扫描、复制、校验三条遍历共用这一套判定。**两侧都要剥 \\?\ 前缀**：
    `os.scandir` 给出的 entry.path 带着长路径前缀；而排除项可能是界面从
    permission_issues 里固化的值——那里面存的是扫描器见过的带前缀形式。
    只剥候选路径、不剥排除项，按分量比较会全部落空，排除形同虚设；
    实测界面勾了「跳过无权限目录」再迁移，permission_denied 照样发生，
    用户被界面自己的补救办法骗了。命令行手打裸路径不受影响，所以这个
    bug 只在界面路径上现形。
    """
    if not excludes:
        return False
    text = strip_long_prefix(str(path))
    return any(is_inside(text, strip_long_prefix(str(item)))
               for item in excludes)


def drive_of(path) -> str:
    return os.path.splitdrive(os.path.abspath(str(path)))[0].upper()


def existing_ancestor(path) -> str:
    r"""返回该路径最近的存在祖先；盘根一定存在，所以必然有返回值。

    预扫描与前置校验都要算目标盘剩余空间，而目标路径（例如 E:\新的\嵌套\目录）
    常常整条链都还不存在。直接对不存在的路径调 disk_usage 会抛 FileNotFoundError，
    被上层吞掉后就变成「剩余 0 字节」，进而误报空间不足、直接拒绝迁移。
    """
    candidate = os.path.abspath(strip_long_prefix(str(path)))
    while not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return candidate


def volume_free_bytes(path) -> int:
    import shutil
    return shutil.disk_usage(existing_ancestor(path)).free


_FREE_CACHE: dict[str, tuple[float, int]] = {}
FREE_CACHE_TTL = 2.0


def cached_free_bytes(path, ttl: float = FREE_CACHE_TTL) -> int:
    """该路径所在卷的剩余空间，按卷根缓存 ttl 秒；查不到返回 -1。

    选择页每敲一个字符就要刷一次剩余空间：卷信息（盘类型/文件系统）已经缓存了，
    而 disk_usage 没有——网络盘或断开的映射盘上它会阻塞到超时，界面直接卡住。
    失败结果同样缓存：失败一次就够了，不必每个按键再等一遍。
    要拿到实时值（例如刚迁移完）就调 clear_free_cache()。
    """
    key = os.path.normcase(volume_root(path) or strip_long_prefix(str(path)))
    now = time.monotonic()
    hit = _FREE_CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    try:
        free = volume_free_bytes(path)
    except OSError:
        free = -1
    _FREE_CACHE[key] = (now, free)
    return free


def clear_free_cache() -> None:
    _FREE_CACHE.clear()


# --- 卷信息（盘类型 / 文件系统） ------------------------------------

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

DRIVE_TYPE_TEXT = {
    DRIVE_REMOVABLE: "可移动盘",
    DRIVE_FIXED: "固定磁盘",
    DRIVE_REMOTE: "网络位置",
    DRIVE_CDROM: "光驱",
    DRIVE_RAMDISK: "内存盘",
}

# 拔得掉、断得开的卷：迁移落点选在这里，链接与备份随时可能一起失效。
UNSTABLE_DRIVE_TYPES = frozenset({DRIVE_REMOVABLE, DRIVE_REMOTE,
                                  DRIVE_CDROM, DRIVE_RAMDISK})

REPARSE_FILESYSTEMS = frozenset({"NTFS", "REFS"})


# FindFirstStreamW 的输出缓冲：LARGE_INTEGER + WCHAR[MAX_PATH + 36]，
# 296 就是 Win32 规定的流名上限（MAX_PATH 260 加上 ":$DATA" 后缀的余量）。
class _StreamData(ctypes.Structure):
    _fields_ = [("StreamSize", ctypes.c_longlong),
                ("StreamName", ctypes.c_wchar * 296)]


DEFAULT_STREAM_NAME = "::$DATA"
FIND_STREAM_INFO_STANDARD = 0
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_K32 = None
_VOLUME_INFO: dict[str, tuple[int, str]] = {}


def _kernel32():
    """第一次用到时才加载 kernel32。

    本模块被 guard、ui、cli 到处导入，不该让「能不能调 Win32」变成导入期的硬条件。
    """
    global _K32
    if _K32 is None:
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetDriveTypeW.restype = wintypes.UINT
        k.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        k.GetVolumeInformationW.restype = wintypes.BOOL
        k.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPDWORD,
            wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPWSTR, wintypes.DWORD]
        k.FindFirstStreamW.restype = ctypes.c_void_p
        k.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                       ctypes.POINTER(_StreamData), wintypes.DWORD]
        k.FindNextStreamW.restype = wintypes.BOOL
        k.FindNextStreamW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_StreamData)]
        k.FindClose.argtypes = [ctypes.c_void_p]
        _K32 = k
    return _K32


def volume_root(path) -> str:
    r"""该路径所在卷的根，形如 "C:\" 或 "\\server\share\"；判断不出来返回 ""。

    必须剥掉 \\?\ 前缀：GetDriveTypeW 与 GetVolumeInformationW 都不认它。
    卷根是纯字符串运算，路径本身不存在也算得出来——目标常常是还没建的深层目录。
    """
    s = strip_long_prefix(str(path))
    if not s:
        return ""
    drive = os.path.splitdrive(os.path.abspath(s))[0]
    if not drive:
        return ""
    return drive if drive.endswith("\\") else drive + "\\"


def _query_volume_info(root: str) -> tuple[int, str]:
    import ctypes
    try:
        k = _kernel32()
    except (OSError, AttributeError):
        return (DRIVE_UNKNOWN, "")
    kind = int(k.GetDriveTypeW(root))
    name = ctypes.create_unicode_buffer(64)
    ok = k.GetVolumeInformationW(root, None, 0, None, None, None, name, len(name))
    return (kind, name.value if ok else "")


def volume_info(path) -> tuple[int, str]:
    """(盘类型, 文件系统名)，查不到时分别是 DRIVE_UNKNOWN 与 ""。

    按卷根缓存，失败结果也缓存：网络卷或已断开的映射盘上这两个调用会阻塞到超时，
    而选择页每敲一个字符就跑一遍前置校验，不缓存等于每键卡一次。
    """
    root = volume_root(path)
    if not root:
        return (DRIVE_UNKNOWN, "")
    key = os.path.normcase(root)
    cached = _VOLUME_INFO.get(key)
    if cached is None:
        cached = _query_volume_info(root)
        _VOLUME_INFO[key] = cached
    return cached


def clear_volume_cache() -> None:
    """清掉卷信息缓存；测试里插拔临时卷时用。"""
    _VOLUME_INFO.clear()


def drive_type(path) -> int:
    return volume_info(path)[0]


def volume_filesystem(path) -> str:
    return volume_info(path)[1]


def supports_reparse_points(path) -> bool:
    """该卷能不能存放 junction / 符号链接。

    问不出文件系统名时一律放行：网络卷、无权限的卷、还没挂上的盘都可能问不出来，
    那时宁可少说一句，也不能把本来能跑的迁移拦死。
    FAT/exFAT 上建链接必然失败，而这个失败发生在复制完几小时之后的建链阶段。
    """
    fs = volume_filesystem(path).upper()
    return fs in REPARSE_FILESYSTEMS if fs else True


# --- 复制保不住的属性 -------------------------------------------------

def is_sparse(st: os.stat_result) -> bool:
    """稀疏文件。复制会把空洞实化，占用空间从「实际写过的块」涨到文件大小。"""
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_SPARSE_FILE)


def is_hardlinked(st: os.stat_result) -> bool:
    """同一份数据被多个目录项共享（st_nlink > 目录项之外的引用数）。

    实测 NTFS 上普通文件的 st_nlink 就是 1，所以 > 1 即可认定是硬链接。
    """
    return int(getattr(st, "st_nlink", 1)) > 1


def has_alternate_streams(path) -> bool:
    """是否带附加数据流（ADS）。

    默认数据流之外的流（下载文件的 Zone.Identifier、资源分支）不会被
    「读字节再写出」的复制带上，所以必须把这件事说出来——否则「校验通过」
    会被当成「一个字节都没丢」。

    FindFirstStreamW 按路径直接查、不必先开句柄；卷不支持该操作
    （部分网络重定向器返回 ERROR_INVALID_FUNCTION）时按「没有」处理：
    宁可少说一句，也不能让它打断整趟扫描。
    """
    try:
        k = _kernel32()
    except (OSError, AttributeError):
        return False
    data = _StreamData()
    handle = k.FindFirstStreamW(long_path(path), FIND_STREAM_INFO_STANDARD,
                                ctypes.byref(data), 0)
    if handle in (None, INVALID_HANDLE_VALUE):
        return False
    try:
        while True:
            if data.StreamName and data.StreamName.upper() != DEFAULT_STREAM_NAME:
                return True
            if not k.FindNextStreamW(handle, ctypes.byref(data)):
                return False
    finally:
        k.FindClose(handle)
