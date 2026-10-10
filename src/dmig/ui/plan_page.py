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
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QListWidgetItem,
                               QTableWidgetItem, QVBoxLayout, QWidget)

import qfluentwidgets as qf

from ..core.models import MigrationPlan, lossy_attribute_text
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

    replan_requested = Signal()
    elevate_requested = Signal()
    takeover_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan: MigrationPlan | None = None
        # 用户映射意图的权威存储，跨 load() 存活。
        # 只靠表格单元格记不住：重扫会按新计划重建表格，用户刚填的值会被清空，
        # 随后 start_migration 拿到空映射、用空映射重建 Migrator，映射就彻底丢了。
        self._mappings: dict[str, str] = {}
        # 本次要跳过的目录（绝对路径），同样跨 load() 存活。
        # 只靠计划回显会丢：重扫之后新计划里那些目录已经不在 permission_issues 里了，
        # 下一次重建 Migrator 就成了空排除项，被跳过的目录会被重新复制。
        self._excludes: list[str] = []
        self._loading = False
        self._symlink_blocked = False

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

        self.deep_check = qf.CheckBox(
            "启用 SHA256 全量校验（较慢；默认已比对大小与时间戳，"
            "哈希能进一步发现两者都相同但内容已损坏的文件）")
        self.skip_reparse_check = qf.CheckBox(
            "跳过下列条目，不按真实内容复制（只保留在备份里）")
        self.skip_denied_check = qf.CheckBox(
            "跳过无权限访问的目录（它们只保留在备份里，删备份前请确认不再需要）")
        self.apply_button = qf.PrimaryPushButton("应用映射并重新扫描")
        self.elevate_button = qf.PushButton("以管理员身份重试")
        self.elevate_button.setEnabled(False)
        self.takeover_button = qf.PushButton("接管这些目录的权限")
        self.takeover_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.elevate_button)
        buttons.addWidget(self.takeover_button)
        buttons.addStretch(1)

        unknown_card, unknown_layout = _card(
            "非链接类重解析点（云占位符 / AppExecLink / Dedup 等，默认按真实内容复制）")
        unknown_layout.addWidget(self.skip_reparse_check)
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
        layout.addWidget(self.skip_denied_check)
        layout.addWidget(self.deep_check)
        layout.addLayout(buttons)

        self.apply_button.clicked.connect(self.replan_requested.emit)
        self.elevate_button.clicked.connect(self.elevate_requested.emit)
        self.takeover_button.clicked.connect(self.takeover_requested.emit)
        self.group_table.itemChanged.connect(self._on_group_item_changed)
        # 跳过与否会改变真实数据量与复制行为，必须重扫，
        # 否则页面显示的计划与实际执行的不一致。
        self.skip_reparse_check.toggled.connect(self._on_skip_toggled)
        self.skip_denied_check.toggled.connect(self._on_skip_toggled)

    # --- 对外接口 ---------------------------------------------------
    def set_options(self, options: dict) -> None:
        """续传：把会话记录的运行参数回显到页面上。

        映射要直接落进 `_mappings`：调用时表格还没有行（计划尚未扫描），
        只填单元格等于什么都没填；而 `_mappings` 是跨 `load()` 的权威存储，
        随后扫描出的分组会照它回显。

        `setChecked` 会触发 toggled → 请求重扫，本方法又是在窗口构造期间被调的，
        那次重扫会打在还没填源/目标的页面上，所以整段放进 `_loading` 保护区间。
        """
        options = options or {}
        self._loading = True
        try:
            for prefix, target in (options.get("mappings") or {}).items():
                self._remember_mapping(str(prefix), str(target))
            self._excludes = [str(p) for p in (options.get("excludes") or [])]
            self.skip_denied_check.setChecked(bool(self._excludes))
            self.deep_check.setChecked(bool(options.get("deep_verify")))
            self.skip_reparse_check.setChecked(
                bool(options.get("skip_unknown_reparse")))
        finally:
            self._loading = False

    def load(self, plan: MigrationPlan, *, already_elevated: bool = False,
             symlink_capable: bool = True) -> None:
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
        dangling = [link for link in plan.links if link.is_dangling]
        if dangling:
            shown = "、".join(link.rel for link in dangling[:3])
            if len(dangling) > 3:
                shown += "…"
            lines.append(f"{len(dangling)} 条链接的目标现在就不存在（断链）：{shown}。"
                         "仍会原样重建，迁移后照样指不到东西。")
        lossy = lossy_attribute_text(plan.sparse_files, plan.hardlinked_files,
                                     plan.ads_files)
        if lossy:
            lines.append(f"复制方式的固有限制：{lossy}。")
        if plan.permission_issues:
            if already_elevated:
                lines.append(f"{len(plan.permission_issues)} 个目录即使提权也访问不了，"
                             "请检查这些路径是否存在或已被占用；"
                             "确实拿不到权限的，勾选下方「跳过无权限访问的目录」把它们排除掉。")
            else:
                lines.append(f"{len(plan.permission_issues)} 个目录无权限访问，"
                             "请点「以管理员身份重试」；"
                             "确实拿不到权限的，勾选下方「跳过无权限访问的目录」把它们排除掉。")
        if plan.excluded:
            shown = "、".join(plan.excluded[:3])
            if len(plan.excluded) > 3:
                shown += "…"
            lines.append(f"已跳过 {len(plan.excluded)} 项（{shown}）："
                         "它们不会复制到目标，只存在于备份里。")
        self._symlink_blocked = bool(plan.file_symlinks) and not symlink_capable
        if self._symlink_blocked:
            if already_elevated:
                lines.append(f"{plan.file_symlinks} 个文件符号链接即使提权也建不起来，"
                             "请开启 Windows 开发者模式，或检查组策略里"
                             "「创建符号链接」权限有没有授予当前用户。")
            else:
                lines.append(f"{plan.file_symlinks} 个文件符号链接需要管理员特权才能重建"
                             "（目录链接会降级为 junction，不受影响）。"
                             "现在不处理，等到复制完才会失败——请点「以管理员身份重试」，"
                             "或开启 Windows 开发者模式。")
        if plan.unknown_reparse:
            if plan.skip_unknown_reparse:
                lines.append(f"{len(plan.unknown_reparse)} 个非链接类重解析点将被跳过，"
                             "它们只存在于备份中——删除备份前请确认不再需要。")
            else:
                lines.append(f"{len(plan.unknown_reparse)} 个非链接类重解析点将按真实内容"
                             "复制（云占位符会触发下载，可能很慢）。")
        if plan.target_not_empty:
            lines.append("提示：目标目录已存在且非空，将按续传/合并处理。")
        self.warn_label.setText("\n".join(lines))

        self._loading = True
        try:
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
                # 计划回传的 mapped_to 优先；计划里没有就用页面自己记住的，
                # 否则用户填一次、重扫一次就被清空，映射随之丢失。
                mapped = group.mapped_to or self._mappings.get(group.prefix, "")
                mapping_item = QTableWidgetItem(mapped)
                mapping_item.setToolTip(mapped)
                self.group_table.setItem(row, 2, mapping_item)
            # setChecked 会触发 toggled，必须在 _loading 保护区间内回显，
            # 否则每次 load() 都会多请求一次重扫。
            self.skip_reparse_check.setChecked(plan.skip_unknown_reparse)
            # 清单为空时勾选毫无意义，却会白白触发一次重扫
            self.skip_reparse_check.setEnabled(bool(plan.unknown_reparse))

            # 新计划里那些目录已经不在 permission_issues 里了，必须靠计划本身
            # 与页面的记忆把排除项续上，否则下一次重建 Migrator 就成了空排除项。
            self._excludes = sorted(set(self._excludes) | set(plan.excludes))
            self.skip_denied_check.setChecked(bool(self._excludes))
            self.skip_denied_check.setEnabled(
                bool(plan.permission_issues) or bool(self._excludes))
        finally:
            self._loading = False

        self.unknown_list.clear()
        for entry in plan.unknown_reparse:
            item = QListWidgetItem(entry.rel)
            item.setToolTip(f"重解析标签 0x{entry.tag:08X}，"
                            f"{'目录' if entry.is_dir else '文件'}")
            self.unknown_list.addItem(item)

        self.elevate_button.setEnabled(
            (bool(plan.permission_issues) or self._symlink_blocked) and not already_elevated)
        if already_elevated:
            self.elevate_button.setToolTip("当前进程已具备管理员权限，无需再次提权")
        elif self._symlink_blocked and not plan.permission_issues:
            self.elevate_button.setToolTip("重建文件符号链接需要管理员特权")
        else:
            self.elevate_button.setToolTip("")

        # 接管属主要求提权：未提权时按钮不可用，提示直接说清要先做什么。
        self.takeover_button.setEnabled(bool(plan.permission_issues) and already_elevated)
        if not already_elevated:
            self.takeover_button.setToolTip("接管属主需要管理员权限，请先点「以管理员身份重试」")
        elif not plan.permission_issues:
            self.takeover_button.setToolTip("当前没有无权限访问的目录")
        else:
            self.takeover_button.setToolTip(
                "先把原始 ACL 记进会话，再接管属主并授予当前用户完全控制；"
                "回滚时按记录还原。原始 ACL 读不到的那些会如实告知，不会静默改动")

    def mappings(self) -> dict[str, str]:
        """用户当前的全部重写映射；留空或与源相同的都视为恒等。

        表格里的行优先（留空 = 明确取消），此外还要带上「行已经不在了、
        但用户填过的映射」：重扫后分组可能变化，只读表格会把它们弄丢。
        """
        result: dict[str, str] = {}
        listed: set[str] = set()
        for row in range(self.group_table.rowCount()):
            source_item = self.group_table.item(row, 0)
            target_item = self.group_table.item(row, 2)
            if source_item is None or target_item is None:
                continue
            source = source_item.text().strip()
            if not source:
                continue
            listed.add(source)
            target = target_item.text().strip()
            if not target:
                continue
            if os.path.normcase(os.path.normpath(target)) == \
                    os.path.normcase(os.path.normpath(source)):
                continue
            result[source] = target
        for source, target in self._mappings.items():
            if source not in listed and target:
                result[source] = target
        return result

    def excludes(self) -> list[str]:
        """本次要跳过的目录（绝对路径）。

        值全部来自 `_excludes`：它由勾选动作固化、由 `set_options` 从会话恢复，
        跨 `load()` 存活。不按当前计划的 permission_issues 现推——重扫之后
        新计划里那些目录已经因为被排除而不再出现，现推会整批丢掉。
        """
        return sorted(set(self._excludes))

    def _remember_mapping(self, prefix: str, value: str) -> None:
        if not prefix:
            return
        if not value or os.path.normcase(os.path.normpath(value)) == \
                os.path.normcase(os.path.normpath(prefix)):
            self._mappings.pop(prefix, None)      # 留空或填回原样都算取消
        else:
            self._mappings[prefix] = value

    def _on_group_item_changed(self, item) -> None:
        """用户直接在「重写为」单元格里打字时也要记住，否则重扫后照样丢。"""
        if self._loading or item.column() != 2:
            return
        prefix_item = self.group_table.item(item.row(), 0)
        if prefix_item is not None:
            self._remember_mapping(prefix_item.text().strip(), item.text().strip())

    def _on_skip_toggled(self, _checked: bool) -> None:
        if self._loading:
            return
        self._sync_excludes()
        self.replan_requested.emit()

    def _sync_excludes(self) -> None:
        """把勾选状态落到 `_excludes` 上。

        勾上时把当前这份无权限清单固化下来，取消时清空。必须固化而不是每次现推：
        重扫之后新计划里那些目录已经因为被排除而不再出现在 permission_issues 中，
        现推会在下一次 `load()` 时把它们整批丢掉。
        """
        if not self.skip_denied_check.isChecked():
            self._excludes = []
            return
        denied = set(self._plan.permission_issues) if self._plan else set()
        self._excludes = sorted(set(self._excludes) | denied)

    def skip_unknown_reparse(self) -> bool:
        return self.skip_reparse_check.isChecked()

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
        prefix_item = self.group_table.item(row, 0)
        if prefix_item is not None:
            self._remember_mapping(prefix_item.text().strip(), mapped_to.strip())

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
