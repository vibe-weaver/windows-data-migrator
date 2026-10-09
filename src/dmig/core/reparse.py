"""Win32 重解析点的原始读写：junction 与符号链接。"""
from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes

from .fsutil import IO_REPARSE_TAG_MOUNT_POINT, long_path

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CreateSymbolicLinkW.restype = wintypes.BOOLEAN
_k32.CreateSymbolicLinkW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FSCTL_GET_REPARSE_POINT = 0x000900A8
FSCTL_SET_REPARSE_POINT = 0x000900A4
SYMLINK_FLAG_DIRECTORY = 0x1
SYMLINK_UNPRIVILEGED_FLAG = 0x2
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
MAX_REPARSE_SIZE = 16 * 1024


def _open_reparse(path, access: int):
    handle = _k32.CreateFileW(long_path(path), access, FILE_SHARE_ALL, None, OPEN_EXISTING,
                              FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def read_raw(path) -> bytes:
    """读出重解析点的原始字节，用于比对与诊断。"""
    handle = _open_reparse(path, GENERIC_READ)
    try:
        buf = ctypes.create_string_buffer(MAX_REPARSE_SIZE)
        returned = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(handle, FSCTL_GET_REPARSE_POINT, None, 0, buf,
                                  MAX_REPARSE_SIZE, ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.raw[:returned.value]
    finally:
        _k32.CloseHandle(handle)


def build_mount_point(substitute_full: str, print_name: str) -> bytes:
    """构造 junction 的重解析点缓冲区。

    MountPointReparseBuffer 只有 4 个 USHORT（没有 Flags）；
    SubstituteName 与 PrintName 都必须以 NUL 结尾，PrintNameOffset 要跨过前者的终止符。
    """
    sub_b = substitute_full.encode("utf-16-le")
    prt_b = print_name.encode("utf-16-le")
    path_buf = sub_b + b"\x00\x00" + prt_b + b"\x00\x00"
    data_len = 8 + len(path_buf)
    buf = ctypes.create_string_buffer(8 + data_len)
    struct.pack_into("<IHH", buf, 0, IO_REPARSE_TAG_MOUNT_POINT, data_len, 0)
    struct.pack_into("<HHHH", buf, 8, 0, len(sub_b), len(sub_b) + 2, len(prt_b))
    buf[16:16 + len(path_buf)] = path_buf
    return buf.raw


def create_junction(link_path, target_path) -> None:
    """原生创建目录联接，不需要管理员权限。"""
    os.mkdir(link_path)
    absolute = os.path.abspath(str(target_path))
    raw = build_mount_point("\\??\\" + absolute, absolute)
    handle = _open_reparse(link_path, GENERIC_WRITE)
    try:
        returned = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(handle, FSCTL_SET_REPARSE_POINT, raw, len(raw),
                                  None, 0, ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(handle)


def create_symlink(link_path, target_path, is_dir: bool) -> None:
    """创建符号链接；权限不足会抛 WinError 1314。"""
    flags = SYMLINK_FLAG_DIRECTORY if is_dir else 0
    if not _k32.CreateSymbolicLinkW(long_path(link_path), str(target_path), flags):
        raise ctypes.WinError(ctypes.get_last_error())
