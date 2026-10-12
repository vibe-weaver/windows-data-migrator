import ctypes
import errno
import os
import stat
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from dmig.core import copier, reparse, scanner
from dmig.core.models import EntryKind
from dmig.core.session import Session

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")

# 备用数据流（ADS）只能走 Win32：Python 的 open() 不认 `文件:流名` 这种路径。
_ads32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ads32.CreateFileW.restype = wintypes.HANDLE
_ads32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                               wintypes.HANDLE]
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
CREATE_ALWAYS, OPEN_EXISTING = 2, 3


def _ads_handle(path, stream, access, creation):
    handle = _ads32.CreateFileW(f"{path}:{stream}", access, 0x1 | 0x2, None,
                                creation, 0x80, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        raise OSError(f"打不开数据流 {path}:{stream}")
    return handle


def _write_ads(path, stream, data: bytes) -> None:
    handle = _ads_handle(path, stream, GENERIC_WRITE, CREATE_ALWAYS)
    try:
        written = wintypes.DWORD(0)
        _ads32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
    finally:
        _ads32.CloseHandle(handle)


def _read_ads(path, stream) -> bytes:
    handle = _ads_handle(path, stream, GENERIC_READ, OPEN_EXISTING)
    try:
        size = os.path.getsize(path) + 4096          # 够装下这个测试的数据
        buf = ctypes.create_string_buffer(max(size, 4096))
        read = wintypes.DWORD(0)
        _ads32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
        return buf.raw[:read.value]
    finally:
        _ads32.CloseHandle(handle)


def _tree(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "one.txt").write_text("one", encoding="utf-8")
    (src / "two.bin").write_bytes(b"2" * 1000)
    external = tmp_path / "ext" / "pkg"
    external.mkdir(parents=True)
    (external / "index.js").write_text("js", encoding="utf-8")
    reparse.create_junction(src / "link-pkg", external)
    return src, external


def _run(tmp_path, src, **kw):
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    return dst, copier.run(plan, sess, **kw), sess


def test_copies_real_files_and_preserves_content(tmp_path):
    src, external = _tree(tmp_path)
    dst, result, sess = _run(tmp_path, src)
    assert (dst / "a" / "one.txt").read_text(encoding="utf-8") == "one"
    assert (dst / "two.bin").read_bytes() == b"2" * 1000
    assert result.copied_files == 2
    assert result.copied_bytes == 1003
    assert not result.failures
    assert sess.done["files"] == 2


def test_recreates_link_without_copying_target(tmp_path):
    src, external = _tree(tmp_path)
    dst, result, sess = _run(tmp_path, src)
    link = dst / "link-pkg"
    assert os.lstat(link).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    # 链接目录里只有来自 external 的内容（external 只有 1 个文件）
    assert (link / "index.js").read_text(encoding="utf-8") == "js"
    assert sess.links and sess.links[0]["rel"] == "link-pkg"


def test_skips_already_copied_files_on_resume(tmp_path):
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    copier.run(plan, sess)
    first = sess.done["files"]

    sess2 = Session.load(sess.path)
    result2 = copier.run(plan, sess2)
    assert result2.skipped == first
    assert sess2.done["files"] == first


def test_skipped_files_are_not_reported_as_copied(tmp_path):
    """C7：全部跳过的续传原来会报「成功复制 2」，实际一个字节都没写。"""
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    copier.run(plan, sess)

    seen = []
    resumed = Session.load(sess.path)
    result = copier.run(plan, resumed, on_progress=seen.append)
    assert (result.copied_files, result.copied_bytes) == (0, 0)
    assert result.skipped == 2
    assert result.skipped_bytes == 1003
    # 进度条要用「已处理」口径，否则全量续传跑完只走到 0%
    assert result.accounted_bytes == 1003
    assert seen[-1].done_bytes == plan.totals.bytes
    assert seen[-1].done_files == 2


def test_progress_callback_reports_bytes(tmp_path):
    src, external = _tree(tmp_path)
    seen = []
    dst, result, sess = _run(tmp_path, src, on_progress=seen.append)
    assert seen, "未收到进度回调"
    assert seen[-1].done_bytes == result.copied_bytes
    assert seen[-1].total_bytes == 1003


def test_cancel_stops_early_and_reports(tmp_path):
    src, external = _tree(tmp_path)
    state = {"n": 0}

    def should_cancel():
        state["n"] += 1
        return state["n"] > 1

    dst, result, sess = _run(tmp_path, src, should_cancel=should_cancel)
    assert result.cancelled is True


_CHILD_LOCK_CODE = """
import ctypes, sys, time
from ctypes import wintypes
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
h = k32.CreateFileW(sys.argv[1], 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    print('FAIL', ctypes.get_last_error(), flush=True)
    sys.exit(1)
print('HELD', flush=True)
time.sleep(30)
"""


def test_locked_file_is_retried_then_recorded(tmp_path, monkeypatch):
    """子进程用 dwShareMode=0 独占文件，逼出重试 + 占用进程定位路径。

    不能用 Python 的 open()：CPython 在 Windows 上默认共享打开，锁不住。
    """
    src, external = _tree(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", _CHILD_LOCK_CODE,
                              str(src / "two.bin")],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "HELD", "子进程未能独占打开文件"
        time.sleep(0.5)
        monkeypatch.setattr(copier, "RETRY_DELAYS", (0.01, 0.01))
        dst, result, sess = _run(tmp_path, src)
        assert result.failures, "独占打开的文件竟然复制成功了"
        failure = result.failures[0]
        assert failure.reason == "locked"
        assert failure.process, "应记录占用进程"
        assert sess.failed_rels() == {"two.bin"}
    finally:
        child.kill()
        child.wait(timeout=10)


def test_copy_one_reports_bytes(tmp_path):
    src = tmp_path / "s.bin"
    src.write_bytes(b"z" * 333)
    dst = tmp_path / "d.bin"
    assert copier.copy_one(src, dst) == 333
    assert dst.read_bytes() == b"z" * 333


# --- 复制原语：能走内核就走内核 -----------------------------------------
#
# 实测 256 字节的小文件：Python 读写循环 532.9 微秒/个，CopyFileW 334.1 微秒/个
# ——2.1 倍。文件多的场景瓶颈是每文件的系统调用延迟而不是带宽，所以这条最有效。
# 但两类情况必须退回分块循环，下面的测试把它们钉住。

def test_kernel_primitive_is_used_for_a_plain_copy(tmp_path, monkeypatch):
    used = []
    monkeypatch.setattr(copier, "_copyfile_kernel",
                        lambda s, d: used.append((s, d)))
    src = tmp_path / "s.bin"
    src.write_bytes(b"z" * 10_000)

    copier.copy_one(src, tmp_path / "d.bin")

    assert len(used) == 1, "普通复制没走内核"


def test_kernel_primitive_is_not_used_for_resume(tmp_path, monkeypatch):
    """断点续传要从中间接着写——CopyFileW 没有偏移续传，必须走分块循环。"""
    used = []
    monkeypatch.setattr(copier, "_copyfile_kernel",
                        lambda s, d: used.append((s, d)))
    src = tmp_path / "s.bin"
    src.write_bytes(b"abcdefghij")
    dst = tmp_path / "d.bin"
    dst.write_bytes(b"abcde")

    written = copier.copy_one(src, dst, offset=5)

    assert used == [], "续传走了内核路径（会从头覆盖，不是接着写）"
    assert written == 5
    assert dst.read_bytes() == b"abcdefghij"


def test_kernel_primitive_is_not_used_when_chunk_progress_is_wanted(
        tmp_path, monkeypatch):
    """调用方要逐块进度时，只有分块循环报得出来。"""
    used = []
    monkeypatch.setattr(copier, "_copyfile_kernel",
                        lambda s, d: used.append((s, d)))
    src = tmp_path / "s.bin"
    src.write_bytes(b"z" * (3 * copier.CHUNK + 7))
    chunks = []

    copier.copy_one(src, tmp_path / "d.bin", on_chunk=chunks.append)

    assert used == []
    assert sum(chunks) == 3 * copier.CHUNK + 7


def test_kernel_copy_preserves_alternate_data_streams(tmp_path):
    """**内核复制会连 ADS 一起带过去，分块循环只读主数据流。**

    这条钉住的是顺带补上的一个数据丢失点：那些带备用数据流的文件（下载来的文件
    常带 Zone.Identifier）以前到目标端就只剩主数据流了。
    """
    src = tmp_path / "s.bin"
    src.write_bytes(b"main")
    _write_ads(src, "Zone.Identifier", b"[ZoneTransfer]\r\nZoneId=3\r\n")

    dst = tmp_path / "d.bin"
    copier.copy_one(src, dst)

    assert dst.read_bytes() == b"main"
    assert _read_ads(dst, "Zone.Identifier") == b"[ZoneTransfer]\r\nZoneId=3\r\n"


def test_a_failed_kernel_copy_raises_with_winerror(tmp_path):
    """失败必须带 `winerror`——`fatal_reason` 正是靠它认「磁盘写满」这类不可重试。

    走 Python 循环时异常来自内置文件对象，自带 errno/winerror；换成 ctypes 调用
    之后必须自己保证这一点，否则磁盘满会被当成普通错误反复重试
    （1+3+10 秒 × 每个文件）。
    """
    missing = tmp_path / "不存在.bin"

    with pytest.raises(OSError) as info:
        copier.copy_one(missing, tmp_path / "out.bin")

    assert info.value.winerror == 2, info.value.winerror      # ERROR_FILE_NOT_FOUND
    assert copier.fatal_reason(info.value) == ""              # 非致命，可重试



def test_same_size_residue_is_overwritten_not_skipped(tmp_path):
    """A2 回归：目标端同大小的残留文件必须被真实内容覆盖。

    修复前只比大小就跳过，残留内容原样留下，默认校验又看不出差别，
    于是「校验通过 → 改名备份 → 删备份」把源数据永久丢掉。
    这里把残留的时间戳也设成与源完全相同，复现最坏情况：实测同一系统
    时钟节拍（约 15ms）内写入的两个文件 st_mtime_ns 完全相等，
    任何基于大小/时间戳的启发式都区分不出来，只能靠会话记账。
    """
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "two.bin").write_bytes(b"9" * 1000)      # 同大小、内容不同的残留
    st = os.stat(src / "two.bin")
    os.utime(dst / "two.bin", ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(dst / "two.bin").st_mtime_ns == st.st_mtime_ns

    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    assert sess.completed_rels() == set(), "全新会话不该有任何记账"
    result = copier.run(plan, sess)
    assert (dst / "two.bin").read_bytes() == b"2" * 1000
    assert result.skipped == 0
    assert not result.failures


def test_resume_skips_only_files_this_session_copied(tmp_path):
    """续传时按会话记账跳过，且跳过的那份确实与源一致。"""
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    first = copier.run(plan, sess)
    assert first.skipped == 0
    assert sess.completed_rels() == {"two.bin", os.path.join("a", "one.txt")}

    resumed = Session.load(sess.path)
    second = copier.run(plan, resumed)
    assert second.skipped == first.copied_files
    assert not second.failures
    assert (dst / "two.bin").read_bytes() == b"2" * 1000


def test_recorded_but_modified_target_is_recopied(tmp_path):
    """记账过、但目标端那份后来被改动过 → 必须重新复制。

    时间戳要显式挪开，不能靠「刚写过所以时间戳一定变了」：写入的 mtime 取自系统
    时钟，而 Windows 的时钟节拍约 15ms，源文件与这次写入落在同一刻度里时两边会
    精确相等，跳过判定成立、测试假通过。
    """
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    copier.run(plan, sess)

    modified = dst / "two.bin"
    modified.write_bytes(b"9" * 1000)                 # 同大小，内容变了
    stamp = os.stat(src / "two.bin").st_mtime + 60    # 时间戳也明确不同
    os.utime(modified, (stamp, stamp))

    resumed = Session.load(sess.path)
    result = copier.run(plan, resumed)
    assert (dst / "two.bin").read_bytes() == b"2" * 1000
    assert result.skipped == 1                        # 只有 a/one.txt 被跳过


def test_non_link_reparse_file_is_copied_verbatim(tmp_path, make_reparse_file,
                                                  raw_reparse_buffer):
    """A3：读不动的重解析点原样搬过去，逐字节一致。"""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    make_reparse_file(src / "sub" / "app.exe")
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    assert plan.totals.files == 1
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    result = copier.run(plan, sess)
    assert not result.failures
    assert reparse.read_raw(dst / "sub" / "app.exe") == raw_reparse_buffer(
        0x8000001B)


def test_non_link_reparse_dir_is_copied(tmp_path, make_reparse_file):
    """目录形态的非链接重解析点只会出现在 dirs 里，也必须被搬过去。"""
    src = tmp_path / "src"
    src.mkdir()
    make_reparse_file(src / "container", is_dir=True)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    result = copier.run(plan, sess)
    assert not result.failures
    st = os.lstat(dst / "container")
    assert getattr(st, "st_reparse_tag", 0) == 0x8000001B


def test_skip_unknown_reparse_leaves_it_out(tmp_path, make_reparse_file):
    """显式跳过时既不计数也不复制，但清单里仍要列明。"""
    src = tmp_path / "src"
    src.mkdir()
    make_reparse_file(src / "app.exe")
    (src / "real.txt").write_text("x", encoding="utf-8")
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst, skip_unknown_reparse=True)
    assert plan.totals.files == 1
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    result = copier.run(plan, sess)
    assert not result.failures
    assert not (dst / "app.exe").exists()
    assert (dst / "real.txt").read_text(encoding="utf-8") == "x"


