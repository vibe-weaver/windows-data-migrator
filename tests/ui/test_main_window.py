import sys

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMessageBox

from dmig.core import permission
from dmig.core.models import CopyFailure, CopyResult, MigrationPlan, PlanTotals, TargetGroup
from dmig.core.migrator import MigrateOutcome
from dmig.core.session import Session
from dmig.ui import main_window as main_window_mod
from dmig.ui.main_window import (LINK_ONLY_BUTTON, PAGE_PLAN, PAGE_PROGRESS,
                                 PAGE_REPORT, PAGE_SELECT, MainWindow)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


class _FakeMigrator:
    def __init__(self, plan, outcome):
        self.plan = plan
        self.outcome = outcome
        self.mappings = {}
        self.deep_verify = False
        self.skip_unknown_reparse = False
        self.cleanup_error = ""
        self.rolled_back = False
        self.cleaned = False
        self.target = getattr(plan, "target", "")
        self.source = getattr(plan, "source", "")
        self.discarded = False
        self.discard_ok = True
        self.adopted = None
        self.adopt_ok = True
        self.took_over = False
        self.takeover_result = (2, "")
        self.plan_calls = 0

    def take_over_denied(self):
        """只替「接管的调用与结果」表态，真实接管逻辑由 test_migrator 覆盖。"""
        self.took_over = True
        return self.takeover_result

    def adopt_session(self, sess):
        """只替「核心接不接受这个会话」表态，接受与否由 adopt_ok 控制。"""
        self.adopted = sess
        return self.adopt_ok

    def plan_it(self, on_progress=None):
        self.plan_calls += 1
        return self.plan

    def execute(self, *, on_progress=None, should_cancel=None, should_pause=None):
        # 真 Migrator 在执行途中会设置 backup_path，界面删备份时读的就是它
        self.backup_path = getattr(self.outcome, "backup_path", None)
        return self.outcome

    def rollback(self):
        self.rolled_back = True
        return True

    def skipped_reparse_count(self):
        return 0

    def cleanup_backup(self, force=False):
        self.cleaned = True
        return True

    def discard_target(self):
        self.discarded = True
        return self.discard_ok


class _Dialog:
    """QMessageBox 替身。

    真实弹窗是模态的，在 qtbot 里会一直转到超时；这里只记下正文并给出固定答复，
    既能断言「警告文案说清楚了后果」，也能分别走确认/取消两条分支。
    """

    StandardButton = QMessageBox.StandardButton

    def __init__(self, answer):
        self.answer = answer
        self.texts: list[str] = []

    def warning(self, parent, title, text, *buttons, **kwargs):
        self.texts.append(text)
        return self.answer

    def information(self, parent, title, text, *buttons, **kwargs):
        self.texts.append(text)
        return self.StandardButton.Ok


def _dialog(monkeypatch, answer=QMessageBox.StandardButton.Yes) -> _Dialog:
    fake = _Dialog(answer)
    monkeypatch.setattr(main_window_mod, "QMessageBox", fake)
    return fake


def _plan(mapped_to=""):
    plan = MigrationPlan(source=r"D:\src", target=r"E:\dst")
    plan.totals = PlanTotals(files=2, bytes=503, dirs=1)
    plan.required_bytes = 503
    plan.free_bytes = 1024 ** 3
    plan.link_groups = [TargetGroup(prefix=r"C:\cache", count=482,
                                    mapped_to=mapped_to)]
    return plan


def _factory(plan, outcome, calls, adopt_ok=True):
    def make(source, target, mappings, deep_verify, session_root,
             skip_unknown_reparse=False, excludes=()):
        migrator = _FakeMigrator(plan, outcome)
        migrator.mappings = dict(mappings)
        migrator.deep_verify = deep_verify
        migrator.skip_unknown_reparse = skip_unknown_reparse
        migrator.excludes = list(excludes)
        migrator.adopt_ok = adopt_ok
        calls.append({"source": source, "target": target,
                      "mappings": dict(mappings), "deep": deep_verify,
                      "skip": skip_unknown_reparse,
                      "excludes": list(excludes)})
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


