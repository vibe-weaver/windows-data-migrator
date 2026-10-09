"""路径、卷与重解析点的基础工具。"""
from __future__ import annotations

import os
import stat

from .models import EntryKind

LONG_PREFIX = "\\\\?\\"
NT_PREFIX = "\\??\\"

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
IO_REPARSE_TAG_DEDUP = 0x80000013


def long_path(p) -> str:
    r"""给绝对路径加 \\?\ 前缀；UNC 与已带前缀的路径原样返回。"""
    s = str(p)
    if s.startswith(LONG_PREFIX) or s.startswith(NT_PREFIX):
        return s
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
