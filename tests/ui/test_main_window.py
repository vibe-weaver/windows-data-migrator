import sys
import time

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMessageBox

from dmig.core import permission
from dmig.core.models import CopyFailure, CopyResult, MigrationPlan, PlanTotals, TargetGroup
from dmig.core.migrator import MigrateOutcome
from dmig.core.session import Session
from dmig.ui import main_window as main_window_mod
from dmig.ui.main_window import (LINK_ONLY_BUTTON, PAGE_HOLDERS, PAGE_PLAN,
                                 PAGE_PROGRESS, PAGE_REPORT, PAGE_SELECT,
                                 MainWindow)

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
        self.cleared_target = False
        self.cleanup_calls = []
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
        # 真扫描器会把用户映射落到分组的 `mapped_to` 上（linker.group_targets）。
        # 替身必须照做，否则「映射已应用」这类判断在替身上永远走不到，
        # 测试就变成在验证替身、而不是验证真货。
        for group in (getattr(self.plan, "link_groups", None) or []):
            group.mapped_to = self.mappings.get(group.prefix, "")
        return self.plan

    def execute(self, *, on_progress=None, should_cancel=None, should_pause=None):
        # 真 Migrator 在执行途中会设置 backup_path，界面删备份时读的就是它
        self.backup_path = getattr(self.outcome, "backup_path", None)
        return self.outcome

    def rollback(self):
        self.rolled_back = True
        # 真 Migrator 会拒绝（源是链接又没有备份时），替身要能表达这条分支，
        # 否则「被拒时界面说了什么」在替身上永远走不到。
        return getattr(self, "rollback_ok", True)

    def can_clear_target(self):
        """真货靠会话记账判断目标端有没有迁移之外的内容。"""
        return getattr(self, "clear_ok", True), getattr(self, "clear_reason", "")

    def has_target(self):
        return getattr(self, "target_exists", True)

    def clear_target(self):
        """回滚后清目标端内容（保留目录本身）。"""
        self.cleared_target = True
        return getattr(self, "clear_ok", True)

    def skipped_reparse_count(self):
        return getattr(self, "only_in_backup", 0)

    def cleanup_backup(self, force=False):
        """默认删得掉；`cleanup_ok=False` 时首次失败、强制才成功。

        真实核心的行为就是这样：备份里若只有「只存在于备份里」的内容（跳过的
        重解析点、被排除的目录），它会拒绝删除，直到调用方明确要求强制。
        替身必须能重现这个两段式，否则「强制删除」那条路测不到。
        """
        self.cleanup_calls.append(force)
        if not getattr(self, "cleanup_ok", True) and not force:
            self.cleanup_error = "备份里有只存在于备份里的内容"
            return False
        self.cleaned = True
        return True

    def discard_target(self):
        self.discarded = True
        return self.discard_ok


class _Dialog:
    """QMessageBox 替身。

    真实弹窗是模态的，在 qtbot 里会一直转到超时；这里只记下正文并给出固定答复，
    既能断言「警告文案说清楚了后果」，也能分别走确认/取消两条分支。

    **答复必须返回 `int`**：真实 Qt 返回的是普通 int，不是 `StandardButton`
    枚举成员。替身若返回枚举，`answer is StandardButton.Yes` 这类判断就会
    在替身上成立、在真货上恒为假——测试全绿而线上点「是」毫无反应。
    """

    StandardButton = QMessageBox.StandardButton

    def __init__(self, answer):
        self.answer = answer
        # 逐次答复：一次流程里可能弹**多个**确认框（例如「确认删除吗」之后再问
        # 「仍要强制删除吗」），只有一个固定答复就测不了「先答应后拒绝」。
        # 列表非空时按顺序取，取完沿用 answer。
        self.answers: list = []
        self.texts: list[str] = []
        # 标题也是弹窗内容的一部分：只记正文就无法断言「成功提示的标题是什么」。
        self.titles: list[str] = []
        # 带勾选项的确认框（_confirm_with_option）：
        # confirm_ok=None 表示沿用 answer（和静态方法的答复保持一致），
        # option_checked 控制那个勾选项的状态。
        self.confirm_ok = None
        self.option_checked = False
        self.option_texts: list[str] = []
        self.option_defaults: list[bool] = []

    def _next_answer(self):
        if self.answers:
            return int(self.answers.pop(0))
        return int(self.answer)

    def warning(self, parent, title, text, *buttons, **kwargs):
        self.titles.append(title)
        self.texts.append(text)
        return self._next_answer()

    def information(self, parent, title, text, *buttons, **kwargs):
        self.titles.append(title)
        self.texts.append(text)
        return int(self.StandardButton.Ok)

    def confirm_with_option(self, parent, title, text, option_text, *,
                            default=False):
        """替 `_confirm_with_option`。

        真货要**构造** QMessageBox（静态方法挂不上 setCheckBox），构造路径没法
        用这个替身表达，所以那一步单独留成模块级函数供替换。
        """
        self.titles.append(title)
        self.texts.append(text)
        self.option_texts.append(option_text)
        self.option_defaults.append(default)
        if self.confirm_ok is None:
            return self.answer == self.StandardButton.Yes, self.option_checked
        return self.confirm_ok, self.option_checked


def _dialog(monkeypatch, answer=QMessageBox.StandardButton.Yes) -> _Dialog:
    fake = _Dialog(answer)
    monkeypatch.setattr(main_window_mod, "QMessageBox", fake)
    # 带勾选项的确认框是构造 QMessageBox 实现的，替身表达不了构造路径，
    # 所以走单独的函数缝；这里一并换掉，两个替身共用同一份记录。
    monkeypatch.setattr(main_window_mod, "_confirm_with_option",
                        fake.confirm_with_option)
    return fake


