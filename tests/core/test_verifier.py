import os
import sys
from pathlib import Path

import pytest

from dmig.core import linker, verifier
from dmig.core.models import (EntryKind, LinkPlan, LinkTargetClass,
                              MigrationPlan, PlanTotals)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _pair(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    for base in (src, dst):
        (base / "a").mkdir(parents=True)
        (base / "a" / "one.txt").write_text("one", encoding="utf-8")
        (base / "two.bin").write_bytes(b"2" * 500)
    return src, dst


def test_identical_trees_pass(tmp_path):
    src, dst = _pair(tmp_path)
    res = verifier.verify(src, dst)
    assert res.ok is True
    assert res.expected_files == res.actual_files == 2
    assert res.expected_bytes == res.actual_bytes == 503


def test_missing_file_is_reported(tmp_path):
    src, dst = _pair(tmp_path)
    (dst / "two.bin").unlink()
    res = verifier.verify(src, dst)
    assert res.ok is False
    assert "two.bin" in res.missing


def test_size_mismatch_is_reported(tmp_path):
    src, dst = _pair(tmp_path)
    (dst / "two.bin").write_bytes(b"2" * 100)
    res = verifier.verify(src, dst)
    assert res.ok is False
    assert "two.bin" in res.mismatch


def test_same_size_residue_with_different_mtime_is_reported(tmp_path):
    """A2 回归：目标端同大小的残留文件必须被默认校验抓出来。

    只比大小时，别人留下的同大小文件会被当成「已复制」放过，于是
    校验通过 → 源目录改名备份 → 删备份 → 源数据永久丢失。
    """
    src, dst = _pair(tmp_path)
    (dst / "two.bin").write_bytes(b"9" * 500)          # 同大小、不同内容
    stale = (src / "two.bin").stat().st_mtime - 86400  # 残留的时间戳与源不同
    os.utime(dst / "two.bin", (stale, stale))
    res = verifier.verify(src, dst)
    assert res.ok is False
    assert "two.bin" in res.mismatch


def test_deep_hash_detects_content_difference(tmp_path):
    """大小与时间戳都对得上时，只有哈希能发现内容已损坏。"""
    src, dst = _pair(tmp_path)
    (dst / "two.bin").write_bytes(b"9" * 500)   # 同大小不同内容
    st = os.stat(src / "two.bin")
    os.utime(dst / "two.bin", ns=(st.st_atime_ns, st.st_mtime_ns))
    assert verifier.verify(src, dst).ok is True
    deep = verifier.verify(src, dst, deep=True)
    assert deep.ok is False
    assert "two.bin" in deep.mismatch


def test_sha256_of_matches_hashlib(tmp_path):
    import hashlib
    f = tmp_path / "x.bin"
    payload = os.urandom(4096)
    f.write_bytes(payload)
    assert verifier.sha256_of(f) == hashlib.sha256(payload).hexdigest()


def test_unreadable_subdirectory_is_reported_not_raised(tmp_path, monkeypatch):
    """ACL 拒绝的子目录必须被记录为校验不通过，而不是让整趟校验抛异常。"""
    src, dst = _pair(tmp_path)
    locked = src / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("s", encoding="utf-8")

    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        if os.path.basename(str(path)) == "locked":
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    res = verifier.verify(src, dst)          # 不得抛异常
    assert res.ok is False
    assert "locked" in res.mismatch


def test_non_link_reparse_point_is_expected_at_target(tmp_path, make_reparse_file):
    """A3：非链接类重解析点要参与校验，目标端缺失必须报出来。"""
    src = tmp_path / "src"
    src.mkdir()
    make_reparse_file(src / "app.exe")
    dst = tmp_path / "dst"
    dst.mkdir()
    res = verifier.verify(src, dst)
    assert res.ok is False
    assert "app.exe" in res.missing


def test_skipped_reparse_is_not_expected(tmp_path, make_reparse_file):
    """显式跳过时，目标端本来就没有它，不能被当成缺失。"""
    import shutil

    src = tmp_path / "src"
    src.mkdir()
    make_reparse_file(src / "app.exe")
    (src / "real.txt").write_text("x", encoding="utf-8")
    dst = tmp_path / "dst"
    dst.mkdir()
    shutil.copy2(src / "real.txt", dst / "real.txt")

    res = verifier.verify(src, dst, skip_unknown_reparse=True)
    assert res.ok is True
    assert res.expected_files == 1


def test_verbatim_copied_reparse_passes_verify(tmp_path, make_reparse_file):
    """原样搬过去的重解析点，默认校验与 deep 校验都要通过。"""
    src = tmp_path / "src"
    src.mkdir()
    make_reparse_file(src / "app.exe")
    dst = tmp_path / "dst"
    dst.mkdir()
    from dmig.core import reparse
    reparse.copy_reparse_data(src / "app.exe", dst / "app.exe")

    assert verifier.verify(src, dst).ok is True
    assert verifier.verify(src, dst, deep=True).ok is True


class _FakeClock:
    """每次读取前进 step 秒，让节流断言与机器速度无关。"""

    def __init__(self, step: float = 0.001):
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _many_files(tmp_path, count: int = 120):
    src, dst = tmp_path / "src", tmp_path / "dst"
    for base in (src, dst):
        base.mkdir()
        for index in range(count):
            (base / f"f{index:04d}.txt").write_text("x", encoding="utf-8")
    return src, dst


def test_verify_reports_progress_with_totals(tmp_path):
    """B6：校验阶段要能驱动进度条，原来整趟一声不响。"""
    src, dst = _pair(tmp_path)
    seen = []
    res = verifier.verify(src, dst, on_progress=seen.append,
                          totals=PlanTotals(files=2, bytes=503))
    assert res.ok is True
    assert seen, "校验期间没有发出任何进度"
    last = seen[-1]
    assert last.phase == "verifying"
    assert last.total_files == 2
    assert last.total_bytes == 503
    assert last.done_files == res.actual_files == 2
    assert last.done_bytes == 503


def test_verify_progress_is_throttled(tmp_path, monkeypatch):
    """B6：进度按 0.1 秒节流，几十个文件不该发出几十次跨线程信号。"""
    src, dst = _many_files(tmp_path, 120)
    monkeypatch.setattr(verifier.time, "monotonic", _FakeClock(step=0.001))
    seen = []
    res = verifier.verify(src, dst, on_progress=seen.append,
                          totals=PlanTotals(files=120, bytes=120))
    assert res.ok is True
    assert res.actual_files == 120
    assert 1 <= len(seen) <= 4, f"120 个文件发出了 {len(seen)} 条进度"


def test_cancelled_verify_is_never_ok(tmp_path):
    """B6 的安全底线：半趟校验绝不能算通过。

    ok=True 会让 migrator 接着去改名源目录——把「没校验完」当成「校验通过」，
    等于拿源数据赌运气。
    """
    src, dst = _pair(tmp_path)
    res = verifier.verify(src, dst, should_cancel=lambda: True)
    assert res.cancelled is True
    assert res.ok is False
    assert not res.missing and not res.mismatch


def test_cancel_stops_walking_early(tmp_path):
    """取消要真的停下来，不是走完全程再打个标记。"""
    src, dst = _many_files(tmp_path, 120)
    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 3

    res = verifier.verify(src, dst, should_cancel=should_cancel)
    assert res.cancelled is True
    assert res.actual_files < 120


# --- C8：逐个核对目标端重建出来的链接 ---------------------------------

def _link(rel, new_target, *, raw="", dangling=False):
    return LinkPlan(rel=rel, kind=EntryKind.JUNCTION,
                    raw_target=raw or new_target, new_target=new_target,
                    target_class=LinkTargetClass.OUTSIDE_SOURCE,
                    is_dir=True, is_dangling=dangling)


def _plan(tmp_path, links):
    plan = MigrationPlan(source=tmp_path / "src", target=tmp_path / "dst")
    plan.links = links
    return plan


def test_verify_links_accepts_correctly_rebuilt_links(tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    linker.create(dst / "ok", store, is_dir=True, prefer_junction=True)
    assert verifier.verify_links(_plan(tmp_path, [_link("ok", str(store))]), dst) == []


def test_verify_links_reports_a_link_pointing_elsewhere(tmp_path):
    """C8：指错了在顶层那条源目录链接上完全看不出来，此前从不校验。

    目标恰好存在（别人留下的同名目录、映射少一个反斜杠）时，
    光看「链接能不能打开」是查不出来的，必须比对计划要求的指向。
    """
    dst = tmp_path / "dst"
    dst.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    linker.create(dst / "nm", wrong, is_dir=True, prefer_junction=True)
    assert verifier.verify_links(_plan(tmp_path, [_link("nm", str(store))]), dst) == ["nm"]


def test_verify_links_reports_a_missing_link(tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    assert verifier.verify_links(_plan(tmp_path, [_link("nm", str(store))]), dst) == ["nm"]


def test_verify_links_excuses_a_link_that_was_already_dangling(tmp_path):
    """断链按设计原样重建；拿可达性去卡会把整趟迁移拦在最后一步。"""
    dst = tmp_path / "dst"
    dst.mkdir()
    gone = tmp_path / "vanished"
    linker.create(dst / "gone", gone, is_dir=True, prefer_junction=True)
    plan = _plan(tmp_path, [_link("gone", str(gone), dangling=True)])
    assert verifier.verify_links(plan, dst) == []


def test_verify_links_still_catches_a_dangling_link_built_in_the_wrong_place(tmp_path):
    """断链只免除「可达」，不免除「指向」：建到别处照样要报出来。"""
    dst = tmp_path / "dst"
    dst.mkdir()
    linker.create(dst / "gone", tmp_path / "somewhere-else",
                  is_dir=True, prefer_junction=True)
    plan = _plan(tmp_path, [_link("gone", str(tmp_path / "vanished"), dangling=True)])
    assert verifier.verify_links(plan, dst) == ["gone"]


def test_verify_links_stops_when_cancelled(tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    plan = _plan(tmp_path, [_link(f"l{i}", str(store)) for i in range(5)])

    checks = {"n": 0}

    def should_cancel():
        checks["n"] += 1
        return checks["n"] > 2          # 第三次问的时候喊停

    assert verifier.verify_links(plan, dst, should_cancel=should_cancel) == ["l0", "l1"]


# --- C4：排除项在校验里同样生效 ---------------------------------------

def test_excluded_subtree_is_not_reported_missing(tmp_path):
    """排除项按计划就没复制，校验再报「缺失」就是假警报，会把整趟迁移拦下。"""
    src, dst = _pair(tmp_path)
    (src / "a" / "one.txt").unlink()
    (dst / "a" / "one.txt").unlink()

    res = verifier.verify(src, dst, excludes=[str(src / "a")])
    assert res.ok is True
    assert res.missing == []
    assert res.expected_files == 1        # a/one.txt 不算


def test_excluded_file_is_not_checked(tmp_path):
    """排除项也可能直接指向一个文件，不能只在下钻前剪枝。"""
    src, dst = _pair(tmp_path)
    (dst / "two.bin").unlink()

    res = verifier.verify(src, dst, excludes=[str(src / "two.bin")])
    assert res.ok is True
    assert res.missing == []
