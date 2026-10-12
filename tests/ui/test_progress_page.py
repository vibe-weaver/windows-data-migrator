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


def test_copying_phase_is_labelled(page):
    page.update_progress(Progress(phase="copying", done_files=1, total_files=10))
    assert page.count_label.text().startswith("复制中")
    assert page.log.toPlainText() == ""


def test_verifying_phase_shows_progress_and_logs_once(page):
    """B6：校验阶段原来既没进度也没日志，几十万文件时界面像卡死了。"""
    page.update_progress(Progress(phase="verifying", done_files=2, total_files=10,
                                  done_bytes=20, total_bytes=100))
    assert page.count_label.text().startswith("校验中")
    assert "2 / 10" in page.count_label.text()
    assert page.percent() == 20
    assert page.log.toPlainText().count("[校验] 开始比对源与目标") == 1

    page.update_progress(Progress(phase="verifying", done_files=7, total_files=10,
                                  done_bytes=70, total_bytes=100))
    assert "7 / 10" in page.count_label.text()
    assert page.log.toPlainText().count("[校验]") == 1, "同一阶段不该反复记日志"


def test_reset_forgets_phase_so_next_run_logs_again(page):
    page.update_progress(Progress(phase="verifying", done_files=1, total_files=2))
    page.reset()
    page.update_progress(Progress(phase="verifying", done_files=1, total_files=2))
    assert page.log.toPlainText().count("[校验]") == 1


def test_fatal_reasons_have_readable_text(page):
    """B2/B4：磁盘满与缺符号链接特权必须能看懂，不能把内部键名甩给用户。"""
    page.add_failure(CopyFailure(rel="big.bin", reason="disk_full"))
    page.add_failure(CopyFailure(rel="link", reason="symlink_privilege"))
    assert page.failure_rows() == [("big.bin", "目标卷空间不足", ""),
                                   ("link", "缺符号链接特权", "")]


# --- 完成后能拿到完整日志文件 -----------------------------------------

def _outcome(log_path="", ok=True, phase="done", message="", backup_path=""):
    from dmig.core.migrator import MigrateOutcome
    return MigrateOutcome(ok, phase, log_path=log_path, message=message,
                          backup_path=backup_path or None)


def test_finish_warns_about_deleting_the_backup_by_hand(page):
    """迁移刚成功那一刻就该看到警告。

    用户往往正是这会儿去资源管理器里把备份目录删掉（"数据都迁好了，这个没用了吧"）。
    等他在报告页点「一键回滚」被拒才第一次听说后果，已经晚了。
    """
    outcome = _outcome(ok=True, phase="done", backup_path=r"C:\wwwwww.bak_1")
    page.finish(outcome, 1.0)

    text = page.backup_warning_text()
    assert "请勿在不确定的情况下删除备份" in text
    assert r"C:\wwwwww.bak_1" in text
    assert "回滚必须依靠备份" in text
    assert "删除备份后将无法再进行回滚" in text


def test_finish_has_no_backup_warning_without_a_backup(page):
    page.finish(_outcome(ok=True, phase="done"), 1.0)
    assert page.backup_warning_text() == ""


def test_second_run_does_not_keep_the_previous_backup_warning(page):
    """第二趟开始后，上一趟的「请勿删除备份」必须消失。

    否则用户在第二趟跑到一半时看到「请勿删除备份：C:\\上一趟的备份」，
    会以为那才是这次要留意的备份——警告指向一个已经过期的对象比没有更糟。
    """
    page.finish(_outcome(ok=True, phase="done", backup_path=r"C:\a.bak_1"), 1.0)
    assert page.backup_warning_text() != ""

    page.reset()
    page.update_progress(Progress(phase="copying", done_files=1, total_files=10))

    assert page.backup_warning_text() == ""


def test_finish_title_uses_a_short_readable_label(page):
    """完成横幅的大标题不能塞整段报错，也不能露内部键名。

    原来取的是 outcome.message——那是一整段带 \\?\ 长路径的异常原文；
    退而取 outcome.phase 又是 rename_failed 这种键名。大标题只该说清
    「卡在哪一步」，细节留给下面的说明行。
    """
    outcome = _outcome(ok=False, phase="rename_failed",
                       message="[WinError 5] 拒绝访问。: "
                               "'\\\\?\\C:\\a' -> '\\\\?\\C:\\b'；正在占用源目录的进程："
                               "WPS PDF (29396)。关掉它们后重试即可")
    page.finish(outcome, 1.0)

    assert page.finish_title.text() == "未完成：源目录改名失败"


def test_finish_detail_still_carries_the_full_reason(page):
    """大标题精简了，完整原因必须在说明行里找得到——不能两头都丢。"""
    outcome = _outcome(ok=False, phase="rename_failed",
                       message="正在占用源目录的进程：WPS PDF (29396)")
    page.finish(outcome, 1.0)
    assert "WPS PDF" in page.finish_detail.text()


def test_open_log_button_is_hidden_without_a_log_file(page):
    page.finish(_outcome(), 1.0)
    assert page.open_log_button.isVisible() is False

def test_open_log_button_appears_when_there_is_a_log(page, tmp_path):
    """界面面板为防刷屏只显示前若干条失败，完整日志还得能打开。"""
    log = tmp_path / "run.log"
    log.write_text("x", encoding="utf-8")
    page.finish(_outcome(str(log)), 1.0)
    assert page.open_log_button.isVisibleTo(page) is True


def test_opening_a_vanished_log_says_so_instead_of_crashing(page, tmp_path):
    """日志可能已经被清理掉；这时要说清楚，而不是点了没反应。"""
    page.finish(_outcome(str(tmp_path / "已经没了.log")), 1.0)
    page.open_log_button.click()
    assert "不存在" in page.log.toPlainText()


def test_open_log_launches_explorer_with_the_file_selected(page, tmp_path,
                                                           monkeypatch):
    import dmig.ui.progress_page as module

    log = tmp_path / "run.log"
    log.write_text("x", encoding="utf-8")
    launched = []
    monkeypatch.setattr(module.subprocess, "Popen", launched.append)

    page.finish(_outcome(str(log)), 1.0)
    page.open_log_button.click()

    assert launched, "应当拉起资源管理器"
    # explorer 的 /select 要求路径和开关拼在同一个参数里，否则含空格的路径会被拆开
    assert launched[0] == ["explorer", f"/select,{log}"]