def test_replan_reports_what_was_applied(qtbot, tmp_path):
    """点「应用映射并重新扫描」后必须给出可感知的结果。

    实测：重扫只要 0.03 秒，状态栏从「正在扫描…」一闪就回到「计划已生成」——
    和点击前完全一样；而表格里「重写为」那格本来就是你刚填的内容。
    所以用户看到的就是「点了没反应」。重扫结束后必须说明
    「应用了什么、影响多少条链接」。
    """
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(ok=True, phase="done",
                                copy=CopyResult(copied_files=2)), calls))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)
    assert window.current_index() == PAGE_PLAN

    window.plan_page.set_mapping(0, r"E:\npm-cache")
    window.replan()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    text = window.statusBar().text()
    assert "映射已应用" in text, f"重扫后必须说明结果，实际状态栏={text!r}"
    assert "482" in text, f"要报出影响多少条链接，实际状态栏={text!r}"


def test_plain_rescan_says_no_mapping_was_set(qtbot, tmp_path):
    """没填映射就重扫（例如改了「跳过重解析点」）时不要谎报「映射已应用」。"""
    calls = []
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(ok=True, phase="done",
                                copy=CopyResult(copied_files=2)), calls))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    window.go_next()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    window.replan()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    text = window.statusBar().text()
    assert "映射已应用" not in text, f"没设映射却报了已应用：{text!r}"


def test_confirm_helper_compares_by_value_not_identity():
    """确认判断必须按值比较——真实 QMessageBox 返回的是普通 int。

    曾经写成 `answer is not QMessageBox.StandardButton.Yes`。真实返回值是
    普通 int（「是」= 16384），`is` 与枚举成员**恒不相等**，于是用户点「是」
    也一律走取消分支：删备份、一键回滚、删除目标、迁移中关窗全都点了没反应。

    测试当时抓不到，是因为替身返回的是枚举成员，`is` 在替身上恰好成立。
    这条测试同时锁住真实形态（int）和替身形态（枚举）。
    """
    yes = QMessageBox.StandardButton.Yes
    no = QMessageBox.StandardButton.No

    # 真实 Qt 的返回形态：普通 int
    assert main_window_mod._confirmed(int(yes)) is True
    assert main_window_mod._confirmed(int(no)) is False
    # 替身可能直接返回枚举，也要成立
    assert main_window_mod._confirmed(yes) is True
    assert main_window_mod._confirmed(no) is False
    # 这就是当初踩的坑：值相等但不是同一个对象
    assert int(yes) is not yes
    assert int(yes) == yes


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


def _rename_blocked(monkeypatch, names):
    """让复验判定为「仍然改不了名」，并点名这些条目还挡着。

    判定走的是 `probe_rename`（整棵树改一次名，O(1)），**不是** `probe_entries`
    ——后者要逐个顶层条目试，0.67 毫秒/条目，而且还跑在主线程上。点名才用后者。
    """
    from dmig.core import guard
    monkeypatch.setattr(
        guard, "probe_rename",
        lambda source: [guard.Issue("rename_blocked", "源目录无法改名")])
    monkeypatch.setattr(guard, "probe_entries", lambda *a, **k: list(names))

def _shutdown(started, ok, message):
    """构造一个 ShutdownResult——用真实类型，不做元组替身。

    `started` 决定界面给不给「强制结束」：被护栏拦下时强制走同一条路，
    给了也是死按钮。
    """
    from dmig.core.locks import ShutdownResult
    return ShutdownResult(started=started, ok=ok, message=message)


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
    # copy_failed 没收录进 PHASE_TEXT，按设计原样显示（宁可露出生词也不吞信息）
    assert window.report_page.title_text() == "未完成：copy_failed"


def test_rollback_and_cleanup_delegate_to_migrator(qtbot, tmp_path, monkeypatch):
    """两个收尾按钮都真的调到了核心。

    这里只走「回滚」：回滚成功之后备份已经不存在了（被改回原名），
    「删除备份」会被正确地禁用——所以删备份那条路要在另一个窗口里验
    （见 test_cleanup_runs_after_confirming）。
    """
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
    assert migrator.cleaned is False, "回滚后没有备份可删，那个按钮该是禁用的"
    assert window.report_page.cleanup_button.isEnabled() is False


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
    assert window.next_button.text() == LINK_ONLY_BUTTON
    # 检验会报「源目录不存在」（它确实已被改名），但这条路必须照样走得通：
    # 用户只差最后一步，卡在这里就只能去命令行或手工建链接。
    _run_check(window, qtbot)
    assert window.select_page.has_blocking_error() is True
    assert window.next_button.isEnabled() is True, "只差建链接时不该被检验拦住"

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


def test_successful_rollback_shows_a_dialog(qtbot, tmp_path, monkeypatch):
    """回滚成功不能只在状态栏留一行字。

    这是把路径结构改回去的动作（链接摘掉、备份改回原名），执行完到底是
    「源路径恢复了」「目标端那份还在不在」都得说清楚，否则用户不敢确信
    操作真的生效了，也不知道接下来该不该清理目标端。
    """
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    window.rollback()

    assert dialog.titles[-1] == "回滚完成"
    text = dialog.texts[-1]
    assert window._migrator.source in text, "要说明源路径已恢复"
    assert window._migrator.target in text, "要说明目标端那份数据还在"
    assert "删除目标" in text, "要给出怎么清理目标端"


def test_successful_backup_deletion_shows_a_dialog(qtbot, tmp_path, monkeypatch):
    """删备份成功也要明确告知，并把「此后无法回滚」讲清楚。"""
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    window.cleanup_backup()

    text = dialog.texts[-1]
    assert "备份已删除" in text
    assert "无法再回滚" in text, "删完就回不去了，这一步必须讲"
    assert "目标端" in text, "要说清目标端数据没受影响"


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


