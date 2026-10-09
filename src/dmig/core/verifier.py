"""复制结果的完整性校验：默认比文件数与字节数，可选逐文件哈希。"""
from __future__ import annotations

import hashlib
import os

from .fsutil import long_path, rel_to, reparse_kind
from .models import VerifyResult


def sha256_of(path, block: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as fh:
        while True:
            chunk = fh.read(block)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _walk_real_files(root, on_error=None):
    """产出 (相对路径, 绝对路径, 大小)，不进入链接目标。

    读不到的目录/文件经 on_error 上报，不抛异常——本工具处理的正是权限受限的目录，
    让整趟校验因为一个 ACL 拒绝的子目录崩掉是不可接受的。
    """
    root_long = long_path(root)
    for dirpath, dirs, files in os.walk(root_long):
        keep = []
        for name in dirs:
            full = os.path.join(dirpath, name)
            try:
                if reparse_kind(os.lstat(full)) is None:
                    keep.append(name)
            except OSError:
                if on_error:
                    on_error(rel_to(full, root))
        dirs[:] = keep
        for name in files:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                if on_error:
                    on_error(rel_to(full, root))
                continue
            if reparse_kind(st) is not None:
                continue
            yield rel_to(full, root), full, int(st.st_size)


def verify(source, target, *, deep: bool = False, on_progress=None) -> VerifyResult:
    result = VerifyResult()
    target_long = long_path(target)

    def note_unreadable(rel: str) -> None:
        # 语义是「这个路径没能通过校验」，归入 mismatch
        result.mismatch.append(rel)

    for rel, full, size in _walk_real_files(source, on_error=note_unreadable):
        result.expected_files += 1
        result.expected_bytes += size
        dst = os.path.join(target_long, rel)
        if not os.path.exists(dst):
            result.missing.append(rel)
            continue
        actual = os.path.getsize(dst)
        result.actual_files += 1
        result.actual_bytes += actual
        if actual != size:
            result.mismatch.append(rel)
            continue
        if deep and sha256_of(full) != sha256_of(dst):
            result.mismatch.append(rel)
        if on_progress:
            on_progress(rel)

    result.ok = not result.missing and not result.mismatch
    return result
