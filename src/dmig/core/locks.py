"""用系统重启管理器（RestartManager）定位占用某个文件的进程。"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

from .fsutil import long_path

ERROR_MORE_DATA = 234
CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63

# 一个 RM 会话里一次注册多少个文件。实测**批量越大越快**：
# 6000 个文件按 300 一批要 0.967 秒，一次 6000 只要 0.408 秒（2.4 倍）。
# 成本近似线性于文件数（约 0.13 毫秒/个），所以批太小的开销主要在会话本身。
# 取 2000 是折中：比 300 快一倍多，单次注册的内存与失败重试面又不会太大。
RM_BATCH = 2000

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


# 本工具**不会**关闭的进程。键是小写可执行文件名（不带 .exe）。
#
# 每条都要有一句**给用户看的理由**：被这些进程占用时，工具不动手，但要如实
# 告诉用户「为什么不帮你关」以及「你该怎么做」。默默不做、或者干脆一起关掉，
# 都比说清楚更糟——关掉 lsass 会立刻重启机器，关掉 explorer 桌面就没了。
PROTECTED_EXE: dict[str, str] = {
    "system": "Windows 内核线程",
    "registry": "系统注册表进程",
    "memory compression": "系统内存压缩进程",
    "smss": "会话管理器，关掉会让系统立即重启",
    "csrss": "客户端/服务器运行时子系统，关掉会蓝屏",
    "wininit": "Windows 启动进程，关掉会让系统重启",
    "winlogon": "登录进程，关掉会把当前用户注销",
    "services": "系统服务宿主，关掉会连带一批系统服务",
    "lsass": "本地安全认证进程，关掉会立刻重启",
    "svchost": "多个系统服务的共用宿主，关掉会连带一批服务",
    "explorer": "资源管理器。关掉它会让桌面、任务栏和所有已打开的文件夹一起消失",
    "dwm": "桌面窗口管理器，关掉会让整个屏幕显示异常",
    "msmpeng": "Windows Defender 的扫描引擎，关掉会让电脑失去实时防护",
    "nissrv": "Windows Defender 的网络检测服务",
    "securityhealthservice": "Windows 安全中心",
}

# 自己、以及自己的父进程链——关掉它们等于把工具自己带走
PROTECTED_REASON_SELF = "本工具自己"
PROTECTED_REASON_PARENT = "启动本工具的父进程，关掉会连带关掉本工具"

UNKNOWN_EXE_REASON = "无法确认这个进程是什么程序，为安全起见不由本工具关闭"


def _proc_exe(pid: int) -> tuple[str, bool]:
    """一次 OpenProcess 拿到可执行文件路径，并顺带判断进程是否已经退出。

    返回 `(路径, 是否已退出)`。两件事**必须合在一次调用里**做：分开写等于每个
    进程双倍系统调用，而且两次之间进程状态可能变（第一次还在、第二次没了），
    判断就会自相矛盾。

    「已退出」和「拿不到权限」必须分开，两者都表现为 OpenProcess 失败：
      87（参数错误）  = 没有这个 PID   -> 已退出，不该参与判断
      5（拒绝访问）   = 存在但打不开   -> 保守当受保护

    用 PROCESS_QUERY_LIMITED_INFORMATION：同用户进程不需要提权。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    ctypes.set_last_error(0)
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "", ctypes.get_last_error() == 87
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _k32.QueryFullProcessImageNameW(handle, 0, buf,
                                               ctypes.byref(size)):
            # 进程在，只是路径取不到——绝不能当成「已经退出」
            return "", False
        return buf.value or "", False
    finally:
        _k32.CloseHandle(handle)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


TH32CS_SNAPPROCESS = 0x2


def _process_parents() -> dict:
    """{PID: 父PID} 全表，一次 Toolhelp 快照拿完。

    **不要用 powershell 查父进程**：那要每个祖先起一次进程，实测整条链 7.7 秒，
    还依赖 PowerShell 起得来。一次快照是毫秒级。
    """
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(_PROCESSENTRY32W)]
    snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    parents: dict = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return {}
        while True:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if not _k32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _k32.CloseHandle(snapshot)
    return parents


def _parent_pids(pid: int) -> set[int]:
    """这个进程的祖先 PID 集合（含它自己），供「别关掉自己人」用。

    取不到快照时只返回它自己——宁可少认几个祖先（那样最多是拦下来不关），
    也不能凭空认为某个系统进程是安全的。
    """
    seen = {pid}
    parents = _process_parents()
    current = pid
    for _ in range(16):                    # 父链不会很深，兜一个上限防环
        parent = parents.get(current, 0)
        if parent <= 0 or parent in seen:
            break
        seen.add(parent)
        current = parent
    return seen