def test_select_check_blocks_the_next_step_on_a_rename_probe_failure(
        qtbot, tmp_path, monkeypatch):
    """「开始检验」查出源目录改不了名时，必须按住「开始扫描」。

    改名是迁移的**最后一步**：复制、校验全做完才会走到那里，而它的失败门槛低得
    多（任何句柄都算）。不在第一步拦住，用户就要白等几个小时。
    """
    from dmig.core import guard
    monkeypatch.setattr(guard, "probe_rename", lambda source: [
        guard.Issue("rename_blocked", "源目录无法改名：拒绝访问", severity="error")])

    (tmp_path / "src").mkdir()                       # 源必须真的存在，否则前置校验先拦
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    assert window.next_button.isEnabled() is False, "没检验过就不该放行"

    _run_check(window, qtbot)          # 真点按钮

    assert "rename_blocked" in {i.code for i in window.select_page.issues()}
    assert window.next_button.isEnabled() is False, "红色错误必须按住下一步"
    assert "未通过" in window.status_line.text()


def _run_check(window, qtbot, timeout=10000):
    """点「开始检验」并等 worker 把结论回填。

    检验是异步的（试探改名与文件数线性相关，0.46 微秒/文件，放主线程会冻界面），
    所以点完必须等 `is_busy()` 落下来——`_on_check_done` 在 `finished` 之前发出，
    等到不忙时结论已经渲染好了。
    """
    window.select_page.check_button.click()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=timeout)


def test_check_with_incomplete_paths_does_not_claim_success(qtbot, tmp_path):
    """路径不全时点「开始检验」，绝不能显示绿字「前置校验通过」。

    空问题列表会走进 `_render_issues` 的绿字分支——而这里根本还没得检验，
    说「通过」是假话。这也是把检验拆成「进忙碌态 / 回填」之后我自己引入过的
    回归：`apply_check([])` 当时对这个分支是错的。
    """
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    window.select_page.set_source(str(tmp_path / "src"))     # 目标留空

    _run_check(window, qtbot)

    text = window.select_page.issues_text()
    assert "请选择" in text
    assert "通过" not in text, f"路径不全都说通过了：{text!r}"
    assert window.select_page.is_checked() is False
    assert window.select_page.has_blocking_error() is False
    # 忙碌态必须已经退出，否则「开始检验」永远灰着
    assert window.select_page.check_button.text() == "开始检验"
    assert window.select_page.check_button.isEnabled() is True
    assert window.next_button.isEnabled() is False


def test_select_check_runs_off_the_main_thread(qtbot, tmp_path, monkeypatch):
    """**检验必须跑在 worker 里，界面只进忙碌态。**

    这条是钉「异步」本身的：试探改名与文件数线性相关（实测 0.46 微秒/文件，
    十万文件 47 ms），慢盘或杀软过滤驱动下到秒级；跑在主线程就是窗口无响应，
    用户看到的就是「点了没反应」。所以把 guard.check 拖慢，点下去之后
    `is_busy()` 必须立刻为真、按钮显示「正在检验…」——同步实现下这两条都不成立。
    """
    from dmig.core import guard

    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)

    def slow_check(source, target):
        time.sleep(0.4)
        return []

    monkeypatch.setattr(guard, "check", slow_check)
    monkeypatch.setattr(guard, "probe_rename", lambda source: [])

    window.select_page.check_button.click()

    assert window.is_busy() is True, "检验期间界面必须是忙碌态，不能同步跑完"
    assert window.select_page.check_button.text() == "正在检验…"
    assert window.select_page.check_button.isEnabled() is False

    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    assert window.select_page.check_button.text() == "开始检验"
    assert window.select_page.has_blocking_error() is False
    assert "检验通过" in window.status_line.text()


def test_a_system_directory_is_refused_before_any_rename(qtbot, tmp_path,
                                                         monkeypatch):
    """系统目录要被红线拦住，而且**一次试探都不做**。

    用户实测：选 C:\\Program Files (x86) 点「开始检验」，界面冻死——根因是
    试探改名会真的把这个系统目录改名再改回，提权进程里会牵动 WOW64 重定向、
    AppCompat 与杀软过滤驱动重新评估整棵子树。
    """
    from dmig.core import guard

    called = []
    monkeypatch.setattr(guard, "probe_rename",
                        lambda source: called.append(source) or [])

    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    window.select_page.set_source(r"C:\Program Files (x86)")
    window.select_page.set_target(str(tmp_path / "dst"))

    _run_check(window, qtbot)

    assert called == [], "系统目录不该被试探改名"
    assert "source_is_system_dir" in {i.code for i in window.select_page.issues()}
    assert window.select_page.has_blocking_error() is True
    assert window.next_button.isEnabled() is False


def test_select_check_lets_a_warning_through(qtbot, tmp_path, monkeypatch):
    """黄色警告不该拦人：同盘迁移不释放空间，但用户可能就是想换个目录。"""
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)                        # 源与目标同盘 -> 一条 warning

    _run_check(window, qtbot)

    assert window.select_page.has_blocking_error() is False
    assert window.next_button.isEnabled() is True
    assert "检验通过" in window.status_line.text()


def test_select_check_does_not_probe_when_the_pre_checks_already_fail(
        qtbot, tmp_path, monkeypatch):
    """前置校验已经有 error 时不去试探：源可能根本不是目录，试探没有意义。

    也顺带保证试探的副作用（路径消失几毫秒）不会在一个注定失败的选择上发生。
    """
    from dmig.core import guard
    called = []
    monkeypatch.setattr(guard, "probe_rename",
                        lambda source: called.append(source) or [])

    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)                        # 源目录不存在 -> error

    _run_check(window, qtbot)

    assert called == []
    assert window.select_page.has_blocking_error() is True


