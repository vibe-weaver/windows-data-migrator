"""迁移前的硬性校验：任一 error 出现就必须拒绝执行。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import locks
from .fsutil import (DRIVE_TYPE_TEXT, UNSTABLE_DRIVE_TYPES, drive_of, is_inside,
                     long_path, reparse_kind, supports_reparse_points,
                     volume_filesystem, volume_free_bytes, volume_info,
                     volume_root)

SPACE_MARGIN = 1.05
SPACE_ALLOWANCE = 512 * 1024 * 1024

# 试探用的临时名后缀。固定值（而不是随机）是为了万一进程死在两次改名之间，
# 下次启动能认出这是自己留下的，而不是用户改的名字。
PROBE_SUFFIX = ".dmig-probe"


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


def probe_rename(source) -> list[Issue]:
    """试探「把源目录改名再改回」，判断迁移最后那步会不会失败。

    为什么值得单列一步：**改名失败的门槛比复制失败低得多**——任何句柄都会让它
    失败，连只读打开都算（实测：只读打开的文件会让父目录的改名报 WinError 5）。
    所以复制、校验全都能过，却卡在最后一步，而那时用户已经等了几个小时。
    试探一次只需几毫秒，且**与文件数无关**（实测 5000 个文件 3.80 ms）。

    **有副作用，界面上必须说明**：
      · 源路径会消失几毫秒——正开着它的编辑器、文件监视器可能报「文件夹已删除」
      · 父目录的修改时间会变（改名计入父目录）

    返回 error 级 Issue（红色、拦住下一步）。试探通过返回空列表。
    """
    src = Path(source)
    if not src.is_dir():
        return []
    probe = src.with_name(src.name + PROBE_SUFFIX)
    try:
        os.rename(long_path(src), long_path(probe))
    except OSError as exc:
        return [_blocked_issue(src, exc)]
    try:
        os.rename(long_path(probe), long_path(src))
    except OSError as exc:
        # 改得过去、改不回来：比单纯被占用严重得多，源目录现在挂在临时名下，
        # 必须让用户立刻知道它去哪了。
        return [Issue(
            "rename_probe_stuck",
            f"源目录改不回来了：它现在叫 {probe}，请手动改回 {src}"
            f"（{getattr(exc, 'strerror', None) or exc}）")]
    return []


def probe_entries(source, names=None, on_progress=None, should_cancel=None) -> list[str]:
    """试探源目录的顶层条目，返回**改名失败**的那些（条目名）。

    用来把「整棵树里有人占着」缩小到「就这几棵子树」：改名一次约 0.67 毫秒，
    几十个顶层条目就是几十毫秒；而拿 RestartManager 全树扫一遍要按文件数算。

    `names` 限定只试探哪些条目；不传则全试。**调用方应当只传子目录**，因为：

      试探一个条目   0.67 毫秒
      让 RM 扫一个文件 0.13 毫秒

    差值 5 倍——**试探普通文件永远是净亏**：没被占用时省下的 RM 工作量还不到
    试探成本的三分之一，被占用时照样得交给 RM。只有当条目是目录、其子树里可能
    有许多文件时，试探才可能赚回来。

    副作用与 `probe_rename` 同源（父目录 mtime 会变、条目会一闪），而且这里是
    逐项来，所以界面必须先把这件事告诉用户，不能偷偷做。

    改名成功但**改不回来**的也计入返回清单：那种现场比被占用严重得多（条目挂在
    `.dmig-probe` 名下），调用方要能察觉。实际几乎不会发生——真有句柄的话第一次
    改名就失败了。
    """
    src = Path(source)
    if not src.is_dir():
        return []
    if names is not None:
        entries = [src / name for name in sorted(names)]
    else:
        try:
            entries = sorted(src.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
    blocked: list[str] = []
    total = len(entries)
    for index, entry in enumerate(entries):
        if should_cancel is not None and should_cancel():
            break
        probe = entry.with_name(entry.name + PROBE_SUFFIX)
        try:
            os.rename(long_path(entry), long_path(probe))
        except OSError:
            blocked.append(entry.name)
        else:
            try:
                os.rename(long_path(probe), long_path(entry))
            except OSError:
                blocked.append(entry.name)
        if on_progress is not None:
            on_progress(index + 1, total)
    return blocked


def _blocked_issue(src: Path, exc: OSError) -> Issue:
    """把改名失败写成人话，并尽量说清「该关掉谁」。"""
    code = getattr(exc, "winerror", None)
    why = {5: "拒绝访问", 32: "文件正被其他程序使用",
           33: "文件的一部分被锁定"}.get(code)
    head = (f"源目录无法改名：{why}（WinError {code}）" if why
            else f"源目录无法改名：{getattr(exc, 'strerror', None) or exc}")
    held = locks.describe(locks.blockers_in_tree(src))
    if held:
        return Issue("rename_blocked",
                     f"{head}。正在占用源目录的进程：{held}。"
                     f"关掉它们之后重新检验；确认无占用再开始迁移。")
    return Issue("rename_blocked",
                 f"{head}，但没查出占用进程（可能是权限不足，或某个进程正把源目录"
                 f"当作工作目录）。请关掉可能占用它的程序后重新检验。")
