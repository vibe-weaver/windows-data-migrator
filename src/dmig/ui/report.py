"""报告统计与 CSV 导出，纯函数、不依赖 Qt。"""
from __future__ import annotations

import csv

from ..core.fsutil import long_path

CSV_HEADER = ["类别", "相对路径", "原因", "占用进程"]

REASON_TEXT = {
    "locked": "文件被占用",
    "io_error": "读写错误",
}

KIND_COPY_FAILED = "未复制"
KIND_MISSING = "缺失"
KIND_MISMATCH = "不一致"


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
    }


def unfinished_rows(outcome) -> list[list[str]]:
    """未完成清单：复制失败 + 校验缺失 + 校验不一致。"""
    rows: list[list[str]] = []
    copy = getattr(outcome, "copy", None)
    if copy:
        for failure in copy.failures:
            rows.append([KIND_COPY_FAILED, failure.rel,
                         REASON_TEXT.get(failure.reason, failure.reason),
                         failure.process])
    verify = getattr(outcome, "verify", None)
    if verify:
        for rel in verify.missing:
            rows.append([KIND_MISSING, rel, "目标不存在", ""])
        for rel in verify.mismatch:
            rows.append([KIND_MISMATCH, rel, "大小或内容不同", ""])
    return rows


def build_rows(outcome) -> list[list[str]]:
    return [list(CSV_HEADER)] + unfinished_rows(outcome)


def write_csv(path, rows) -> None:
    """写 UTF-8 BOM 的 CSV，Excel 双击打开不乱码。"""
    with open(long_path(path), "w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)
