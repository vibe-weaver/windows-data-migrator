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


RM_BATCH = 300


def _query(paths: list[str]) -> list[ProcessInfo]:
    """一个 RM 会话里查这批路径，返回占用它们的进程（无占用/失败返回空）。"""
    if _rm is None or not paths:
        return []
    session = wintypes.DWORD(0)
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    if _rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return []
    try:
        files = (wintypes.LPCWSTR * len(paths))(*[long_path(p) for p in paths])
        if _rm.RmRegisterResources(session, len(paths), files, 0, None, 0, None) != 0:
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


def who_locks(path) -> list[ProcessInfo]:
    """返回正在占用该路径的进程；无占用或查询失败返回空列表。"""
    return _query([str(path)])


def who_locks_many(paths, batch: int = RM_BATCH) -> list[ProcessInfo]:
    """批量查占用，返回去重后的进程清单。

    逐个查每个文件都要起一次 RM 会话（实测约 30ms），改名失败时要排查的往往是
    整棵树里成百上千个文件；实测 300 个文件共用一个会话只要约 80ms。

    RM 只回答「这批文件里有哪些被谁占用」，不做逐文件归属，所以这里返回并集：
    改名失败要的正是「关掉哪些程序」这份清单。目录不进 RM——实测注册目录
    查不到任何进程，调用方要排查目录就得把里面的文件传进来。
    """
    found: dict[tuple[str, int], ProcessInfo] = {}
    items = [str(p) for p in paths]
    for start in range(0, len(items), max(batch, 1)):
        for proc in _query(items[start:start + max(batch, 1)]):
            found.setdefault((proc.name, proc.pid), proc)
    return list(found.values())
