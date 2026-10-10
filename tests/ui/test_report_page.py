import csv
import io
import sys

import pytest
from PySide6.QtGui import QColor

from dmig.core.models import CopyFailure, CopyResult, VerifyResult
from dmig.core.migrator import MigrateOutcome
from dmig.ui import theme
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


def test_discard_enabled_only_for_partial_target(page):
    """B1 的出口：源完好、目标半份时可清；已成功或已改名时不可清。"""
    page.load(_outcome(backup_path=None))
    assert page.discard_button.isEnabled() is True

    page.load(_outcome())                     # 有备份 = 已经改过名
    assert page.discard_button.isEnabled() is False

    page.load(_outcome(ok=True, phase="done"))
    assert page.discard_button.isEnabled() is False


def test_discard_button_emits_signal(page, qtbot):
    page.load(_outcome(backup_path=None))
    with qtbot.waitSignal(page.discard_requested, timeout=2000):
        page.discard_button.click()


def test_fatal_reasons_are_translated(page):
    """磁盘写满这类中止原因不能直接把英文代号甩给用户。"""
    page.load(_outcome(
        phase="aborted", backup_path=None,
        copy=CopyResult(copied_files=0, failures=[
            CopyFailure(rel="big.bin", reason="disk_full")]),
        message="目标卷空间不足，已立即停止"))
    assert page.rows()[0] == ["未复制", "big.bin", "目标卷空间不足", ""]


# --- C5：断链在报告里标红 ---------------------------------------------

_DANGLING_LINKS = [
    {"rel": "gone", "kind": "junction", "raw_target": "D:\\vanished",
     "new_target": "D:\\vanished", "is_dangling": True},
]


def test_dangling_row_is_painted_red(page):
    """断链不标红的话，用户会以为是迁移把链接弄坏了。"""
    page.load(_outcome(links=_DANGLING_LINKS))
    rows = page.rows()
    assert rows[-1][0] == "断链"
    index = len(rows) - 1
    assert page.table.item(index, 0).foreground().color() == \
        QColor(theme.error_color())
    # 完整指向挂在 tooltip 上：表格里只放结论，免得把「相对路径」列挤没
    assert "D:\\vanished" in page.table.item(index, 1).toolTip()


def test_dangling_count_is_a_stat(page):
    page.load(_outcome(links=_DANGLING_LINKS))
    assert page.summary_text("dangling") == "1"


def test_non_dangling_rows_keep_default_color(page):
    page.load(_outcome())
    assert page.table.item(0, 0).foreground().color() != \
        QColor(theme.error_color())


def test_dangling_links_reach_the_exported_csv(page, tmp_path):
    page.load(_outcome(links=_DANGLING_LINKS))
    target = tmp_path / "报告.csv"
    page.export_csv(target)
    text = target.read_text(encoding="utf-8-sig")
    assert "断链" in text
    assert "D:\\vanished" in text


# --- C9：复制保不住的属性 ---------------------------------------------

def test_lossy_attributes_get_a_row(page):
    page.load(_outcome(hardlinked_files=482, ads_files=7))
    rows = page.rows()
    assert rows[-1][0] == "未保留属性"
    assert "482 个硬链接" in rows[-1][2]
    assert "7 个文件带附加数据流" in rows[-1][2]


def test_nothing_lossy_means_no_extra_row(page):
    page.load(_outcome())
    assert all(row[0] != "未保留属性" for row in page.rows())


def test_dir_time_failure_gets_a_row(page):
    page.load(_outcome(copy=CopyResult(dir_times_failed=["a\\b"])))
    assert ["目录时间戳", "a\\b", "目录时间戳未能写回，该目录会显示为迁移时刻", ""] \
        in page.rows()
