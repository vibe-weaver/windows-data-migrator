import ctypes
import os
import subprocess
import sys
import time
import uuid
from ctypes import wintypes

import pytest

from dmig.core import guard, locks

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
SHARE_ALL = 0x1 | 0x2 | 0x4

# 一个持着文件不放的替身。用它验证「关闭」时不会碰到系统进程。
HOLDER_SRC = """\
import ctypes, sys, time
from ctypes import wintypes
k = ctypes.WinDLL('kernel32')
k.CreateFileW.restype = wintypes.HANDLE
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                          wintypes.HANDLE]
h = k.CreateFileW(sys.argv[1], 0x80000000, 7, None, 3, 0x80, None)
print('OPENED', flush=True)
time.sleep(120)
"""


def _hold(path):
    """本进程持有一个文件句柄（模拟「工具自己占着」）。

    打不开就直接抛：这个替身静默失败过一次——传了不存在的文件，句柄是 -1，
    于是「试探没被挡住」的假象一路传到断言里，查起来费劲。
    """
    handle = _k32.CreateFileW(str(path), GENERIC_READ, SHARE_ALL, None,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        raise OSError(f"替身打不开 {path}（文件不存在？）")
    return handle


def _release(handle):
    if handle:
        _k32.CloseHandle(handle)


@pytest.fixture
def spawn_holder(tmp_path):
    """起一个持着文件的独立进程；用完杀掉。"""
    created = []

    def start(path):
        script = tmp_path / f"holder_{uuid.uuid4().hex}.py"
        script.write_text(HOLDER_SRC, encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(script), str(path)],
                                stdout=subprocess.PIPE, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        proc.stdout.readline()
        time.sleep(0.4)
        created.append(proc)
        return proc

    yield start
    for proc in created:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)



