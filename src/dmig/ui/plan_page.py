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
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QListWidgetItem,
                               QTableWidgetItem, QVBoxLayout, QWidget)

import qfluentwidgets as qf

from ..core.models import MigrationPlan, lossy_attribute_text
from . import chrome
from .format import estimate_seconds, human_bytes, human_duration
from .theme import ACCENT_PAIR, MUTED, WARNING

GROUP_HEADERS = ["链接目标目录", "链接数", "重写为", "迁移后指向"]
MAPPING_COLUMN = 2          # 用户可编辑的那一列
DERIVED_COLUMN = 3          # 只读：应用映射后链接实际会指向哪里
LOCK_HINT = "执行时检测"
MAPPING_PLACEHOLDER = "留空 = 保持原样"

# 非链接类重解析点卡片的说明。这类东西对用户完全陌生：既不是链接，也不是普通
# 文件/目录；而且不勾会触发云端下载、勾了又可能造成不可逆的丢失，必须讲清楚。
UNKNOWN_REPARSE_CAPTION = (
    "这类条目不是链接，也不是普通文件或目录：云占位符（OneDrive 按需文件，"
    "本地只有一个指针、内容在云端）、AppExecLink（应用商店应用的执行别名）、"
    "Dedup（服务器去重卷）等。它们背后都有真实数据，所以默认按真实内容复制——"
    "能直读的读出内容（云占位符会触发下载，可能很慢、很占流量），"
    "读不动的把重解析数据原样搬过去。\n"
    "勾选后这些条目完全不复制，只留在备份里；源目录删除后备份就是唯一副本，"
    "而备份通常也会跟着被删。请先确认它们确实不再需要。")

# 归属表上方的说明。必须讲清**什么条件才该填**：工具只改链接指向，不替用户搬运
# 那个目录；而「旧路径还在」时根本不需要填。真实案例：用户把链接映射到一个空目录，
# 结果经原路径再也读不到数据——原说明只讲了「怎么操作」，没讲「什么情况下才该填」；
# 而早期那句「已经把该目录搬到别处时才填」也不准：迁移成链接之后原路径照常可用，
# 链接会自己穿过那一层，留空就够了。
GROUP_CAPTION = ("「重写为」留空 = 保持原样，多数情况都该留空。只有那个目录原来的"
                 "位置以后不再存在时才需要填（比如你把它剪切走了、原目录已删）；"
                 "如果它只是被迁移成了链接、原路径仍然可用，留空就够了——"
                 "链接会自己穿过那一层。工具只改链接指向，不会替你搬运目录；"
                 "填一个还没有数据的路径会让链接指空。"
                 "右列「迁移后指向」实时显示结果。")

# 归属表为空时的说明。真实案例：用户把 Windows 快捷方式（.lnk）当成了
# 目录软链接，扫描出链接数 0、归属表空，然后反复点「应用映射并重新扫描」——
# 什么都不会发生。界面必须说清「这里本来就没有链接可映射」，
# 否则用户只会以为按钮坏了。
NO_GROUP_HINT = ("源目录里没有指向树外（源树之外）的链接，无需映射。"
                 "注意 Windows 快捷方式（.lnk）不是链接，会被当作普通文件复制。")


def mapping_summary(plan, mappings) -> str:
    """重扫结束后给用户的一句话结论。

    实测：重扫只要 0.03 秒，状态栏从「正在扫描…」闪回「计划已生成」——
    和点击前一模一样，而表格里「重写为」那格本来就是你填的内容。
    不额外说明「应用了什么、影响多少条链接」，用户看到的就是「点了没反应」。
    """
    if not mappings:
        return "已重新扫描（未设置任何映射）"
    applied = [g for g in (getattr(plan, "link_groups", None) or [])
               if getattr(g, "mapped_to", "")]
    if not applied:
        return (f"已重新扫描：{len(mappings)} 条映射没有匹配到任何链接，"
                f"链接指向未改变")
    links = sum(int(getattr(g, "count", 0) or 0) for g in applied)
    return f"映射已应用：{len(applied)} 组、共 {links:,} 条链接的指向将被重写"

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
MAPPING_COLUMN_WIDTH = 180


