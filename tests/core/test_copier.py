import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dmig.core import copier, reparse, scanner
from dmig.core.models import EntryKind
from dmig.core.session import Session

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


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
