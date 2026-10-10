"""属主/权限检测，以及提权后的属主接管与 ACL 备份恢复。"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from ctypes import wintypes


def _plain_path(path) -> str:
    r"""安全 API 专用路径。

    这些 API 不接受 \\?\ 前缀——SetFileSecurityW 会直接返回 ERROR_BAD_PATHNAME(161)。
    实测 GetNamedSecurityInfoW / SetFileSecurityW / takeown / icacls 都必须用普通绝对路径。
    """
    return os.path.abspath(str(path))

_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

TOKEN_QUERY = 0x0008
TokenUser = 1
TokenElevation = 20
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
SDDL_REVISION_1 = 1

# 必须显式声明原型：64 位下不声明 argtypes/restype，ctypes 会按 int 处理导致指针截断
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.GetCurrentProcess.argtypes = []
_k32.LocalFree.restype = ctypes.c_void_p
_k32.LocalFree.argtypes = [ctypes.c_void_p]

_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.HANDLE)]
_adv.GetTokenInformation.restype = wintypes.BOOL
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_adv.ConvertSidToStringSidW.restype = wintypes.BOOL
_adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
_adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
_adv.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p)]
_adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
_adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.DWORD)]
_adv.SetFileSecurityW.restype = wintypes.BOOL
_adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


def is_elevated() -> bool:
    handle = wintypes.HANDLE()
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY,
                                 ctypes.byref(handle)):
        return False
    try:
        info = _TOKEN_ELEVATION()
        size = wintypes.DWORD()
        if not _adv.GetTokenInformation(handle, TokenElevation, ctypes.byref(info),
                                        ctypes.sizeof(info), ctypes.byref(size)):
            return False
        return bool(info.TokenIsElevated)
    finally:
        _k32.CloseHandle(handle)


def _sid_to_string(sid) -> str:
    """把 SID 指针转成 S-1-5-... 形式的字符串。

    sid 可以是 int（POINTER(c_void_p)[0] 的返回值）或 c_void_p 实例。
    注意：不能写 ctypes.c_void_p(已有c_void_p实例)，会抛 TypeError: cannot be converted to pointer。
    """
    value = sid.value if hasattr(sid, "value") else sid
    out = wintypes.LPWSTR()
    if not _adv.ConvertSidToStringSidW(ctypes.c_void_p(value), ctypes.byref(out)):
        return ""
    try:
        return out.value or ""
    finally:
        _k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))


def current_user_sid() -> str:
    handle = wintypes.HANDLE()
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY,
                                 ctypes.byref(handle)):
        return ""
    try:
        size = wintypes.DWORD(0)
        # 第一次调用只为拿所需长度，预期失败
        _adv.GetTokenInformation(handle, TokenUser, None, 0, ctypes.byref(size))
        if not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if not _adv.GetTokenInformation(handle, TokenUser, buf, size.value,
                                        ctypes.byref(size)):
            return ""
        # TOKEN_USER 的第一个成员是 SID_AND_ATTRIBUTES{ PSID Sid; DWORD Attributes; }
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        return _sid_to_string(sid_ptr)
    finally:
        _k32.CloseHandle(handle)


def _owner_sid_raw(path):
    """取属主 SID 指针。

    注意 GetNamedSecurityInfoW 的第 4 个参数是 PSID*（指向 SID 指针的指针），
    不是字符串缓冲区——按 LPWSTR 读会得到乱码。
    """
    sid = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    rc = _adv.GetNamedSecurityInfoW(_plain_path(path), SE_FILE_OBJECT,
                                    OWNER_SECURITY_INFORMATION,
                                    ctypes.byref(sid), None, None, None,
                                    ctypes.byref(sd))
    if rc != 0:
        return None, None
    return sid, sd


def owner_sid(path) -> str | None:
    if not os.path.exists(path):
        return None
    sid, sd = _owner_sid_raw(path)
    try:
        if not sid:
            return None
        text = _sid_to_string(sid)
        return text or None
    finally:
        if sd:
            _k32.LocalFree(sd)


def is_owner(path) -> bool:
    owner = owner_sid(path)
    return bool(owner) and owner == current_user_sid()


def backup_acl(path) -> str | None:
    """把安全描述符导出成 SDDL 文本，供回滚时还原。"""
    sid = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    info = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    rc = _adv.GetNamedSecurityInfoW(_plain_path(path), SE_FILE_OBJECT, info,
                                    ctypes.byref(sid), None, None, None,
                                    ctypes.byref(sd))
    if rc != 0 or not sd:
        return None
    out = wintypes.LPWSTR()
    try:
        if not _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                sd, SDDL_REVISION_1, info, ctypes.byref(out), None):
            return None
        return out.value
    finally:
        _k32.LocalFree(sd)


def restore_acl(path, sddl: str) -> bool:
    """还原安全描述符。

    设置属主需要 SeRestorePrivilege，未提权时必然失败；那种情况下降级为只还原 DACL
    （守住「不能被锁在外面」这个关键点），并如实返回是否至少成功了一项。
    """
    if not sddl:
        return False
    sd = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(sd), ctypes.byref(size)):
        return False
    try:
        target = _plain_path(path)
        # SetFileSecurityW 返回非零表示成功
        if _adv.SetFileSecurityW(
                target, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, sd):
            return True
        return bool(_adv.SetFileSecurityW(target, DACL_SECURITY_INFORMATION, sd))
    finally:
        _k32.LocalFree(sd)


_SYMLINK_CAPABLE: bool | None = None


def can_create_symlink() -> bool:
    """当前进程能否创建符号链接。实测一次，结果按进程缓存。

    不查注册表：开发者模式、组策略与 SeCreateSymbolicLinkPrivilege 的组合太多，
    注册表只是代理指标。本机实测未提权且开发者模式关闭时，带不带
    SYMLINK_UNPRIVILEGED_FLAG 全部失败于 WinError 1314，只有真建一次才算数。

    只有**文件**符号链接必须靠它：目录符号链接可以降级成 junction（无需特权）。
    """
    global _SYMLINK_CAPABLE
    if _SYMLINK_CAPABLE is not None:
        return _SYMLINK_CAPABLE

    from . import reparse

    folder = tempfile.mkdtemp(prefix="dmig-symlink-probe-")
    target = os.path.join(folder, "target")
    link = os.path.join(folder, "link")
    try:
        with open(target, "wb"):
            pass
        reparse.create_symlink(link, target, False)
        _SYMLINK_CAPABLE = True
    except OSError:
        _SYMLINK_CAPABLE = False
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return _SYMLINK_CAPABLE


def take_ownership(path) -> bool:
    """提权后接管属主并授予当前用户完全控制；未提权直接返回 False。"""
    if not is_elevated():
        return False
    user = os.environ.get("USERNAME", "")
    target = _plain_path(path)
    commands = [
        ["takeown", "/F", target, "/A", "/R", "/D", "Y"],
        ["icacls", target, "/grant", f"{user}:(OI)(CI)F", "/T", "/C", "/Q"],
    ]
    for cmd in commands:
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="mbcs", creationflags=flags)
        if proc.returncode != 0:
            return False
    return True


def relaunch_elevated(argv: list[str]) -> bool:
    """用 runas 重新启动自身；用户取消 UAC 时返回 False。"""
    SW_SHOWNORMAL = 1
    params = subprocess.list2cmdline(argv[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", argv[0], params, None, SW_SHOWNORMAL)
    return int(rc) > 32