@dataclass(frozen=True)
class Holder:
    """一个占用了源目录内容的进程，以及能不能由本工具关闭。"""

    name: str                 # RestartManager 给的友好名（可能是「WPS PDF」）
    pid: int
    exe: str = ""             # 可执行文件完整路径；取不到为空串
    protected_reason: str = ""    # 非空表示不关，且这就是给用户看的理由

    @property
    def exe_name(self) -> str:
        return os.path.basename(self.exe) if self.exe else ""

    @property
    def can_close(self) -> bool:
        return not self.protected_reason


def _classify(pid: int, exe: str, *, self_pids: set[int]) -> str:
    """这个进程该不该由本工具关闭；返回理由，空串表示可以关。

    判据是**可执行文件名**，不是 RestartManager 给的友好名——那个是「WPS PDF」
    这样的显示名，靠它判黑名单会漏。
    """
    if pid in self_pids:
        return (PROTECTED_REASON_SELF if pid == os.getpid()
                else PROTECTED_REASON_PARENT)
    if not exe:
        return UNKNOWN_EXE_REASON
    stem = os.path.splitext(os.path.basename(exe))[0].lower()
    return PROTECTED_EXE.get(stem, "")


def find_holders(paths, batch: int = RM_BATCH, *, on_progress=None,
                 should_cancel=None) -> list[Holder]:
    """扫这批文件，返回占用它们的进程（含"能不能关"与理由）。

    与 `who_locks_many` 的差别：那个只给人看的名字，这个要判断「该不该动手」，
    所以必须拿到**可执行文件路径**——RestartManager 给的 `strAppName` 是友好名
    （「WPS PDF」而不是 wpspdf.exe），靠它判黑名单会漏。
    """
    self_pids = _parent_pids(os.getpid())
    found: dict[tuple[str, int], Holder] = {}
    items = [str(p) for p in paths]
    step = max(batch, 1)
    for start in range(0, len(items), step):
        if should_cancel is not None and should_cancel():
            break
        for proc in _query(items[start:start + step]):
            key = (proc.name, proc.pid)
            if key in found:
                continue
            exe, gone = _proc_exe(proc.pid)
            if gone:
                # RM 的快照里可能有刚退出的进程：它已经不占着任何东西了，
                # 列出来只会让用户以为还要关它。
                continue
            found[key] = Holder(
                name=proc.name, pid=proc.pid, exe=exe,
                protected_reason=_classify(proc.pid, exe,
                                           self_pids=self_pids))
        if on_progress is not None:
            on_progress(min(start + step, len(items)), len(items))
    return list(found.values())



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


def blockers_in_tree(root, limit: int = 2000) -> list[ProcessInfo]:
    """采样一棵树里的文件，问重启管理器是谁占着。

    RM 实测注册目录查不到任何进程，只能拿文件去问；而诊断要的答案是
    「关掉哪个程序」，采样足够，不值得为此完整遍历几十万个文件。

    链接类重解析点既不下钻也不采集：它们指向别处，不是这棵树的内容。
    （改动前这段逻辑在 Migrator._blocking_processes 里，改名试探也要用，
    所以挪到这里共用一份。）
    """
    import os

    from .fsutil import is_link_kind, long_path, reparse_kind

    sample: list[str] = []
    for dirpath, dirs, files in os.walk(long_path(root)):
        keep = []
        for name in dirs:
            full = os.path.join(dirpath, name)
            try:
                if reparse_kind(os.lstat(full)) is None:
                    keep.append(name)
            except OSError:
                continue
        dirs[:] = keep
        for name in files:
            full = os.path.join(dirpath, name)
            try:
                if is_link_kind(reparse_kind(os.lstat(full))):
                    continue
            except OSError:
                continue
            sample.append(full)
            if len(sample) >= limit:
                break
        if len(sample) >= limit:
            break
    if not sample:
        return []
    return who_locks_many(sample)


def describe(processes) -> str:
    """把进程清单写成「名字 (PID)」一行，供提示文案用。"""
    return "、".join(f"{p.name} ({p.pid})" for p in processes)


# --- 关闭占用进程 -----------------------------------------------------------
#
# RmShutdown 是微软给安装程序用的官方通道：它让程序自己走正常退出流程，
# 该提示保存的还会提示保存。实测对**无窗口的控制台进程同样有效**（会收到
# 控制台关闭事件），所以不必一上来就用 TerminateProcess 硬杀。