def test_mapping_survives_rescan_and_reaches_migration(qtbot, tmp_path):
    """A1 回归：填过映射 → 重扫 → 开始迁移，映射必须还在。

    重扫会用新计划重建表格；一旦「重写为」列被清空，start_migration 就会拿
    空映射重建 Migrator，链接重写悄悄失效（实测过：目标端 junction 仍指向原路径）。
    """
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(mapped_to=r"E:\npm-cache"), MigrateOutcome(True, "done"), calls))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()                                   # 第一次扫描
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    window.plan_page.set_mapping(0, r"E:\npm-cache")
    window.plan_page.apply_button.click()              # 应用映射并重新扫描
    qtbot.waitUntil(lambda: len(calls) >= 2, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert calls[-1]["mappings"] == {r"C:\cache": r"E:\npm-cache"}
    # 重扫之后表格里的值没有被清空
    assert window.plan_page.group_rows() == [(r"C:\cache", 482, r"E:\npm-cache")]

    window.go_next()                                   # 开始迁移
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert calls[-1]["mappings"] == {r"C:\cache": r"E:\npm-cache"}
    assert len(calls) == 2, "映射没变，不该用空映射重建 Migrator"


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


def test_rollback_and_cleanup_delegate_to_migrator(qtbot, tmp_path, monkeypatch):
    _dialog(monkeypatch)                       # 破坏性动作现在都要确认
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


def _run_to_report(window, qtbot, tmp_path):
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PLAN, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)


def test_status_line_carries_the_real_failure_reason(qtbot, tmp_path):
    """B1：原来只显示「未完成：rename_failed」，用户既不知道原因也没有下一步。"""
    outcome = MigrateOutcome(ok=False, phase="rename_failed",
                             message="源目录被占用：notepad.exe (4242)")
    window = MainWindow(migrator_factory=_factory(_plan(), outcome, []))
    qtbot.addWidget(window)
    _run_to_report(window, qtbot, tmp_path)
    assert "notepad.exe (4242)" in window.status_line.text()
    assert window.report_page.message_text() == "源目录被占用：notepad.exe (4242)"


def test_discard_target_deletes_partial_copy(qtbot, tmp_path, monkeypatch):
    """B1 的出口：复制失败留下半成品目标时，报告页要能一键清掉。"""
    dialog = _dialog(monkeypatch)
    outcome = MigrateOutcome(ok=False, phase="copy_failed", message="复制失败")
    window = MainWindow(migrator_factory=_factory(_plan(), outcome, []))
    qtbot.addWidget(window)
    _run_to_report(window, qtbot, tmp_path)

    assert window.report_page.discard_button.isEnabled() is True
    window.report_page.discard_button.click()
    assert window._migrator.discarded is True
    assert window.status_line.text() == "目标目录已删除"
    assert "源目录不受影响" in dialog.texts[0]


def test_discard_dialog_pushes_back_after_rename_failure(qtbot, tmp_path, monkeypatch):
    """改名失败时目标已通过校验，删掉等于白复制一遍，必须先劝一句。"""
    dialog = _dialog(monkeypatch, answer=QMessageBox.StandardButton.No)
    outcome = MigrateOutcome(ok=False, phase="rename_failed", message="源目录被占用")
    window = MainWindow(migrator_factory=_factory(_plan(), outcome, []))
    qtbot.addWidget(window)
    _run_to_report(window, qtbot, tmp_path)

    window.report_page.discard_button.click()
    assert "关掉那个程序后重试迁移更划算" in dialog.texts[0]
    assert window._migrator.discarded is False
    assert window.status_line.text() == "已保留目标目录"


def test_discard_refusal_reports_why(qtbot, tmp_path, monkeypatch):
    """核心层拒绝删除（源已是指向目标的链接）时，理由必须原样冒到界面上。"""
    _dialog(monkeypatch)
    outcome = MigrateOutcome(ok=False, phase="link_verify_failed", message="链接校验失败")
    window = MainWindow(migrator_factory=_factory(_plan(), outcome, []))
    qtbot.addWidget(window)
    _run_to_report(window, qtbot, tmp_path)

    migrator = window._migrator
    migrator.discard_ok = False
    migrator.cleanup_error = "源目录已经是指向目标的链接，目标端是活数据，不能删"
    window.report_page.discard_button.click()
    assert migrator.discarded is True
    assert "活数据" in window.status_line.text()


