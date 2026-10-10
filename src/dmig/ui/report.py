"""报告统计与 CSV 导出，纯函数、不依赖 Qt。"""
from __future__ import annotations

import csv
import os

from ..core.fsutil import long_path
from ..core.models import lossy_attribute_text

CSV_HEADER = ["类别", "相对路径", "原因", "占用进程"]

REASON_TEXT = {
    "locked": "文件被占用",
    "io_error": "读写错误",
    "disk_full": "目标卷空间不足",
    "symlink_privilege": "创建符号链接需要管理员特权",
}

KIND_COPY_FAILED = "未复制"
KIND_MISSING = "缺失"
KIND_MISMATCH = "不一致"
KIND_DANGLING = "断链"
KIND_REWRITTEN = "链接重写"
KIND_DIR_TIME = "目录时间戳"
KIND_ATTRS = "未保留属性"
KIND_EXCLUDED = "已跳过"

DANGLING_REASON = "链接目标不存在，已原样重建"
DIR_TIME_REASON = "目录时间戳未能写回，该目录会显示为迁移时刻"
EXCLUDED_REASON = "按排除项跳过，没有复制到目标，只存在于备份里"
WHOLE_TREE = "（整棵树）"


def _links(outcome) -> list[dict]:
    return list(getattr(outcome, "links", None) or [])


def _changed(link: dict) -> bool:
    """这条链接的目标是否真的被改写过。"""
    raw = str(link.get("raw_target", ""))
    new = str(link.get("new_target", ""))
    return os.path.normcase(raw) != os.path.normcase(new)


def dangling_rows(outcome) -> list[list[str]]:
    """断链清单（界面表格用）：原因只写结论。

    具体的指向留给 CSV 与 tooltip——表格的「原因」列是 ResizeToContents，
    塞进两条完整路径会把整张表撑爆，把「相对路径」挤得只剩几个字。
    """
    rows: list[list[str]] = []
    for link in _links(outcome):
        if link.get("is_dangling"):
            rows.append([KIND_DANGLING, str(link.get("rel", "")),
                         DANGLING_REASON, ""])
    return rows


def link_rows(outcome) -> list[list[str]]:
    """链接清单（CSV 用）：断链与被改写过的链接，重写前后值一并留档。

    断链要留档是因为工具照样原样重建了它，用户事后必须能查到它本来就指着空气；
    重写要留档是因为映射填错时（少一个反斜杠、指到别的盘），
    这是唯一能逐条对上号的凭据。
    既没断也没改写的链接不进这张表：几万条「原样保留」会把真正要看的淹掉，
    完整清单在会话文件的 links 字段里。
    """
    rows: list[list[str]] = []
    for link in _links(outcome):
        dangling = bool(link.get("is_dangling"))
        if not dangling and not _changed(link):
            continue
        detail = f"{link.get('raw_target', '')} → {link.get('new_target', '')}"
        reason = f"目标不存在，已原样重建：{detail}" if dangling else detail
        rows.append([KIND_DANGLING if dangling else KIND_REWRITTEN,
                     str(link.get("rel", "")), reason, ""])
    return rows


def summarize(outcome) -> dict[str, int]:
    """把一次迁移的结果压成几个计数，供界面显示。"""
    copy = getattr(outcome, "copy", None)
    verify = getattr(outcome, "verify", None)
    return {
        "copied_files": copy.copied_files if copy else 0,
        "copied_bytes": copy.copied_bytes if copy else 0,
        "skipped": copy.skipped if copy else 0,
        "failed": len(copy.failures) if copy else 0,
        "missing": len(verify.missing) if verify else 0,
        "mismatch": len(verify.mismatch) if verify else 0,
        "dangling": len(dangling_rows(outcome)),
    }


def failure_rows(outcome) -> list[list[str]]:
    """复制失败 + 校验缺失 + 校验不一致 + 目录时间戳写失败。"""
    rows: list[list[str]] = []
    copy = getattr(outcome, "copy", None)
    if copy:
        for failure in copy.failures:
            rows.append([KIND_COPY_FAILED, failure.rel,
                         REASON_TEXT.get(failure.reason, failure.reason),
                         failure.process])
        for rel in getattr(copy, "dir_times_failed", None) or []:
            rows.append([KIND_DIR_TIME, rel or ".", DIR_TIME_REASON, ""])
    for rel in getattr(outcome, "excluded", None) or []:
        rows.append([KIND_EXCLUDED, str(rel), EXCLUDED_REASON, ""])
    verify = getattr(outcome, "verify", None)
    if verify:
        for rel in verify.missing:
            rows.append([KIND_MISSING, rel, "目标不存在", ""])
        for rel in verify.mismatch:
            rows.append([KIND_MISMATCH, rel, "大小或内容不同", ""])
    return rows


def attribute_rows(outcome) -> list[list[str]]:
    """复制保不住的属性汇成一行披露。

    稀疏、硬链接、ADS 三样都不会让复制报错，结果却与原件不同：
    不写出来，用户看到「校验通过」会以为一个字节都没变。
    「相对路径」列填整棵树，因为它们不是某条路径的问题，而是复制方式的固有限制。
    """
    text = lossy_attribute_text(
        getattr(outcome, "sparse_files", 0) or 0,
        getattr(outcome, "hardlinked_files", 0) or 0,
        getattr(outcome, "ads_files", 0) or 0)
    return [[KIND_ATTRS, WHOLE_TREE, text, ""]] if text else []


def unfinished_rows(outcome) -> list[list[str]]:
    """界面表格的内容：没做完的事 + 断链 + 保不住的属性。"""
    return failure_rows(outcome) + dangling_rows(outcome) + attribute_rows(outcome)


def build_rows(outcome) -> list[list[str]]:
    """CSV 的内容：没做完的事 + 保不住的属性 + 完整链接清单（含重写前后值）。"""
    return ([list(CSV_HEADER)] + failure_rows(outcome) + attribute_rows(outcome)
            + link_rows(outcome))


def write_csv(path, rows) -> None:
    """写 UTF-8 BOM 的 CSV，Excel 双击打开不乱码。"""
    with open(long_path(path), "w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)
