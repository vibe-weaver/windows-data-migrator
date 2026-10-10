"""迁移前的硬性校验：任一 error 出现就必须拒绝执行。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .fsutil import (DRIVE_TYPE_TEXT, UNSTABLE_DRIVE_TYPES, drive_of, is_inside,
                     reparse_kind, supports_reparse_points, volume_filesystem,
                     volume_free_bytes, volume_info, volume_root)

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


def _volume_checks(src: Path, dst: Path, link_count: int) -> list[Issue]:
    """卷层面的三件事：同卷、文件系统能不能存链接、盘是不是拔得掉的。

    源卷必须支持链接：迁移的最后一步就是在源路径上建一条指向目标的链接，
    这一步失败意味着「数据已经搬走、原地却留不下入口」，比复制失败难收拾得多。
    目标卷只在树内真有链接要重建时才致命——扫描前无从得知，所以那时只作提示。
    """
    issues: list[Issue] = []
    src_root = volume_root(src)
    dst_root = volume_root(dst)

    if src_root and os.path.normcase(src_root) == os.path.normcase(dst_root):
        issues.append(Issue(
            "same_volume",
            f"源与目标在同一个卷 {src_root} 上：迁移不会释放空间，"
            f"备份还与原件同盘，这块盘一出问题两边一起丢",
            severity="warning"))

    if not supports_reparse_points(src):
        issues.append(Issue(
            "source_no_reparse",
            f"源所在卷（{src_root}，{volume_filesystem(src) or '未知文件系统'}）"
            f"不支持 junction/符号链接，最后一步无法在 {src} 留下指向新位置的入口"))

    if not supports_reparse_points(dst):
        where = f"源树内那 {link_count} 条链接" if link_count else "源树内的链接"
        issues.append(Issue(
            "target_no_reparse",
            f"目标卷（{dst_root}，{volume_filesystem(dst) or '未知文件系统'}）"
            f"不支持 junction/符号链接，{where}无法在目标端重建",
            severity="error" if link_count else "warning"))

    for label, root, code in (("源", src_root, "source_volume_kind"),
                              ("目标", dst_root, "target_volume_kind")):
        kind = volume_info(root)[0]
        if kind not in UNSTABLE_DRIVE_TYPES:
            continue
        issues.append(Issue(
            code,
            f"{label}卷是{DRIVE_TYPE_TEXT[kind]}（{root}）："
            f"设备移除或连接断开后，链接与备份都会一起失效",
            severity="warning"))

    return issues


def check(source, target, required_bytes: int = 0, link_count: int = 0) -> list[Issue]:
    """前置校验。link_count 传扫描出的链接条数，未扫描时按 0 处理。"""
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

    issues.extend(_volume_checks(src, dst, link_count))

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