def test_successful_run_does_not_offer_discard(qtbot, tmp_path):
    """迁移成功后源已是指向目标的链接，绝不能给出删除目标的入口。"""
    outcome = MigrateOutcome(ok=True, phase="done", backup_path=r"E:\src.bak_20261009")
    window = MainWindow(migrator_factory=_factory(_plan(), outcome, []))
    qtbot.addWidget(window)
    _run_to_report(window, qtbot, tmp_path)
    assert window.report_page.discard_button.isEnabled() is False
    assert window.report_page.rollback_button.isEnabled() is True


def test_plan_page_warns_when_file_symlinks_need_privilege(qtbot, tmp_path, monkeypatch):
    """B4：文件符号链接缺特权原来要等整棵树复制完才暴露。"""
    plan = _plan()
    plan.file_symlinks = 3
    monkeypatch.setattr(permission, "can_create_symlink", lambda: False)
    monkeypatch.setattr(permission, "is_elevated", lambda: False)
    window = MainWindow(migrator_factory=_factory(plan, MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PLAN, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    warning = window.plan_page.warning_text()
    assert "3 个文件符号链接" in warning
    assert "以管理员身份重试" in warning
    assert window.plan_page.elevate_button.isEnabled() is True


def test_symlink_probe_is_skipped_without_file_symlinks(qtbot, tmp_path, monkeypatch):
    """探测要真建一次符号链接，没有文件符号链接时不该白花这个开销。"""
    probes = []

    def probe():
        probes.append(1)
        return False

    monkeypatch.setattr(permission, "can_create_symlink", probe)
    monkeypatch.setattr(permission, "is_elevated", lambda: False)
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PLAN, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    assert probes == []
    assert "符号链接" not in window.plan_page.warning_text()
    assert window.plan_page.elevate_button.isEnabled() is False


# --- C1：从会话恢复 --------------------------------------------------
def _restored(qtbot, monkeypatch, tmp_path, source, adopt_ok=True, options=None):
    """在 tmp_path 下造一个会话文件，再用 session_id 打开主窗口。"""
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(main_window_mod, "default_root", lambda: sessions)
    sess = Session.create(source, str(tmp_path / "dst"), root=sessions)
    sess.mark_done(r"a\one.txt", 3)
    sess.options = dict(options or {})
    sess.save()
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), calls, adopt_ok=adopt_ok),
        session_id=sess.id)
    qtbot.addWidget(window)
    return window, sess


def test_restored_session_is_adopted_by_the_migrator(qtbot, tmp_path, monkeypatch):
    """只回填源/目标的话 execute() 会另起一个新会话，已复制的文件全部重复制一遍。"""
    window, sess = _restored(qtbot, monkeypatch, tmp_path, str(tmp_path / "src"))
    assert "已记录 1 个文件" in window.status_line.text()
    assert window.select_page.source() == str(tmp_path / "src")
    assert window.select_page.target() == str(tmp_path / "dst")

    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    # restore_session 是从磁盘重新 load 的，比 id 而不是比对象
    assert window._migrator.adopted is window._restored_session
    assert window._migrator.adopted.id == sess.id


def test_restored_session_options_reach_the_migrator(qtbot, tmp_path, monkeypatch):
    """C6：会话记的链接映射与深度校验要一路带到新建的 Migrator。

    只回填源/目标的话，续传用空映射重建，目标端几百个 junction 会被重新指回
    迁移前的旧位置。
    """
    outside = str(tmp_path / "outside")
    moved = str(tmp_path / "moved")
    window, sess = _restored(qtbot, monkeypatch, tmp_path, str(tmp_path / "src"),
                             options={"mappings": {outside: moved},
                                      "deep_verify": True})
    assert "1 条链接映射" in window.status_line.text()
    assert window.plan_page.mappings() == {outside: moved}
    assert window.plan_page.deep_verify() is True

    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert window._migrator.adopted.id == sess.id
    assert window._migrator.mappings == {outside: moved}
    assert window._migrator.deep_verify is True


