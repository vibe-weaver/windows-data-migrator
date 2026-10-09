"""向导第三步：执行进度、可滚动日志与失败清单。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QGroupBox, QHeaderView, QHBoxLayout, QLabel,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..core.models import CopyFailure, Progress
from .format import human_bytes, human_duration, human_speed

FAILURE_HEADERS = ["相对路径", "原因", "占用进程"]

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

        self.bar = QProgressBar()
        self.bar.setRange(0, BAR_STEPS)
        self.bar.setValue(0)
        self.current_label = QLabel("-")
        self.speed_label = QLabel("-")
        self.eta_label = QLabel("-")
        self.count_label = QLabel("0 / 0")

        stats = QHBoxLayout()
        for title, widget in (("当前文件", self.current_label),
                              ("速度", self.speed_label),
                              ("剩余", self.eta_label)):
            stats.addWidget(QLabel(title))
            stats.addWidget(widget, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText("迁移日志")

        self.pause_button = QPushButton("暂停")
        self.cancel_button = QPushButton("取消")
        controls = QHBoxLayout()
        controls.addWidget(self.pause_button)
        controls.addWidget(self.cancel_button)
        controls.addStretch(1)

        self.failure_table = QTableWidget(0, len(FAILURE_HEADERS))
        self.failure_table.setHorizontalHeaderLabels(FAILURE_HEADERS)
        # 列宽显式分配：默认 100px 会把「相对路径」截成 "data\..."，
        # 用户看不出到底是哪个文件失败；路径列需要更多空间。
        header = self.failure_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        failure_box = QGroupBox("失败清单（双击「占用进程」列可定位进程）")
        failure_layout = QVBoxLayout(failure_box)
        failure_layout.addWidget(self.failure_table)

        layout = QVBoxLayout(self)
        layout.addWidget(self.bar)
        layout.addWidget(self.count_label)
        layout.addLayout(stats)
        layout.addWidget(self.log, 1)
        layout.addWidget(failure_box, 1)
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
        self.failure_table.setItem(row, 2, QTableWidgetItem(failure.process))
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

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        item = self.failure_table.item(row, 2)
        if item is not None and item.text().strip():
            self.locate_requested.emit(item.text())
