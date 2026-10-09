"""主窗口：四步向导的装配与线程调度。

窗口基类换成 `FluentWindow`，外壳（品牌栏 / 步骤条 / 内容区 / 操作栏）交给
`chrome`。三条实测结论直接决定了这里的写法：

1. `theme.apply_theme()` **必须在任何页面/控件构造之前**调用。该库的主题是
   逐个控件注册样式的（`common/style_sheet.py` 遍历 `styleSheetManager` 重刷），
   `app.styleSheet()` 始终为空；顺序错了就会出现「控件文字变白、背景还是白的」。
2. 必须 `setMicaEffectEnabled(False)`：Mica 开着时窗口背景跟随**操作系统**主题
   而不是库的主题，强制主题与系统不符时就是白底白字，浅色机器上也没法验证深色。
3. 背景用 `setCustomBackgroundColor` 显式指定库自带的浅/深背景色
   （`theme.WINDOW_BACKGROUND`），不自造配色。
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import (QApplication, QHBoxLayout, QMessageBox,
                               QStackedWidget, QVBoxLayout, QWidget)

import qfluentwidgets as qf

from ..core.migrator import Migrator
from ..core.session import Session, default_root
from . import chrome, theme
from .plan_page import PlanPage
from .progress_page import ProgressPage
from .report_page import ReportPage
from .select_page import SelectPage
from .shell import parse_process_label, process_image_path, relaunch_elevated
from .workers import MigrateWorker, PlanWorker, VerifyWorker

PAGE_SELECT = 0
PAGE_PLAN = 1
PAGE_PROGRESS = 2
PAGE_REPORT = 3


def default_migrator_factory(source, target, mappings, deep_verify, session_root):
    return Migrator(source, target, mappings=mappings, deep_verify=deep_verify,
                    session_root=session_root)


class StatusLine(QWidget):
    """底部状态行，替代 `QStatusBar`。

    `QStatusBar` 是原生 Qt 控件，不参与 Fluent 的逐控件主题注册，深色模式下
    它仍是默认的黑色文字，落在窗口的深色背景上就是一行看不见的字。这里换成
    Fluent 的 `CaptionLabel`，颜色由库的主题负责。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 0, 24, 10)
        row.setSpacing(0)
        self._label = qf.CaptionLabel("")
        row.addWidget(self._label)
        row.addStretch(1)

    def showMessage(self, message: str) -> None:      # noqa: N802 - 对齐 QStatusBar
        self._label.setText(message)

    def text(self) -> str:
        return self._label.text()