def test_backup_warning_disappears_after_the_backup_is_deleted(qtbot, tmp_path,
                                                               monkeypatch):
    """删掉备份之后，那条警告必须撤掉。

    否则报告页会继续喊「请勿删除备份」，而那个目录早就不在了——警告指向一个
    不存在的东西，比没有警告更糟：用户会以为删除没生效，或者以为还有备份可回滚。
    """
    window, _ = _finished_window(qtbot, tmp_path, monkeypatch,
                                 answer=QMessageBox.StandardButton.Yes)
    assert window.report_page.backup_warning_text() != ""

    window.cleanup_backup()

    assert window._migrator.cleaned is True
    assert window.report_page.backup_warning_text() == ""


def test_deleting_the_target_disables_its_button(qtbot, tmp_path, monkeypatch):
    """删掉目标端之后那个按钮要关掉。

    再点一次只会得到「目标目录不存在，无需清理」——自相矛盾，用户不知道
    到底是删成功了还是出了问题。
    """
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    window.rollback()                            # 回滚后目标端才是可清的孤儿
    assert window.report_page.discard_button.isEnabled() is True

    window.discard_target()

    assert window._migrator.discarded is True
    assert window.report_page.discard_button.isEnabled() is False


def test_log_sink_is_detached_when_the_worker_is_released(qtbot, tmp_path,
                                                         monkeypatch):
    """迁移结束后必须显式摘掉日志接收方，别让行为依赖 GC 时机。

    接收方是挂在 worker 信号上的方法；worker 一被释放那个信号就是死对象。
    不摘的话「收尾动作的日志进不进界面面板」要看 worker 有没有恰好被回收——
    同一操作两种结果。
    """
    window, _ = _finished_window(qtbot, tmp_path, monkeypatch,
                                 answer=QMessageBox.StandardButton.Yes)
    assert window._migrator.log_sink is None

    # 摘掉之后走收尾动作不该出问题（日志只进文件）
    window.cleanup_backup()
    assert window._migrator.cleaned is True


def test_every_table_is_read_only(qtbot):
    """四张表都必须是只读的。

    `qf.TableWidget` 默认 `DoubleClicked|EditKeyPressed|AnyKeyPressed`——敲任意
    一个键就进编辑态，格子变成带清空按钮的输入框，看着像坏了。表里全是从文件
    系统查出来的事实，不该能改。
    """
    import qfluentwidgets as qf

    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)

    no_edit = qf.TableWidget.EditTrigger.NoEditTriggers
    tables = {
        "占用者": window.holders_page.table,
        "计划分组": window.plan_page.group_table,
        "失败清单": window.progress_page.failure_table,
        "未完成清单": window.report_page.table,
    }
    for name, table in tables.items():
        assert table.editTriggers() == no_edit, f"{name}表还能编辑"


def test_the_mapping_editor_still_works_after_disabling_cell_editing(qtbot):
    """禁用表格的编辑触发器不能连带废掉「重写为」那个输入框。

    它是 setCellWidget 放进格子的独立控件，自己处理事件，不走表的编辑触发器——
    但这条约束很容易在后续改动里被弄坏，而坏了的表现是「映射改不了」，
    离原因很远。
    """
    from dmig.core.models import TargetGroup

    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    plan = _plan()
    plan.link_groups = [TargetGroup(prefix=r"\\server\share\pkg", count=3,
                                    mapped_to="")]
    window.plan_page.load(plan)

    editor = window.plan_page.mapping_editor(0)
    assert editor is not None, "「重写为」的输入框没了"
    assert editor.isEnabled() is True
    editor.setText(r"E:\repos\pkg")
    assert window.plan_page.mapping_editor(0).text() == r"E:\repos\pkg"


def test_no_page_leaks_markdown_into_labels(qtbot):
    """所有页面上的文案都不许出现 Markdown 记号——Qt 会把它原样显示成星号。

    这个错在本项目里犯过三次（计划页的映射说明、报告页的「目标端未删除」、
    占用页的说明），所以不再靠人眼看：把每个页面的控件文字全过一遍。
    """
    from PySide6.QtWidgets import QLabel

    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)

    bad = []
    for index in range(window.stack.count()):
        window.stack.setCurrentIndex(index)
        page = window.stack.widget(index)
        for label in page.findChildren(QLabel):
            if "**" in label.text():
                bad.append((index, label.text()[:70]))

    assert bad == [], f"这些文案漏了 Markdown 记号：{bad}"


def test_holders_entry_only_appears_for_a_rename_failure(qtbot, tmp_path,
                                                         monkeypatch):
    """「找出占用进程」只在改名被挡住时露出来。

    别的错误（源不存在、空间不足）用那一页解决不了，摆个按钮只会把人带错地方。
    """
    from dmig.core import guard
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)

    _run_check(window, qtbot)          # 只有一个同卷警告
    assert window.select_page.holders_button.isHidden() is True

    monkeypatch.setattr(guard, "probe_rename", lambda source: [
        guard.Issue("rename_blocked", "源目录无法改名：拒绝访问", severity="error")])
    _run_check(window, qtbot)
    assert window.select_page.holders_button.isVisibleTo(
        window.select_page) is True


def test_holders_channel_navigates_and_scans(qtbot, tmp_path, monkeypatch):
    """点通道直接跳到那一页，并立刻开始扫描。"""
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    monkeypatch.setattr(guard, "probe_entries", lambda *a, **k: ["build"])
    monkeypatch.setattr(locks, "find_holders", lambda *a, **k: [])

    window.select_page.holders_button.click()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    assert window.current_index() == PAGE_HOLDERS
    assert window.holders_page.state() in ("holders", "none")
    # 步骤条仍停在第一步：这是子流程，不是向导的第 5 步
    assert window.step_bar.current() == PAGE_SELECT


