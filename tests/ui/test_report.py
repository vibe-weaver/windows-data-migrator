import csv
import io

from dmig.core.models import CopyFailure, CopyResult, VerifyResult
from dmig.core.migrator import MigrateOutcome
from dmig.ui import report


def _outcome(**kwargs):
    defaults = dict(
        ok=False,
        phase="copy_failed",
        copy=CopyResult(copied_files=2, copied_bytes=1003, skipped=1,
                        failures=[CopyFailure(rel="x\\y.db", reason="locked",
                                              process="chrome.exe (1234)")]),
        verify=VerifyResult(ok=False, missing=["m.txt"], mismatch=["n.bin"]),
    )
    defaults.update(kwargs)
    return MigrateOutcome(**defaults)


def test_summarize_counts_everything():
    stats = report.summarize(_outcome())
    assert stats == {"copied_files": 2, "copied_bytes": 1003, "skipped": 1,
                     "failed": 1, "missing": 1, "mismatch": 1, "dangling": 0}


def test_summarize_tolerates_missing_parts():
    stats = report.summarize(MigrateOutcome(ok=True, phase="done"))
    assert stats == {"copied_files": 0, "copied_bytes": 0, "skipped": 0,
                     "failed": 0, "missing": 0, "mismatch": 0, "dangling": 0}


def test_unfinished_rows_translates_reason():
    rows = report.unfinished_rows(_outcome())
    assert rows[0] == ["未复制", "x\\y.db", "文件被占用", "chrome.exe (1234)"]
    assert rows[1] == ["缺失", "m.txt", "目标不存在", ""]
    assert rows[2] == ["不一致", "n.bin", "大小或内容不同", ""]


def test_unknown_reason_is_kept_verbatim():
    bad = _outcome(copy=CopyResult(failures=[CopyFailure("a", "link_failed: boom")]))
    assert report.unfinished_rows(bad)[0][2] == "link_failed: boom"


def test_build_rows_prepends_header():
    rows = report.build_rows(_outcome())
    assert rows[0] == report.CSV_HEADER
    assert len(rows) == 4


def test_write_csv_is_excel_friendly(tmp_path):
    target = tmp_path / "报告.csv"
    report.write_csv(target, report.build_rows(_outcome()))
    text = target.read_text(encoding="utf-8-sig")
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["类别", "相对路径", "原因", "占用进程"]
    assert parsed[1][1] == "x\\y.db"


# --- C5：链接的断链与重写 ---------------------------------------------

_LINKS = [
    # 被改写过目标
    {"rel": "cache", "kind": "junction",
     "raw_target": "D:\\npm-cache", "new_target": "E:\\npm-cache",
     "is_dangling": False},
    # 断链：源里就已经指着空气，工具照样原样重建
    {"rel": "gone", "kind": "junction",
     "raw_target": "D:\\vanished", "new_target": "D:\\vanished",
     "is_dangling": True},
    # 既没断也没改写：相对路径原样保留
    {"rel": "kept", "kind": "symlink",
     "raw_target": "..\\sibling", "new_target": "..\\sibling",
     "is_dangling": False},
]


def test_summarize_counts_dangling_links():
    assert report.summarize(_outcome(links=_LINKS))["dangling"] == 1


def test_dangling_links_reach_the_unfinished_list():
    """断链要出现在报告清单里，用户才会知道它本来就指着空气，不是迁移弄坏的。"""
    rows = report.unfinished_rows(_outcome(links=_LINKS))
    assert [report.KIND_DANGLING, "gone", report.DANGLING_REASON, ""] in rows


def test_link_rows_keep_the_before_and_after_values():
    rows = report.link_rows(_outcome(links=_LINKS))
    by_rel = {row[1]: row for row in rows}
    assert by_rel["cache"] == ["链接重写", "cache",
                               "D:\\npm-cache → E:\\npm-cache", ""]
    assert by_rel["gone"][0] == report.KIND_DANGLING
    assert "目标不存在，已原样重建：D:\\vanished → D:\\vanished" in by_rel["gone"][2]
    # 既没断也没改写的链接不进清单，否则几万条「原样保留」会把要看的淹掉
    assert "kept" not in by_rel


def test_csv_carries_the_link_table():
    rows = report.build_rows(_outcome(links=_LINKS))
    assert rows[0] == report.CSV_HEADER
    kinds = [row[0] for row in rows]
    assert report.KIND_REWRITTEN in kinds
    assert report.KIND_DANGLING in kinds


def test_outcome_without_links_is_tolerated():
    """老会话文件、以及测试里的假 Migrator 都不会带 links。"""
    assert report.link_rows(MigrateOutcome(ok=True, phase="done")) == []
    assert report.summarize(MigrateOutcome(ok=True, phase="done"))["dangling"] == 0


# --- C9：复制保不住的属性与目录时间戳 ---------------------------------

def test_lossy_attributes_are_disclosed_in_one_row():
    """稀疏/硬链接/ADS 都不会让复制报错，结果却与原件不同，报告必须写出来。"""
    rows = report.attribute_rows(
        _outcome(sparse_files=2, hardlinked_files=5, ads_files=12))
    assert len(rows) == 1
    assert rows[0][0] == report.KIND_ATTRS
    assert "2 个稀疏文件会被实化" in rows[0][2]
    assert "5 个硬链接会变成各自独立的副本" in rows[0][2]
    assert "12 个文件带附加数据流" in rows[0][2]


def test_no_lossy_attributes_means_no_row():
    assert report.attribute_rows(_outcome()) == []


def test_lossy_attributes_reach_the_lists():
    outcome = _outcome(ads_files=3)
    assert report.attribute_rows(outcome)[0] in report.unfinished_rows(outcome)
    assert report.attribute_rows(outcome)[0] in report.build_rows(outcome)


def test_dir_time_failures_are_listed_per_path():
    rows = report.failure_rows(_outcome(copy=CopyResult(dir_times_failed=["a\\b"])))
    assert rows[0] == [report.KIND_DIR_TIME, "a\\b", report.DIR_TIME_REASON, ""]


# --- C4：被排除的条目 -------------------------------------------------

def test_excluded_items_are_listed_with_the_backup_warning():
    """被排除的内容没复制到目标、只存在于备份里，删备份前用户必须有据可查。"""
    rows = [row for row in report.failure_rows(_outcome(excluded=["locked", "a\\b"]))
            if row[0] == report.KIND_EXCLUDED]
    assert rows == [["已跳过", "locked", report.EXCLUDED_REASON, ""],
                    ["已跳过", "a\\b", report.EXCLUDED_REASON, ""]]
    assert "只存在于备份里" in report.EXCLUDED_REASON


def test_nothing_excluded_means_no_rows():
    assert [row for row in report.failure_rows(_outcome())
            if row[0] == report.KIND_EXCLUDED] == []
