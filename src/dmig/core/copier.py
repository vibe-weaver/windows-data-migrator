"""复制引擎：真实文件走内核整文件复制（退路是分块复制），链接项在目标端重建。"""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
import time
from ctypes import wintypes
from pathlib import Path

from . import linker, reparse
from .fsutil import (is_excluded, is_link_kind, long_path, rel_to, reparse_kind,
                     same_content_stat)
from .locks import who_locks
from .models import CopyFailure, CopyResult, EntryKind, Progress

RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0, 10.0)
CHUNK = 1024 * 1024

# 重试不会好转的错误：磁盘写满（39/112）、建符号链接缺特权（1314）。
# 逐文件重试 1+3+10 秒，10 万个文件就是 389 小时——必须整趟立刻中止。
FATAL_BY_WINERROR = {
    39: "disk_full",                 # ERROR_HANDLE_DISK_FULL
    112: "disk_full",                # ERROR_DISK_FULL
    reparse.ERROR_PRIVILEGE_NOT_HELD: "symlink_privilege",
}

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CopyFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL]
_k32.CopyFileW.restype = wintypes.BOOL


def fatal_reason(exc: OSError) -> str:
    """返回致命原因代号，非致命返回空串。"""
    code = getattr(exc, "winerror", 0) or 0
    if code in FATAL_BY_WINERROR:
        return FATAL_BY_WINERROR[code]
    # 配额耗尽等路径只带 errno，不带 winerror
    return "disk_full" if exc.errno == errno.ENOSPC else ""


def _copyfile_kernel(src_long: str, dst_long: str) -> None:
    """内核整文件复制。失败抛带 `winerror` 的异常。

    单独一个函数是为了**能被替换**：测试要验证「什么时候走了内核、什么时候没走」，
    直接去 patch `ctypes` 的函数指针既脆又难读。
    """
    if not _k32.CopyFileW(src_long, dst_long, False):
        raise ctypes.WinError(ctypes.get_last_error())


