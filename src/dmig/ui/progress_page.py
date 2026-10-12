"""向导第三步：执行进度、可滚动日志与失败清单。

进度条用 Fluent 的 `qf.ProgressBar`，但**必须显式关掉数值动画**
（`useAni=False`）：默认开着动画时 `setValue()` 是异步过渡的，
`percent()`、`reset()` 这类同步读取会拿到过渡途中的中间值。

颜色一律不在这里写死——统计文字与标题用库控件的语义色，
深浅两套主题由 `theme` 统一负责。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import qfluentwidgets as qf

from ..core.models import CopyFailure, Progress
from .chrome import (FAIL_ICON, Card, Rule, StatusBadge, fluent_icon,
                     make_section_header)
from .format import human_bytes, human_duration, human_speed
from .report import backup_warning_text, phase_text
from .theme import ACCENT_PAIR, ERROR, MUTED, SUCCESS

FAILURE_HEADERS = ["相对路径", "原因", "占用进程"]

# 双击哪一列才触发「定位占用进程」。测试断言的是 2，改这里等于改行为。
PROCESS_COLUMN = 2

REASON_TEXT = {
    "locked": "被占用",
    "io_error": "读写错误",
    "disk_full": "目标卷空间不足",
    "symlink_privilege": "缺符号链接特权",
    "link_target_excluded": "链接目标被排除",
}

PHASE_TEXT = {
    "copying": "复制中 ",
    "verifying": "校验中 ",
}

BAR_STEPS = 1000


def make_section_subtext(text: str) -> qf.CaptionLabel:
    """分区标题下的灰色小字说明，把括号里的操作提示从标题里解放出来。"""
    label = qf.CaptionLabel(text)
    label.setTextColor(*MUTED)
    return label


class ProgressPage(QWidget):
    """③ 执行页。"""

    pause_toggled = Signal(bool)
    cancel_requested = Signal()
    locate_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paused = False
        self._phase = ""

        # 构造签名是 (parent, useAni)，第二个位置参数**不是** parent；
        # 这里用命名参数，避免把 useAni 当成 parent 传进去。
        self.bar = qf.ProgressBar(None, useAni=False)
        self.bar.setRange(0, BAR_STEPS)
        self.bar.setValue(0)
        self.bar.setFixedHeight(10)

        self.count_label = qf.BodyLabel("0 / 0")
        # 百分比放在计数行右侧，用强调色大字给进度一个视觉锚点
        self.percent_label = qf.SubtitleLabel("0%")
        self.percent_label.setTextColor(*ACCENT_PAIR)
        self.current_label = qf.BodyLabel("-")
        self.speed_label = qf.BodyLabel("-")
        self.eta_label = qf.BodyLabel("-")

        # 统计三组：标签在上、值在下，组间竖发丝线——比「标签: 值」的
        # 横排读起来更像仪表盘，当前文件路径长，占更宽的一格
        stats = QHBoxLayout()
        stats.setSpacing(12)
        stats.setContentsMargins(0, 2, 0, 0)
        stat_specs = (("当前文件", self.current_label, 3),
                      ("速度", self.speed_label, 1),
                      ("剩余", self.eta_label, 1))
        for i, (title, widget, stretch) in enumerate(stat_specs):
            box = QVBoxLayout()
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(2)
            cap = qf.CaptionLabel(title)
            cap.setTextColor(*MUTED)
            box.addWidget(cap)
            box.addWidget(widget)
            stats.addLayout(box, stretch)
            if i < len(stat_specs) - 1:
                stats.addWidget(Rule(Qt.Orientation.Vertical))

        # 三格实时指标要能整组隐藏（结束后这些值已经没有意义），
        # 所以套一层容器——布局对象本身没有 setVisible。
        self.stats_widget = QWidget()
        self.stats_widget.setLayout(stats)

        self.log = qf.PlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText("迁移日志")
        # 等宽字体：路径、PID 对齐，滚动日志扫读不串行
        log_font = self.log.font()
        log_font.setPointSize(9)
        log_font.setFamily("Consolas")
        self.log.setFont(log_font)

        self.failure_table = qf.TableWidget()
        # qf.TableWidget() 不接受行列数，必须自己 setColumnCount / setRowCount
        self.failure_table.setColumnCount(len(FAILURE_HEADERS))
        self.failure_table.setRowCount(0)
        self.failure_table.setHorizontalHeaderLabels(FAILURE_HEADERS)
        self.failure_table.verticalHeader().setDefaultSectionSize(36)
        # 只读表。qf.TableWidget 默认带 AnyKeyPressed：敲任意键就进编辑态，
        # 格子变成带清空按钮的输入框——迁移记录不该能改。
        self.failure_table.setEditTriggers(
            qf.TableWidget.EditTrigger.NoEditTriggers)
        # 列宽显式分配：默认 100px 会把「相对路径」截成 "data\..."，
        # 用户看不出到底是哪个文件失败；路径列与进程列都需要更多空间。
        header = self.failure_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PROCESS_COLUMN, QHeaderView.ResizeMode.Stretch)

        # 计数行：左侧阶段+计数，右侧百分比大字
        count_row = QHBoxLayout()
        count_row.setContentsMargins(0, 0, 0, 0)
        count_row.setSpacing(12)
        count_row.addWidget(self.count_label, 1)
        count_row.addWidget(self.percent_label, 0,
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # ---- 进行中形态：进度条 + 计数 + 三格实时指标 ----
        self.running_widget = QWidget()
        running_layout = QVBoxLayout(self.running_widget)
        running_layout.setContentsMargins(0, 0, 0, 0)
        running_layout.setSpacing(10)
        running_layout.addWidget(self.bar)
        running_layout.addLayout(count_row)
        running_layout.addWidget(self.stats_widget)

        # ---- 已结束形态：状态徽章 + 一句结论 ----
        # 迁移结束后不能再摆着「当前文件 -」「剩余 -」和一串陈旧的速度值：
        # 那会让「已经跑完」看起来像「卡住了」，这正是改造前最刺眼的毛病。
        self.finish_badge = StatusBadge(44)
        self.finish_title = qf.TitleLabel("")
        self.finish_detail = qf.BodyLabel("")
        self.finish_detail.setWordWrap(True)
        self.finish_detail.setTextColor(*MUTED)
        finish_text = QVBoxLayout()
        finish_text.setContentsMargins(0, 0, 0, 0)
        finish_text.setSpacing(2)
        finish_text.addWidget(self.finish_title)
        finish_text.addWidget(self.finish_detail)
        finish_row = QHBoxLayout()
        finish_row.setContentsMargins(0, 2, 0, 2)
        finish_row.setSpacing(14)
        finish_row.addWidget(self.finish_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        finish_row.addLayout(finish_text, 1)
        # 界面面板为防刷屏只列前若干条失败，完整日志（含参数、逐条失败清单）
        # 还得能打开。默认藏起来，只有真的写出了日志文件才露出来。
        self.open_log_button = qf.PushButton("打开日志文件")
        self.open_log_button.setMinimumHeight(32)
        self.open_log_button.clicked.connect(self._on_open_log_clicked)
        self.open_log_button.hide()
        finish_row.addWidget(self.open_log_button, 0,
                             Qt.AlignmentFlag.AlignVCenter)
        self.finish_widget = QWidget()
        self.finish_widget.setLayout(finish_row)
        self.finish_widget.hide()

        progress_card = Card()
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(20, 14, 20, 16)
        progress_layout.setSpacing(10)
        progress_layout.addWidget(make_section_header("迁移进度", fluent_icon("SYNC")))
        progress_layout.addWidget(self.running_widget)
        progress_layout.addWidget(self.finish_widget)
        # 迁移刚成功就把「别手动删备份」说在前面：用户往往正是这会儿去资源管理器
        # 把备份删掉（"数据都迁好了，这个没用了吧"）。等他在报告页点「一键回滚」
        # 被拒才第一次知道后果，已经晚了。文案与报告页共用一份，免得两处漂移。
        self.backup_warning = qf.BodyLabel("")
        self.backup_warning.setWordWrap(True)
        self.backup_warning.setTextColor(*ERROR)
        self.backup_warning.hide()
        progress_layout.addWidget(self.backup_warning)

        self.failure_card = Card()
        failure_layout = QVBoxLayout(self.failure_card)
        failure_layout.setContentsMargins(20, 14, 20, 16)
        failure_layout.setSpacing(10)
        failure_layout.addWidget(make_section_header(
            "失败清单", fluent_icon(FAIL_ICON)))
        failure_layout.addWidget(make_section_subtext(
            "双击「占用进程」列可定位进程"))
        failure_layout.addWidget(self.failure_table)
        # 没有失败项时整卡隐藏：否则白占一大片、把日志区挤成细条。
        # 有失败时按内容增高，但给上限，多出来的行走表格自己的滚动条。
        self.failure_card.setMaximumHeight(280)
        self.failure_card.hide()

        log_card = Card()
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(20, 14, 20, 16)
        log_layout.setSpacing(10)
        log_layout.addWidget(make_section_header("迁移日志", fluent_icon("DOCUMENT")))
        log_layout.addWidget(self.log, 1)

        self.pause_button = qf.PushButton("暂停")
        self.cancel_button = qf.PushButton("取消")
        for btn in (self.pause_button, self.cancel_button):
            btn.setMinimumWidth(96)
            btn.setMinimumHeight(34)
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(10)
        controls.addStretch(1)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.cancel_button)
        # 暂停/取消只在进行中有意义，结束后整行收起
        self.controls_widget = QWidget()
        self.controls_widget.setLayout(controls)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(14)
        layout.addWidget(progress_card)
        # 只有日志吃剩余空间。原来日志与失败清单各拿 stretch 1，
        # 两块都大半空着，整页看起来像没加载出来。
        layout.addWidget(log_card, 1)
        layout.addWidget(self.failure_card)
        layout.addWidget(self.controls_widget)

        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.failure_table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.set_running(False)

    # --- 对外接口 ---------------------------------------------------
    def update_progress(self, progress: Progress) -> None:
        # 重新跑一趟时可能会先来进度、后调 reset：这里顺手切回进行中形态，
        # 避免上一趟的「迁移完成」横幅还挂在新一趟的进度上面。
        self._show_running_form()
        phase = getattr(progress, "phase", "") or ""
        if phase != self._phase:
            self._phase = phase
            if phase == "verifying":
                self.append_log("[校验] 开始比对源与目标…")
        total = max(int(progress.total_bytes or 0), 0)
        done = max(int(progress.done_bytes or 0), 0)
        if total <= 0:
            self.bar.setValue(BAR_STEPS)
        else:
            self.bar.setValue(min(BAR_STEPS, int(done * BAR_STEPS / total)))
        self.current_label.setText(progress.current or "-")
        self.speed_label.setText(human_speed(progress.speed_bps))
        self.eta_label.setText(human_duration(progress.eta_seconds)
                               if progress.eta_seconds else "-")
        self.count_label.setText(
            f"{PHASE_TEXT.get(phase, '')}"
            f"{progress.done_files:,} / {progress.total_files:,} 个文件，"
            f"{human_bytes(done)} / {human_bytes(total)}")
        self.percent_label.setText(f"{self.percent()}%")

    def percent(self) -> int:
        return int(self.bar.value() * 100 / BAR_STEPS)

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(text)
        # 明确滚到底：日志的价值在最新几行，靠 appendPlainText 的隐式行为
        # 在光标被挪动过之后就不可靠了。
        scroll = self.log.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    def backup_warning_text(self) -> str:
        """「别手动删备份」的警告正文；没有备份时为空串。"""
        return "" if self.backup_warning.isHidden() else self.backup_warning.text()

    def _on_open_log_clicked(self) -> None:
        """在资源管理器里定位日志文件。

        日志可能已经被清理掉（会话目录是用户数据，随时可能被删），
        这时要说清楚，而不是点了没反应。
        """
        path = getattr(self, "_log_path", "")
        if not path or not Path(path).exists():
            self.append_log(f"[日志] 文件不存在，可能已被清理：{path or '（未记录）'}")
            return
        try:
            # /select 必须和路径拼成同一个参数：拆成两个的话，含空格的路径
            # 会被 explorer 当成另一个开关丢掉，结果只打开「文档」。
            subprocess.Popen(["explorer", f"/select,{path}"])
        except OSError as exc:
            self.append_log(f"[日志] 打开失败：{exc}")

    def add_failure(self, failure: CopyFailure) -> None:
        row = self.failure_table.rowCount()
        self.failure_table.insertRow(row)
        self.failure_table.setItem(row, 0, QTableWidgetItem(failure.rel))
        self.failure_table.setItem(row, 1, QTableWidgetItem(
            REASON_TEXT.get(failure.reason, failure.reason)))
        self.failure_table.setItem(row, PROCESS_COLUMN,
                                   QTableWidgetItem(failure.process))
        self.failure_card.setVisible(True)
        suffix = f"（{failure.process}）" if failure.process else ""
        self.append_log(f"[失败] {failure.rel}："
                        f"{REASON_TEXT.get(failure.reason, failure.reason)}{suffix}")

    def set_failures(self, failures) -> None:
        self.failure_table.setRowCount(0)
        for failure in failures:
            self.add_failure(failure)
        # 空清单整卡收起。数据照旧留在表里（failure_rows() 读得到），
        # 只是不占版面——没有失败项时那一大片空白比没有信息更糟。
        self.failure_card.setVisible(self.failure_table.rowCount() > 0)

    def failure_rows(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for row in range(self.failure_table.rowCount()):
            rows.append(tuple(self.failure_table.item(row, col).text()
                              for col in range(len(FAILURE_HEADERS))))
        return rows

    def finish(self, outcome, elapsed_seconds: float | None = None) -> None:
        """迁移结束：把「进行中」形态收成一句结论。

        这一步以前是缺失的——`set_running(False)` 只禁用按钮，页面于是永远
        停在最后那一帧（「校验中 100%」「当前文件 -」「剩余 -」，速度还是
        结束前的陈旧采样），看起来像卡住而不是跑完。

        `outcome` 沿用 `MigrateOutcome`，`elapsed_seconds` 由调用方计时传入
        （core 里没有耗时字段，也不该为一个显示值去改 core）。
        """
        ok = bool(getattr(outcome, "ok", False))
        self._show_finished_form(ok)

        title = "迁移完成" if ok else "迁移未完成"
        if not ok:
            # 大标题只说「卡在哪一步」。把 outcome.message 塞进来的话，那是一整段
            # 带 \\?\ 长路径的异常原文，会把标题撑成一坨；退而取 phase 又是
            # rename_failed 这种内部键名。完整原因走下面的说明行。
            title = f"未完成：{phase_text(getattr(outcome, 'phase', ''))}"
        self.finish_title.setText(title)
        self.finish_title.setTextColor(*(SUCCESS if ok else ERROR))
        self.finish_detail.setText(self._finish_summary(outcome, elapsed_seconds))
        warning = backup_warning_text(getattr(outcome, "backup_path", None))
        self.backup_warning.setText(warning)
        self.backup_warning.setVisible(bool(warning))
        # 有日志文件才露出按钮：没有会话（比如只跑扫描）时这个入口毫无意义
        self._log_path = str(getattr(outcome, "log_path", "") or "")
        self.open_log_button.setVisible(bool(self._log_path))
        # 结束后按钮没有意义；同时把进度条钉在 100%，
        # 未完成的场合保留它当时的位置更有信息量，所以只在成功时钉满。
        if ok:
            self.bar.setValue(BAR_STEPS)
        self.set_running(False)

    def _finish_summary(self, outcome, elapsed_seconds: float | None) -> str:
        """结论行：只报用户关心的量，不堆内部字段。

        失败原因排在最前：大标题现在只说「卡在哪一步」，具体原因
        （谁占着文件、缺什么权限）必须在说明行里找得到，否则两头都丢。
        """
        parts: list[str] = []
        message = (getattr(outcome, "message", "") or "").strip()
        if message:
            parts.append(message)
        copy = getattr(outcome, "copy", None)
        if copy is not None:
            parts.append(f"已复制 {copy.copied_files:,} 个文件"
                         f"（{human_bytes(copy.copied_bytes)}）")
            if copy.skipped:
                parts.append(f"跳过 {copy.skipped:,} 项")
            if copy.failures:
                parts.append(f"{len(copy.failures):,} 项失败")
        verify = getattr(outcome, "verify", None)
        if verify is not None and not verify.ok:
            missing = len(getattr(verify, "missing", []) or [])
            if missing:
                parts.append(f"缺失 {missing:,} 个文件")
        if elapsed_seconds:
            parts.append(f"耗时 {human_duration(elapsed_seconds)}")
        return " · ".join(parts) if parts else "没有需要报告的内容"

    def _show_running_form(self) -> None:
        self.finish_widget.hide()
        # 上一趟的「请勿删除备份」不能挂在新一趟的进度上：用户会以为那才是
        # 这次要留意的备份。警告的正文在 finish() 里才重新填。
        self.backup_warning.hide()
        self.running_widget.show()
        self.controls_widget.show()

    def _show_finished_form(self, ok: bool) -> None:
        self.finish_badge.set_state(ok)
        self.running_widget.hide()
        self.finish_widget.show()
        # 暂停/取消在结束后整行收起，而不是留两个点不动的灰按钮
        self.controls_widget.hide()

    def reset(self) -> None:
        self.bar.setValue(0)
        self.current_label.setText("-")
        self.speed_label.setText("-")
        self.eta_label.setText("-")
        self.count_label.setText("0 / 0")
        self.percent_label.setText("0%")
        self.log.clear()
        self.failure_table.setRowCount(0)
        self.failure_card.hide()
        self._paused = False
        self._phase = ""
        self.cancel_button.setText("取消")
        self._show_running_form()
        self.pause_button.setText("暂停")

    def set_running(self, running: bool) -> None:
        # 未运行时暂停/取消都不可点：Qt 里 disabled 按钮的 click() 不触发信号，
        # 这正是「迁移结束后按钮失效」的既定语义，不要绕过。
        self.pause_button.setEnabled(running)
        self.cancel_button.setEnabled(running)
        if running:
            self.cancel_button.setText("取消")
        if not running:
            self._paused = False
            self.pause_button.setText("暂停")

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_button.setText("继续" if self._paused else "暂停")
        self.pause_toggled.emit(self._paused)

    def _on_cancel_clicked(self) -> None:
        """取消的即时反馈。

        原来直接 `cancel_requested.emit`：点了之后按钮文字与可用状态都不变，
        要等拷贝循环走到下一个检查点才停，用户以为没点上、会反复点。
        """
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("正在停止…")
        self.pause_button.setEnabled(False)
        self.append_log("[取消] 正在停止…已复制的内容会保留")
        self.cancel_requested.emit()

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        if column != PROCESS_COLUMN:
            return
        item = self.failure_table.item(row, PROCESS_COLUMN)
        if item is not None and item.text().strip():
            self.locate_requested.emit(item.text())