def test_holders_page_returns_and_rechecks(qtbot, tmp_path, monkeypatch):
    """返回时重跑检验，让第一步的红线如实反映现在的状态。"""
    from dmig.core import guard
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    calls = []
    monkeypatch.setattr(guard, "probe_entries", lambda *a, **k: [])
    monkeypatch.setattr(guard, "probe_rename",
                        lambda s: calls.append(s) or [])

    window.stack.setCurrentIndex(PAGE_HOLDERS)
    window.holders_page.back_button.click()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    assert window.current_index() == PAGE_SELECT
    assert calls, "返回时应当重新做了改名试探"



def test_protected_processes_are_reported_with_reasons(qtbot, tmp_path,
                                                       monkeypatch):
    """批次里有受保护进程时，界面要把「为什么不动手」原样转达，**且不给强制按钮**。

    走真的关闭路径（替身核心），不是自己设一段文字再读回来——那种测法什么都
    证明不了。

    **不给强制按钮是重点**：强制结束走的是同一条护栏，一样会被拦下来，
    给了就是个按了必然没反应的死按钮，比不给更糟。
    """
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    _rename_blocked(monkeypatch, ["held"])
    monkeypatch.setattr(
        locks, "shutdown_holders",
        lambda paths, force=False: _shutdown(
            False, False,
            "这批文件同时被下面这些程序占用，本工具不会关闭它们：\n"
            "资源管理器 (8812)——关掉它会让桌面、任务栏和所有已打开的文件夹一起消失\n"
            "\n请手动关掉它们，再回来重新检验。"))

    window._holder_paths = ["x"]
    window.shutdown_holders(force=False)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    page = window.holders_page
    assert "桌面" in page.verdict_text()
    assert "请手动关掉它们" in page.verdict_text()
    assert page.can_force() is False, "被护栏拦下时强制也没用，不该给死按钮"


def test_holder_files_come_from_the_worker_not_a_second_walk(
        qtbot, tmp_path, monkeypatch):
    """文件清单必须由 worker 交回来，界面**不许自己再收一遍**。

    实测踩到的坑：worker 里收了一遍，`_on_holders_found` 又在**主线程**收了一遍
    ——同一棵树走两遍本就是浪费，而第二遍占着主线程，4 万文件就把界面冻住
    2.4 秒（平均帧间隔 0.86 毫秒，却单次停顿 2441 毫秒，用户感受就是卡死）。
    """
    import inspect

    from dmig.ui import main_window as mw

    source = inspect.getsource(mw.MainWindow._on_holders_found)
    assert "collect_files" not in source, "界面又自己去收文件了"

    from dmig.core import guard, locks
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.dat").write_bytes(b"x")     # 得有文件，才验证得了「清单被交回来」
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    monkeypatch.setattr(guard, "probe_entries", lambda *a, **k: [])
    monkeypatch.setattr(locks, "find_holders", lambda *a, **k: [])

    window.select_page.holders_button.click()
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    # 关闭时要拿同一批去注册——这里确认它确实被带回来了（而不是空的）
    assert window._holder_paths, "worker 没有把文件清单交回来"


def test_scan_skips_narrowing_when_there_are_too_many_top_level_entries(
        qtbot, tmp_path, monkeypatch):
    """顶层条目太多时跳过「逐个试探」。

    试探是 0.67 毫秒/条目，而它想省的只是 RM 的 0.13 毫秒/文件。顶层本身就有
    几万条目时，这一步比它要省的那步还贵（外推 10 万条目要 67 秒）。
    """
    from dmig.ui import workers
    from dmig.ui.workers import HolderScanWorker

    src = tmp_path / "src"
    src.mkdir()
    for i in range(workers.NARROW_LIMIT + 5):
        (src / f"f{i}.dat").write_bytes(b"x")

    probed = []
    monkeypatch.setattr(workers.guard, "probe_entries",
                        lambda *a, **k: probed.append(1) or [])
    monkeypatch.setattr(workers.locks, "find_holders", lambda *a, **k: [])

    worker = HolderScanWorker(str(src))
    got = []
    worker.found.connect(lambda *a: got.append(a))
    worker.run()

    assert probed == [], "条目太多还去逐个试探"
    holders, blocked, note, files = got[0]
    assert blocked == []
    assert "跳过" in note and "条目" in note, note
    assert len(files) == workers.NARROW_LIMIT + 5, "兜底要扫整棵树"


def test_scan_still_narrows_when_the_tree_is_normal(qtbot, tmp_path, monkeypatch):
    """条目数正常时照旧缩范围——别为了优化把常规路径也改掉了。"""
    from dmig.ui import workers
    from dmig.ui.workers import HolderScanWorker

    src = tmp_path / "src"
    for name in ("held", "free"):
        (src / name).mkdir(parents=True)
    (src / "held" / "a.dat").write_bytes(b"x")

    probed = []
    monkeypatch.setattr(workers.guard, "probe_entries",
                        lambda *a, **k: probed.append(1) or ["held"])
    monkeypatch.setattr(workers.locks, "find_holders", lambda *a, **k: [])

    worker = HolderScanWorker(str(src))
    got = []
    worker.found.connect(lambda *a: got.append(a))
    worker.run()

    assert probed == [1], "常规情况应当照旧缩范围"
    holders, blocked, note, files = got[0]
    assert blocked == ["held"]
    # 只收嫌疑子树的文件，不是整棵
    assert len(files) == 1 and files[0].endswith("a.dat")


def test_rm_batch_is_large_enough_to_amortise_the_session():
    """RM 的批量不能太小——实测批量越大越快。

    6000 个文件按 300 一批要 0.967 秒，一次 6000 只要 0.408 秒（2.4 倍）。
    成本主要在会话本身，批太小等于把开销付了几十遍。
    """
    from dmig.core import locks

    assert locks.RM_BATCH >= 1000, locks.RM_BATCH


