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
