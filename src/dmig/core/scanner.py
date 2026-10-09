"""预扫描：统计真实数据量、收集链接计划与风险清单，全程只读。"""
from __future__ import annotations

import os
from pathlib import Path

from . import guard, linker
from .fsutil import long_path, rel_to, reparse_kind, volume_free_bytes
from .models import EntryKind, LinkPlan, LinkTargetClass, MigrationPlan, PlanTotals

LINK_KINDS = (EntryKind.JUNCTION, EntryKind.SYMLINK_DIR, EntryKind.SYMLINK_FILE)


def iter_entries(root):
    """深度优先生成真实条目；遇到重解析点只产出自身，绝不进入其目标。"""
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as it:
                entries = list(it)
        except PermissionError as exc:
            raise exc
        for entry in entries:
            try:
                st = os.lstat(entry.path)
            except OSError:
                continue
            rel = rel_to(entry.path, root)
            yield entry.path, rel, st
            if reparse_kind(st) is None and entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)


def scan(source, target, mappings: dict[str, str] | None = None,
         on_progress=None) -> MigrationPlan:
    source = Path(source)
    target = Path(target)
    mappings = mappings or {}

    plan = MigrationPlan(source=source, target=target)
    totals = PlanTotals()
    link_targets: list[str] = []

    stack = [str(source)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as it:
                entries = list(it)
        except PermissionError:
            plan.permission_issues.append(current)
            plan.needs_elevation = True
            continue
        except OSError:
            continue

        for entry in entries:
            try:
                st = os.lstat(entry.path)
            except OSError:
                continue
            rel = rel_to(entry.path, source)
            kind = reparse_kind(st)

            if kind in LINK_KINDS:
                try:
                    raw = os.readlink(entry.path)
                except OSError:
                    continue
                resolved = linker.resolve_target(
                    raw, entry.path, str(source), str(target), mappings)
                is_dir = kind in (EntryKind.JUNCTION, EntryKind.SYMLINK_DIR)
                plan.links.append(LinkPlan(rel=rel, kind=kind, raw_target=raw,
                                           new_target=resolved.new_target,
                                           target_class=resolved.target_class,
                                           is_dir=is_dir,
                                           is_dangling=resolved.is_dangling))
                if resolved.target_class is LinkTargetClass.OUTSIDE_SOURCE:
                    link_targets.append(resolved.new_target)
                if on_progress:
                    on_progress(rel)
                continue

            if kind is EntryKind.UNKNOWN_REPARSE:
                plan.unknown_reparse.append(rel)
                continue

            if entry.is_dir(follow_symlinks=False):
                totals.dirs += 1
                stack.append(entry.path)
            else:
                totals.files += 1
                totals.bytes += int(st.st_size)
                if on_progress:
                    on_progress(rel)

    plan.totals = totals
    plan.required_bytes = totals.bytes
    plan.link_groups = linker.group_targets(link_targets)
    try:
        base = target if target.exists() else target.parent
        plan.free_bytes = volume_free_bytes(str(base))
    except OSError:
        plan.free_bytes = 0
    plan.target_not_empty = bool(target.exists() and any(target.iterdir()))
    return plan