def test_vanishing_directory_does_not_crash_the_run(tmp_path, monkeypatch):
    """B3 顺带加固：遍历途中目录被别的进程删掉，不该让整个任务崩掉。"""
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    real_lstat = os.lstat

    def flaky(path, *args, **kwargs):
        if os.path.basename(str(path)) == "a":
            raise FileNotFoundError(2, "系统找不到指定的文件。", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", flaky)
    result = copier.run(plan, sess)          # 不得抛异常
    assert result.copied_files == 1          # a/one.txt 随目录一起消失


def test_fatal_reason_maps_unrecoverable_errors():
    """B2：只有「重试也不会好转」的错误才算致命，普通拒绝访问不算。"""
    assert copier.fatal_reason(OSError(28, "磁盘空间不足", None, 112)) == "disk_full"
    assert copier.fatal_reason(OSError(0, "磁盘已满", None, 39)) == "disk_full"
    assert copier.fatal_reason(OSError(22, "客户端没有所需的特权", None, 1314)) \
        == "symlink_privilege"
    # 只带 errno（配额耗尽等路径）也要认出来
    assert copier.fatal_reason(OSError(errno.ENOSPC, "no space left")) == "disk_full"
    # 拒绝访问是可以通过关掉程序/提权恢复的，绝不能因此中止整趟
    assert copier.fatal_reason(OSError(13, "拒绝访问", None, 5)) == ""


def test_disk_full_aborts_whole_run_without_retrying(tmp_path, monkeypatch):
    """B2：磁盘写满必须立刻停，不能把剩下的文件逐个重试 14 秒。

    10 万个文件按 1+3+10 秒重试要耗 389 小时才失败完，而每一条失败都还要
    往已经满了的盘上再写一遍。
    """
    src = tmp_path / "src"
    src.mkdir()
    for index in range(6):
        (src / f"f{index}.bin").write_bytes(b"x" * 100)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")

    def full(*args, **kwargs):
        raise OSError(28, "磁盘空间不足", None, 112)

    monkeypatch.setattr(copier, "copy_one", full)
    started = time.monotonic()
    result = copier.run(plan, sess)          # 默认重试间隔是 1/3/10 秒
    elapsed = time.monotonic() - started

    assert result.aborted == "disk_full"
    assert len(result.failures) == 1, "整趟只该记一条致命失败，而不是每个文件一条"
    assert result.failures[0].reason == "disk_full"
    assert result.copied_files == 0
    assert elapsed < 1.0, f"致命错误竟然还在重试，耗时 {elapsed:.1f}s"
    assert sess.phase == "aborted"
    assert sess.failed_rels() == {"f0.bin"} or len(sess.failed_rels()) == 1


def test_symlink_privilege_failure_aborts_immediately(tmp_path, monkeypatch):
    """B2/B4：建链缺特权（WinError 1314）同样整趟中止，不逐条失败。"""
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")

    def denied(*args, **kwargs):
        raise OSError(22, "客户端没有所需的特权", None, 1314)

    monkeypatch.setattr(copier.linker, "create", denied)
    result = copier.run(plan, sess)
    assert result.aborted == "symlink_privilege"
    assert len(result.failures) == 1
    assert sess.phase == "aborted"


def test_links_are_created_before_bulk_copy(tmp_path, monkeypatch):
    """B4：链接重建排在批量复制之前，权限/ACL 问题几秒内就暴露。

    让每个文件的复制都失败，链接依然必须已经建好——原来链接是第 3 趟，
    几十万个文件复制完才轮到它，权限错误要等几小时才看得见。
    """
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    sess = Session.create(src, dst, root=tmp_path / "sessions")

    def denied(*args, **kwargs):
        raise OSError(13, "拒绝访问", None, 5)

    monkeypatch.setattr(copier, "copy_one", denied)
    result = copier.run(plan, sess, retry_delays=(0.0, 0.0))

    assert result.copied_files == 0
    assert result.aborted == "", "普通拒绝访问不该中止整趟"
    link = dst / "link-pkg"
    assert os.lstat(link).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert sess.links and sess.links[0]["rel"] == "link-pkg"


# --- C6：续传第二趟会再走一遍建链 -----------------------------------
def test_second_pass_leaves_a_correct_link_alone(tmp_path, monkeypatch):
    """已经指对的链接不能被拆了重建。

    删掉之后万一建不回来（缺特权、目标盘出问题），一条本来好好的链接就没了；
    而续传的每一趟都会把建链整批重走一遍，这不是边缘情况。
    """
    src, external = _tree(tmp_path)
    moved = tmp_path / "moved"
    dst = tmp_path / "dst"
    mappings = {str(external): str(moved)}
    first = Session.create(src, dst, root=tmp_path / "sessions")
    assert not copier.run(scanner.scan(src, dst, mappings=mappings), first).failures

    calls = []
    monkeypatch.setattr(copier.linker, "create",
                        lambda *a, **k: calls.append(a) or "junction")
    second = Session.create(src, dst, root=tmp_path / "sessions")
    result = copier.run(scanner.scan(src, dst, mappings=mappings), second)

    assert calls == []
    assert not result.failures
    # 记录照样要补上：会话里的 links 是报告与回滚的依据，不能因为跳过就缺项
    assert second.links[0]["rel"] == "link-pkg"
    assert second.links[0]["kind"] == "junction"
    assert second.links[0]["new_target"] == str(moved)


def test_second_pass_replaces_a_dangling_link(tmp_path):
    """目标端已有一条**指向不存在位置**的链接时必须换掉，而不是撞 WinError 183。

    这种链接 `os.path.exists` 报 False，原来那套跟着链接走的「已存在」判定
    整个漏掉它，紧接着 os.mkdir 就失败——续传死在一条早就建好的链接上。
    """
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "marker.txt").write_text("new", encoding="utf-8")
    dst.mkdir()
    reparse.create_junction(dst / "link-pkg", tmp_path / "gone")
    assert not os.path.exists(dst / "link-pkg")

    plan = scanner.scan(src, dst, mappings={str(external): str(moved)})
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    result = copier.run(plan, sess)

    assert not result.failures
    assert (dst / "link-pkg" / "marker.txt").read_text(encoding="utf-8") == "new"