def _card(title: str, icon_name: str = "") -> tuple[chrome.Card, QVBoxLayout]:
    """带图标标题的卡片。"""
    card = chrome.Card()
    box = QVBoxLayout(card)
    box.setContentsMargins(20, 14, 20, 16)
    box.setSpacing(10)
    icon = chrome.fluent_icon(icon_name) if icon_name else None
    header = chrome.make_section_header(title, icon)
    box.addWidget(header)
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
        self.metric_labels: dict[str, qf.BodyLabel] = {}
        for key, _label in METRICS:
            self.metric_labels[key] = self.metric_row.value_widget(METRIC_LABELS[key])
        metrics_card, metrics_layout = _card("迁移计划", "LIBRARY")
        metrics_layout.addWidget(self.metric_row)

        # 摘要信息：源 / 目标 / 目标盘剩余做成「标签在上、值在下」的三列，
        # 提示条目逐条一行（圆点 + 文字），不再挤成一整块密不透风的文字。
        self._info_values: dict[str, qf.BodyLabel] = {}
        info_grid = QHBoxLayout()
        info_grid.setContentsMargins(0, 0, 0, 0)
        info_grid.setSpacing(16)
        for i, (title, key) in enumerate((("源", "source"),
                                          ("目标", "target"),
                                          ("目标盘剩余", "free"))):
            box = QVBoxLayout()
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(2)
            cap = qf.CaptionLabel(title)
            cap.setTextColor(*MUTED)
            val = qf.BodyLabel("-")
            val.setToolTip("")
            box.addWidget(cap)
            box.addWidget(val)
            info_grid.addLayout(box, 1)
            self._info_values[key] = val
            if i < 2:
                info_grid.addWidget(chrome.Rule(Qt.Orientation.Vertical))
        self._info_grid_layout = info_grid

        # 提示条目容器：load() 时重建
        self._hints: list[QWidget] = []
        self._hint_layout = QVBoxLayout()
        self._hint_layout.setContentsMargins(0, 0, 0, 0)
        self._hint_layout.setSpacing(7)
        # warning_text() 的数据源：摘要各行文本按展示顺序拼接
        self._summary_texts: list[str] = []

        warn_card, warn_layout = _card("计划摘要", "INFO")
        warn_layout.addLayout(info_grid)
        warn_layout.addWidget(chrome.Rule(Qt.Orientation.Horizontal))
        warn_layout.addLayout(self._hint_layout)

        self.group_table = qf.TableWidget()
        # qf.TableWidget() 不接受行列数，必须显式设置
        self.group_table.setColumnCount(len(GROUP_HEADERS))
        self.group_table.setRowCount(0)
        self.group_table.setHorizontalHeaderLabels(GROUP_HEADERS)
        # 只读表——但「重写为」那一列是 setCellWidget 放进去的输入框，**不受影响**
        # （格子控件自己处理事件，不走表的编辑触发器）。而 qf.TableWidget 默认带
        # AnyKeyPressed：不在格子里、敲任意键进编辑态，格子会变成带清空按钮的
        # 输入框，看着像坏了。
        self.group_table.setEditTriggers(
            qf.TableWidget.EditTrigger.NoEditTriggers)
        # 列宽必须显式分配：默认 100px 会把「链接目标目录」这条最长、
        # 也最关键的信息截成 "C:..."，用户根本看不出链接指向哪个仓库。
        header = self.group_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(MAPPING_COLUMN, QHeaderView.ResizeMode.Fixed)
        self.group_table.setColumnWidth(MAPPING_COLUMN, MAPPING_COLUMN_WIDTH)
        header.setSectionResizeMode(DERIVED_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.group_table.verticalHeader().setDefaultSectionSize(36)

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
        self.apply_button.setMinimumWidth(196)
        self.apply_button.setMinimumHeight(34)
        self.elevate_button = qf.PushButton("以管理员身份重试")
        self.elevate_button.setEnabled(False)
        self.takeover_button = qf.PushButton("接管这些目录的权限")
        self.takeover_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.elevate_button)
        buttons.addWidget(self.takeover_button)
        buttons.addStretch(1)

        unknown_card, unknown_layout = _card(
            "非链接类重解析点（云占位符 / AppExecLink / Dedup 等，默认按真实内容复制）",
            "HELP")
        self.unknown_caption = qf.CaptionLabel(UNKNOWN_REPARSE_CAPTION)
        self.unknown_caption.setWordWrap(True)
        unknown_layout.addWidget(self.unknown_caption)
        unknown_layout.addWidget(self.skip_reparse_check)
        unknown_layout.addWidget(self.unknown_list)

        groups_card, groups_layout = _card("链接目标归属表", "LINK")
        # 留一个引用：说明文字要能读出来（测试与调试），也便于以后统一改文案
        self.group_caption = qf.CaptionLabel(GROUP_CAPTION)
        self.group_caption.setWordWrap(True)
        groups_layout.addWidget(self.group_caption)
        groups_layout.addWidget(self.group_table)
        # 空表说明：没有链接可映射时必须说出来，否则「点了没反应」
        self.group_hint = qf.CaptionLabel("")
        self.group_hint.setTextColor(*MUTED)
        self.group_hint.setWordWrap(True)
        self.group_hint.hide()
        groups_layout.addWidget(self.group_hint)

        options_card, options_layout = _card("复制选项", "SETTING")
        options_layout.addWidget(self.deep_check)
        options_layout.addWidget(self.skip_denied_check)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(14)
        layout.addWidget(metrics_card)
        layout.addWidget(warn_card)
        layout.addWidget(groups_card, 1)
        layout.addWidget(unknown_card)
        layout.addWidget(options_card)
        layout.addLayout(buttons)

        self.apply_button.clicked.connect(self.replan_requested.emit)
        self.elevate_button.clicked.connect(self.elevate_requested.emit)
        self.takeover_button.clicked.connect(self.takeover_requested.emit)
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

        self._info_values["source"].setText(str(plan.source))
        self._info_values["target"].setText(str(plan.target))
        self._info_values["free"].setText(human_bytes(plan.free_bytes))
        summary: list[tuple[str, bool]] = []     # (文本, 是否警示)
        if plan.link_groups:
            outside = sum(group.count for group in plan.link_groups)
            summary.append(
                (f"{outside} 个链接指向源树之外；默认保持原样，原路径照常可用。"
                 "只有那些目录原来的位置以后不再存在时，才需要在下方填写新路径。",
                 False))
        dangling = [link for link in plan.links if link.is_dangling]
        if dangling:
            shown = "、".join(link.rel for link in dangling[:3])
            if len(dangling) > 3:
                shown += "…"
            summary.append(
                (f"{len(dangling)} 条链接的目标现在就不存在（断链）：{shown}。"
                 "仍会原样重建，迁移后照样指不到东西。", True))
        lossy = lossy_attribute_text(plan.sparse_files, plan.hardlinked_files,
                                     plan.ads_files)
        if lossy:
            summary.append((f"复制方式的固有限制：{lossy}。", False))
        if plan.permission_issues:
            if already_elevated:
                summary.append(
                    (f"{len(plan.permission_issues)} 个目录即使提权也访问不了，"
                     "请检查这些路径是否存在或已被占用；"
                     "确实拿不到权限的，勾选下方「跳过无权限访问的目录」把它们排除掉。",
                     True))
            else:
                summary.append(
                    (f"{len(plan.permission_issues)} 个目录无权限访问，"
                     "请点「以管理员身份重试」；"
                     "确实拿不到权限的，勾选下方「跳过无权限访问的目录」把它们排除掉。",
                     True))
        if plan.excluded:
            shown = "、".join(plan.excluded[:3])
            if len(plan.excluded) > 3:
                shown += "…"
            summary.append(
                (f"已跳过 {len(plan.excluded)} 项（{shown}）："
                 "它们不会复制到目标，只存在于备份里。", False))
        self._symlink_blocked = bool(plan.file_symlinks) and not symlink_capable
        if self._symlink_blocked:
            if already_elevated:
                summary.append(
                    (f"{plan.file_symlinks} 个文件符号链接即使提权也建不起来，"
                     "请开启 Windows 开发者模式，或检查组策略里"
                     "「创建符号链接」权限有没有授予当前用户。", True))
            else:
                summary.append(
                    (f"{plan.file_symlinks} 个文件符号链接需要管理员特权才能重建"
                     "（目录链接会降级为 junction，不受影响）。"
                     "现在不处理，等到复制完才会失败——请点「以管理员身份重试」，"
                     "或开启 Windows 开发者模式。", True))
        if plan.unknown_reparse:
            if plan.skip_unknown_reparse:
                summary.append(
                    (f"{len(plan.unknown_reparse)} 个非链接类重解析点将被跳过，"
                     "它们只存在于备份中——删除备份前请确认不再需要。", False))
            else:
                summary.append(
                    (f"{len(plan.unknown_reparse)} 个非链接类重解析点将按真实内容"
                     "复制（云占位符会触发下载，可能很慢）。", False))
        if plan.target_not_empty:
            summary.append(("提示：目标目录已存在且非空，将按续传/合并处理。",
                            False))
        self._render_summary(summary)

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
                # 用**真正的输入框**而不是普通单元格：单元格没有任何视觉提示，
                # 第一次用的人根本不知道那里能填（真实反馈：「不仔细看根本看不到」）。
                editor = qf.LineEdit()
                editor.setPlaceholderText(MAPPING_PLACEHOLDER)
                editor.setText(mapped)
                editor.setToolTip(mapped)
                editor.textChanged.connect(
                    lambda text, r=row: self._on_mapping_edited(r, text))
                self.group_table.setCellWidget(row, MAPPING_COLUMN, editor)

                derived = QTableWidgetItem("")
                derived.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.group_table.setItem(row, DERIVED_COLUMN, derived)

            self._refresh_derived_targets()

            # 没有分组 = 没有链接可映射。空表配一个可点的按钮会让人以为按钮坏了，
            # 所以明说一句，并把按钮置灰。
            # 表格**整块隐藏**而不是留在那里配一行说明：表格会把卡片高度占满，
            # 说明文字被挤到可视区外，用户根本看不到（渲染实测过）。
            # 注意：勾选「跳过重解析点」是直接发信号触发重扫的，不走这个按钮，
            # 所以置灰不会挡住那条路径。
            has_groups = self.group_table.rowCount() > 0
            self.apply_button.setEnabled(has_groups)
            self.group_table.setVisible(has_groups)
            if has_groups:
                self.group_hint.hide()
            else:
                self.group_hint.setText(NO_GROUP_HINT)
                self.group_hint.show()
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
            editor = self.mapping_editor(row)
            if source_item is None or editor is None:
                continue
            source = source_item.text().strip()
            if not source:
                continue
            listed.add(source)
            target = editor.text().strip()
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

    def mapping_editor(self, row: int) -> "qf.LineEdit | None":
        """某一行的「重写为」输入框。"""
        return self.group_table.cellWidget(row, MAPPING_COLUMN)

    def derived_target_text(self, row: int) -> str:
        """某一行的「迁移后指向」——应用当前映射后，链接实际会指向哪里。"""
        item = self.group_table.item(row, DERIVED_COLUMN)
        return item.text() if item is not None else ""

    def group_caption_text(self) -> str:
        """归属表上方的说明文字（讲清前提，避免误用）。"""
        return self.group_caption.text()

    def unknown_caption_text(self) -> str:
        """非链接类重解析点卡片上的说明文字。"""
        return self.unknown_caption.text()

    def _on_mapping_edited(self, row: int, text: str) -> None:
        """用户在输入框里打字时记住它，否则重扫后照样丢。"""
        if self._loading:
            return
        prefix_item = self.group_table.item(row, 0)
        if prefix_item is not None:
            self._remember_mapping(prefix_item.text().strip(), text.strip())
        self._refresh_derived_targets()

    def _refresh_derived_targets(self) -> None:
        """把「迁移后指向」重算一遍。

        这是「点应用看不出变化」的根治：左边那列是分组前缀（原始指向）永远不变，
        「重写为」显示的是用户自己填的字也不变，所以原来点完按钮表格毫无动静。
        把结果单独摆一列，填完立刻能看见。
        """
        effective = self.mappings()
        for row in range(self.group_table.rowCount()):
            prefix_item = self.group_table.item(row, 0)
            item = self.group_table.item(row, DERIVED_COLUMN)
            if prefix_item is None or item is None:
                continue
            prefix = prefix_item.text().strip()
            target = effective.get(prefix) or prefix
            item.setText(target)
            item.setToolTip(target)
            # 映射没生效时用普通色，生效了才用强调色——一眼看出哪几行会变
            changed = bool(effective.get(prefix))
            item.setForeground(QBrush(chrome.pick_color(
                ACCENT_PAIR if changed else MUTED)))

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
        """(链接目标目录, 链接数, 重写为) —— 对外接口，实现随控件形态变化。"""
        rows: list[tuple[str, int, str]] = []
        for row in range(self.group_table.rowCount()):
            editor = self.mapping_editor(row)
            rows.append((self.group_table.item(row, 0).text(),
                         int(self.group_table.item(row, 1).text()),
                         editor.text() if editor is not None else ""))
        return rows

    def set_mapping(self, row: int, mapped_to: str) -> None:
        editor = self.mapping_editor(row)
        if editor is not None:
            editor.setText(mapped_to)
            # tooltip 同步，否则改完映射后悬停看到的还是旧值
            editor.setToolTip(mapped_to)
        prefix_item = self.group_table.item(row, 0)
        if prefix_item is not None:
            self._remember_mapping(prefix_item.text().strip(), mapped_to.strip())
            self._refresh_derived_targets()

    def metric_text(self, key: str) -> str:
        """读取某个指标的文本。

        注意：**绝不能把这个方法命名为 `metric`**。QWidget 继承自 QPaintDevice，
        `metric(PaintDeviceMetric)` 是 Qt 的虚函数，绘制系统靠它查询 DPI /
        设备像素比。用 Python 覆盖它会让每次绘制都走进这里、
        拿 PaintDeviceMetric 当 key 去查字典并抛 KeyError，直接导致控件无法绘制。
        """
        return self.metric_labels[key].text()

    def _render_summary(self, entries: list[tuple[str, bool]]) -> None:
        """重建提示条目区：每条一行（圆点 + 文字），警示条目用警示色圆点。

        旧实现把所有条目 setText 进同一个 label，换行后就是一整块密不透风
        的文字，条目之间没有呼吸感；逐条排版后扫读顺序清晰。
        `_summary_texts` 首部永远带上信息三行，`warning_text()` 的读法
        与旧版一致。
        """
        # 清空旧条目：widget 用 deleteLater，layout 里残留的空位也移除
        while self._hint_layout.count():
            item = self._hint_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._hints.clear()

        info = [f"源：{self._info_values['source'].text()}",
                f"目标：{self._info_values['target'].text()}",
                f"目标盘剩余：{self._info_values['free'].text()}"]
        texts = info + [text for text, _warn in entries]
        for text, warn in entries:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            dot = qf.CaptionLabel("●")
            dot.setStyleSheet("font-size: 7px;")
            dot.setTextColor(*(WARNING if warn else ACCENT_PAIR))
            dot.setFixedWidth(14)
            dot.setAlignment(Qt.AlignmentFlag.AlignVCenter |
                             Qt.AlignmentFlag.AlignHCenter)
            label = qf.BodyLabel(text)
            label.setWordWrap(True)
            row_layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(label, 1)
            self._hint_layout.addWidget(row)
            self._hints.append(row)
        self._summary_texts = texts

    def warning_text(self) -> str:
        """摘要全文（信息三行 + 提示条目），供测试与调试读取。"""
        return "\n".join(self._summary_texts)

    def group_hint_text(self) -> str:
        """归属表为空时的说明文字；有分组时返回空串。

        用 `isHidden()` 而不是 `isVisible()`：前者只反映显式的 hide/show，
        与父窗口有没有显示无关，测试里不需要真把窗口 show 出来。
        """
        return "" if self.group_hint.isHidden() else self.group_hint.text()

    def deep_verify(self) -> bool:
        return self.deep_check.isChecked()

    def unknown_count(self) -> int:
        return self.unknown_list.count()


