"""复制结果的完整性校验：默认比文件数、字节数与时间戳，可选逐文件哈希。"""
from __future__ import annotations

import hashlib
import os
import stat
import time

from . import linker, reparse
from .fsutil import (MTIME_TOLERANCE_SECONDS, is_excluded, is_link_kind, long_path,
                     rel_to, reparse_kind)
from .models import Progress, VerifyResult

PROGRESS_INTERVAL = 0.1


def sha256_of(path) -> str:
    """整文件 SHA-256。

    用 hashlib.file_digest 而不是自己按块读：读与摘要都走 C 层，
    省掉每 1MB 一次 Python 层的 read/update 循环——deep 校验可能读几十 GB。
    """
    with open(long_path(path), "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def is_reparse_point(path) -> bool:
    try:
        return reparse_kind(os.lstat(long_path(path))) is not None
    except OSError:
        return False


def _walk_entries(root, on_error=None, excludes=()):
    """产出 (相对路径, 绝对路径, lstat 结果, 是否非链接重解析点)。

    任何重解析点都不下钻；链接项直接跳过（它们由 linker 负责校验），
    非链接的重解析点则要产出——里面是有真实数据语义的。
    读不到的目录/文件经 on_error 上报，不抛异常：本工具处理的正是权限受限的目录，
    让整趟校验因为一个 ACL 拒绝的子目录崩掉是不可接受的。
    excludes 里的内容按计划就没复制，必须整棵跳过，否则会报一堆假的「缺失」。
    """
    root_long = long_path(root)
    for dirpath, dirs, files in os.walk(root_long):
        keep = []
        for name in dirs:
            full = os.path.join(dirpath, name)
            if is_excluded(full, excludes):
                continue
            try:
                st = os.lstat(full)
            except OSError:
                if on_error:
                    on_error(rel_to(full, root))
                continue
            kind = reparse_kind(st)
            if kind is None:
                keep.append(name)
            elif not is_link_kind(kind):
                yield rel_to(full, root), full, st, True
        dirs[:] = keep
        for name in files:
            full = os.path.join(dirpath, name)
            if is_excluded(full, excludes):
                continue
            try:
                st = os.lstat(full)
            except OSError:
                if on_error:
                    on_error(rel_to(full, root))
                continue
            kind = reparse_kind(st)
            if is_link_kind(kind):
                continue
            yield rel_to(full, root), full, st, kind is not None


def verify_links(plan, target, *, should_cancel=None) -> list[str]:
    """逐个核对目标端重建出来的链接，返回指错或缺失的相对路径。

    只验顶层那条链接是不够的：源树里成百上千个 junction 都要在目标端重建，
    指错了（映射少一个反斜杠、建到别人留下的同名条目上）在顶层完全看不出来，
    而错误会一直留到用户某天点开那条链接才发现。
    链接数量远小于文件数，逐个 readlink 是毫秒级的事，不值得为此加节流。

    计划里标记为断链的只核对「指向」不核对「可达」：它们在源里就已经指着空气，
    工具按设计原样重建，拿可达性去卡会把整趟迁移拦在最后一步。
    """
    wrong: list[str] = []
    root = long_path(target)
    for link in plan.links:
        if should_cancel is not None and should_cancel():
            break
        dst = os.path.join(root, link.rel)
        if not linker.points_to(dst, link.new_target):
            wrong.append(link.rel)
        elif not link.is_dangling and not os.path.exists(dst):
            # 迁移前目标就是可访问的，迁移后也必须可访问，否则链接等于报废
            wrong.append(link.rel)
    return wrong


def verify(source, target, *, deep: bool = False, on_progress=None,
           should_cancel=None, totals=None,
           skip_unknown_reparse: bool = False, excludes=()) -> VerifyResult:
    """校验目标端与源是否一致。

    on_progress 收 `Progress`（phase="verifying"），0.1 秒节流——逐个文件上报
    会把 UI 事件队列塞满。should_cancel 每个文件问一次，取消后 result.cancelled
    为真且 **ok 必为 False**：半趟校验通过就等于没校验，绝不能据此改名源目录。
    excludes 里的内容按计划没复制，必须与扫描、复制用同一份，否则全是假缺失。
    """
    result = VerifyResult()
    target_long = long_path(target)
    started = time.monotonic()
    last_emit = 0.0
    checked_bytes = 0
    total_files = int(getattr(totals, "files", 0) or 0)
    total_bytes = int(getattr(totals, "bytes", 0) or 0)

    def emit(current: str) -> None:
        nonlocal last_emit
        if on_progress is None:
            return
        now = time.monotonic()
        if current and (now - last_emit) < PROGRESS_INTERVAL:
            return
        elapsed = max(now - started, 1e-6)
        speed = checked_bytes / elapsed
        remaining = max(total_bytes - checked_bytes, 0)
        on_progress(Progress(
            phase="verifying", done_files=result.actual_files + len(result.missing),
            done_bytes=checked_bytes, total_files=total_files,
            total_bytes=total_bytes, current=current, speed_bps=speed,
            eta_seconds=(remaining / speed) if speed > 0 else 0.0))
        last_emit = now

    def note_unreadable(rel: str) -> None:
        # 语义是「这个路径没能通过校验」，归入 mismatch
        result.mismatch.append(rel)

    for rel, full, st, is_reparse in _walk_entries(source, on_error=note_unreadable,
                                                   excludes=excludes):
        if should_cancel is not None and should_cancel():
            result.cancelled = True
            emit("")
            return result

        if is_reparse and skip_unknown_reparse:
            continue            # 计划里就决定了不复制，目标端本来就没有

        if is_reparse and (st.st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY):
            # 目录形态的重解析点：大小与时间戳都没有可比语义，
            # 只校验「目标端同样是个重解析点」。它在计划里计入 dirs，
            # 因此这里也不动 expected_files，保持两边口径一致。
            dst_dir = os.path.join(target_long, rel)
            if not is_reparse_point(dst_dir):
                result.mismatch.append(rel)
            emit(rel)
            continue

        size = int(st.st_size)
        result.expected_files += 1
        dst = os.path.join(target_long, rel)
        try:
            dst_st = os.lstat(dst)
        except OSError:
            result.missing.append(rel)
            continue

        result.expected_bytes += size
        actual = int(dst_st.st_size)
        result.actual_files += 1
        result.actual_bytes += actual
        checked_bytes += size
        if actual != size:
            result.mismatch.append(rel)
            continue

        if is_reparse:
            # 时间戳对重解析点是尽力而为（某些标签连 utime 都不允许），
            # 因此不比 mtime；deep 模式下两侧都还是重解析点时直接比重解析数据。
            if deep and is_reparse_point(dst):
                if reparse.read_raw(full) != reparse.read_raw(dst):
                    result.mismatch.append(rel)
                    continue
        else:
            if abs(st.st_mtime - dst_st.st_mtime) > MTIME_TOLERANCE_SECONDS:
                # 大小相同但时间戳不同：目标端很可能是别人留下的同大小残留，
                # 不能当成「已复制」放过——放过就意味着删备份后源数据永久丢失。
                # 这里的容差只用于吸收 FAT/exFAT 的时间戳截断：复制端只在
                # mtime 精确相等时才跳过，残留文件必然被真实复制覆盖。
                result.mismatch.append(rel)
                continue
            if deep and sha256_of(full) != sha256_of(dst):
                result.mismatch.append(rel)
                continue
        emit(rel)

    result.ok = not (result.cancelled or result.missing or result.mismatch)
    emit("")
    return result
