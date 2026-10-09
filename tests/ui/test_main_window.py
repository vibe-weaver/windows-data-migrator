import sys

import pytest

from dmig.core.models import CopyFailure, CopyResult, MigrationPlan, PlanTotals, TargetGroup
from dmig.core.migrator import MigrateOutcome
from dmig.ui.main_window import (PAGE_PLAN, PAGE_PROGRESS, PAGE_REPORT,
                                 PAGE_SELECT, MainWindow)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


class _FakeMigrator:
    def __init__(self, plan, outcome):
        self.plan = plan
        self.outcome = outcome
        self.mappings = {}
        self.rolled_back = False
        self.cleaned = False

    def plan_it(self, on_progress=None):
        return self.plan

    def execute(self, *, on_progress=None, should_cancel=None, should_pause=None):
        return self.outcome

    def rollback(self):
        self.rolled_back = True
        return True

    def cleanup_backup(self):
        self.cleaned = True
        return True


def _plan():
    plan = MigrationPlan(source=r"D:\src", target=r"E:\dst")
    plan.totals = PlanTotals(files=2, bytes=503, dirs=1)
    plan.required_bytes = 503
    plan.free_bytes = 1024 ** 3
    plan.link_groups = [TargetGroup(prefix=r"C:\cache", count=482, mapped_to="")]
    return plan


def _factory(plan, outcome, calls):
    def make(source, target, mappings, deep_verify, session_root):
        migrator = _FakeMigrator(plan, outcome)
        migrator.mappings = dict(mappings)
        calls.append({"source": source, "target": target,
                      "mappings": dict(mappings), "deep": deep_verify})
        return migrator
    return make


def _select(window, tmp_path):
    window.select_page.set_source(str(tmp_path / "src"))
    window.select_page.set_target(str(tmp_path / "dst"))


def test_wizard_walks_all_four_pages(qtbot, tmp_path):
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(ok=True, phase="done",
                                copy=CopyResult(copied_files=2)), calls))
    qtbot.addWidget(window)
    assert window.current_index() == PAGE_SELECT

    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PLAN, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert window.plan_page.metric_text("files") == "2"

    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PROGRESS, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert window.report_page.summary_text("copied_files") == "2"

    window.go_next()
    assert window.current_index() == PAGE_REPORT


def test_go_back_returns_to_select(qtbot, tmp_path):
    window = MainWindow(migrator_factory=_factory(_plan(),
                                                  MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PLAN, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_back()
    assert window.current_index() == PAGE_SELECT


def test_mapping_edit_reaches_the_factory(qtbot, tmp_path):
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), calls))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PLAN, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    window.plan_page.set_mapping(0, r"E:\npm-cache")
    window.plan_page.apply_button.click()
    qtbot.waitUntil(lambda: len(calls) >= 2, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert calls[-1]["mappings"] == {r"C:\cache": r"E:\npm-cache"}


def test_failed_migration_populates_failure_table(qtbot, tmp_path):
    outcome = MigrateOutcome(
        ok=False, phase="copy_failed",
        copy=CopyResult(failures=[CopyFailure(rel="x\\y.db", reason="locked",
                                              process="chrome.exe (1234)")]))
    window = MainWindow(migrator_factory=_factory(_plan(), outcome, []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PLAN, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert window.progress_page.failure_rows() == [
        ("x\\y.db", "被占用", "chrome.exe (1234)")]
    assert window.report_page.title_text() == "未完成：copy_failed"


def test_rollback_and_cleanup_delegate_to_migrator(qtbot, tmp_path):
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done",
                                backup_path=r"E:\src.bak_20261009"), calls))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    migrator = window._migrator
    window.report_page.rollback_button.click()
    assert migrator.rolled_back is True
    window.report_page.cleanup_button.click()
    assert migrator.cleaned is True


def test_deep_verify_flag_is_forwarded(qtbot, tmp_path):
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), calls))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.plan_page.deep_check.setChecked(True)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert calls[-1]["deep"] is True


def test_pause_signal_toggles_worker(qtbot, tmp_path):
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PLAN, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PROGRESS, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    # 迁移早已结束，此时再点暂停不应抛异常
    window.progress_page.toggle_pause()
    window.progress_page.toggle_pause()
    assert window.progress_page.is_paused is False


def test_report_page_becomes_final_step(qtbot, tmp_path):
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()
    assert window.current_index() == PAGE_REPORT
    assert window.next_button.isEnabled() is False
