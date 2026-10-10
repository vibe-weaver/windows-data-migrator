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

from ..core.migrator import Migrator, awaiting_source_link
from ..core.session import Session, default_root
from . import chrome, theme
from .plan_page import PlanPage
from .progress_page import ProgressPage
from .report_page import ReportPage
from .select_page import SelectPage
from .shell import parse_process_label, process_image_path, relaunch_elevated
from .workers import MigrateWorker, PlanWorker, TakeoverWorker, VerifyWorker

PAGE_SELECT = 0
PAGE_PLAN = 1
PAGE_PROGRESS = 2
PAGE_REPORT = 3

# 会话停在改名之后时，下一步做的事已经不是「扫描」而是补建源目录链接
LINK_ONLY_BUTTON = "补建源目录链接"


def default_migrator_factory(source, target, mappings, deep_verify, session_root,
                             skip_unknown_reparse=False, excludes=()):
    return Migrator(source, target, mappings=mappings, deep_verify=deep_verify,
                    session_root=session_root,
                    skip_unknown_reparse=skip_unknown_reparse,
                    excludes=excludes)


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
        # 重新校验的比对基准与结果；基准在迁移成功后是备份而不是源
        self._reverify_base = ""
        self._reverify_result = None
        self._elevate_session = None
        self._restored_session = None

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
        self.plan_page.replan_requested.connect(self.replan)
        self.plan_page.elevate_requested.connect(self.restart_elevated)
        self.plan_page.takeover_requested.connect(self.take_over_denied)
        self.progress_page.pause_toggled.connect(self._on_pause_toggled)
        self.progress_page.cancel_requested.connect(self.cancel_migration)
        self.progress_page.locate_requested.connect(self.locate_process)
        self.report_page.rollback_requested.connect(self.rollback)
        self.report_page.cleanup_requested.connect(self.cleanup_backup)
        self.report_page.discard_requested.connect(self.discard_target)
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
            if self._awaiting_link_only():
                # 源目录已经不在了，扫描只能得到一棵空树；跳过计划页，
                # 直接进执行页把最后一步（补建源目录链接）走完。
                self.start_migration()
            else:
                self.start_plan()
        elif index == PAGE_PLAN:
            self.start_migration()
        elif index == PAGE_PROGRESS:
            self.stack.setCurrentIndex(PAGE_REPORT)
            self._update_buttons()
        self._sync_step()

    def _awaiting_link_only(self) -> bool:
        """恢复的会话是否停在「数据已就位、只差源目录链接」。

        这种现场下前置校验必然报「源目录不存在」，照常走会让下一步永远灰着，
        用户明明只差最后一步，却只能去命令行或手工建链接。
        """
        return awaiting_source_link(self._restored_session, self.select_page.source())

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
            link_only = self._awaiting_link_only()
            self.next_button.setText(LINK_ONLY_BUTTON if link_only else "开始扫描")
            ready = bool(self.select_page.source()) and bool(self.select_page.target()) \
                and (link_only or not self.select_page.has_blocking_error())
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
    def _make_migrator(self, mappings, deep_verify, skip_reparse, excludes):
        migrator = self._factory(self.select_page.source(),
                                 self.select_page.target(),
                                 mappings, deep_verify, None, skip_reparse,
                                 excludes=excludes)
        if self._restored_session is None:
            return migrator
        if migrator.adopt_session(self._restored_session):
            return migrator
        # 用户改了源或目标：旧会话记的是**另一棵树**的相对路径，沿用它
        # 会把不相干的文件当成「已复制」而跳过，只能丢弃、按新任务处理
        self._restored_session = None
        return migrator

    def start_plan(self) -> None:
        if self.is_busy():
            return
        self._migrator = self._make_migrator(self.plan_page.mappings(),
                                             self.plan_page.deep_verify(),
                                             self.plan_page.skip_unknown_reparse(),
                                             self.plan_page.excludes())
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
        # 建链实测要创建临时目录，只在真有文件符号链接时才做；
        # 目录符号链接能降级成 junction，不需要这个特权。
        capable = (permission.can_create_symlink()
                   if getattr(plan, "file_symlinks", 0) else True)
        self.plan_page.load(plan, already_elevated=permission.is_elevated(),
                            symlink_capable=capable)
        self.stack.setCurrentIndex(PAGE_PLAN)
        self.statusBar().showMessage("计划已生成")
        self._update_buttons()

    def replan(self) -> None:
        """映射或重解析点策略变了：用新参数重建 Migrator 并重新扫描。"""
        if self.is_busy():
            return
        self.start_plan()

    # --- ② → ③ ------------------------------------------------------
    def start_migration(self) -> None:
        if self.is_busy():
            return
        mappings = self.plan_page.mappings()
        deep_verify = self.plan_page.deep_verify()
        skip_reparse = self.plan_page.skip_unknown_reparse()
        excludes = self.plan_page.excludes()
        # 复用旧 Migrator 的前提是四个开关都没变：映射、深度校验、重解析点策略、
        # 排除项都会改变 scanner/copier/verifier 的行为，漏比任何一个都会让新设置被悄悄丢弃。
        if (self._migrator is None
                or mappings != getattr(self._migrator, "mappings", {})
                or deep_verify != getattr(self._migrator, "deep_verify", False)
                or skip_reparse != getattr(self._migrator, "skip_unknown_reparse", False)
                or excludes != getattr(self._migrator, "excludes", [])):
            self._migrator = self._make_migrator(mappings, deep_verify, skip_reparse,
                                                excludes)
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
        self.statusBar().showMessage(
            "迁移完成" if outcome.ok
            else f"未完成：{getattr(outcome, 'message', '') or outcome.phase}")

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
        if self._restored_session is not None:
            return self._restored_session.id
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

    def take_over_denied(self) -> None:
        """接管无权限目录的属主：先存 ACL 再改属主，然后重扫。

        没有这一步，无权限目录只剩「排除掉」一条路，而排除意味着那些内容
        只留在备份里——想全量迁移的用户就没辙了。
        """
        if self.is_busy():
            return
        if self._migrator is None:
            # 还没有计划就没有「哪些目录读不到」这份清单，先扫一遍
            self.start_plan()
            return
        worker = TakeoverWorker(self._migrator)
        worker.done.connect(self._on_taken_over)
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        self.statusBar().showMessage("正在接管目录权限（takeown 可能要跑一会儿）…")
        worker.start()

    def _on_taken_over(self, taken: int, error: str) -> None:
        plan = getattr(self._migrator, "plan", None)
        if plan is not None:
            # 权限变了，界面要换成重扫后的那一份
            self._on_planned(plan)
        if error:
            self.statusBar().showMessage(error)
            QMessageBox.warning(self, "接管未完成", error)
            return
        # 排在 _on_planned 之后：它会把状态行改成「计划已生成」，
        # 顺序反了用户就看不到「接管了几个目录」这个结论。
        self.statusBar().showMessage(
            f"已接管 {taken} 个目录的属主；原始 ACL 已记入会话，回滚时会还原")

    def restore_session(self, session_id: str) -> None:
        session = Session.load(default_root() / f"{session_id}.json")
        if session is None:
            self.statusBar().showMessage(f"未找到会话 {session_id}，按新任务处理")
            return
        self.select_page.set_source(session.source)
        self.select_page.set_target(session.target)
        # 记住它：新建 Migrator 时要接管这个会话，才能沿用 .done 记账续传。
        # 只回填源/目标的话，execute() 会另起一个新会话，已复制的文件全部重复制一遍。
        self._restored_session = session
        # 链接重写映射也要一并回填，否则续传会用空映射重建 Migrator，
        # 目标端那几百个 junction 全部指回迁移前的旧位置（马上要变成断链）。
        self.plan_page.set_options(getattr(session, "options", None))
        if self._awaiting_link_only():
            self.statusBar().showMessage(
                f"已恢复会话 {session.id}：数据已复制到目标并通过校验，"
                f"源目录已改名为备份，只差源目录链接——点「{LINK_ONLY_BUTTON}」完成最后一步")
            return
        done = int(session.done.get("files", 0))
        detail = (f"，已记录 {done:,} 个文件，与源完全一致的将直接跳过" if done
                  else "，尚无可续传的复制记录")
        mapped = (session.options or {}).get("mappings") or {}
        if mapped:
            detail += f"，沿用 {len(mapped)} 条链接映射"
        self.statusBar().showMessage(f"已恢复会话 {session.id}{detail}")

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
        phase = getattr(self._outcome, "phase", "") if self._outcome else ""
        note = ""
        if phase == "done":
            note = ("\n\n迁移已经完成：回滚会摘掉源路径上那条链接、把备份改回原名。"
                    "迁移之后写进目标端的新数据（跑过的构建、新下载的文件）"
                    "会变成孤儿——它们还留在目标目录里，但从此没人指向它们。")
        answer = QMessageBox.warning(
            self, "回滚",
            f"将退回迁移前的状态：\n{self._migrator.source}{note}\n\n确认回滚吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer is not QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("已取消回滚")
            return
        ok = self._migrator.rollback()
        if ok:
            detail = getattr(self._migrator, "cleanup_error", "")
            self.statusBar().showMessage(
                f"已回滚到原目录；{detail}" if detail else "已回滚到原目录")
        else:
            self.statusBar().showMessage("回滚失败")

    def cleanup_backup(self) -> None:
        if self._migrator is None:
            return
        # 删备份是彻底不可逆的一步：删掉之后就没有任何退回迁移前状态的手段了
        answer = QMessageBox.warning(
            self, "删除备份",
            f"将删除源数据的最后一份副本：\n{self._migrator.backup_path}\n\n"
            "目标端已经通过校验，但删掉之后就无法再回滚了。\n\n确认删除吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer is not QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("已保留备份")
            return
        if self._migrator.cleanup_backup():
            self.statusBar().showMessage("备份已删除")
            return

        reason = getattr(self._migrator, "cleanup_error", "") or "备份未能删除"
        # 备份里有「只存在于备份里」的内容时（跳过的重解析点、被排除的目录），
        # 拒绝删除是正确的，但必须给出强制删除的出口，否则用户被堵死在这里。
        counters = [getattr(self._migrator, name, None) for name in
                    ("skipped_reparse_count", "excluded_count")]
        if not any(callable(counter) and counter() > 0 for counter in counters):
            self.statusBar().showMessage(reason)
            return

        answer = QMessageBox.warning(
            self, "备份未删除", f"{reason}\n\n仍要强制删除吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer is not QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("已保留备份")
            return
        if self._migrator.cleanup_backup(force=True):
            self.statusBar().showMessage("备份已删除")
        else:
            self.statusBar().showMessage(
                getattr(self._migrator, "cleanup_error", "") or "备份未能删除")

    def discard_target(self) -> None:
        """删掉目标端的半成品（源目录完好、迁移没走完时的清理出口）。"""
        if self._migrator is None:
            return
        phase = getattr(self._outcome, "phase", "") if self._outcome else ""
        note = ""
        if phase == "rename_failed":
            note = ("\n\n注意：目标端已经通过校验，改名失败通常只是某个程序占着源目录。"
                    "关掉那个程序后重试迁移更划算——删除等于丢掉这份已复制好的数据。")
        answer = QMessageBox.warning(
            self, "删除目标目录",
            f"将删除目标目录里已复制的内容：\n{self._migrator.target}\n"
            f"源目录不受影响。{note}\n\n确认删除吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer is not QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("已保留目标目录")
            return
        if self._migrator.discard_target():
            self.statusBar().showMessage("目标目录已删除")
        else:
            self.statusBar().showMessage(
                getattr(self._migrator, "cleanup_error", "") or "目标目录未能删除")

    def reverify(self) -> None:
        if self.is_busy() or self._migrator is None:
            return
        # 基准必须问 migrator：迁移成功后源路径已经变成指向目标的链接，
        # 拿它当原件就是拿目标和目标自己比，校验永远通过。
        baseline, error = self._migrator.reverify_baseline()
        if error:
            self.statusBar().showMessage(error)
            QMessageBox.information(self, "无法重新校验", error)
            return
        plan = getattr(self._migrator, "plan", None)
        worker = VerifyWorker(
            baseline, self._migrator.target,
            deep=self.plan_page.deep_verify(), totals=getattr(plan, "totals", None),
            skip_unknown_reparse=bool(getattr(plan, "skip_unknown_reparse", False)),
            excludes=getattr(plan, "excludes", ()) or ())
        self._reverify_base = baseline
        worker.progressed.connect(
            lambda p: self.statusBar().showMessage(
                f"正在校验：{p.done_files:,} / {p.total_files:,} 个文件"))
        worker.verified.connect(self._on_reverified)
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        worker.start()

    def _on_reverified(self, result) -> None:
        self._reverify_result = result
        if getattr(result, "cancelled", False):
            self.statusBar().showMessage("校验已取消")
            return
        if result.ok:
            base = getattr(self, "_reverify_base", "")
            suffix = "" if base == str(self._migrator.source) \
                else f"（比对基准：{base}）"
            self.statusBar().showMessage(f"重新校验通过{suffix}")
            return

        problems = list(result.missing) + list(result.mismatch)
        self.statusBar().showMessage(
            f"重新校验未通过：{len(result.missing)} 个缺失、"
            f"{len(result.mismatch)} 个不一致")
        preview = "\n".join(problems[:5])
        more = f"\n…共 {len(problems)} 项" if len(problems) > 5 else ""
        QMessageBox.warning(
            self, "重新校验未通过",
            f"目标端与原件不一致：{len(result.missing)} 个缺失、"
            f"{len(result.mismatch)} 个不一致。\n\n{preview}{more}")

    # --- 退出 -------------------------------------------------------
    def closeEvent(self, event) -> None:      # noqa: N802 - Qt 命名
        worker = self._worker
        if worker is not None and worker.isRunning():
            # 迁移中关窗 = 中途放弃。已经复制的内容不会退回、源目录也还没改名，
            # 所以必须问一句，并把「可以续传」这条退路讲清楚。
            answer = QMessageBox.warning(
                self, "迁移尚未完成",
                "迁移还在进行中，现在关闭会中断它：\n"
                "· 已经复制到目标端的内容会保留，不会被撤销\n"
                "· 源目录不受影响\n"
                "· 之后可以用会话文件续传，已复制的文件会直接跳过\n\n"
                "确认关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer is not QMessageBox.StandardButton.Yes:
                event.ignore()
                return
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
