import os
import sys

import pytest

from dmig.core import reparse, scanner
from dmig.core.models import EntryKind, LinkTargetClass

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _build_tree(tmp_path):
    src = tmp_path / "src"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "a.txt").write_text("hello", encoding="utf-8")
    (src / "big.bin").write_bytes(b"0" * 2048)

    external = tmp_path / "external" / "pkg-x"
    external.mkdir(parents=True)
    (external / "index.js").write_text("x", encoding="utf-8")

    inside = src / "docs" / "inner"
    inside.mkdir()
    (inside / "i.txt").write_text("i", encoding="utf-8")

    (src / "nm").mkdir()
    reparse.create_junction(src / "nm" / "pkg-x", external)
    reparse.create_junction(src / "nm" / "inner-link", inside)
    return src, external, inside


def test_scan_counts_only_real_files(tmp_path):
    src, external, inside = _build_tree(tmp_path)
    plan = scanner.scan(src, tmp_path / "dst")
    # 真实文件：docs/a.txt、big.bin、docs/inner/i.txt、external/index.js 不算
    assert plan.totals.files == 3
    assert plan.totals.bytes == 5 + 2048 + 1
    assert plan.totals.dirs >= 3


def test_scan_does_not_follow_links(tmp_path):
    src, external, inside = _build_tree(tmp_path)
    plan = scanner.scan(src, tmp_path / "dst")
    rels = {link.rel for link in plan.links}
    assert "nm\\pkg-x" in rels and "nm\\inner-link" in rels
    assert all("external" not in link.rel for link in plan.links)


def test_scan_classifies_inside_and_outside_links(tmp_path):
    src, external, inside = _build_tree(tmp_path)
    dst = tmp_path / "dst"
    plan = scanner.scan(src, dst)
    by_rel = {link.rel: link for link in plan.links}

    outer = by_rel["nm\\pkg-x"]
    assert outer.kind is EntryKind.JUNCTION
    assert outer.target_class is LinkTargetClass.OUTSIDE_SOURCE
    assert outer.new_target == str(external)

    inner = by_rel["nm\\inner-link"]
    assert inner.target_class is LinkTargetClass.INSIDE_SOURCE
    assert inner.new_target == str(dst / "docs" / "inner")


def test_scan_applies_user_mapping_to_outside_target(tmp_path):
    src, external, inside = _build_tree(tmp_path)
    mapped_root = tmp_path / "moved-external"
    plan = scanner.scan(src, tmp_path / "dst",
                        mappings={str(tmp_path / "external"): str(mapped_root)})
    outer = next(l for l in plan.links if l.rel == "nm\\pkg-x")
    assert outer.new_target.startswith(str(mapped_root))


def test_scan_groups_external_targets(tmp_path):
    src, external, inside = _build_tree(tmp_path)
    plan = scanner.scan(src, tmp_path / "dst")
    assert plan.link_groups
    assert sum(g.count for g in plan.link_groups) == 1


def test_scan_sets_space_fields(tmp_path):
    src, external, inside = _build_tree(tmp_path)
    plan = scanner.scan(src, tmp_path / "dst")
    assert plan.required_bytes == 2054
    assert plan.free_bytes > 0


def test_scan_reports_permission_issue(tmp_path, monkeypatch):
    src, external, inside = _build_tree(tmp_path)
    real_scandir = os.scandir

    def fake_scandir(path):
        if os.path.basename(str(path)) == "docs":
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    plan = scanner.scan(src, tmp_path / "dst")
    assert any("docs" in p for p in plan.permission_issues)
    assert plan.needs_elevation is True


def test_scan_counts_non_link_reparse_as_content(tmp_path, make_reparse_file):
    """A3：非链接类重解析点默认计入真实数据量，并列入清单。"""
    src = tmp_path / "src"
    src.mkdir()
    make_reparse_file(src / "app.exe")
    plan = scanner.scan(src, tmp_path / "dst")
    assert plan.totals.files == 1
    assert [u.rel for u in plan.unknown_reparse] == ["app.exe"]
    assert plan.unknown_reparse[0].tag == 0x8000001B
    assert plan.unknown_reparse[0].is_dir is False
    assert plan.skip_unknown_reparse is False


def test_scan_marks_dir_shaped_reparse(tmp_path, make_reparse_file):
    src = tmp_path / "src"
    src.mkdir()
    make_reparse_file(src / "container", is_dir=True)
    plan = scanner.scan(src, tmp_path / "dst")
    assert plan.totals.dirs >= 1
    assert plan.totals.files == 0
    assert plan.unknown_reparse[0].is_dir is True


def test_scan_can_skip_non_link_reparse(tmp_path, make_reparse_file):
    src = tmp_path / "src"
    src.mkdir()
    make_reparse_file(src / "app.exe")
    (src / "real.txt").write_text("x", encoding="utf-8")
    plan = scanner.scan(src, tmp_path / "dst", skip_unknown_reparse=True)
    assert plan.totals.files == 1          # 只剩真实文件
    assert len(plan.unknown_reparse) == 1  # 清单里仍然列明
    assert plan.skip_unknown_reparse is True


