import os
import sys
from pathlib import Path

import pytest

from dmig.core import linker
from dmig.core.models import LinkTargetClass

SRC = r"D:\myapp\data"
DST = r"E:\myapp\data"

# 注意：这些是纯字符串逻辑测试，路径不需要真实存在。
# 断链与否由 ResolvedTarget.is_dangling 表达，与 target_class 正交。


def test_relative_target_is_kept_as_is():
    r = linker.resolve_target(r"..\..\.pnpm\foo\node_modules\foo",
                              r"D:\myapp\data\node_modules\foo",
                              SRC, DST, {})
    assert r.target_class is LinkTargetClass.RELATIVE
    assert r.new_target == r"..\..\.pnpm\foo\node_modules\foo"


def test_absolute_target_inside_source_is_rewritten():
    raw = r"D:\myapp\data\node_modules\.pnpm\bar"
    r = linker.resolve_target(
        raw, r"D:\myapp\data\node_modules\bar", SRC, DST, {})
    assert r.target_class is LinkTargetClass.INSIDE_SOURCE
    assert r.new_target == r"E:\myapp\data\node_modules\.pnpm\bar"


def test_absolute_target_outside_source_is_kept():
    raw = r"C:\Users\me\AppData\Local\npm-cache\_npx\abc\node_modules\accepts"
    r = linker.resolve_target(
        raw, r"D:\myapp\data\node_modules\accepts", SRC, DST, {})
    assert r.target_class is LinkTargetClass.OUTSIDE_SOURCE
    assert r.new_target == raw
    # 本机上该路径不存在，但「关系」分类不受存在性影响
    assert r.is_dangling == (not os.path.exists(raw))


def test_long_prefix_form_is_normalised_before_rewrite():
    raw = "\\\\?\\D:\\myapp\\data\\x"
    r = linker.resolve_target(
        raw, r"D:\myapp\data\node_modules\x", SRC, DST, {})
    assert r.target_class is LinkTargetClass.INSIDE_SOURCE
    assert r.new_target == r"E:\myapp\data\x"


def test_user_mapping_overrides_outside_target():
    raw = r"C:\Users\me\AppData\Local\npm-cache\_npx\abc\node_modules\accepts"
    mappings = {r"C:\Users\me\AppData\Local\npm-cache": r"E:\npm-cache"}
    r = linker.resolve_target(
        raw, r"D:\myapp\data\node_modules\accepts", SRC, DST, mappings)
    assert r.target_class is LinkTargetClass.OUTSIDE_SOURCE
    assert r.new_target == r"E:\npm-cache\_npx\abc\node_modules\accepts"


def test_user_mapping_respects_boundary():
    r"""C:\cache 不能匹配 C:\cache2\..."""
    mappings = {r"C:\cache": r"E:\cache"}
    assert linker.apply_mappings(r"C:\cache2\x", mappings) == r"C:\cache2\x"
    assert linker.apply_mappings(r"C:\cache\a", mappings) == r"E:\cache\a"
    assert linker.apply_mappings(r"C:\cache", mappings) == r"E:\cache"


def test_dangling_target_keeps_class_and_flags_dangling():
    raw = r"C:\definitely\missing\path\xyz"
    r = linker.resolve_target(
        raw, r"D:\myapp\data\node_modules\zzz", SRC, DST, {})
    assert r.target_class is LinkTargetClass.OUTSIDE_SOURCE
    assert r.is_dangling is True
    assert r.new_target == raw


def test_dangling_inside_source_keeps_rewritten_path():
    raw = r"D:\myapp\data\gone\x"
    r = linker.resolve_target(
        raw, r"D:\myapp\data\node_modules\y", SRC, DST, {})
    assert r.target_class is LinkTargetClass.INSIDE_SOURCE
    assert r.new_target == r"E:\myapp\data\gone\x"


def test_group_targets_uses_longest_common_prefix():
    targets = [
        r"C:\Users\me\AppData\Local\npm-cache\_npx\a1\node_modules\accepts",
        r"C:\Users\me\AppData\Local\npm-cache\_npx\b2\node_modules\ajv",
    ]
    groups = linker.group_targets(targets)
    assert len(groups) == 1
    assert groups[0].prefix == r"C:\Users\me\AppData\Local\npm-cache\_npx"
    assert groups[0].count == 2


def test_group_targets_splits_by_drive():
    """每个盘符自成一组；组内只有一个目标时，前缀取该目标的父目录。"""
    groups = linker.group_targets([r"C:\cache\a\x", r"D:\other\b\y"])
    assert len(groups) == 2
    assert {g.prefix for g in groups} == {r"C:\cache\a", r"D:\other\b"}
    assert all(g.count == 1 for g in groups)