class MainWindow(qf.FluentWindow):
    """四步向导：选择 → 计划 → 执行 → 报告。"""

    def __init__(self, migrator_factory=None, session_id=None, parent=None):
        # 主题必须在任何控件之前落定：控件是在构造时注册样式的。
        theme.apply_theme("auto")
        super().__init__(parent)

        # 关掉 Mica，并显式指定背景色，让窗口背景跟随库的主题而非系统主题。
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(*theme.WINDOW_BACKGROUND)

        self.setWindowTitle("数据目录迁移工具")
        self.resize(1000, 740)

        # 本应用是线性向导，不需要左侧导航栏。
        self.navigationInterface.hide()

        self._factory = migrator_factory or default_migrator_factory
        self._migrator = None
        self._worker = None
        self._outcome = None
        self._elevate_session = None

        self.select_page = SelectPage()
        self.plan_page = PlanPage()
        self.progress_page = ProgressPage()
        self.report_page = ReportPage()

        self.stack = QStackedWidget()
        for page in (self.select_page, self.plan_page,
                     self.progress_page, self.report_page):
            self.stack.addWidget(page)

        self.brand_bar = chrome.BrandBar()
        self.step_bar = chrome.StepBar(["选择目录", "确认计划", "执行迁移", "完成"])

        self.back_button = qf.PushButton("上一步")
        self.next_button = qf.PrimaryPushButton("下一步")
        self.action_bar = chrome.ActionBar()
        self.action_bar.add_left(self.back_button)
        self.action_bar.add_right(self.next_button)
        self.status_line = StatusLine()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.brand_bar)
        layout.addWidget(self.step_bar)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.action_bar)
        layout.addWidget(self.status_line)

        # 库自带的 stackedWidget 是 QFrame 而不是 QStackedWidget，用不上；
        # 隐藏后把自建容器放进同一个 widgetLayout（它已经预留了标题栏的 48px）。
        self.stackedWidget.hide()
        self.widgetLayout.addWidget(central, 1)

        self.next_button.clicked.connect(self.go_next)
        self.back_button.clicked.connect(self.go_back)
        self.select_page.changed.connect(self._update_buttons)
        self.plan_page.mappings_applied.connect(self.replan_with_mappings)
        self.plan_page.elevate_requested.connect(self.restart_elevated)
        self.progress_page.pause_toggled.connect(self._on_pause_toggled)
        self.progress_page.cancel_requested.connect(self.cancel_migration)
        self.progress_page.locate_requested.connect(self.locate_process)
        self.report_page.rollback_requested.connect(self.rollback)
        self.report_page.cleanup_requested.connect(self.cleanup_backup)
        self.report_page.reverify_requested.connect(self.reverify)

        if session_id:
            self.restore_session(session_id)
        self._update_buttons()

    def statusBar(self) -> StatusLine:                # noqa: N802 - 对齐 QMainWindow
        """保留原来的 `statusBar().showMessage(...)` 调用点。"""
        return self.status_line

    # --- 导航 -------------------------------------------------------
    def current_index(self) -> int:
        return self.stack.currentIndex()

    def is_busy(self) -> bool:
        return self._worker is not None

    def go_next(self) -> None:
        index = self.current_index()
        if index == PAGE_SELECT:
            self.start_plan()
        elif index == PAGE_PLAN:
            self.start_migration()
        elif index == PAGE_PROGRESS:
            self.stack.setCurrentIndex(PAGE_REPORT)
            self._update_buttons()
        self._sync_step()

    def go_back(self) -> None:
        if self.is_busy():
            return
        if self.current_index() == PAGE_PLAN:
            self.stack.setCurrentIndex(PAGE_SELECT)
            self._update_buttons()
        self._sync_step()

    def _sync_step(self) -> None:
        """步骤条与内容区始终同步（切换可能发生在异步回调里）。"""
        self.step_bar.set_current(self.stack.currentIndex())

    def _update_buttons(self) -> None:
        index = self.current_index()
        busy = self.is_busy()
        self._sync_step()
        self.back_button.setEnabled(not busy and index == PAGE_PLAN)
        if index == PAGE_SELECT:
            self.next_button.setText("开始扫描")
            ready = bool(self.select_page.source()) and bool(self.select_page.target()) \
                and not self.select_page.has_blocking_error()
            self.next_button.setEnabled(ready and not busy)
        elif index == PAGE_PLAN:
            self.next_button.setText("开始迁移")
            self.next_button.setEnabled(not busy)
        elif index == PAGE_PROGRESS:
            self.next_button.setText("查看报告")
            self.next_button.setEnabled(not busy)
        else:
            self.next_button.setText("完成")
            self.next_button.setEnabled(False)

    # --- 线程调度 ---------------------------------------------------
    def _begin_worker(self, worker) -> None:
        self._worker = worker
        worker.finished.connect(self._on_worker_finished)
        self._update_buttons()

    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.wait(5000)
        self._update_buttons()

    def _on_worker_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"出错：{message}")
        QMessageBox.warning(self, "操作失败", message)

    # --- ① → ② ------------------------------------------------------
    def start_plan(self) -> None:
        if self.is_busy():
            return
        self._migrator = self._factory(self.select_page.source(),
                                       self.select_page.target(),
                                       self.plan_page.mappings(),
                                       self.plan_page.deep_verify(), None)
        worker = PlanWorker(self._migrator)
        worker.planned.connect(self._on_planned)
        worker.progressed.connect(
            lambda rel: self.statusBar().showMessage(f"扫描：{rel}"))
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        self.statusBar().showMessage("正在扫描…")
        worker.start()

    def _on_planned(self, plan) -> None:
        from ..core import permission
        self.plan_page.load(plan, already_elevated=permission.is_elevated())
        self.stack.setCurrentIndex(PAGE_PLAN)
        self.statusBar().showMessage("计划已生成")
        self._update_buttons()

    def replan_with_mappings(self) -> None:
        """用户改了重写映射：用新映射重建 Migrator 并重新扫描。"""
        if self.is_busy():
            return
        self.start_plan()

    # --- ② → ③ ------------------------------------------------------
    def start_migration(self) -> None:
        if self.is_busy():
            return
        mappings = self.plan_page.mappings()
        deep_verify = self.plan_page.deep_verify()
        # 复用旧 Migrator 的前提是映射与深度校验开关都没变：
        # 两者都会改变 scanner/verifier 的行为，只比较映射会让新的开关被悄悄丢弃。
        if (self._migrator is None
                or mappings != getattr(self._migrator, "mappings", {})
                or deep_verify != getattr(self._migrator, "deep_verify", False)):
            self._migrator = self._factory(self.select_page.source(),
                                           self.select_page.target(),
                                           mappings,
                                           deep_verify, None)
        self.progress_page.reset()
        worker = MigrateWorker(self._migrator)
        worker.progressed.connect(self.progress_page.update_progress)
        worker.done.connect(self._on_migrated)
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        self.stack.setCurrentIndex(PAGE_PROGRESS)
        self.progress_page.set_running(True)
        self._update_buttons()
        self.statusBar().showMessage("正在迁移…")
        worker.start()

    def _on_migrated(self, outcome) -> None:
        self._outcome = outcome
        if getattr(outcome, "copy", None):
            self.progress_page.set_failures(outcome.copy.failures)
        self.report_page.load(outcome)
        self.progress_page.set_running(False)
        self.statusBar().showMessage("迁移完成" if outcome.ok
                                     else f"未完成：{outcome.phase}")

    def _on_pause_toggled(self, paused: bool) -> None:
        worker = self._worker
        if isinstance(worker, MigrateWorker):
            worker.pause() if paused else worker.resume()

    def cancel_migration(self) -> None:
        worker = self._worker
        if isinstance(worker, MigrateWorker):
            worker.cancel()
            self.statusBar().showMessage("正在取消…")

    # --- 提权 -------------------------------------------------------
    def _ensure_session_id(self) -> str:
        if self._migrator is not None and getattr(self._migrator, "session", None):
            return self._migrator.session.id
        if self._elevate_session is None:
            self._elevate_session = Session.create(self.select_page.source(),
                                                   self.select_page.target())
        return self._elevate_session.id

    def restart_elevated(self) -> None:
        session_id = self._ensure_session_id()
        if relaunch_elevated(session_id):
            self.statusBar().showMessage(
                f"已请求以管理员身份重启，会话 {session_id}")
        else:
            QMessageBox.warning(self, "提权失败",
                                "用户取消了 UAC 授权，或系统拒绝了提权请求。")

    def restore_session(self, session_id: str) -> None:
        session = Session.load(default_root() / f"{session_id}.json")
        if session is None:
            self.statusBar().showMessage(f"未找到会话 {session_id}，按新任务处理")
            return
        self.select_page.set_source(session.source)
        self.select_page.set_target(session.target)
        self.statusBar().showMessage(f"已以管理员身份恢复会话 {session.id}")

    # --- 失败清单与收尾 ---------------------------------------------
    def locate_process(self, label: str) -> None:
        name, pid = parse_process_label(label)
        if pid is None:
            QMessageBox.information(self, "占用进程",
                                    f"{label or '未知进程'}\n未能解析出 PID。")
            return
        path = process_image_path(pid)
        text = f"进程：{name}\nPID：{pid}"
        if path:
            text += f"\n路径：{path}"
        else:
            text += "\n（拿不到可执行文件路径，可能权限不足或进程已退出）"
        QMessageBox.information(self, "占用进程", text)

    def rollback(self) -> None:
        if self._migrator is None:
            return
        ok = self._migrator.rollback()
        self.statusBar().showMessage("已回滚到原目录" if ok else "回滚失败")

    def cleanup_backup(self) -> None:
        if self._migrator is None:
            return
        ok = self._migrator.cleanup_backup()
        self.statusBar().showMessage("备份已删除" if ok else "没有可删除的备份")

    def reverify(self) -> None:
        if self.is_busy():
            return
        worker = VerifyWorker(self.select_page.source(), self.select_page.target(),
                              deep=self.plan_page.deep_verify())
        worker.verified.connect(
            lambda result: self.statusBar().showMessage(
                "重新校验通过" if result.ok else "重新校验未通过"))
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        worker.start()

    # --- 退出 -------------------------------------------------------
    def closeEvent(self, event) -> None:      # noqa: N802 - Qt 命名
        worker = self._worker
        if worker is not None and worker.isRunning():
            if hasattr(worker, "cancel"):
                worker.cancel()
            worker.wait(5000)
        event.accept()


def run_gui(argv=None) -> int:
    """创建 QApplication 并显示主窗口；返回进程退出码。"""
    from ..entry import parse_launch_args

    argv = list(sys.argv if argv is None else argv)
    app = QApplication.instance() or QApplication(argv)
    args = parse_launch_args(argv)
    window = MainWindow(session_id=args["session"])
    window.show()
    return app.exec()
