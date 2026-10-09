"""用系统重启管理器（RestartManager）定位占用某个文件的进程。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from .fsutil import long_path

ERROR_MORE_DATA = 234
CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63

try:
    _rm = ctypes.WinDLL("Rstrtmgr", use_last_error=True)
except OSError:  # pragma: no cover - 非 Windows 或极老系统
    _rm = None


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD),
                ("ProcessStartTime", wintypes.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [("Process", _RM_UNIQUE_PROCESS),
                ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
                ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
                ("ApplicationType", ctypes.c_uint),
                ("AppStatus", ctypes.c_ulong),
                ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL)]


@dataclass(frozen=True)
class ProcessInfo:
    name: str
    pid: int


def who_locks(path) -> list[ProcessInfo]:
    """返回正在占用该路径的进程；无占用或查询失败返回空列表。"""
    if _rm is None:
        return []
    session = wintypes.DWORD(0)
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    if _rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return []
    try:
        files = (wintypes.LPCWSTR * 1)(long_path(path))
        if _rm.RmRegisterResources(session, 1, files, 0, None, 0, None) != 0:
            return []
        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reasons = wintypes.DWORD(0)
        rc = _rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count),
                           None, ctypes.byref(reasons))
        if rc != ERROR_MORE_DATA or needed.value == 0:
            return []
        array = (_RM_PROCESS_INFO * needed.value)()
        count = wintypes.UINT(needed.value)
        if _rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count),
                         array, ctypes.byref(reasons)) != 0:
            return []
        return [ProcessInfo(name=array[i].strAppName or "<unknown>",
                            pid=int(array[i].Process.dwProcessId))
                for i in range(count.value)]
    finally:
        _rm.RmEndSession(session)
