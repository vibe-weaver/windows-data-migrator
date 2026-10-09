"""向导第二步：展示迁移计划、链接目标归属表与用户重写映射。

「被占用数」在预扫描阶段拿不到：核心引擎的 MigrationPlan 不含文件清单，
对 50 万个文件逐个调 RestartManager 会让预扫描慢几个数量级。
因此这里如实显示「执行时检测」，真实占用清单在执行页给出。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.models import MigrationPlan
from .format import estimate_seconds, human_bytes, human_duration

GROUP_HEADERS = ["链接目标目录", "链接数", "重写为"]
LOCK_HINT = "执行时检测"


class PlanPage(QWidget):
    """② 计划页。"""

    mappings_applied = Signal()
    elevate_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan: MigrationPlan | None = None

        self.metric_labels: dict[str, QLabel] = {}
        metrics_box = QGroupBox("迁移计划")
        metrics_layout = QVBoxLayout(metrics_box)
        for key, title in (("bytes", "真实数据量"), ("files", "文件数"),
                           ("dirs", "目录数"), ("links", "链接数"),
                           ("elevation", "需提权目录"), ("locks", "被占用"),
                           ("eta", "预计耗时")):
            row = QHBoxLayout()
            name = QLabel(title)
            name.setMinimumWidth(96)
            value = QLabel("-")
            value.setObjectName(f"metric_{key}")
            row.addWidget(name)
            row.addWidget(value, 1)
            metrics_layout.addLayout(row)
            self.metric_labels[key] = value

        self.warn_label = QLabel("")
        self.warn_label.setWordWrap(True)

        self.group_table = QTableWidget(0, len(GROUP_HEADERS))
        self.group_table.setHorizontalHeaderLabels(GROUP_HEADERS)
        self.group_table.horizontalHeader().setStretchLastSection(True)

        self.unknown_list = QListWidget()

        self.deep_check = QCheckBox("启用 SHA256 全量校验（较慢，但能发现同大小不同内容）")
        self.apply_button = QPushButton("应用映射并重新扫描")
        self.elevate_button = QPushButton("以管理员身份重试")
        self.elevate_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.elevate_button)
        buttons.addStretch(1)

        unknown_box = QGroupBox("未知重解析点（不会被复制）")
        unknown_layout = QVBoxLayout(unknown_box)
        unknown_layout.addWidget(self.unknown_list)

        groups_box = QGroupBox("链接目标归属表")
        groups_layout = QVBoxLayout(groups_box)
        groups_layout.addWidget(QLabel(
            "「重写为」留空表示保持原样；填写新路径后点「应用映射并重新扫描」，"
            "指向该目录的链接会全部改指到新位置。"))
        groups_layout.addWidget(self.group_table)

        layout = QVBoxLayout(self)
        layout.addWidget(metrics_box)
        layout.addWidget(self.warn_label)
        layout.addWidget(groups_box, 1)
        layout.addWidget(unknown_box)
        layout.addWidget(self.deep_check)
        layout.addLayout(buttons)

        self.apply_button.clicked.connect(self.mappings_applied.emit)
        self.elevate_button.clicked.connect(self.elevate_requested.emit)

    # --- 对外接口 ---------------------------------------------------
    def load(self, plan: MigrationPlan, *, already_elevated: bool = False) -> None:
        self._plan = plan
        totals = plan.totals
        self.metric_labels["bytes"].setText(human_bytes(plan.required_bytes))
        self.metric_labels["files"].setText(f"{totals.files:,}")
        self.metric_labels["dirs"].setText(f"{totals.dirs:,}")
        self.metric_labels["links"].setText(f"{len(plan.links):,}")
        self.metric_labels["elevation"].setText(str(len(plan.permission_issues)))
        self.metric_labels["locks"].setText(LOCK_HINT)
        self.metric_labels["eta"].setText(
            human_duration(estimate_seconds(plan.required_bytes, totals.files)))

        lines = [f"源：{plan.source}",
                 f"目标：{plan.target}",
                 f"目标盘剩余：{human_bytes(plan.free_bytes)}"]
        if plan.link_groups:
            outside = sum(group.count for group in plan.link_groups)
            lines.append(f"{outside} 个链接指向源树之外；若那些目录也要迁移，"
                         "请在下方为每个目标目录填写重写后的新路径。")
        if plan.permission_issues:
            if already_elevated:
                lines.append(f"{len(plan.permission_issues)} 个目录即使提权也访问不了，"
                             "请检查这些路径是否存在或已被占用。")
            else:
                lines.append(f"{len(plan.permission_issues)} 个目录无权限访问，"
                             "请点「以管理员身份重试」。")
        if plan.unknown_reparse:
            lines.append(f"{len(plan.unknown_reparse)} 个未知重解析点不会被复制。")
        if plan.target_not_empty:
            lines.append("提示：目标目录已存在且非空，将按续传/合并处理。")
        self.warn_label.setText("\n".join(lines))

        self.group_table.setRowCount(0)
        for group in plan.link_groups:
            row = self.group_table.rowCount()
            self.group_table.insertRow(row)
            prefix_item = QTableWidgetItem(group.prefix)
            prefix_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.group_table.setItem(row, 0, prefix_item)
            count_item = QTableWidgetItem(str(group.count))
            count_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.group_table.setItem(row, 1, count_item)
            self.group_table.setItem(row, 2, QTableWidgetItem(group.mapped_to or ""))

        self.unknown_list.clear()
        for rel in plan.unknown_reparse:
            self.unknown_list.addItem(rel)

        self.elevate_button.setEnabled(bool(plan.permission_issues) and not already_elevated)
        if already_elevated:
            self.elevate_button.setToolTip("当前进程已具备管理员权限，无需再次提权")
        else:
            self.elevate_button.setToolTip("")

    def mappings(self) -> dict[str, str]:
        """只收集真正改动过的映射；留空或与源相同的都视为恒等。"""
        result: dict[str, str] = {}
        for row in range(self.group_table.rowCount()):
            source_item = self.group_table.item(row, 0)
            target_item = self.group_table.item(row, 2)
            if source_item is None or target_item is None:
                continue
            source = source_item.text().strip()
            target = target_item.text().strip()
            if not source or not target:
                continue
            if os.path.normcase(os.path.normpath(target)) == \
                    os.path.normcase(os.path.normpath(source)):
                continue
            result[source] = target
        return result

    def group_rows(self) -> list[tuple[str, int, str]]:
        rows: list[tuple[str, int, str]] = []
        for row in range(self.group_table.rowCount()):
            rows.append((self.group_table.item(row, 0).text(),
                         int(self.group_table.item(row, 1).text()),
                         self.group_table.item(row, 2).text()))
        return rows

    def set_mapping(self, row: int, mapped_to: str) -> None:
        item = self.group_table.item(row, 2)
        if item is not None:
            item.setText(mapped_to)

    def metric(self, key: str) -> str:
        return self.metric_labels[key].text()

    def warning_text(self) -> str:
        return self.warn_label.text()

    def deep_verify(self) -> bool:
        return self.deep_check.isChecked()

    def unknown_count(self) -> int:
        return self.unknown_list.count()