def test_groups_use_origin_and_report_mapping(tmp_path):
    """A1：归属表按映射前的原始目标分组，并回显已应用的映射。

    按映射后的路径分组会让用户填一次映射、重扫一次就换了行，
    「重写为」列永远对不上自己刚填的那一行。
    """
    src, external, inside = _build_tree(tmp_path)
    moved = tmp_path / "moved-external"
    plan = scanner.scan(src, tmp_path / "dst",
                        mappings={str(tmp_path / "external"): str(moved)})
    assert len(plan.link_groups) == 1
    group = plan.link_groups[0]
    assert group.prefix == str(tmp_path / "external")   # 原始位置，不是映射后的
    assert group.mapped_to == str(moved)


class _FakeClock:
    """每次读取前进 step 秒，让节流断言与机器速度无关。"""

    def __init__(self, step: float = 0.001):
        self.now = 1000.0          # 起点非零：last_emit 用 0.0 表示「还没发过」
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_scan_progress_is_throttled(tmp_path, monkeypatch):
    """B5：进度必须节流，逐个文件上报会把 UI 淹掉。

    实测 10594 个文件就是 10594 次跨线程信号，Qt 事件队列排不完，
    整个扫描期间界面都是卡的。
    """
    src = tmp_path / "src"
    src.mkdir()
    total = 120
    for index in range(total):
        (src / f"f{index:04d}.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(scanner.time, "monotonic", _FakeClock(step=0.001))
    seen: list[str] = []
    plan = scanner.scan(src, tmp_path / "dst", on_progress=seen.append)

    assert plan.totals.files == total
    assert seen, "第一条进度必发，否则小目录树扫完一条都看不到"
    # 0.1 秒的节流窗口、每次调用前进 1ms，120 个文件最多两三条
    assert len(seen) <= 3, f"{total} 个文件发出了 {len(seen)} 条进度"


def test_scan_counts_file_symlinks(tmp_path, monkeypatch):
    """B4：只有**文件**符号链接非特权不可，计划要单独数出来。

    本机没有 SeCreateSymbolicLinkPrivilege，实测连用 FSCTL_SET_REPARSE_POINT
    手工造一个符号链接都被拒（WinError 1314），造不出真身，只能在分类层替身：
    把树里已有的 junction 报成 SYMLINK_FILE。真实建链行为由 test_linker /
    test_copier 覆盖，这一条盯的是计数口径——它决定执行前要不要拦下整趟迁移。
    """
    src, external, inside = _build_tree(tmp_path)
    real_kind = scanner.reparse_kind

    def as_file_symlink(st):
        kind = real_kind(st)
        return EntryKind.SYMLINK_FILE if scanner.is_link_kind(kind) else kind

    monkeypatch.setattr(scanner, "reparse_kind", as_file_symlink)
    plan = scanner.scan(src, tmp_path / "dst")

    assert len(plan.links) == 2
    assert plan.file_symlinks == 2
    assert all(link.is_dir is False for link in plan.links)


def test_junctions_are_not_counted_as_file_symlinks(tmp_path):
    """junction 与目录符号链接都不需要特权，不该被算进 file_symlinks。"""
    src, external, inside = _build_tree(tmp_path)
    plan = scanner.scan(src, tmp_path / "dst")
    assert len(plan.links) == 2
    assert plan.file_symlinks == 0


# --- C4：排除目录 -----------------------------------------------------

def test_excluded_subtree_is_not_counted(tmp_path):
    """排除项连统计都不该进：否则真实数据量、空间校验与预计耗时全都虚高。"""
    src = tmp_path / "src"
    (src / "keep").mkdir(parents=True)
    (src / "keep" / "a.txt").write_text("a", encoding="utf-8")
    (src / "junk").mkdir()
    (src / "junk" / "big.bin").write_bytes(b"x" * 5000)

    plan = scanner.scan(src, tmp_path / "dst", excludes=[str(src / "junk")])
    assert plan.totals.files == 1
    assert plan.totals.dirs == 1
    assert plan.required_bytes == 1
    assert plan.excluded == ["junk"]
    assert plan.excludes == [str(src / "junk")]


def test_excluded_junction_is_not_planned(tmp_path):
    """被排除目录里的链接也不该进计划，否则目标端会多出一条指向外部的链接。"""
    src, external, inside = _build_tree(tmp_path)
    plan = scanner.scan(src, tmp_path / "dst", excludes=[str(src / "nm")])
    assert plan.links == []
    assert plan.excluded == ["nm"]


def test_unreadable_directory_can_be_excluded_instead_of_denied(tmp_path, monkeypatch):
    """拿不到权限的目录不该只有「整趟拒绝执行」一条路。"""
    src = tmp_path / "src"
    (src / "locked").mkdir(parents=True)
    (src / "open.txt").write_text("x", encoding="utf-8")
    real_scandir = os.scandir

    def fake_scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    denied = scanner.scan(src, tmp_path / "dst")
    assert denied.permission_issues and denied.needs_elevation is True

    plan = scanner.scan(src, tmp_path / "dst", excludes=[str(src / "locked")])
    assert plan.permission_issues == []
    assert plan.excluded == ["locked"]
    assert plan.totals.files == 1