def test_second_pass_replaces_a_link_pointing_somewhere_else(tmp_path):
    """上一趟没有映射、这一趟有：指错位置的链接要按新计划重建。"""
    src, external = _tree(tmp_path)
    dst = tmp_path / "dst"
    first = Session.create(src, dst, root=tmp_path / "sessions")
    assert not copier.run(scanner.scan(src, dst), first).failures
    assert (dst / "link-pkg" / "index.js").read_text(encoding="utf-8") == "js"

    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "marker.txt").write_text("new", encoding="utf-8")
    second = Session.create(src, dst, root=tmp_path / "sessions")
    result = copier.run(scanner.scan(src, dst, mappings={str(external): str(moved)}),
                        second)

    assert not result.failures
    assert (dst / "link-pkg" / "marker.txt").read_text(encoding="utf-8") == "new"
    assert not (dst / "link-pkg" / "index.js").exists()


# --- C9：目录时间戳 ---------------------------------------------------
def test_directory_timestamps_are_copied(tmp_path):
    """文件一直走 copystat，目录此前完全没管。

    目标端目录的 mtime 全是「刚刚」，依赖目录时间戳做增量同步或
    构建缓存失效判断的工具会把整棵树当成新内容重扫一遍。
    """
    src = tmp_path / "src"
    deep = src / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "f.txt").write_text("x", encoding="utf-8")
    old = 1_000_000_000                     # 2001-09-09，绝不会与「刚刚」混淆
    # 自深向浅设置，否则给上层写时间戳会把刚设好的下层值顶掉
    for level in (deep, src / "a", src):
        os.utime(level, (old, old))

    dst, result, sess = _run(tmp_path, src)

    assert not result.failures
    assert result.dir_times_failed == []
    for rel in (".", "a", os.path.join("a", "b")):
        src_dir = src if rel == "." else src / rel
        dst_dir = dst if rel == "." else dst / rel
        assert int(os.stat(dst_dir).st_mtime) == old, rel
        # 精确一致：copystat 按 ns 写回，NTFS 存 100ns 刻度
        assert os.stat(dst_dir).st_mtime == os.stat(src_dir).st_mtime, rel