def test_shutdown_reported_as_failed_but_actually_worked_says_solved(
        qtbot, tmp_path, monkeypatch):
    """**实测踩到的坑**：RmShutdown 报失败，但程序其实已经放开了文件。

    RmShutdown 返回 351，微软的定义是「有些程序关掉了、有些没有」——部分成功。
    原来 `_on_shutdown_done` 里写的是 `if not ok: return`，把复验整个跳过，
    用户看到「请求关闭失败」，非得再点一次「重新扫描」才发现其实成了。

    真正的结论只能由改名试探给出：只要现在改得动名，就必须报「已解除」。
    """
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    monkeypatch.setattr(
        locks, "shutdown_holders",
        lambda paths, force=False: _shutdown(True, False, "有的程序关掉了，有的没有关掉"))
    monkeypatch.setattr(guard, "probe_entries", lambda *a, **k: [])

    window._holder_paths = ["x"]
    window.shutdown_holders(force=False)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    page = window.holders_page
    assert "已解除" in page.verdict_text(), page.verdict_text()
    assert "失败" not in page.verdict_text()
    assert page.can_force() is False, "已经解决了就不该再给强制结束"

def test_shutdown_failed_and_still_blocked_offers_force(qtbot, tmp_path,
                                                        monkeypatch):
    """真没解决时：如实说，并给强制结束这条路。"""
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    monkeypatch.setattr(
        locks, "shutdown_holders",
        lambda paths, force=False: _shutdown(True, False, "有的程序关掉了，有的没有关掉"))
    _rename_blocked(monkeypatch, ["held"])

    window._holder_paths = ["x"]
    window.shutdown_holders(force=False)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    page = window.holders_page
    assert "有的程序关掉了" in page.verdict_text()
    assert "held" in page.verdict_text()
    assert page.can_force() is True


def test_partial_shutdown_error_text_is_honest():
    """351 的文案必须说清「有的关掉了」。

    原来写的是「系统拒绝了这次关闭操作」——而 351 的实际含义是部分成功
    （有些关掉了、有些没有）。说成彻底失败，用户会以为白点了。
    """
    from dmig.core import locks

    text = locks._SHUTDOWN_ERROR[351]
    assert "有的" in text and "没有" in text
    assert "拒绝" not in text


def test_successful_shutdown_rechecks_the_probe(qtbot, tmp_path, monkeypatch):
    """关闭之后必须重新试探：**能改名才算解决了**，不能凭"关掉了"就说成功。"""
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    monkeypatch.setattr(locks, "shutdown_holders",
                        lambda paths, force=False: _shutdown(True, True, "已请求占用程序关闭"))
    monkeypatch.setattr(guard, "probe_entries", lambda *a, **k: [])

    window._holder_paths = ["x"]
    window.shutdown_holders(force=False)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    assert "已解除" in window.holders_page.verdict_text()


def test_shutdown_that_did_not_help_says_so(qtbot, tmp_path, monkeypatch):
    """关掉了程序但源目录仍改不了名时，不能报成功。"""
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    monkeypatch.setattr(locks, "shutdown_holders",
                        lambda paths, force=False: _shutdown(True, True, "已请求占用程序关闭"))
    _rename_blocked(monkeypatch, ["build"])

    window._holder_paths = ["x"]
    window.shutdown_holders(force=False)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    text = window.holders_page.verdict_text()
    assert "仍然无法改名" in text
    assert "build" in text


def test_forced_shutdown_needs_its_own_confirmation(qtbot, tmp_path, monkeypatch):
    """强制结束会丢未保存的内容，必须单独确认一次。"""
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.No)
    called = []
    monkeypatch.setattr(locks, "shutdown_holders",
                        lambda paths, force=False: called.append(force) or _shutdown(True, True, "ok"))
    monkeypatch.setattr(guard, "probe_entries", lambda *a, **k: [])

    window._holder_paths = ["x"]
    window.shutdown_holders(force=True)

    assert called == [], "用户点了「否」就不该动"
    assert "强制结束" in dialog.titles[-1]
    assert "未保存" in dialog.texts[-1]
    # Qt 不渲染 Markdown，弹窗里漏了 ** 就会原样显示星号
    assert "**" not in dialog.texts[-1], dialog.texts[-1]


def test_holder_scan_uses_one_file_walker_for_scan_and_shutdown():
    """扫描与关闭必须共用同一套「收哪些文件」的口径。

    各写一套走法迟早分叉成「扫出来了却关不掉」：表格里列着某个进程，
    关闭时却没把它的文件注册进去，于是 RmShutdown 什么都没关，界面还报成功。
    """
    import inspect

    from dmig.ui import workers

    source = inspect.getsource(workers.HolderScanWorker.run)
    assert "collect_files" in source
    # 缩了范围就要用上：不许在缩完范围之后又去扫整棵树
    assert "_sample_files" not in source


def test_scan_limits_itself_to_the_blocked_subtrees(tmp_path):
    """缩小范围要真的落在扫描上，否则 RM 那一步还是整棵树。"""
    from dmig.ui.workers import collect_files

    src = tmp_path / "src"
    for name in ("held", "free"):
        (src / name).mkdir(parents=True)
    (src / "held" / "a.txt").write_text("x", encoding="utf-8")
    (src / "free" / "b.txt").write_text("x", encoding="utf-8")

    only_held = collect_files([str(src / "held")])
    assert [p.replace("\\", "/").split("/")[-2] for p in only_held] == ["held"]


def test_rollback_switches_the_report_to_a_restore_view(qtbot, tmp_path,
                                                        monkeypatch):
    """回滚成功后报告页必须改成「恢复成功」。

    继续顶着「迁移成功」会让人以为迁移还生效着——而它已经被撤销了：
    源路径回来了、目标端那份也清了。这一页要回答的问题整个变了。
    """
    window, _ = _finished_window(qtbot, tmp_path, monkeypatch,
                                 answer=QMessageBox.StandardButton.Yes)
    assert window.report_page.title_text() == "迁移成功"

    window.rollback()

    page = window.report_page
    assert page.title_text() == "恢复成功"
    assert page.is_showing_restore() is True
    assert page.stats_visible() is False, "复制了多少在恢复之后没有意义"
    assert page.table_visible() is False
    assert "已恢复" in page.restore_text("source")
    assert "保留未清" in page.restore_text("target")