def test_who_locks_finds_holder_process(tmp_path):
    locked = tmp_path / "locked.db"
    locked.write_bytes(b"x" * 4096)

    code = ("import time,sys\n"
            "h=open(sys.argv[1],'rb+')\n"
            "print('HELD', flush=True)\n"
            "time.sleep(20)\n")
    child = subprocess.Popen([sys.executable, "-c", code, str(locked)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "HELD"
        time.sleep(0.6)
        found = locks.who_locks(locked)
        assert found, "未定位到占用进程"
        assert any(p.pid == child.pid for p in found), \
            f"预期包含 PID {child.pid}，实际 {found}"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_who_locks_returns_empty_for_unlocked_file(tmp_path):
    free = tmp_path / "free.txt"
    free.write_text("nothing", encoding="utf-8")
    assert locks.who_locks(free) == []


def test_who_locks_many_spans_batches(tmp_path):
    """批量查询：batch 故意小于文件数，占用者落在最后一批也不能漏。"""
    files = []
    for index in range(5):
        path = tmp_path / f"f{index}.bin"
        path.write_bytes(b"x" * 1024)
        files.append(path)

    code = ("import time,sys\n"
            "h=open(sys.argv[1],'rb+')\n"
            "print('HELD', flush=True)\n"
            "time.sleep(20)\n")
    child = subprocess.Popen([sys.executable, "-c", code, str(files[4])],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "HELD"
        time.sleep(0.6)
        found = locks.who_locks_many(files, batch=2)
        assert any(p.pid == child.pid for p in found), \
            f"预期包含 PID {child.pid}，实际 {found}"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_who_locks_many_dedups_and_handles_empty(tmp_path):
    assert locks.who_locks_many([]) == []
    free = tmp_path / "free.txt"
    free.write_text("nothing", encoding="utf-8")
    # 同一个文件重复传入也不该重复回报进程
    assert locks.who_locks_many([free, free, free]) == []


def test_parent_pids_includes_self_and_a_spawned_child_sees_us(
        tmp_path, spawn_holder):
    """祖先链要认得自己人，也要认得别人的父进程。"""
    assert os.getpid() in locks._parent_pids(os.getpid())

    path = tmp_path / "x.txt"
    path.write_text("x", encoding="utf-8")
    proc = spawn_holder(path)

    chain = locks._parent_pids(proc.pid)
    assert proc.pid in chain
    assert os.getpid() in chain, "替身的父进程就是我们，必须认出来"


def test_parent_pids_does_not_shell_out():
    """父进程链必须一次原生快照拿完。

    原来每个祖先起一次 PowerShell，实测整条链 **7.7 秒**——它拖慢每一次扫描和
    每一次关闭，还依赖 PowerShell 起得来。这条测试盯住「不许再用外部进程」。

    只看**调用**，不看注释：函数文档里正写着「不要用 powershell 查」，那是解释，
    不是实现。
    """
    import inspect

    source = (inspect.getsource(locks._parent_pids)
              + inspect.getsource(locks._process_parents))
    for pattern in ("subprocess.run", "subprocess.Popen", "import subprocess",
                    "check_output", "Popen("):
        assert pattern not in source, f"又回到外部进程了：{pattern}"
    assert "Toolhelp32Snapshot" in source


# --- 缩小范围：先找到是哪几棵子树被占 -------------------------------------

def test_probe_entries_flags_only_the_blocked_ones(tmp_path):
    """只报被占用的那几个顶层条目，其余不动声色。"""
    src = tmp_path / "src"
    for name in ("free1", "free2", "held"):
        (src / name).mkdir(parents=True)
    (src / "held" / "b.txt").write_text("x", encoding="utf-8")

    handle = _hold(src / "held" / "b.txt")
    try:
        blocked = guard.probe_entries(src)
    finally:
        _release(handle)

    assert blocked == ["held"]
    # 所有条目都要恢复原名，一个不能少
    assert sorted(p.name for p in src.iterdir()) == ["free1", "free2", "held"]


def test_probe_entries_is_clean_on_a_free_tree(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "b.txt").write_text("x", encoding="utf-8")

    assert guard.probe_entries(src) == []
    assert sorted(p.name for p in src.iterdir()) == ["a", "b.txt"]


def test_probe_entries_reports_progress(tmp_path):
    src = tmp_path / "src"
    for name in ("a", "b", "c"):
        (src / name).mkdir(parents=True)
    seen = []
    guard.probe_entries(src,
                        on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_probe_entries_can_be_cancelled(tmp_path):
    src = tmp_path / "src"
    for name in ("a", "b", "c"):
        (src / name).mkdir(parents=True)
    assert guard.probe_entries(src, should_cancel=lambda: True) == []


def test_probe_entries_can_be_limited_to_given_names(tmp_path):
    """`names` 限定试探范围——调用方据此只试子目录。

    普通文件不该被试探（试一个 0.67 毫秒，RM 扫一个才 0.13 毫秒），所以调用方
    会传一份"只含子目录"的名单进来。
    """
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir()
    (src / "loose.dat").write_bytes(b"x")
    (src / "b" / "held.dat").write_bytes(b"x")     # 句柄得有文件可开

    handle = _hold(src / "b" / "held.dat")
    try:
        # 只试 a；b 明明被占着，但没让它试，所以不该出现在结果里
        assert guard.probe_entries(src, names=["a"]) == []
        # 试全了就能抓到
        assert guard.probe_entries(src, names=["a", "b"]) == ["b"]
    finally:
        _release(handle)


def test_probe_entries_with_an_empty_name_list_does_nothing(tmp_path):
    """名单为空 = 什么都不试（全平铺的目录就是这样）。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "loose.dat").write_bytes(b"x")
    assert guard.probe_entries(src, names=[]) == []
    assert (src / "loose.dat").exists()


# --- 黑名单 ---------------------------------------------------------------

def test_every_protected_process_has_a_reason_for_users():
    """受保护进程的每一条都要有一句给用户看的理由。

    用户看到的必须是「为什么不帮你关」——默默不做、或者干脆一起关掉，
    都比说清楚更糟。
    """
    assert locks.PROTECTED_EXE
    for name, reason in locks.PROTECTED_EXE.items():
        assert name == name.lower(), f"{name} 应当是小写可执行文件名"
        assert reason and len(reason) > 6, f"{name} 的理由太短：{reason!r}"


def test_the_desktop_shell_is_protected():
    """explorer 必须在黑名单里：关掉它桌面和任务栏一起消失。"""
    assert "explorer" in locks.PROTECTED_EXE
    assert "桌面" in locks.PROTECTED_EXE["explorer"]


def test_self_and_its_ancestors_are_protected():
    # exe 传空串：自己/父进程的判定在解析可执行文件**之前**就该命中，
    # 不依赖能不能拿到路径。
    assert locks._classify(os.getpid(), "",
                           self_pids={os.getpid()}) == locks.PROTECTED_REASON_SELF
    assert locks._classify(4242, "", self_pids={os.getpid(), 4242}) == \
        locks.PROTECTED_REASON_PARENT


def test_unresolvable_process_is_treated_as_protected():
    """查不到可执行文件的进程一律不关。

    证明不了它是什么，就不能假设它安全——宁可这一次不动。
    """
    reason = locks._classify(999999999, "", self_pids=set())
    assert reason == locks.UNKNOWN_EXE_REASON


def test_an_exited_process_is_distinguished_from_an_inaccessible_one():
    """「进程已经没了」和「拿不到权限」必须分开。

    两者都表现为 OpenProcess 失败，但后果相反：拿不到权限要保守（当受保护），
    进程已经没了则不该参与判断——否则 RestartManager 快照里一个刚退出的进程
    会把整批拦下来，用户看到「无法确认 XXX 是什么程序」，而那个程序早不在了。

    实测：不存在的 PID -> GetLastError 87（参数错误）；
    存在但打不开（如 System）-> 5（拒绝访问）。
    """
    path, gone = locks._proc_exe(os.getpid())
    assert gone is False and path.lower().endswith("python.exe")

    assert locks._proc_exe(999999999)[1] is True
    # System（PID 4）存在但打不开：不能当成「已经没了」，它的路径取不到
    path4, gone4 = locks._proc_exe(4)
    assert gone4 is False
    assert path4 == ""


def test_find_holders_ignores_exited_processes(tmp_path, monkeypatch):
    """RM 快照里的已退出进程不该出现在清单里。

    列出来用户就会以为还要关它，而它已经不占着任何东西了。
    """
    path = tmp_path / "x.txt"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(locks, "_query",
                        lambda chunk: [locks.ProcessInfo("幽灵", 999999999),
                                       locks.ProcessInfo("活的", os.getpid())])

    holders = locks.find_holders([path])

    assert [h.name for h in holders] == ["活的"], holders


# --- 定位占用者：要能判断「该不该动手」 -----------------------------------

def test_find_holders_marks_a_normal_app_closeable(tmp_path, spawn_holder):
    path = tmp_path / "x.txt"
    path.write_text("x", encoding="utf-8")
    proc = spawn_holder(path)

    holders = locks.find_holders([path])

    mine = [h for h in holders if h.pid == proc.pid]
    assert mine, f"没认出替身进程：{holders}"
    assert mine[0].can_close is True
    assert mine[0].exe.lower().endswith("python.exe")


def test_find_holders_marks_our_own_process_protected(tmp_path):
    """工具自己占着文件时必须标成不可关——否则会把自己关掉。"""
    path = tmp_path / "x.txt"
    path.write_text("x", encoding="utf-8")
    handle = _hold(path)
    try:
        holders = locks.find_holders([path])
    finally:
        _release(handle)

    me = [h for h in holders if h.pid == os.getpid()]
    assert me, f"没认出自己：{holders}"
    assert me[0].can_close is False
    assert "本工具自己" in me[0].protected_reason


# --- 关闭：护栏优先 -------------------------------------------------------

def test_shutdown_refuses_when_a_protected_process_is_in_the_batch(tmp_path):
    """批次里混进受保护进程时，**一个都不关**。

    这是整套设计里最容易出事的地方：`RmShutdown` 是整批语义，放行就等于把
    explorer / lsass 之类一起关掉。所以宁可这次不动，也不能顺带关错。
    """
    path = tmp_path / "x.txt"
    path.write_text("x", encoding="utf-8")
    handle = _hold(path)
    try:
        result = locks.shutdown_holders([path])
    finally:
        _release(handle)

    assert result.ok is False
    assert result.started is False, "被护栏拦下 = 强制也走同一条路，界面不该给那个按钮"
    assert "不会关闭" in result.message
    assert "本工具自己" in result.message


def test_shutdown_reports_when_there_is_nothing_to_register():
    result = locks.shutdown_holders([])
    assert result.ok is False and result.message


def test_shutdown_actually_closes_a_plain_holder(tmp_path, spawn_holder):
    """真调一次 RmShutdown——只对自建的替身进程。

    这台机器上优雅关闭对**无窗口的控制台进程**不一定生效：RmShutdown 走的是
    窗口消息通道，没有窗口的进程收不到，返回 351（部分成功）且进程不退出。
    所以这里不把「优雅关闭必然让替身退出」当断言——那是环境相关的，换台机器
    就可能翻。

    要盯的两件事是：
      1. 优雅关闭确实**发起过**（started=True），且没把替身误判成受保护进程
         （替身不在黑名单，护栏不该拦它——拦了说明黑名单误伤面太宽）；
      2. force=True 必然真关掉替身（这一步走 TerminateProcess，与窗口无关）。
    """
    path = tmp_path / "x.txt"
    path.write_text("x", encoding="utf-8")
    proc = spawn_holder(path)

    result = locks.shutdown_holders([path])

    assert result.started is True, "替身不在黑名单，优雅关闭应当发起过"
    if result.ok:
        # 这台机器上优雅关闭够用——替身已退出
        deadline = time.time() + 10
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
        assert proc.poll() is not None, "优雅关闭报成功，替身却没退出"
        return

    # 优雅关闭没成（351 之类）：force_may_help 必须为真，且 force 真能关掉
    assert result.force_may_help is True, "优雅没成，应当允许 force 再试"
    force_result = locks.shutdown_holders([path], force=True)
    assert force_result.ok is True, force_result.message
    deadline = time.time() + 15
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.2)
    assert proc.poll() is not None, "强制结束也没让替身退出"