# --- C4：排除目录 -----------------------------------------------------
def test_excluded_subtree_is_not_copied(tmp_path):
    """被排除的目录连目标端的目录项都不建：用户排除的就是「不要碰这些」。"""
    src = tmp_path / "src"
    (src / "keep").mkdir(parents=True)
    (src / "keep" / "a.txt").write_text("a", encoding="utf-8")
    (src / "junk").mkdir()
    (src / "junk" / "big.bin").write_bytes(b"x" * 5000)

    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst, excludes=[str(src / "junk")])
    result = copier.run(plan, Session.create(src, dst, root=tmp_path / "sessions"))

    assert not result.failures
    assert (dst / "keep" / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (dst / "junk").exists()
    assert result.copied_files == 1


def test_link_whose_target_is_excluded_is_not_rebuilt(tmp_path):
    """链接指向的源侧目录被 --exclude 排除时，目标端不会有那份数据，
    原样重建只会留一条指空的链接且无人知晓——跳过重建并记一笔到失败清单，
    让它出现在报告里，而不是沉默地建出断链。
    """
    src = tmp_path / "src"
    src.mkdir()
    inside = src / "inner"
    inside.mkdir()
    (inside / "i.txt").write_text("i", encoding="utf-8")
    (src / "keep.txt").write_text("k", encoding="utf-8")
    reparse.create_junction(src / "link-inner", inside)   # 指向源树内的 inner

    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst, excludes=[str(inside)])
    sess = Session.create(src, dst, root=tmp_path / "sessions")
    result = copier.run(plan, sess)

    reasons = [f.reason for f in result.failures]
    assert "link_target_excluded" in reasons, "应记录 link_target_excluded"
    assert not os.path.lexists(dst / "link-inner"), "不应在目标侧建出断链"
    assert (dst / "keep.txt").read_text(encoding="utf-8") == "k"