def test_failed_rollback_keeps_the_migration_report(qtbot, tmp_path, monkeypatch):
    """回滚被拒时迁移结果仍然成立，那一页不该被换成恢复态。"""
    window, _ = _finished_window(qtbot, tmp_path, monkeypatch,
                                 answer=QMessageBox.StandardButton.Yes)
    window._migrator.rollback_ok = False
    window._migrator.cleanup_error = "没有可还原的备份"

    window.rollback()

    assert window.report_page.title_text() == "迁移成功"
    assert window.report_page.is_showing_restore() is False


def test_rollback_refreshes_the_backup_warning_and_buttons(qtbot, tmp_path,
                                                           monkeypatch):
    """回滚成功后备份就没了（它被改回原名），界面必须跟上。

    否则报告页会继续喊「请勿删除备份」，还摆着一个能点的「一键回滚」和
    「删除备份」——这三样对应的备份都已经不存在了。此时目标端成了孤儿，
    反倒需要把「删除目标」放开。
    """
    window, _ = _finished_window(qtbot, tmp_path, monkeypatch,
                                 answer=QMessageBox.StandardButton.Yes)
    assert window.report_page.backup_warning_text() != ""

    window.rollback()

    assert window.report_page.backup_warning_text() == "", "备份没了，警告要撤"
    assert window.report_page.rollback_button.isEnabled() is False
    assert window.report_page.cleanup_button.isEnabled() is False
    assert window.report_page.discard_button.isEnabled() is True, \
        "目标端成了孤儿，该能清掉"


def test_rollback_offers_a_default_off_delete_target_option(qtbot, tmp_path,
                                                            monkeypatch):
    """「同时删除目标端内容」要显式勾选，且默认不勾。

    它会销毁迁移之后写进目标端的数据，默认勾上等于替用户做了决定。
    """
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    window.rollback()

    assert "删除目标端" in dialog.option_texts[-1]
    assert dialog.option_defaults[-1] is False, "默认必须不勾"
    assert window._migrator.cleared_target is False, "没勾就不该动目标端"


def test_rollback_clears_the_target_when_the_option_is_checked(qtbot, tmp_path,
                                                               monkeypatch):
    """勾选后清掉目标端里同步过去的内容，但**保留目录本身**。

    勾选项写的就是「删除目标端里同步过去的内容」——是内容，不是目录。
    而目标目录很可能是用户自己先建好的空目录（合入已有目录是被支持的场景），
    把它一起删掉是越权。
    """
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    dialog.option_checked = True
    window.rollback()

    assert window._migrator.cleared_target is True
    assert window._migrator.discarded is False, "不该把整个目录删掉"
    assert "已清空" in dialog.texts[-1]
    assert "目录本身保留" in dialog.texts[-1]


def test_rollback_skips_target_deletion_when_it_has_extras(qtbot, tmp_path,
                                                            monkeypatch):
    """目标端有迁移之外的内容时，只跳过删除这一步，回滚照做。

    用户要的是回滚，不该被目标端的情况挡回去；但也不能顺手销毁他后写入的数据。
    """
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    dialog.option_checked = True
    window._migrator.clear_ok = False
    window._migrator.clear_reason = "目标端有 2 项不属于这次迁移的内容（a.txt 等）"

    window.rollback()

    assert window._migrator.rolled_back is True, "回滚必须照做"
    assert window._migrator.cleared_target is False, "有额外内容时不碰目标端"
    assert "不属于这次迁移" in dialog.texts[-1]
    assert "删除目标" in dialog.texts[-1], "要告诉用户怎么手动清"


def test_cancelled_rollback_never_touches_the_target(qtbot, tmp_path, monkeypatch):
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.No)
    dialog.option_checked = True
    window.rollback()

    assert window._migrator.rolled_back is False
    assert window._migrator.discarded is False


def test_rollback_reports_a_missing_target_as_nothing_to_do(qtbot, tmp_path,
                                                            monkeypatch):
    """目标端本来就不存在时，别说成「未能删除」。

    那句话自相矛盾：既说没删掉、又说无需清理，用户不知道到底出了什么事。
    """
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    dialog.option_checked = True
    window._migrator.target_exists = False

    window.rollback()

    text = dialog.texts[-1]
    assert "本就不存在" in text
    assert "未能删除" not in text
    assert "没有需要清理的内容" in text


def test_refused_rollback_explains_why(qtbot, tmp_path, monkeypatch):
    """回滚被拒时必须说明原因，不能只留「回滚失败」四个字。

    真实反馈：用户手动删掉备份后点「一键回滚」，只看到状态栏一句「回滚失败」，
    详细原因（没有可还原的备份、摘掉链接会让源路径彻底消失）被丢掉了——
    用户以为工具坏了。隔壁的「删除备份」早就用弹窗告知原因，回滚这条漏了。
    """
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    window._migrator.rollback_ok = False
    window._migrator.cleanup_error = "没有可还原的备份（备份已被删除）"

    window.rollback()

    assert "没有可还原的备份" in window.status_line.text()
    assert "没有可还原的备份" in dialog.texts[-1], "要用弹窗说清，不能只写状态栏"
    assert "源目录未做任何改动" in dialog.texts[-1], "要说明没有副作用"


def test_refused_rollback_without_a_reason_still_says_something(qtbot, tmp_path,
                                                                monkeypatch):
    """cleanup_error 为空时也要给出可读的一句，不能显示空白。"""
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    window._migrator.rollback_ok = False
    window._migrator.cleanup_error = ""

    window.rollback()

    assert "回滚未执行" in dialog.texts[-1]


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


