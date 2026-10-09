"""迁移前的硬性校验：任一 error 出现就必须拒绝执行。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .fsutil import drive_of, is_inside, reparse_kind, volume_free_bytes

SPACE_MARGIN = 1.05
SPACE_ALLOWANCE = 512 * 1024 * 1024


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: str = "error"


def has_error(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


def _is_drive_root(path: Path) -> bool:
    text = os.path.abspath(str(path))
    return os.path.normcase(text).rstrip("\\/") == os.path.normcase(drive_of(text)).rstrip("\\/")


def check(source, target, required_bytes: int = 0) -> list[Issue]:
    src = Path(source)
    dst = Path(target)
    issues: list[Issue] = []

    if not src.exists():
        issues.append(Issue("source_missing", f"源目录不存在：{src}"))
        return issues
    if not src.is_dir():
        issues.append(Issue("source_not_dir", f"源不是目录：{src}"))
        return issues

    try:
        st = os.lstat(src)
        if reparse_kind(st) is not None:
            issues.append(Issue("source_is_reparse",
                                f"源本身是链接，应迁移它的目标而不是链接：{src}"))
    except OSError:
        pass

    if os.path.normcase(os.path.abspath(str(src))) == os.path.normcase(os.path.abspath(str(dst))):
        issues.append(Issue("same_path", "源目录与目标目录相同"))

    if _is_drive_root(src):
        issues.append(Issue("source_is_root", f"拒绝迁移盘符根目录：{src}"))

    if is_inside(dst, src):
        issues.append(Issue("target_inside_source",
                            f"目标位于源子树内，会导致递归复制：{dst}"))
    if is_inside(src, dst):
        issues.append(Issue("source_inside_target",
                            f"源位于目标子树内，后续删除目标会波及源：{src}"))

    if dst.exists():
        try:
            if any(dst.iterdir()):
                issues.append(Issue("target_not_empty",
                                    f"目标目录已存在且非空，将按续传/合并处理：{dst}",
                                    severity="warning"))
        except OSError:
            pass

    try:
        free = volume_free_bytes(str(dst if dst.exists() else dst.parent))
    except OSError:
        free = 0
    need = int(required_bytes * SPACE_MARGIN) + SPACE_ALLOWANCE
    if required_bytes and free < need:
        issues.append(Issue(
            "insufficient_space",
            f"目标盘剩余 {free / 2**20:.0f} MB，需要约 {need / 2**20:.0f} MB"))

    return issues
