"""界面侧的系统动作：提权重启自身、定位占用进程。"""
from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from pathlib import Path

PROCESS_LABEL_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<pid>\d+)\)")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def build_relaunch_argv(session_id: str) -> list[str]:
    """提权重启用的完整命令行：打包后是 exe，开发时是 python app.py。"""
    if getattr(sys, "frozen", False):
        argv = [sys.executable]
    else:
        argv = [sys.executable, str(Path(__file__).resolve().parents[3] / "app.py")]
    return argv + ["--elevated", "--session", session_id]


def relaunch_elevated(session_id: str) -> bool:
    from ..core import permission
    return permission.relaunch_elevated(build_relaunch_argv(session_id))


def parse_process_label(label: str) -> tuple[str, int | None]:
    """把 "chrome.exe (1234), code.exe (5678)" 解析成第一个进程的名字与 PID。"""
    text = (label or "").strip()
    if not text:
        return "", None
    match = PROCESS_LABEL_RE.match(text)
    if not match:
        return text, None
    return match.group("name").strip(), int(match.group("pid"))


def process_image_path(pid: int) -> str:
    """取进程可执行文件的完整路径；取不到返回空串。"""
    if pid <= 0:
        return ""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        k32.CloseHandle(handle)