def test_cleanup_offers_a_forced_delete_when_only_the_backup_has_content(
        qtbot, tmp_path, monkeypatch):
    """备份里有「只存在于备份里」的内容时，核心会拒绝删除——界面必须给强制出口。

    否则用户被堵死在这里：备份删不掉、又没有别的办法，只能自己去资源管理器手动
    处理；而界面上明明摆着一个「删除备份」按钮。
    """
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    window._migrator.cleanup_ok = False
    window._migrator.only_in_backup = 3      # 跳过的重解析点 / 被排除的目录

    window.cleanup_backup()

    assert window._migrator.cleanup_calls == [False, True], \
        "应当先试普通删除、被拒后再强制"
    assert window._migrator.cleaned is True
    assert "仍要强制删除吗" in dialog.texts[-1]
    assert "备份已删除" in window.status_line.text()


def test_forced_cleanup_can_be_declined(qtbot, tmp_path, monkeypatch):
    """强制删除删的是最后一份副本，用户点「否」就必须就此打住。"""
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    # 第一个确认框（确认删除吗）答应，第二个（仍要强制删除吗）拒绝
    dialog.answers = [QMessageBox.StandardButton.Yes,
                      QMessageBox.StandardButton.No]
    window._migrator.cleanup_ok = False
    window._migrator.only_in_backup = 1

    window.cleanup_backup()

    assert window._migrator.cleanup_calls == [False], "只许试一次普通删除"
    assert window._migrator.cleaned is False, "用户说了不删"
    assert "已保留备份" in window.status_line.text()


def test_cleanup_failure_without_only_in_backup_content_says_why(
        qtbot, tmp_path, monkeypatch):
    """不是「只存在于备份里」的失败（比如文件被占用）不该给强制按钮——
    强制删同样删不掉，那会是个按了没反应的死按钮。"""
    window, dialog = _finished_window(qtbot, tmp_path, monkeypatch,
                                      answer=QMessageBox.StandardButton.Yes)
    window._migrator.cleanup_ok = False
    window._migrator.only_in_backup = 0

    window.cleanup_backup()

    assert window._migrator.cleanup_calls == [False]
    assert window._migrator.cleaned is False
    assert "仍要强制删除吗" not in dialog.texts[-1]
    assert "手动删除" in dialog.texts[-1]


def test_scan_probes_only_subdirectories_not_plain_files(qtbot, tmp_path,
                                                         monkeypatch):
    """只试探子目录，平铺的普通文件直接交给 RM。

    试探一个条目 0.67 毫秒，RM 扫一个文件只要 0.13 毫秒——**试文件永远净亏**：
    没被占用时省下的还不到成本的三分之一，被占用时照样得交给 RM。只有目录可能
    赚回来，它的子树里可能有成百上千个文件。
    """
    from dmig.ui import workers
    from dmig.ui.workers import HolderScanWorker

    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.dat").write_bytes(b"x")
    for i in range(20):
        (src / f"loose{i:02d}.dat").write_bytes(b"x")     # 平铺文件，不该被试探

    seen = {}

    def fake_probe(source, names=None, **kwargs):
        seen["names"] = list(names) if names is not None else None
        return []

    monkeypatch.setattr(workers.guard, "probe_entries", fake_probe)
    monkeypatch.setattr(workers.locks, "find_holders", lambda *a, **k: [])

    HolderScanWorker(str(src)).run()

    assert seen["names"] == ["sub"], seen["names"]
    assert "loose00.dat" not in (seen["names"] or [])


def test_successful_recheck_does_not_walk_every_entry(qtbot, tmp_path,
                                                      monkeypatch):
    """复验成功时不该去逐个试探顶层条目。

    判定用整棵树改一次名就够（O(1)，实测 25 毫秒，与文件数无关）；逐个试探是
    0.67 毫秒/条目，而且这一段跑在**主线程**上——平铺目录上是几秒的界面冻结。
    只有真的没解决，才值得去点名是哪些条目还挡着。
    """
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    walked = []
    monkeypatch.setattr(guard, "probe_rename", lambda source: [])
    monkeypatch.setattr(guard, "probe_entries",
                        lambda *a, **k: walked.append(1) or [])
    monkeypatch.setattr(
        locks, "shutdown_holders",
        lambda paths, force=False: _shutdown(True, True, "已请求占用程序关闭"))

    window._holder_paths = ["x"]
    window.shutdown_holders(force=False)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    assert "已解除" in window.holders_page.verdict_text()
    assert walked == [], "复验成功还去逐个试探顶层条目了"


def test_shutdown_survives_a_probe_failure(qtbot, tmp_path, monkeypatch):
    """复验本身出问题时，页面也**必须**从「正在关闭」里放出来。

    卡在那一页、重新扫描还灰着，用户就彻底动不了了——那正是上一次的教训
    （结论出来了界面却卡住）。槽函数里绝不能让异常逃出去。
    """
    from dmig.core import guard, locks
    (tmp_path / "src").mkdir()
    window = MainWindow(migrator_factory=_factory(
        _plan(), MigrateOutcome(True, "done"), []))
    qtbot.addWidget(window)
    _select(window, tmp_path)
    monkeypatch.setattr(
        locks, "shutdown_holders",
        lambda paths, force=False: _shutdown(True, True, "已请求占用程序关闭"))

    def boom(*_a, **_k):
        raise OSError("试探改名时炸了")

    monkeypatch.setattr(guard, "probe_rename", boom)

    window._holder_paths = ["x"]
    window.shutdown_holders(force=False)
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=5000)

    page = window.holders_page
    assert page.state() == "done", "还卡在关闭中"
    assert page.rescan_button.isEnabled() is True, "用户动不了"
    assert page.bar.isVisibleTo(page) is False
    assert "没能确认" in page.verdict_text()


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