RmForceShutdown = 0x1

_SHUTDOWN_ERROR = {
    5: "有程序拒绝了关闭请求",
    # 351 在微软的定义里是「有些程序关掉了、有些没有」——**部分成功**，不是全败。
    # 实测遇到的就是它：报失败，而目标程序其实已经放开了文件。文案必须说清
    # 「有的关掉了」，否则用户以为白点了，只能靠再点一次扫描才发现成了。
    350: "系统拒绝了关闭，需要重启才能继续",
    351: "有的程序关掉了，有的没有关掉",
    352: "系统拒绝了关闭，需要重启才能继续",
    1223: "有关闭请求被取消（可能弹了「是否保存」对话框）",
}


def _list_session(session) -> list[ProcessInfo]:
    """一个已注册资源的会话里，正在使用这些资源的进程。"""
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


@dataclass(frozen=True)
class ShutdownResult:
    """关闭动作的结果。

    `started` 表示有没有**真的向系统发起**关闭，和成功与否是两件事——分开是因为
    界面上的「强制结束」按钮要据此决定给不给：

      被护栏拦下（started=False）-> 强制走的是同一条路，一样会被拦，按钮是死的
      发起过但没成（started=True）-> 强制还有意义

    给用户一个按了必然没反应的按钮，比不给更糟。
    """

    started: bool = False
    ok: bool = False
    message: str = ""

    @property
    def force_may_help(self) -> bool:
        return self.started and not self.ok


def shutdown_holders(paths, *, force: bool = False) -> ShutdownResult:
    """关掉正在使用这批文件的进程。返回 (是否发起了关闭, 给用户看的说明)。

    **整批语义，没有逐个选择。** `RmShutdown` 关掉的是「所有正在使用已注册资源
    的进程」，RestartManager 也不提供「哪个文件被哪个进程占」的归属。

    **所以调用关闭之前必须复查这一批里有没有受保护的进程——有就什么都不做。**
    否则关掉 WPS PDF 的同时会把 explorer（它可能正为缩略图打开着同一个文件）
    一起关掉，桌面就没了。宁可这一次不动，也不能顺带关掉不该关的。

    `force=False` 走优雅关闭（程序可以提示保存、也可以拒绝）；`force=True` 才
    强制结束，那一步必须由界面单独确认并警告未保存内容会丢失。
    """
    if _rm is None:
        return ShutdownResult(False, False, "系统里没有重启管理器（Rstrtmgr），无法关闭程序")
    items = [str(p) for p in paths]
    if not items:
        return ShutdownResult(False, False, "没有要注册的文件")

    session = wintypes.DWORD(0)
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    if _rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return ShutdownResult(False, False, "起不了重启管理器会话")
    try:
        files = (wintypes.LPCWSTR * len(items))(*[long_path(p) for p in items])
        if _rm.RmRegisterResources(session, len(items), files, 0, None, 0,
                                   None) != 0:
            return ShutdownResult(False, False, "注册要检查的文件失败")

        self_pids = _parent_pids(os.getpid())
        blocked: list[str] = []
        for proc in _list_session(session):
            exe, gone = _proc_exe(proc.pid)
            if gone:
                continue           # 已经退出的进程拦不住任何东西
            reason = _classify(proc.pid, exe, self_pids=self_pids)
            if reason:
                blocked.append(f"{proc.name} ({proc.pid})——{reason}")
        if blocked:
            # 一个字都没动。把「为什么不动」原样交给界面去说。
            # started=False：强制结束走的是同一条护栏，一样会被拦，
            # 界面据此不给那个按钮——按了必然没反应的按钮比不给更糟。
            return ShutdownResult(
                False, False,
                "这批文件同时被下面这些程序占用，本工具不会关闭它们：\n"
                + "\n".join(blocked)
                + "\n\n请手动关掉它们（右键任务栏图标 → 关闭窗口），"
                  "再回来重新检验。")

        rc = _rm.RmShutdown(session, RmForceShutdown if force else 0, None)
        if rc != 0:
            how = "强制结束" if force else "请求关闭"
            why = _SHUTDOWN_ERROR.get(rc, f"错误码 {rc}")
            return ShutdownResult(True, False, f"{how}失败：{why}")
        return ShutdownResult(True, True,
                              "已强制结束占用程序" if force else "已请求占用程序关闭")
    finally:
        _rm.RmEndSession(session)






