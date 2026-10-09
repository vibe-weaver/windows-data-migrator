import sys

import pytest

from dmig.core.models import CopyFailure, Progress
from dmig.ui.progress_page import ProgressPage

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


@pytest.fixture
def page(qtbot):
    widget = ProgressPage()
    qtbot.addWidget(widget)
    return widget


def test_progress_is_rendered(page):
    page.update_progress(Progress(phase="copying", done_files=5, done_bytes=512,
                                  total_files=10, total_bytes=1024,
                                  current="a/b.txt", speed_bps=1024,
                                  eta_seconds=30))
    assert page.percent() == 50
    assert page.current_label.text() == "a/b.txt"
    assert "5 / 10" in page.count_label.text()
    assert page.speed_label.text() == "1.0 KB/s"
    assert page.eta_label.text() == "30 秒"


def test_zero_total_does_not_divide_by_zero(page):
    page.update_progress(Progress(phase="copying", total_bytes=0, total_files=0))
    assert page.percent() == 100
    assert page.current_label.text() == "-"


def test_pause_button_toggles(page):
    states = []
    page.pause_toggled.connect(states.append)
    page.toggle_pause()
    assert states == [True]
    assert page.pause_button.text() == "继续"
    page.toggle_pause()
    assert states == [True, False]
    assert page.pause_button.text() == "暂停"


def test_failure_table_records_process(page):
    page.add_failure(CopyFailure(rel="x\\y.db", reason="locked",
                                 process="chrome.exe (1234)"))
    page.add_failure(CopyFailure(rel="z.bin", reason="io_error"))
    assert page.failure_rows() == [("x\\y.db", "被占用", "chrome.exe (1234)"),
                                   ("z.bin", "读写错误", "")]
    assert "chrome.exe (1234)" in page.log.toPlainText()


def test_set_failures_replaces_table(page):
    page.add_failure(CopyFailure(rel="old", reason="locked"))
    page.set_failures([CopyFailure(rel="new", reason="locked")])
    assert page.failure_rows() == [("new", "被占用", "")]


def test_locate_signal_carries_process_label(page):
    page.add_failure(CopyFailure(rel="x", reason="locked",
                                 process="chrome.exe (1234)"))
    seen = []
    page.locate_requested.connect(seen.append)
    page.failure_table.cellDoubleClicked.emit(0, 2)
    assert seen == ["chrome.exe (1234)"]


def test_double_click_without_process_emits_nothing(page):
    page.add_failure(CopyFailure(rel="x", reason="io_error"))
    seen = []
    page.locate_requested.connect(seen.append)
    page.failure_table.cellDoubleClicked.emit(0, 2)
    assert seen == []


def test_reset_clears_everything(page):
    page.add_failure(CopyFailure(rel="x", reason="locked"))
    page.append_log("hello")
    page.update_progress(Progress(done_bytes=10, total_bytes=10))
    page.reset()
    assert page.failure_rows() == []
    assert page.log.toPlainText() == ""
    assert page.percent() == 0


def test_cancel_button_emits(page, qtbot):
    page.set_running(True)
    with qtbot.waitSignal(page.cancel_requested, timeout=2000):
        page.cancel_button.click()


def test_set_running_disables_controls(page):
    page.set_running(False)
    assert page.pause_button.isEnabled() is False
    assert page.cancel_button.isEnabled() is False
    page.set_running(True)
    assert page.pause_button.isEnabled() is True