def test_single_target_under_drive_root_keeps_its_own_path():
    """单个目标、且它就挂在盘根下时，前缀不能退化成盘根。

    真实案例：源树里只有一条链接指向 `C:\\wwwwwww2`（在 C 盘根目录下）。
    取父目录得到 `C:\\` —— 归属表里显示一个「C:\\」，用户根本看不出它指哪个
    目录；而拿 `C:\\` 当映射键等于匹配整个 C 盘下的所有目标。
    这种情况目标自己才是合适的「链接目标目录」。

    多目标分支早就防了这一手（前缀退化为盘根时改用各自父目录），单目标分支漏了。
    """
    groups = linker.group_targets([r"C:\wwwwwww2"])
    assert len(groups) == 1
    assert groups[0].prefix == r"C:\wwwwwww2"
    assert groups[0].count == 1


def test_single_target_under_drive_root_can_still_be_mapped():
    """改成目标自己当前缀之后，填写「重写为」依然要生效。"""
    groups = linker.group_targets([r"C:\wwwwwww2"],
                                  {r"C:\wwwwwww2": r"E:\npm\wwwwwww2"})
    assert groups[0].mapped_to == r"E:\npm\wwwwwww2"


def test_group_targets_falls_back_to_parent_when_lcp_is_drive_root():
    """同盘但只在盘根处公共时，退化为按各自父目录分组。"""
    groups = linker.group_targets([r"C:\one\a", r"C:\two\b"])
    assert {g.prefix for g in groups} == {r"C:\one", r"C:\two"}


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_create_and_verify_junction(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "f.txt").write_text("ok", encoding="utf-8")
    link = tmp_path / "link"
    kind = linker.create(link, store, is_dir=True, prefer_junction=True)
    assert kind == "junction"
    assert linker.verify(link) is True


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_verify_detects_broken_link(tmp_path):
    missing = tmp_path / "missing"
    link = tmp_path / "link"
    linker.create(link, missing, is_dir=True, prefer_junction=True)
    assert linker.verify(link) is False


# --- 指向比对（C8：目标端每个链接都要能核对） -------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_points_to_matches_the_planned_target(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    link = tmp_path / "link"
    linker.create(link, store, is_dir=True, prefer_junction=True)
    assert linker.points_to(link, str(store)) is True
    assert linker.points_to(link, str(tmp_path / "elsewhere")) is False


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_points_to_reads_a_dangling_link(tmp_path):
    """断链的目标不存在，只能靠 readlink 读出指向——exists 那套在这种现场全是 False。"""
    missing = tmp_path / "vanished"
    link = tmp_path / "link"
    linker.create(link, missing, is_dir=True, prefer_junction=True)
    assert os.path.exists(link) is False
    assert linker.resolved_target(link) == str(missing)
    assert linker.points_to(link, str(missing)) is True


def test_resolved_target_of_a_missing_entry_is_empty(tmp_path):
    assert linker.resolved_target(tmp_path / "nope") == ""


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_points_to_resolves_a_relative_wanted_against_the_link_dir(tmp_path):
    """相对链接的计划目标就是那个相对值。

    不按链接所在目录解析就直接比，任意一条相对链接都会被判成「指错了」——
    续传会白拆重建一遍，C8 的逐链接核对更会把整趟迁移拦在最后一步。
    """
    real = tmp_path / "a" / "target"
    real.mkdir(parents=True)
    (tmp_path / "a" / "b").mkdir()
    link = tmp_path / "a" / "b" / "link"
    linker.create(link, real, is_dir=True, prefer_junction=True)

    assert linker.points_to(link, r"..\target") is True
    assert linker.points_to(link, r"..\other") is False


def test_group_targets_reports_applied_mapping():
    """A1：分组要回显已应用的映射，界面重扫后才能把值填回同一行。"""
    targets = [r"C:\cache\a\x", r"C:\cache\b\y"]
    groups = linker.group_targets(targets, {r"C:\cache": r"E:\cache"})
    assert len(groups) == 1
    assert groups[0].prefix == r"C:\cache"
    assert groups[0].mapped_to == r"E:\cache"


def test_group_targets_without_mapping_leaves_blank():
    groups = linker.group_targets([r"C:\cache\a\x", r"C:\cache\b\y"], {})
    assert groups[0].mapped_to == ""


def test_resolve_target_exposes_origin():
    """映射前的原始目标要单独返回，归属表按它分组。"""
    raw = r"C:\cache\_npx\abc"
    r = linker.resolve_target(raw, r"D:\src\nm\abc", SRC, DST,
                              {r"C:\cache": r"E:\cache"})
    assert r.origin_target == raw
    assert r.new_target == r"E:\cache\_npx\abc"
