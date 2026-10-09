"""向导第三步：执行进度、可滚动日志与失败清单。

进度条用 Fluent 的 `qf.ProgressBar`，但**必须显式关掉数值动画**
（`useAni=False`）：默认开着动画时 `setValue()` 是异步过渡的，
`percent()`、`reset()` 这类同步读取会拿到过渡途中的中间值。

颜色一律不在这里写死——统计文字与标题用库控件的语义色，
深浅两套主题由 `theme` 统一负责。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import qfluentwidgets as qf

from ..core.models import CopyFailure, Progress
from .chrome import Card
from .format import human_bytes, human_duration, human_speed

FAILURE_HEADERS = ["相对路径", "原因", "占用进程"]

# 双击哪一列才触发「定位占用进程」。测试断言的是 2，改这里等于改行为。
PROCESS_COLUMN = 2

REASON_TEXT = {
    "locked": "被占用",
    "io_error": "读写错误",
}

BAR_STEPS = 1000


class ProgressPage(QWidget):
    """③ 执行页。"""

    pause_toggled = Signal(bool)
    cancel_requested = Signal()
    locate_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paused = False

        # 构造签名是 (parent, useAni)，第二个位置参数**不是** parent；
        # 这里用命名参数，避免把 useAni 当成 parent 传进去。
        self.bar = qf.ProgressBar(None, useAni=False)
        self.bar.setRange(0, BAR_STEPS)
        self.bar.setValue(0)

        self.count_label = qf.BodyLabel("0 / 0")
        self.current_label = qf.BodyLabel("-")
        self.speed_label = qf.BodyLabel("-")
        self.eta_label = qf.BodyLabel("-")

        stats = QHBoxLayout()
        stats.setSpacing(8)
        for title, widget in (("当前文件", self.current_label),
                              ("速度", self.speed_label),
                              ("剩余", self.eta_label)):
            stats.addWidget(qf.CaptionLabel(title))
            stats.addWidget(widget, 1)

        self.log = qf.PlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText("迁移日志")

        self.failure_table = qf.TableWidget()
        # qf.TableWidget() 不接受行列数，必须自己 setColumnCount / setRowCount
        self.failure_table.setColumnCount(len(FAILURE_HEADERS))
        self.failure_table.setRowCount(0)
        self.failure_table.setHorizontalHeaderLabels(FAILURE_HEADERS)
        # 列宽显式分配：默认 100px 会把「相对路径」截成 "data\..."，
        # 用户看不出到底是哪个文件失败；路径列与进程列都需要更多空间。
        header = self.failure_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PROCESS_COLUMN, QHeaderView.ResizeMode.Stretch)

        failure_card = Card()
        failure_layout = QVBoxLayout(failure_card)
        failure_layout.setContentsMargins(16, 12, 16, 16)
        failure_layout.setSpacing(8)
        failure_layout.addWidget(qf.StrongBodyLabel(
            "失败清单（双击「占用进程」列可定位进程）"))
        failure_layout.addWidget(self.failure_table, 1)

        log_card = Card()
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(16, 12, 16, 16)
        log_layout.setSpacing(8)
        log_layout.addWidget(qf.StrongBodyLabel("迁移日志"))
        log_layout.addWidget(self.log, 1)

        self.pause_button = qf.PushButton("暂停")
        self.cancel_button = qf.PushButton("取消")
        controls = QHBoxLayout()
        controls.addWidget(self.pause_button)
        controls.addWidget(self.cancel_button)
        controls.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.bar)
        layout.addWidget(self.count_label)
        layout.addLayout(stats)
        layout.addWidget(log_card, 1)
        layout.addWidget(failure_card, 1)
        layout.addLayout(controls)

        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.failure_table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.set_running(False)

    # --- 对外接口 ---------------------------------------------------
    def update_progress(self, progress: Progress) -> None:
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
            f"{progress.done_files:,} / {progress.total_files:,} 个文件，"
            f"{human_bytes(done)} / {human_bytes(total)}")

    def percent(self) -> int:
        return int(self.bar.value() * 100 / BAR_STEPS)

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def add_failure(self, failure: CopyFailure) -> None:
        row = self.failure_table.rowCount()
        self.failure_table.insertRow(row)
        self.failure_table.setItem(row, 0, QTableWidgetItem(failure.rel))
        self.failure_table.setItem(row, 1, QTableWidgetItem(
            REASON_TEXT.get(failure.reason, failure.reason)))
        self.failure_table.setItem(row, PROCESS_COLUMN,
                                   QTableWidgetItem(failure.process))
        suffix = f"（{failure.process}）" if failure.process else ""
        self.append_log(f"[失败] {failure.rel}："
                        f"{REASON_TEXT.get(failure.reason, failure.reason)}{suffix}")

    def set_failures(self, failures) -> None:
        self.failure_table.setRowCount(0)
        for failure in failures:
            self.add_failure(failure)

    def failure_rows(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for row in range(self.failure_table.rowCount()):
            rows.append(tuple(self.failure_table.item(row, col).text()
                              for col in range(len(FAILURE_HEADERS))))
        return rows

    def reset(self) -> None:
        self.bar.setValue(0)
        self.current_label.setText("-")
        self.speed_label.setText("-")
        self.eta_label.setText("-")
        self.count_label.setText("0 / 0")
        self.log.clear()
        self.failure_table.setRowCount(0)
        self._paused = False
        self.pause_button.setText("暂停")

    def set_running(self, running: bool) -> None:
        # 未运行时暂停/取消都不可点：Qt 里 disabled 按钮的 click() 不触发信号，
        # 这正是「迁移结束后按钮失效」的既定语义，不要绕过。
        self.pause_button.setEnabled(running)
        self.cancel_button.setEnabled(running)
        if not running:
            self._paused = False
            self.pause_button.setText("暂停")

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_button.setText("继续" if self._paused else "暂停")
        self.pause_toggled.emit(self._paused)

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        if column != PROCESS_COLUMN:
            return
        item = self.failure_table.item(row, PROCESS_COLUMN)
        if item is not None and item.text().strip():
            self.locate_requested.emit(item.text())
