"""向导第二步：展示迁移计划、链接目标归属表与用户重写映射。

「被占用数」在预扫描阶段拿不到：核心引擎的 MigrationPlan 不含文件清单，
对 50 万个文件逐个调 RestartManager 会让预扫描慢几个数量级。
因此这里如实显示「执行时检测」，真实占用清单在执行页给出。

界面改用 qfluentwidgets 的 Fluent 控件与 `chrome` 外壳组件；本模块不出现
任何颜色字面量，语义色一律从 `theme` 导入（深浅色两套值都给）。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import qfluentwidgets as qf

from ..core.models import MigrationPlan
from . import chrome
from .format import estimate_seconds, human_bytes, human_duration
from .theme import MUTED

GROUP_HEADERS = ["链接目标目录", "链接数", "重写为"]
LOCK_HINT = "执行时检测"

# (键, 面板上的中文标题)，顺序即展示顺序
METRICS: tuple[tuple[str, str], ...] = (
    ("bytes", "真实数据量"),
    ("files", "文件数"),
    ("dirs", "目录数"),
    ("links", "链接数"),
    ("elevation", "需提权目录"),
    ("locks", "被占用"),
    ("eta", "预计耗时"),
)

# MetricRow 是按「显示名」索引的（它把传入的第一个元素同时当作查找键和标题），
# 所以要用中文名去调 set_value。这里做一次英文键 -> 中文显示名的转换，
# 避免调用处散落中文字面量。
METRIC_LABELS: dict[str, str] = dict(METRICS)

# 「重写为」列固定宽度：链接目标目录通常很长，这一列要给用户足够的手填空间。
# 取 240 会让整表超出视口、底部长出横向滚动条，240 的右半截本来就是空的，
# 因此收到 180：手填路径仍然够用，同时消灭横向滚动条。
MAPPING_COLUMN_WIDTH = 180


def _card(title: str) -> tuple[chrome.Card, QVBoxLayout]:
    """带标题的卡片。"""
    card = chrome.Card()
    box = QVBoxLayout(card)
    box.setContentsMargins(16, 12, 16, 16)
    box.setSpacing(8)
    box.addWidget(qf.StrongBodyLabel(title))
    return card, box


class PlanPage(QWidget):
    """② 计划页。"""

    mappings_applied = Signal()
    elevate_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan: MigrationPlan | None = None

        # 大数字由 MetricRow 负责排版；值标签在这里先建好并**指定 parent**，
        # 再交给 MetricRow 接管——必须是 MetricRow 的子控件：交给 `addLayout`
        # 的裸 QWidget 在 PySide6 里不会被重新 parent，会变成一个永远不可见的
        # 孤立顶层控件（实测整行只剩标题、数字完全看不到）。
        #
        # 注意：MetricRow 会把传入的第一个元素**同时**当作查找键和显示标题，
        # 所以这里必须传中文显示名；英文键另用 metric_labels 维护，
        # 否则界面上会显示成 "bytes" / "files" 而不是「真实数据量」/「文件数」。
        self.metric_row = chrome.MetricRow([(label, "-") for _, label in METRICS])
        self.metric_labels: dict[str, qf.TitleLabel] = {}
        for box_index, (key, _label) in enumerate(METRICS):
            box = self.metric_row.layout().itemAt(box_index).layout()
            # layout 里的子项被接管后就是 MetricRow 的子控件，parent 有了，可见性随父
            self.metric_labels[key] = box.itemAt(0).widget()
        metrics_card, metrics_layout = _card("迁移计划")
        metrics_layout.addWidget(self.metric_row)

        # 摘要信息：默认用库的正文色，不做人为着色，深浅色下都清晰
        self.warn_label = qf.BodyLabel("")
        self.warn_label.setWordWrap(True)
        self.warn_label.setTextColor(*MUTED)
        warn_card, warn_layout = _card("计划摘要")
        warn_layout.addWidget(self.warn_label)

        self.group_table = qf.TableWidget()
        # qf.TableWidget() 不接受行列数，必须显式设置
        self.group_table.setColumnCount(len(GROUP_HEADERS))
        self.group_table.setRowCount(0)
        self.group_table.setHorizontalHeaderLabels(GROUP_HEADERS)
        # 列宽必须显式分配：默认 100px 会把「链接目标目录」这条最长、
        # 也最关键的信息截成 "C:..."，用户根本看不出链接指向哪个仓库。
        header = self.group_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.group_table.setColumnWidth(2, MAPPING_COLUMN_WIDTH)

        self.unknown_list = qf.ListWidget()
        # 未知重解析点通常为空或个位数；不给它无限高度，
        # 否则空列表会撑出一大片空白，把归属表挤扁
        self.unknown_list.setMinimumHeight(56)
        self.unknown_list.setMaximumHeight(132)

        self.deep_check = qf.CheckBox("启用 SHA256 全量校验（较慢，但能发现同大小不同内容）")
        self.apply_button = qf.PrimaryPushButton("应用映射并重新扫描")
        self.elevate_button = qf.PushButton("以管理员身份重试")
        self.elevate_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.elevate_button)
        buttons.addStretch(1)

        unknown_card, unknown_layout = _card("未知重解析点（不会被复制）")
        unknown_layout.addWidget(self.unknown_list)

        groups_card, groups_layout = _card("链接目标归属表")
        groups_layout.addWidget(qf.CaptionLabel(
            "「重写为」留空表示保持原样；填写新路径后点「应用映射并重新扫描」，"
            "指向该目录的链接会全部改指到新位置。"))
        groups_layout.addWidget(self.group_table)

        layout = QVBoxLayout(self)
        layout.addWidget(metrics_card)
        layout.addWidget(warn_card)
        layout.addWidget(groups_card, 1)
        layout.addWidget(unknown_card)
        layout.addWidget(self.deep_check)
        layout.addLayout(buttons)

        self.apply_button.clicked.connect(self.mappings_applied.emit)
        self.elevate_button.clicked.connect(self.elevate_requested.emit)

    # --- 对外接口 ---------------------------------------------------
    def load(self, plan: MigrationPlan, *, already_elevated: bool = False) -> None:
        self._plan = plan
        totals = plan.totals
        # 走 MetricRow.set_value 而不是直接 setText：
        # 该方法会按值的显示宽度重判字号（「执行时检测」这类长文本要降一档），
        # 绕过它会让大字号把相邻两格挤在一起。
        for key, value in (
            ("bytes", human_bytes(plan.required_bytes)),
            ("files", f"{totals.files:,}"),
            ("dirs", f"{totals.dirs:,}"),
            ("links", f"{len(plan.links):,}"),
            ("elevation", str(len(plan.permission_issues))),
            ("locks", LOCK_HINT),
            ("eta", human_duration(
                estimate_seconds(plan.required_bytes, totals.files))),
        ):
            self.metric_row.set_value(METRIC_LABELS[key], value)

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
            # 路径很长仍有被截断的可能，挂上 tooltip 保证全文可见
            prefix_item.setToolTip(group.prefix)
            self.group_table.setItem(row, 0, prefix_item)
            count_item = QTableWidgetItem(str(group.count))
            count_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            count_item.setToolTip(str(group.count))
            self.group_table.setItem(row, 1, count_item)
            mapping_item = QTableWidgetItem(group.mapped_to or "")
            mapping_item.setToolTip(group.mapped_to or "")
            self.group_table.setItem(row, 2, mapping_item)

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
            # tooltip 同步，否则改完映射后悬停看到的还是旧值
            item.setToolTip(mapped_to)

    def metric_text(self, key: str) -> str:
        """读取某个指标的文本。

        注意：**绝不能把这个方法命名为 `metric`**。QWidget 继承自 QPaintDevice，
        `metric(PaintDeviceMetric)` 是 Qt 的虚函数，绘制系统靠它查询 DPI /
        设备像素比。用 Python 覆盖它会让每次绘制都走进这里、
        拿 PaintDeviceMetric 当 key 去查字典并抛 KeyError，直接导致控件无法绘制。
        """
        return self.metric_labels[key].text()

    def warning_text(self) -> str:
        return self.warn_label.text()

    def deep_verify(self) -> bool:
        return self.deep_check.isChecked()

    def unknown_count(self) -> int:
        return self.unknown_list.count()
