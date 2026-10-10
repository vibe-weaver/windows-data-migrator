"""向导第四步：结果统计、未完成清单与收尾动作。

界面改用 Fluent 控件：标题用 TitleLabel，两个分区用 `chrome.Card` 圆角卡片，
统计值用 `qf.StrongBodyLabel`，清单用 `qf.TableWidget`。
颜色一律从 `theme` 取（成功 `theme.SUCCESS` / 未完成 `theme.ERROR`），
本模块不出现任何颜色字面量。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QTableWidgetItem, QVBoxLayout,
                               QWidget)

import qfluentwidgets as qf

from . import chrome, theme
from .format import human_bytes
from .report import (CSV_HEADER, KIND_DANGLING, build_rows, link_rows, summarize,
                     unfinished_rows, write_csv)

STAT_ROWS = (("copied_files", "成功复制（个）"),
             ("copied_bytes", "成功复制（字节）"),
             ("skipped", "跳过（已存在）"),
             ("failed", "复制失败"),
             ("missing", "校验缺失"),
             ("mismatch", "校验不一致"),
             ("dangling", "断链（原样重建）"))


class ReportPage(QWidget):
    """④ 报告页。"""

    rollback_requested = Signal()
    cleanup_requested = Signal()
    reverify_requested = Signal()
    discard_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outcome = None

        self.title_label = qf.TitleLabel("-")

        self.message_label = qf.BodyLabel("")
        self.message_label.setWordWrap(True)
        # 运行日志的位置：失败现场往往已经变了（源被改名成备份、目标端着半成品），
        # 复盘全过程只能靠这份日志，所以摆在报告里而不是只留在终端输出里。
        self.log_label = qf.CaptionLabel("")
        self.log_label.setWordWrap(True)
        self.log_label.setTextColor(*theme.MUTED)
        self.log_label.hide()

        stats_card = chrome.Card()
        stats_layout = QVBoxLayout(stats_card)
        stats_layout.setContentsMargins(20, 16, 20, 16)
        stats_layout.setSpacing(10)
        stats_layout.addWidget(qf.StrongBodyLabel("统计"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        self.stat_labels: dict[str, qf.StrongBodyLabel] = {}
        for key, title in STAT_ROWS:
            value = qf.StrongBodyLabel("0")
            value.setObjectName(f"stat_{key}")
            form.addRow(qf.BodyLabel(title), value)
            self.stat_labels[key] = value
        stats_layout.addLayout(form)

        # qf.TableWidget() 不接受行列数，行数用 setRowCount，列数用 setColumnCount
        self.table = qf.TableWidget()
        self.table.setColumnCount(len(CSV_HEADER))
        self.table.setRowCount(0)
        self.table.setHorizontalHeaderLabels(CSV_HEADER)
        # 列宽显式分配：默认 100px 会把「相对路径」截成 "data\..."，
        # 用户看不出是哪个文件没完成。
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        table_card = chrome.Card()
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(20, 16, 20, 16)
        table_layout.setSpacing(10)
        table_layout.addWidget(qf.StrongBodyLabel("未完成清单"))
        table_layout.addWidget(self.table, 1)

        # 破坏性动作用普通按钮，不抢主色
        self.export_button = qf.PushButton("导出 CSV")
        self.reverify_button = qf.PushButton("重新校验")
        self.discard_button = qf.PushButton("删除目标")
        self.rollback_button = qf.PushButton("一键回滚")
        self.cleanup_button = qf.PushButton("删除备份")
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(10)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.reverify_button)
        buttons.addStretch(1)
        buttons.addWidget(self.discard_button)
        buttons.addWidget(self.rollback_button)
        buttons.addWidget(self.cleanup_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(self.title_label)
        layout.addWidget(self.message_label)
        layout.addWidget(self.log_label)
        layout.addWidget(stats_card)
        layout.addWidget(table_card, 1)
        layout.addLayout(buttons)

        self.export_button.clicked.connect(self._on_export_clicked)
        self.reverify_button.clicked.connect(self.reverify_requested.emit)
        self.discard_button.clicked.connect(self.discard_requested.emit)
        self.rollback_button.clicked.connect(self.rollback_requested.emit)
        self.cleanup_button.clicked.connect(self.cleanup_requested.emit)
        self.set_actions_enabled(False, False)

    # --- 对外接口 ---------------------------------------------------
    def load(self, outcome) -> None:
        self._outcome = outcome
        stats = summarize(outcome)
        ok = bool(outcome.ok)
        self.title_label.setText("迁移成功" if ok else f"未完成：{outcome.phase}")
        # 成功/未完成用库与 theme 的语义色区分，深浅模式各一个值
        self.title_label.setTextColor(*(theme.SUCCESS if ok else theme.ERROR))
        for key in self.stat_labels:
            value = stats[key]
            text = human_bytes(value) if key == "copied_bytes" else f"{value:,}"
            self.stat_labels[key].setText(text)

        self.table.setRowCount(0)
        rows = unfinished_rows(outcome)
        detail = {row[1]: row[2] for row in link_rows(outcome)}
        red = QBrush(QColor(theme.error_color()))
        for row in rows:
            index = self.table.rowCount()
            self.table.insertRow(index)
            for column, text in enumerate(row):
                item = QTableWidgetItem(text)
                if row[0] == KIND_DANGLING:
                    item.setForeground(red)
                    # 表格里只放结论，完整指向靠 tooltip 与导出的 CSV 兜住
                    item.setToolTip(detail.get(row[1], text))
                self.table.setItem(index, column, item)

        self.message_label.setText(getattr(outcome, "message", "") or "")
        log_path = getattr(outcome, "log_path", "") or ""
        self.log_label.setText(f"运行日志：{log_path}" if log_path else "")
        self.log_label.setVisible(bool(log_path))
        has_backup = bool(getattr(outcome, "backup_path", None))
        # 没成功又没备份 = 源目录完好、目标端留着半成品，这正是需要清理出口的现场
        self.set_actions_enabled(has_backup, has_backup,
                                 discard=not ok and not has_backup)

    def set_actions_enabled(self, rollback: bool, cleanup: bool,
                            discard: bool = False) -> None:
        self.rollback_button.setEnabled(rollback)
        self.cleanup_button.setEnabled(cleanup)
        self.discard_button.setEnabled(discard)

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
