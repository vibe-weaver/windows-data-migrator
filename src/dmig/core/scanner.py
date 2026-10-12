"""预扫描：统计真实数据量、收集链接计划与风险清单，全程只读。"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from . import linker
from .fsutil import (has_alternate_streams, is_excluded, is_hardlinked, is_link_kind,
                     is_sparse, long_path, rel_to, reparse_kind, strip_long_prefix,
                     volume_free_bytes)
from .models import (EntryKind, LinkPlan, LinkTargetClass, MigrationPlan,
                     PlanTotals, UnknownReparse)

PROGRESS_INTERVAL = 0.1


def scan(source, target, mappings: dict[str, str] | None = None,
         on_progress=None, skip_unknown_reparse: bool = False,
         excludes=(), probe_ads: bool = True) -> MigrationPlan:
    source = Path(source)
    target = Path(target)
    mappings = mappings or {}
    # 排除项来自用户输入或界面上那份计划里的无权限清单，形态不一（正/反斜杠、
    # 尾部分隔符），这里统一取成绝对路径，三条遍历共用同一份。
    excludes = [os.path.abspath(str(item)) for item in excludes if str(item).strip()]

    plan = MigrationPlan(source=source, target=target, excludes=excludes)
    plan.ads_probed = probe_ads
    totals = PlanTotals()
    link_targets: list[str] = []
    last_emit = 0.0

    def emit(rel: str) -> None:
        """节流上报进度：第一条必发，之后每 0.1 秒最多一条。

        逐个文件上报会把 UI 淹掉——实测 10594 个文件就是 10594 次跨线程信号，
        Qt 事件队列排不完，整个扫描期间界面都是卡的。
        """
        nonlocal last_emit
        now = time.monotonic()
        if last_emit and now - last_emit < PROGRESS_INTERVAL:
            return
        last_emit = now
        if on_progress:
            on_progress(rel)

    stack = [str(source)]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(long_path(current))
        except PermissionError:
            plan.permission_issues.append(current)
            plan.needs_elevation = True
            continue
        except OSError:
            continue
        # 惰性迭代，不先 list(it)：单个目录有几十万条目时，把整份清单物化下来
        # 只是白白制造一波内存峰值。句柄在 with 里关掉；遍历期间只把子目录压栈、
        # 不递归，所以不会同时开着一堆目录句柄。
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    # 目录读到一半出错（网络盘断开、条目被并发删掉）：保留已经扫到的
                    # 部分就跳出这个目录。旧写法靠 list(it) 那层的 except 兜住，
                    # 换成惰性迭代后必须在这里兜，否则一个坏条目会把整趟扫描带崩。
                    break
                if is_excluded(entry.path, excludes):
                    plan.excluded.append(rel_to(entry.path, source))
                    continue
                try:
                    st = os.lstat(entry.path)
                except OSError:
                    continue
                rel = rel_to(entry.path, source)
                kind = reparse_kind(st)

                if is_link_kind(kind):
                    try:
                        # readlink 在 Windows 上会把 junction 的 NT 前缀 \\??\ 归一化成
                        # \\?\；raw_target 原样落库会让报告/CSV 暴露这个前缀，这里先剥掉，
                        # 与 new_target 保持一致。resolve_target 内部也会剥，传剥过的值无副作用。
                        raw = strip_long_prefix(os.readlink(entry.path))
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
                    if kind is EntryKind.SYMLINK_FILE:
                        # 只有**文件**符号链接非特权不可：目录符号链接能降级成 junction。
                        plan.file_symlinks += 1
                    if resolved.target_class is LinkTargetClass.OUTSIDE_SOURCE:
                        link_targets.append(resolved.origin_target)
                    emit(rel)
                    continue

                if kind is EntryKind.UNKNOWN_REPARSE:
                    # 云占位符、AppExecLink、Dedup 这类都带真实数据语义，
                    # 默认当内容复制；跳过它们就等于把唯一副本留在备份里等删。
                    is_dir = bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
                    plan.unknown_reparse.append(
                        UnknownReparse(rel=rel, tag=getattr(st, "st_reparse_tag", 0),
                                       is_dir=is_dir))
                    emit(rel)
                    if skip_unknown_reparse:
                        continue
                    if is_dir:
                        totals.dirs += 1
                    else:
                        totals.files += 1
                        totals.bytes += int(st.st_size)
                    continue

                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue                # 遍历途中消失或读不到，跳过即可
                if is_dir:
                    totals.dirs += 1
                    stack.append(entry.path)
                else:
                    totals.files += 1
                    totals.bytes += int(st.st_size)
                    _note_lossy_attributes(plan, entry.path, st, probe_ads)
                    emit(rel)

    plan.totals = totals
    plan.required_bytes = totals.bytes
    plan.skip_unknown_reparse = skip_unknown_reparse
    plan.link_groups = linker.group_targets(link_targets, mappings)
    try:
        base = target if target.exists() else target.parent
        plan.free_bytes = volume_free_bytes(str(base))
    except OSError:
        plan.free_bytes = 0
    plan.target_not_empty = bool(target.exists() and any(target.iterdir()))
    return plan


def _note_lossy_attributes(plan: MigrationPlan, path, st, probe_ads: bool = True) -> None:
    """记下复制保不住的三类属性：稀疏、硬链接、附加数据流。

    「读字节再写出」这种复制方式对它们无一例外是破坏性的：稀疏文件的空洞被实化、
    硬链接拆成彼此独立的副本、ADS 干脆不带走。三样都不会报错，复制照样成功，
    所以只能在这里数出来提前说，否则用户看到「校验通过」会以为一个字节都没变。

    稀疏与硬链接复用 lstat 已经带回来的信息，零额外开销；ADS 要多问一次文件系统
    （FindFirstStreamW），**这是整个扫描里唯一新增的每文件调用**——几十万个文件下
    是可测的常数开销，所以留了 probe_ads 开关（命令行 --no-ads-scan）。关掉时
    ads_files 恒为 0，plan.ads_probed 会如实记为 False，不会把「没问过」当成「没有」。
    """
    if is_sparse(st):
        plan.sparse_files += 1
    if is_hardlinked(st):
        plan.hardlinked_files += 1
    if probe_ads and has_alternate_streams(path):
        plan.ads_files += 1
