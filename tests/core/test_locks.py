import subprocess
import sys
import time

import pytest

from dmig.core import locks

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


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