def test_rejected_session_is_dropped_not_offered_again(qtbot, tmp_path, monkeypatch):
    """用户改了源或目标时核心会拒绝接管，旧会话必须丢掉、按新任务处理。

    `.done` 记的是**相对路径**，换一棵树沿用就是把不相干的文件当成已复制而跳过。
    """
    window, sess = _restored(qtbot, monkeypatch, tmp_path,
                             str(tmp_path / "elsewhere"), adopt_ok=False)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert window._migrator.adopted.id == sess.id
    assert window._restored_session is None

    window.plan_page.deep_check.setChecked(True)     # 逼 start_migration 重建 Migrator
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert window._migrator.adopted is None


def test_missing_session_falls_back_to_a_new_task(qtbot, tmp_path, monkeypatch):
    """提权重启后会话文件找不到（被清理、传错 ID）时不能崩，按新任务继续。"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(main_window_mod, "default_root", lambda: sessions)
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []), session_id="20261009-000000-dead")
    qtbot.addWidget(window)
    assert "按新任务处理" in window.status_line.text()
    assert window._restored_session is None


def test_session_stopped_after_the_rename_can_still_finish(qtbot, tmp_path, monkeypatch):
    """C6：会话停在改名之后，源目录已经不在了。

    前置校验必然报「源目录不存在」，原来下一步按钮就永远灰着——用户明明
    只差补建一条链接，却只能去命令行或手工建。
    """
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(main_window_mod, "default_root", lambda: sessions)
    src = tmp_path / "src"                     # 已改名，此刻什么都不是
    dst = tmp_path / "dst"
    dst.mkdir()
    backup = tmp_path / "src.bak_20261010"
    backup.mkdir()
    sess = Session.create(src, dst, root=sessions)
    sess.phase = "link_failed"
    sess.backup_path = str(backup)
    sess.save()

    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done", link_kind="junction",
                                backup_path=str(backup)), []), session_id=sess.id)
    qtbot.addWidget(window)

    assert "只差源目录链接" in window.status_line.text()
    assert window.select_page.has_blocking_error() is True
    assert window.next_button.text() == LINK_ONLY_BUTTON
    assert window.next_button.isEnabled() is True

    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    # 计划页被跳过：源目录都不在了，扫出来的只能是一棵空树
    assert window.current_index() == PAGE_PROGRESS
    assert window._migrator.plan_calls == 0
    assert window._migrator.adopted.id == sess.id

    window.go_next()
    assert window.current_index() == PAGE_REPORT
    assert window.report_page.title_text() == "迁移成功"


# --- C4：排除目录的接线 -----------------------------------------------
def test_denied_directories_can_be_excluded_from_the_page(qtbot, tmp_path):
    """页面上勾了「跳过无权限目录」，排除项必须真的传到 Migrator。

    这条走的是「计划页 → 工厂」的接线：勾选只写进页面状态却没传给核心的话，
    用户看到的与执行的就是两回事。
    """
    calls = []
    plan = _plan()
    plan.permission_issues = [str(tmp_path / "locked")]
    window = MainWindow(migrator_factory=_factory(plan, MigrateOutcome(True, "done"),
                                                  calls))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert window.current_index() == PAGE_PLAN
    assert window.plan_page.skip_denied_check.isEnabled() is True

    window.plan_page.skip_denied_check.setChecked(True)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()
    qtbot.waitUntil(lambda: window.current_index() == PAGE_PROGRESS, timeout=5000)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    assert calls[-1]["excludes"] == [str(tmp_path / "locked")]


# --- C2：接管属主的接线 -----------------------------------------------
def test_takeover_request_reaches_the_migrator(qtbot, tmp_path):
    """接管按钮此前根本不存在，take_ownership 在整个流程里零调用点。"""
    plan = _plan()
    plan.permission_issues = [str(tmp_path / "locked")]
    window = MainWindow(migrator_factory=_factory(plan, MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    window.take_over_denied()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    assert window._migrator.took_over is True
    assert "已接管 2 个目录" in window.status_line.text()
    assert "回滚时会还原" in window.status_line.text()


def test_takeover_without_a_plan_scans_first(qtbot, tmp_path):
    """还没有计划就没有「哪些目录读不到」这份清单，接管只能先扫一遍。"""
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), calls))
    qtbot.addWidget(window)
    _select(window, tmp_path)

    window.take_over_denied()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert calls, "应当先走一次扫描"
    assert window._migrator.took_over is False


# --- C10：破坏性动作先确认 -------------------------------------------
def _finished_window(qtbot, tmp_path, monkeypatch, *, answer):
    """跑到报告页的窗口：此时回滚与删备份都可用。"""
    dialog = _dialog(monkeypatch, answer)
    backup = str(tmp_path / "src.bak_20261009120000")
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done", backup_path=backup), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    window.go_next()                       # 执行 → 报告
    assert window.current_index() == PAGE_REPORT
    return window, dialog


def test_rollback_asks_before_undoing(qtbot, tmp_path, monkeypatch):
    """回滚会摘掉链接、把备份改回原名，点一下就执行是不可接受的。"""
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.No)
    window.rollback()
    assert window._migrator.rolled_back is False
    assert "已取消回滚" in window.status_line.text()
    assert "确认回滚吗" in dialog.texts[-1]


def test_rollback_warns_about_orphaned_new_data(qtbot, tmp_path, monkeypatch):
    """迁移后写进目标的新数据会被孤立，这句话必须出现，否则用户想不到。"""
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.No)
    window.rollback()
    assert "孤儿" in dialog.texts[-1]


def test_rollback_runs_after_confirming(qtbot, tmp_path, monkeypatch):
    window, _ = _finished_window(qtbot, tmp_path, monkeypatch,
                                 answer=QMessageBox.StandardButton.Yes)
    window.rollback()
    assert window._migrator.rolled_back is True
    assert "已回滚到原目录" in window.status_line.text()


def test_cleanup_asks_before_deleting_the_backup(qtbot, tmp_path, monkeypatch):
    """删备份之后就无法再回滚了，这一步必须问。"""
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.No)
    window.cleanup_backup()
    assert window._migrator.cleaned is False
    assert "已保留备份" in window.status_line.text()
    assert "最后一份副本" in dialog.texts[-1]


def test_cleanup_runs_after_confirming(qtbot, tmp_path, monkeypatch):
    window, _ = _finished_window(qtbot, tmp_path, monkeypatch,
                                 answer=QMessageBox.StandardButton.Yes)
    window.cleanup_backup()
    assert window._migrator.cleaned is True


class _RunningWorker:
    """只替「还有活儿在跑」这件事表态，好让关窗走确认分支。"""

    def __init__(self):
        self.cancelled = False
        self.waited = False

    def isRunning(self):                       # noqa: N802 - Qt 命名
        return True

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout):                   # noqa: N802 - Qt 命名
        self.waited = True
        return True


def test_closing_mid_migration_asks_first(qtbot, tmp_path, monkeypatch):
    """迁移中关窗此前是静默取消退出：用户以为关掉就一切复原了。"""
    dialog = _dialog(monkeypatch, QMessageBox.StandardButton.No)
    window = MainWindow(migrator_factory=_factory(_plan(), MigrateOutcome(True, "done"),
                                                  []))
    qtbot.addWidget(window)
    worker = _RunningWorker()
    window._worker = worker

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is False
    assert worker.cancelled is False
    assert "迁移还在进行中" in dialog.texts[-1]
    assert "源目录不受影响" in dialog.texts[-1]
    assert "续传" in dialog.texts[-1]


def test_closing_is_allowed_after_confirming(qtbot, tmp_path, monkeypatch):
    _dialog(monkeypatch, QMessageBox.StandardButton.Yes)
    window = MainWindow(migrator_factory=_factory(_plan(), MigrateOutcome(True, "done"),
                                                  []))
    qtbot.addWidget(window)
    worker = _RunningWorker()
    window._worker = worker

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is True
    assert worker.cancelled is True
    assert worker.waited is True


def test_closing_idle_window_does_not_ask(qtbot, tmp_path, monkeypatch):
    """没有任务在跑时不该弹任何窗，否则正常退出都被拦一下。"""
    dialog = _dialog(monkeypatch, QMessageBox.StandardButton.Yes)
    window = MainWindow(migrator_factory=_factory(_plan(), MigrateOutcome(True, "done"),
                                                  []))
    qtbot.addWidget(window)

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is True
    assert dialog.texts == []
