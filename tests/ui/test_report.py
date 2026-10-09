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
                     "failed": 1, "missing": 1, "mismatch": 1}


def test_summarize_tolerates_missing_parts():
    stats = report.summarize(MigrateOutcome(ok=True, phase="done"))
    assert stats == {"copied_files": 0, "copied_bytes": 0, "skipped": 0,
                     "failed": 0, "missing": 0, "mismatch": 0}


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
