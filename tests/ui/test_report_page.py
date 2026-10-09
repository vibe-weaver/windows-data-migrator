import csv
import io
import sys

import pytest

from dmig.core.models import CopyFailure, CopyResult, VerifyResult
from dmig.core.migrator import MigrateOutcome
from dmig.ui.report_page import ReportPage

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _outcome(**kwargs):
    defaults = dict(
        ok=False, phase="copy_failed",
        copy=CopyResult(copied_files=2, copied_bytes=1003, skipped=1,
                        failures=[CopyFailure(rel="x\\y.db", reason="locked",
                                              process="chrome.exe (1234)")]),
        verify=VerifyResult(ok=False, missing=["m.txt"]),
        backup_path=r"E:\src.bak_20261009120000",
        message="2 个文件未能复制",
    )
    defaults.update(kwargs)
    return MigrateOutcome(**defaults)


@pytest.fixture
def page(qtbot):
    widget = ReportPage()
    qtbot.addWidget(widget)
    return widget


def test_summary_is_rendered(page):
    page.load(_outcome())
    assert page.summary_text("copied_files") == "2"
    assert page.summary_text("copied_bytes") == "1003 B"
    assert page.summary_text("skipped") == "1"
    assert page.summary_text("failed") == "1"
    assert page.summary_text("missing") == "1"
    assert page.summary_text("mismatch") == "0"
    assert "copy_failed" in page.title_text()
    assert page.message_text() == "2 个文件未能复制"


def test_success_title(page):
    page.load(_outcome(ok=True, phase="done"))
    assert page.title_text() == "迁移成功"


def test_rows_show_unfinished_items(page):
    page.load(_outcome())
    assert page.rows()[0] == ["未复制", "x\\y.db", "文件被占用", "chrome.exe (1234)"]
    assert page.rows()[1] == ["缺失", "m.txt", "目标不存在", ""]


def test_export_csv_writes_expected_content(page, tmp_path):
    page.load(_outcome())
    target = tmp_path / "报告.csv"
    page.export_csv(target)
    parsed = list(csv.reader(io.StringIO(target.read_text(encoding="utf-8-sig"))))
    assert parsed[0] == ["类别", "相对路径", "原因", "占用进程"]
    assert parsed[1][1] == "x\\y.db"
    assert parsed[1][2] == "文件被占用"
    assert parsed[1][3] == "chrome.exe (1234)"
    assert parsed[2][0] == "缺失"


def test_buttons_follow_backup_presence(page):
    page.load(_outcome(backup_path=None))
    assert page.cleanup_button.isEnabled() is False
    assert page.rollback_button.isEnabled() is False
    page.load(_outcome())
    assert page.cleanup_button.isEnabled() is True
    assert page.rollback_button.isEnabled() is True


def test_rollback_and_cleanup_signals(page, qtbot):
    page.load(_outcome())
    with qtbot.waitSignal(page.rollback_requested, timeout=2000):
        page.rollback_button.click()
    with qtbot.waitSignal(page.cleanup_requested, timeout=2000):
        page.cleanup_button.click()


def test_reverify_signal(page, qtbot):
    page.load(_outcome())
    with qtbot.waitSignal(page.reverify_requested, timeout=2000):
        page.reverify_button.click()