def copy_one(src, dst, offset: int = 0, on_chunk=None) -> int:
    """复制单个文件，返回写入的字节数。

    **能走内核就走内核。** 实测 256 字节的小文件：Python 读写循环 532.9 微秒/个，
    `CopyFileW` 334.1 微秒/个——2.1 倍。文件多的场景瓶颈是**每文件的系统调用
    延迟**而不是带宽，所以这一条对"几万个文件"最有效。

    两种情况必须退回分块循环：
      · `offset` 非 0 —— 断点续传要从中间接着写，`CopyFileW` 没有偏移续传
      · 给了 `on_chunk` —— 调用方要逐块进度

    顺带：`CopyFileW` 会连**备用数据流（ADS）**一起带过去，而分块循环只读主数据流。
    也就是说这一改动顺带补上了一个数据丢失点。

    失败一律抛 `WinError`（带 `winerror`）：上层的 `fatal_reason` 正是按它判断
    磁盘写满之类不可重试的错误。
    """
    if offset == 0 and on_chunk is None:
        src_long, dst_long = long_path(src), long_path(dst)
        _copyfile_kernel(src_long, dst_long)
        return os.path.getsize(src_long)

    written = 0
    with open(long_path(src), "rb") as fsrc:
        if offset:
            fsrc.seek(offset)
        mode = "r+b" if offset and os.path.exists(long_path(dst)) else "wb"
        with open(long_path(dst), mode) as fdst:
            if offset:
                fdst.seek(offset)
            while True:
                chunk = fsrc.read(CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)
                written += len(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
    return written


def _clear_readonly(path) -> None:
    try:
        st = os.stat(long_path(path))
        if not (st.st_mode & 0o200):
            os.chmod(long_path(path), st.st_mode | 0o200)
    except OSError:
        pass


def _already_copied(src_st, dst_path) -> bool:
    """目标端这份是否与源**精确**一致（大小相同且 mtime_ns 相等）。

    这是跳过判定的第二道关，用来确认「我们复制过去的那份后来没被动过」；
    第一道关是会话记账（`Session.completed_rels`）。时间戳必须精确比较：
    copystat 以 ns 精度写回、NTFS 存 100ns 刻度，我们自己复制的文件必然相等；
    FAT/exFAT 只有 2 秒精度，续传时会重复制一遍，慢但是安全。
    """
    try:
        return same_content_stat(src_st, os.stat(long_path(dst_path)))
    except OSError:
        return False


def _copy_unknown_reparse(src_path, dst_path) -> None:
    """复制一个非链接类重解析点，失败抛 OSError。

    顺序是「先直读、读不动再原样搬」：云占位符直读会触发下载、得到真实内容，
    这正是想要的；AppExecLink 连 open() 都被拒（实测 Invalid argument，
    CopyFileW 报 1920），只能把重解析数据逐字节搬到目标端。
    """
    try:
        copy_one(src_path, dst_path)
    except OSError:
        reparse.copy_reparse_data(src_path, dst_path)
    try:
        shutil.copystat(long_path(src_path), long_path(dst_path))
    except OSError:
        pass                     # 时间戳是尽力而为，校验对重解析点另有规则


def _link_points_to(path, wanted) -> bool:
    """目标端这条链接是否已经指向计划要求的位置。

    续传第二趟会把建链整批再走一遍。已经指对的绝不能拆了重建：删掉之后
    万一建不回来（缺特权、目标盘出问题），一条本来好好的链接就没了。
    """
    return linker.points_to(long_path(path), wanted)


def _existing_link_kind(path) -> str:
    kind = reparse_kind(os.lstat(long_path(path)))
    return "junction" if kind is EntryKind.JUNCTION else "symlink"


def _link_record(link, kind: str) -> dict:
    """会话里的链接记录：重写前后的值与断链状态都要留档。

    报告要据此把断链标红、给出每条链接改写前后的指向。只记 new_target 的话，
    映射填错时用户手里没有任何能逐条对上号的凭据。
    """
    return {"rel": link.rel, "kind": kind,
            "raw_target": link.raw_target,
            "new_target": link.new_target,
            "is_dangling": link.is_dangling}


def _remove_link_entry(path) -> None:
    """删掉目标端已有的同名条目，好按新计划重建；本来就不存在则什么都不做。

    存在性必须走 lstat：junction 指向的目标可能已经不在了，`os.path.exists`
    会跟着链接走、报 False，「已存在」被漏判，紧接着 os.mkdir 撞 WinError 183，
    续传就死在一条早就建好的链接上。
    先 rmdir 再 unlink：目录形态（junction / 目录符号链接）只能 rmdir 摘，
    文件形态只能 unlink，两者对另一种形态都会失败。
    """
    try:
        os.lstat(long_path(path))
    except OSError:
        return
    try:
        os.rmdir(long_path(path))
    except OSError:
        os.unlink(long_path(path))


def copy_dir_times(source, target, excludes=()) -> list[str]:
    """把目录的时间戳补到目标端，返回写不成功的相对路径。

    文件一直走 copystat，目录此前完全没管：迁移完目标端目录的 mtime 全是「刚刚」，
    依赖目录时间戳做增量同步或构建缓存失效判断的工具会整棵重扫一遍。
    必须**自底向上**——给子目录写时间戳会顺手改掉父目录的 mtime，
    先写父再写子等于父目录那一次白写。

    写不成功只记账不抛：目录可能被别的进程占着、或带着只读属性，
    为此把整趟刚复制完的迁移判死是不成比例的。
    """
    failed: list[str] = []
    roots = [root for root, dirs, _files in os.walk(long_path(source))]
    target_root = long_path(target)
    for root in reversed(roots):
        if is_excluded(root, excludes):
            continue
        rel = rel_to(root, source)
        dst_dir = target_root if rel == "." else os.path.join(target_root, rel)
        try:
            shutil.copystat(root, dst_dir)
        except OSError:
            failed.append(rel)
    return failed


def run(plan, sess, *, on_progress=None, should_cancel=None, should_pause=None,
        retry_delays: tuple[float, ...] | None = None) -> CopyResult:
    # 不能在签名里写 retry_delays=RETRY_DELAYS：默认值在定义时就绑死了，
    # 之后改模块常量（测试加速、调参）都会被静默忽略。
    delays = RETRY_DELAYS if retry_delays is None else tuple(retry_delays)
    source = Path(plan.source)
    target = Path(plan.target)
    result = CopyResult()
    started = time.monotonic()
    last_emit = 0.0
    totals = plan.totals
    skip_unknown = bool(getattr(plan, "skip_unknown_reparse", False))
    excludes = list(getattr(plan, "excludes", None) or [])
    done_rels = sess.completed_rels()

    def emit(current: str) -> None:
        nonlocal last_emit
        if on_progress is None:
            return
        now = time.monotonic()
        if current and (now - last_emit) < 0.1:      # 节流，但结束时的空 current 必发
            return
        elapsed = max(now - started, 1e-6)
        done_bytes = result.accounted_bytes
        speed = done_bytes / elapsed
        remaining = max(totals.bytes - done_bytes, 0)
        on_progress(Progress(
            phase="copying", done_files=result.copied_files + result.skipped,
            done_bytes=done_bytes, total_files=totals.files,
            total_bytes=totals.bytes, current=current, speed_bps=speed,
            eta_seconds=(remaining / speed) if speed > 0 else 0.0))
        last_emit = now

    def wait_if_paused() -> bool:
        while should_pause is not None and should_pause():
            time.sleep(0.2)
            if should_cancel is not None and should_cancel():
                return False
        return True

    def fatal_abort(rel: str, exc: OSError):
        """致命错误就地中止整趟；不是致命错误则返回 None，调用方照常继续。

        磁盘写满时逐个文件重试 1+3+10 秒，10 万个文件要耗 389 小时才失败完，
        而且每条失败都白写一遍目标盘。这类错误只能立刻停。
        """
        reason = fatal_reason(exc)
        if not reason:
            return None
        result.failures.append(CopyFailure(rel=rel, reason=reason))
        sess.mark_failed(rel, reason, str(exc))
        result.aborted = reason
        sess.phase = "aborted"
        sess.save()
        emit("")
        return result

    def cancelled() -> CopyResult:
        result.cancelled = True
        sess.save()
        emit("")
        return result

    # 1) 建目录骨架
    for root, dirs, _files in os.walk(long_path(source)):
        if should_cancel is not None and should_cancel():
            return cancelled()
        rel = rel_to(root, source)
        dest_dir = target if rel == "." else target / rel
        try:
            os.makedirs(long_path(dest_dir), exist_ok=True)
        except OSError as exc:
            stopped = fatal_abort(str(dest_dir), exc)
            if stopped is not None:
                return stopped
            result.failures.append(CopyFailure(rel=str(dest_dir), reason=f"mkdir: {exc}"))
        descend = []
        for name in dirs:
            full = os.path.join(root, name)
            if is_excluded(full, excludes):
                continue                # 被排除：连目录项都不建，更不下钻
            try:
                st = os.lstat(full)
            except OSError:
                continue                # 遍历途中被别的进程删掉，跳过即可
            kind = reparse_kind(st)
            if kind is None:
                descend.append(name)
            elif not is_link_kind(kind) and not skip_unknown:
                # 目录形态的非链接重解析点（WCI、云文件夹占位符）只出现在 dirs 里，
                # 第二趟的文件循环根本看不到它，必须在这里搬，否则静默丢失。
                child_rel = rel_to(full, source)
                try:
                    reparse.copy_reparse_data(full, target / child_rel)
                except OSError as exc:
                    stopped = fatal_abort(child_rel, exc)
                    if stopped is not None:
                        return stopped
                    result.failures.append(
                        CopyFailure(rel=child_rel, reason=f"reparse_failed: {exc}"))
                    sess.mark_failed(child_rel, f"reparse_failed: {exc}")
        dirs[:] = descend

    # 2) 链接项——刻意排在批量复制之前
    # 建链接要特权（文件符号链接）或目标目录的写权限，失败是「整批都失败」的性质。
    # 放在几十万个文件复制之后再试，等于让用户等几小时才看到一条权限错误。
    for link in plan.links:
        if should_cancel is not None and should_cancel():
            return cancelled()
        dst_link = target / link.rel
        # 链接指向的源侧目标被 --exclude 排除时，目标端不会有那份数据，
        # 原样重建只会建出一条断链且无人知晓——跳过重建并如实记一笔，
        # 让它出现在失败清单里，而不是沉默地留个指空链接。
        if is_excluded(link.raw_target, excludes):
            result.failures.append(CopyFailure(
                rel=link.rel, reason="link_target_excluded"))
            sess.mark_failed(link.rel, "link_target_excluded")
            emit(link.rel)
            continue
        try:
            os.makedirs(long_path(os.path.dirname(str(dst_link))), exist_ok=True)
            if _link_points_to(dst_link, link.new_target):
                # 上一趟已经建对了：只补会话记录，不拆了重建
                sess.links.append(_link_record(link, _existing_link_kind(dst_link)))
                emit(link.rel)
                continue
            _remove_link_entry(dst_link)
            try:
                kind = linker.create(dst_link, link.new_target, link.is_dir,
                                     prefer_junction=link.kind is EntryKind.JUNCTION)
            except OSError:
                if link.is_dir:
                    kind = linker.create(dst_link, link.new_target, True,
                                         prefer_junction=False)
                else:
                    raise
            sess.links.append(_link_record(link, kind))
            emit(link.rel)
        except OSError as exc:
            stopped = fatal_abort(link.rel, exc)
            if stopped is not None:
                return stopped
            result.failures.append(
                CopyFailure(rel=link.rel, reason=f"link_failed: {exc}"))
            sess.mark_failed(link.rel, f"link_failed: {exc}")

    # 3) 真实文件
    for root, dirs, files in os.walk(long_path(source)):
        keep = []
        for name in dirs:
            full = os.path.join(root, name)
            if is_excluded(full, excludes):
                continue
            try:
                if reparse_kind(os.lstat(full)) is None:
                    keep.append(name)
            except OSError:
                continue                # 遍历途中消失，跳过即可
        dirs[:] = keep
        for name in files:
            if should_cancel is not None and should_cancel():
                return cancelled()
            if not wait_if_paused():
                return cancelled()

            src_path = os.path.join(root, name)
            if is_excluded(src_path, excludes):
                continue
            try:
                st = os.lstat(src_path)
            except OSError:
                continue
            kind = reparse_kind(st)
            if is_link_kind(kind):
                continue                # 链接项已在第 2 趟重建
            if kind is EntryKind.UNKNOWN_REPARSE and skip_unknown:
                continue                # 用户显式选择跳过，清单里已列明

            rel = rel_to(src_path, source)
            dst_path = target / rel
            # 两个条件都要满足：会话记账证明「这份是我们复制的」，
            # 大小与时间戳证明「它后来没被动过」。缺一不可，见 completed_rels。
            if rel in done_rels and _already_copied(st, dst_path):
                result.skipped += 1
                result.skipped_bytes += int(st.st_size)
                continue

            _clear_readonly(dst_path)
            if kind is EntryKind.UNKNOWN_REPARSE:
                try:
                    _copy_unknown_reparse(src_path, dst_path)
                except OSError as exc:
                    stopped = fatal_abort(rel, exc)
                    if stopped is not None:
                        return stopped
                    result.failures.append(
                        CopyFailure(rel=rel, reason=f"reparse_failed: {exc}"))
                    sess.mark_failed(rel, f"reparse_failed: {exc}")
                    continue
            else:
                error: OSError | None = None
                for attempt in range(len(delays) + 1):
                    try:
                        copy_one(src_path, dst_path)
                        shutil.copystat(long_path(src_path), long_path(dst_path))
                        error = None
                        break
                    except OSError as exc:
                        stopped = fatal_abort(rel, exc)
                        if stopped is not None:
                            return stopped
                        error = exc
                        if attempt < len(delays):
                            time.sleep(delays[attempt])
                if error is not None:
                    held = who_locks(src_path)
                    desc = ", ".join(f"{p.name} ({p.pid})" for p in held) or "-"
                    reason = "locked" if held else "io_error"
                    result.failures.append(
                        CopyFailure(rel=rel, reason=reason, process=desc))
                    sess.mark_failed(rel, reason, desc)
                    continue

            result.copied_files += 1
            result.copied_bytes += int(st.st_size)
            sess.mark_done(rel, st.st_size)
            emit(rel)

    # 4) 目录时间戳——必须最后做，且自底向上
    result.dir_times_failed = sorted(copy_dir_times(source, target, excludes))

    sess.phase = "copied"
    sess.save()
    emit("")
    return result
