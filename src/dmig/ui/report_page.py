"""向导第四步：结果统计、未完成清单与收尾动作。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .format import human_bytes
from .report import CSV_HEADER, build_rows, summarize, unfinished_rows, write_csv

STAT_ROWS = (("copied_files", "成功复制（个）"),
             ("copied_bytes", "成功复制（字节）"),
             ("skipped", "跳过（已存在）"),
             ("failed", "复制失败"),
             ("missing", "校验缺失"),
             ("mismatch", "校验不一致"))


class ReportPage(QWidget):
    """④ 报告页。"""

    rollback_requested = Signal()
    cleanup_requested = Signal()
    reverify_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outcome = None

        self.title_label = QLabel("-")
        self.message_label = QLabel("")
        self.message_label.setWordWrap(True)

        self.stat_labels: dict[str, QLabel] = {}
        stats_box = QGroupBox("统计")
        stats_layout = QFormLayout(stats_box)
        for key, title in STAT_ROWS:
            value = QLabel("0")
            value.setObjectName(f"stat_{key}")
            stats_layout.addRow(title, value)
            self.stat_labels[key] = value

        self.table = QTableWidget(0, len(CSV_HEADER))
        self.table.setHorizontalHeaderLabels(CSV_HEADER)
        self.table.horizontalHeader().setStretchLastSection(True)

        table_box = QGroupBox("未完成清单")
        table_layout = QVBoxLayout(table_box)
        table_layout.addWidget(self.table)

        self.export_button = QPushButton("导出 CSV")
        self.reverify_button = QPushButton("重新校验")
        self.rollback_button = QPushButton("一键回滚")
        self.cleanup_button = QPushButton("删除备份")
        buttons = QHBoxLayout()
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.reverify_button)
        buttons.addStretch(1)
        buttons.addWidget(self.rollback_button)
        buttons.addWidget(self.cleanup_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.message_label)
        layout.addWidget(stats_box)
        layout.addWidget(table_box, 1)
        layout.addLayout(buttons)

        self.export_button.clicked.connect(self._on_export_clicked)
        self.reverify_button.clicked.connect(self.reverify_requested.emit)
        self.rollback_button.clicked.connect(self.rollback_requested.emit)
        self.cleanup_button.clicked.connect(self.cleanup_requested.emit)
        self.set_actions_enabled(False, False)

    # --- 对外接口 ---------------------------------------------------
    def load(self, outcome) -> None:
        self._outcome = outcome
        stats = summarize(outcome)
        self.title_label.setText("迁移成功" if outcome.ok else f"未完成：{outcome.phase}")
        for key in self.stat_labels:
            value = stats[key]
            text = human_bytes(value) if key == "copied_bytes" else f"{value:,}"
            self.stat_labels[key].setText(text)

        self.table.setRowCount(0)
        for row in unfinished_rows(outcome):
            index = self.table.rowCount()
            self.table.insertRow(index)
            for column, text in enumerate(row):
                self.table.setItem(index, column, QTableWidgetItem(text))

        self.message_label.setText(getattr(outcome, "message", "") or "")
        has_backup = bool(getattr(outcome, "backup_path", None))
        self.set_actions_enabled(rollback=has_backup, cleanup=has_backup)

    def set_actions_enabled(self, rollback: bool, cleanup: bool) -> None:
        self.rollback_button.setEnabled(rollback)
        self.cleanup_button.setEnabled(cleanup)

    def summary_text(self, key: str) -> str:
        return self.stat_labels[key].text()

    def rows(self) -> list[list[str]]:
        result: list[list[str]] = []
        for row in range(self.table.rowCount()):
            result.append([self.table.item(row, column).text()
                           for column in range(len(CSV_HEADER))])
        return result

    def export_csv(self, path) -> None:
        rows = build_rows(self._outcome) if self._outcome is not None \
            else [list(CSV_HEADER)]
        write_csv(path, rows)

    def title_text(self) -> str:
        return self.title_label.text()

    def message_text(self) -> str:
        return self.message_label.text()

    def _on_export_clicked(self) -> None:
        chosen, _filter = QFileDialog.getSaveFileName(self, "导出报告",
                                                      "迁移报告.csv",
                                                      "CSV 文件 (*.csv)")
        if not chosen:
            return
        self.export_csv(chosen)
        self.message_label.setText(f"报告已导出：{chosen}")
