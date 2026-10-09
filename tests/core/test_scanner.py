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
