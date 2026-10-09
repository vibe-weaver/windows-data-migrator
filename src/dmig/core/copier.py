"""复制引擎：真实文件走分块复制，链接项在目标端重建。"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import linker
from .fsutil import long_path, rel_to, reparse_kind
from .locks import who_locks
from .models import (CopyFailure, CopyResult, EntryKind, LinkTargetClass,
                     Progress)

RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0, 10.0)
CHUNK = 1024 * 1024
LINK_KINDS = (EntryKind.JUNCTION, EntryKind.SYMLINK_DIR, EntryKind.SYMLINK_FILE)


def copy_one(src, dst, offset: int = 0, on_chunk=None) -> int:
    """分块复制单个文件，返回本次写入的字节数。"""
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


def _is_same_size(src, dst) -> bool:
    try:
        return os.path.getsize(long_path(src)) == os.path.getsize(long_path(dst))
    except OSError:
        return False


def run(plan, sess, *, on_progress=None, should_cancel=None, should_pause=None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS) -> CopyResult:
    source = Path(plan.source)
    target = Path(plan.target)
    result = CopyResult()
    started = time.monotonic()
    last_emit = 0.0
    totals = plan.totals

    def emit(current: str) -> None:
        nonlocal last_emit
        if on_progress is None:
            return
        now = time.monotonic()
        if current and (now - last_emit) < 0.1:      # 节流，但结束时的空 current 必发
            return
        elapsed = max(now - started, 1e-6)
        speed = result.copied_bytes / elapsed
        remaining = max(totals.bytes - result.copied_bytes, 0)
        on_progress(Progress(
            phase="copying", done_files=result.copied_files,
            done_bytes=result.copied_bytes, total_files=totals.files,
            total_bytes=totals.bytes, current=current, speed_bps=speed,
            eta_seconds=(remaining / speed) if speed > 0 else 0.0))
        last_emit = now

    def wait_if_paused() -> bool:
        while should_pause is not None and should_pause():
            time.sleep(0.2)
            if should_cancel is not None and should_cancel():
                return False
        return True

    # 1) 建目录骨架
    for root, dirs, _files in os.walk(long_path(source)):
        if should_cancel is not None and should_cancel():
            result.cancelled = True
            return result
        rel = rel_to(root, source)
        dest_dir = target if rel == "." else target / rel
        try:
            os.makedirs(long_path(dest_dir), exist_ok=True)
        except OSError as exc:
            result.failures.append(CopyFailure(rel=str(dest_dir), reason=f"mkdir: {exc}"))
        dirs[:] = [d for d in dirs
                   if reparse_kind(os.lstat(os.path.join(root, d))) is None]

    # 2) 真实文件
    for root, dirs, files in os.walk(long_path(source)):
        dirs[:] = [d for d in dirs
                   if reparse_kind(os.lstat(os.path.join(root, d))) is None]
        for name in files:
            if should_cancel is not None and should_cancel():
                result.cancelled = True
                sess.save()
                return result
            if not wait_if_paused():
                result.cancelled = True
                sess.save()
                return result

            src_path = os.path.join(root, name)
            try:
                st = os.lstat(src_path)
            except OSError:
                continue
            if reparse_kind(st) is not None:
                continue

            rel = rel_to(src_path, source)
            dst_path = target / rel
            if _is_same_size(src_path, dst_path):
                result.skipped += 1
                result.copied_files += 1
                result.copied_bytes += int(st.st_size)
                continue

            _clear_readonly(dst_path)
            error: OSError | None = None
            for attempt in range(len(retry_delays) + 1):
                try:
                    copy_one(src_path, dst_path)
                    shutil.copystat(long_path(src_path), long_path(dst_path))
                    error = None
                    break
                except OSError as exc:
                    error = exc
                    if attempt < len(retry_delays):
                        time.sleep(retry_delays[attempt])
            if error is not None:
                held = who_locks(src_path)
                desc = ", ".join(f"{p.name} ({p.pid})" for p in held) or "-"
                reason = "locked" if held else "io_error"
                result.failures.append(CopyFailure(rel=rel, reason=reason, process=desc))
                sess.mark_failed(rel, reason, desc)
                continue

            result.copied_files += 1
            result.copied_bytes += int(st.st_size)
            sess.mark_done(rel, st.st_size)
            emit(rel)

    # 3) 链接项
    for link in plan.links:
        if should_cancel is not None and should_cancel():
            result.cancelled = True
            break
        dst_link = target / link.rel
        try:
            os.makedirs(long_path(os.path.dirname(str(dst_link))), exist_ok=True)
            if os.path.exists(long_path(dst_link)) or os.path.islink(long_path(dst_link)):
                if os.path.isdir(long_path(dst_link)) and not os.path.islink(long_path(dst_link)):
                    os.rmdir(long_path(dst_link))
                else:
                    os.unlink(long_path(dst_link))
            try:
                kind = linker.create(dst_link, link.new_target, link.is_dir,
                                     prefer_junction=link.kind is EntryKind.JUNCTION)
            except OSError:
                if link.is_dir:
                    kind = linker.create(dst_link, link.new_target, True,
                                         prefer_junction=False)
                else:
                    raise
            sess.links.append({"rel": link.rel, "kind": kind,
                               "raw_target": link.raw_target,
                               "new_target": link.new_target})
            if on_progress:
                emit(link.rel)
        except OSError as exc:
            result.failures.append(
                CopyFailure(rel=link.rel, reason=f"link_failed: {exc}"))
            sess.mark_failed(link.rel, f"link_failed: {exc}")

    sess.phase = "copied"
    sess.save()
    emit("")
    return result
