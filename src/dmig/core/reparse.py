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
FSCTL_DELETE_REPARSE_POINT = 0x000900AC
SYMLINK_FLAG_DIRECTORY = 0x1
SYMLINK_UNPRIVILEGED_FLAG = 0x2
ERROR_PRIVILEGE_NOT_HELD = 1314
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
    """创建符号链接；权限不足会抛 WinError 1314。

    先带 `SYMLINK_UNPRIVILEGED_FLAG` 试一次：Windows 10 1703+ 开启开发者模式后，
    非管理员进程也能建符号链接。仅当失败原因确实是缺特权（1314）时才退回
    不带该 flag 的调用（提权进程走这条）；其它错误原样抛出，不要掩盖。
    """
    base = SYMLINK_FLAG_DIRECTORY if is_dir else 0
    if _k32.CreateSymbolicLinkW(long_path(link_path), str(target_path),
                                base | SYMLINK_UNPRIVILEGED_FLAG):
        return
    error = ctypes.get_last_error()
    if error != ERROR_PRIVILEGE_NOT_HELD:
        raise ctypes.WinError(error)
    if not _k32.CreateSymbolicLinkW(long_path(link_path), str(target_path), base):
        raise ctypes.WinError(ctypes.get_last_error())


def set_reparse_data(path, raw: bytes) -> None:
    """把一段重解析点缓冲区原样写到已存在的文件/目录上。"""
    handle = _open_reparse(path, GENERIC_WRITE)
    try:
        returned = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(handle, FSCTL_SET_REPARSE_POINT, raw, len(raw),
                                  None, 0, ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(handle)


def copy_reparse_data(src, dst) -> None:
    """原样搬运一个重解析点（不解读其语义，逐字节复制重解析数据）。

    有些标签根本没法当普通文件读：实测 AppExecLink（0x8000001B）的
    open() 一律失败于 Invalid argument，CopyFileW 也报 WinError 1920，
    但 FSCTL_GET_REPARSE_POINT / SET_REPARSE_POINT 能读出并原样写回。

    可重复执行：续传的第二趟会把已经搬过的条目再走一遍，那时目标端
    已经带着同一个未处理标签——open() 打不开它，FSCTL_SET_REPARSE_POINT
    也不接受覆盖，整趟会失败在一个早就复制好的文件上。所以已存在就不再
    新建，写入前先摘掉旧标签。
    """
    import stat as _stat

    raw = read_raw(src)
    st = os.lstat(long_path(src))
    if st.st_file_attributes & _stat.FILE_ATTRIBUTE_DIRECTORY:
        os.makedirs(long_path(dst), exist_ok=True)
    else:
        parent = os.path.dirname(long_path(str(dst)))
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.lexists(long_path(dst)):
            with open(long_path(dst), "wb"):
                pass
    if getattr(os.lstat(long_path(dst)), "st_reparse_tag", 0):
        delete_reparse_point(dst)
    set_reparse_data(dst, raw)


def delete_reparse_point(path) -> None:
    """摘掉重解析数据，把条目还原成普通文件/目录；本来就不是重解析点则什么都不做。

    带未处理标签的**目录**形态重解析点连 scandir 都会失败于 WinError 1920，
    删备份时先摘标签再删就没有这个坎。删除缓冲区只要 8 字节头：
    标签照抄原值、ReparseDataLength 填 0。
    """
    tag = getattr(os.lstat(long_path(path)), "st_reparse_tag", 0)
    if not tag:
        return
    header = struct.pack("<IHH", tag, 0, 0)
    handle = _open_reparse(path, GENERIC_WRITE)
    try:
        returned = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(handle, FSCTL_DELETE_REPARSE_POINT, header,
                                  len(header), None, 0, ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(handle)
