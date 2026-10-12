"""向导第四步：结果统计、未完成清单与收尾动作。

布局：顶部是「徽章 + 大标题 + 结论」的状态区，徽章用圆形语义色底把
成功/失败一眼锚定；统计用两行 `chrome.MetricRow` 指标网格（标签在上、
数值在下，竖发丝线分格），比一列「标签： 值」的表单清爽得多。
颜色一律从 `theme` 取（成功 `theme.SUCCESS` / 未完成 `theme.ERROR`），
本模块不出现任何颜色字面量（白色徽章图标除外——它画在实色底上）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QGridLayout, QFileDialog, QHBoxLayout,
                               QHeaderView, QMessageBox, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import qfluentwidgets as qf

from . import chrome, theme
from .format import human_bytes
from .report import (CSV_HEADER, KIND_DANGLING, TARGET_STATE_TEXT, backup_warning_text,
                     build_restore_rows, build_rows, link_rows, phase_text,
                     summarize, unfinished_rows, write_csv)

# (键, 面板上的中文标题)，顺序即展示顺序；网格按每行 4 格折行
STAT_ROWS = (("copied_files", "成功复制（个）"),
             ("copied_bytes", "成功复制（字节）"),
             ("skipped", "跳过（已存在）"),
             ("failed", "复制失败"),
             ("missing", "校验缺失"),
             ("mismatch", "校验不一致"),
             ("dangling", "断链（原样重建）"))

# 非零时要标红的统计项：它们是「出了问题」的计数
ERROR_KEYS = ("failed", "missing", "mismatch")

# 恢复态要交代的四件事。回滚之后用户关心的是「原路径回来了吗、目标端那份怎么样了」，
# 而统计网格讲的「搬过去多少」在那一刻已经没有意义。
RESTORE_ROWS = (("source", "源目录"),
                ("backup", "备份"),
                ("target", "目标端"),
                ("acl", "权限"))


class ReportPage(QWidget):
    """④ 报告页。"""

    rollback_requested = Signal()
    cleanup_requested = Signal()
    reverify_requested = Signal()
    discard_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outcome = None
        # 回滚之后的恢复结果；非 None 表示这一页此刻讲的是「恢复」而不是「迁移」。
        # 导出与按钮可用性都要看它，光看版面藏没藏会漏掉这两处。
        self._restored = None

        self.title_label = qf.DisplayLabel("-")
        # 状态徽章：圆形语义色底 + 白图标，成功/失败一眼可辨
        self.status_badge = chrome.StatusBadge(52)

        self.message_label = qf.BodyLabel("")
        self.message_label.setWordWrap(True)
        self.message_label.setTextColor(*theme.MUTED)
        # 运行日志的位置：失败现场往往已经变了（源被改名成备份、目标端着半成品），
        # 复盘全过程只能靠这份日志，所以摆在报告里而不是只留在终端输出里。
        self.log_label = qf.CaptionLabel("")
        self.log_label.setWordWrap(True)
        self.log_label.setTextColor(*theme.MUTED)
        self.log_label.hide()

        # 备份还在就警告别手删：手删之后源路径上那条链接成了唯一入口，回滚只能
        # 拒绝执行，而这个理由用户完全想不到（他只会觉得工具坏了）。摆在标题正
        # 下方，是这一页最显眼的位置。
        self.backup_warning = qf.BodyLabel("")
        self.backup_warning.setWordWrap(True)
        self.backup_warning.setTextColor(*theme.ERROR)
        self.backup_warning.hide()

        # 统计网格：MetricRow 的值标签直接挂进 stat_labels，
        # `summary_text()` 的读法与旧表单实现完全一致。
        self.metric_rows: list[chrome.MetricRow] = []
        self.stat_labels: dict[str, qf.BodyLabel] = {}
        stats_card = chrome.Card()
        stats_layout = QVBoxLayout(stats_card)
        stats_layout.setContentsMargins(20, 14, 20, 16)
        stats_layout.setSpacing(6)
        stats_layout.addWidget(chrome.make_section_header(
            "统计", chrome.fluent_icon("INFO")))
        for chunk in (STAT_ROWS[:4], STAT_ROWS[4:]):
            if not chunk:
                continue
            row = chrome.MetricRow([(title, "0") for _, title in chunk])
            self.metric_rows.append(row)
            for key, title in chunk:
                self.stat_labels[key] = row.value_widget(title)
            stats_layout.addWidget(row)

        # qf.TableWidget() 不接受行列数，行数用 setRowCount，列数用 setColumnCount
        self.table = qf.TableWidget()
        self.table.setColumnCount(len(CSV_HEADER))
        self.table.setRowCount(0)
        self.table.setHorizontalHeaderLabels(CSV_HEADER)
        self.table.verticalHeader().setDefaultSectionSize(36)
        # 只读表。qf.TableWidget 默认带 AnyKeyPressed：敲任意键就进编辑态，
        # 格子变成带清空按钮的输入框——报告里的字不该能改。
        self.table.setEditTriggers(qf.TableWidget.EditTrigger.NoEditTriggers)
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
        table_layout.setContentsMargins(20, 14, 20, 16)
        table_layout.setSpacing(10)
        table_layout.addWidget(chrome.make_section_header(
            "未完成清单", chrome.fluent_icon("DOCUMENT")))
        table_layout.addWidget(self.table, 1)

        # 恢复态的信息块。回滚之后这一页要回答的是另一组问题，所以整块换掉
        # 统计网格与未完成清单，而不是在同一处塞两套含义不同的数字。
        self.restore_card = chrome.Card()
        restore_layout = QVBoxLayout(self.restore_card)
        restore_layout.setContentsMargins(20, 14, 20, 16)
        restore_layout.setSpacing(10)
        restore_layout.addWidget(chrome.make_section_header(
            "恢复结果", chrome.fluent_icon("INFO")))
        self.restore_values: dict[str, qf.BodyLabel] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        for index, (key, caption) in enumerate(RESTORE_ROWS):
            label = qf.CaptionLabel(caption)
            label.setAlignment(Qt.AlignmentFlag.AlignTop)
            value = qf.BodyLabel("-")
            value.setWordWrap(True)
            self.restore_values[key] = value
            grid.addWidget(label, index, 0)
            grid.addWidget(value, index, 1)
        restore_layout.addLayout(grid)
        # 卡片本身带伸缩权重（它可见时得顶掉隐藏的清单卡片，否则多出来的空间
        # 会把标题行撑高、徽章和标题分家）；但里面的四行要贴顶，所以尾部再收一下。
        restore_layout.addStretch(1)
        self.restore_card.hide()
        # 恢复态要藏起来的两块，留引用
        self.stats_card = stats_card
        self.table_card = table_card

        # 破坏性动作用普通按钮，不抢主色；按钮做扎实一点便于点击
        self.export_button = qf.PushButton("导出 CSV")
        self.reverify_button = qf.PushButton("重新校验")
        self.discard_button = qf.PushButton("删除目标")
        self.rollback_button = qf.PushButton("一键回滚")
        self.cleanup_button = qf.PushButton("删除备份")
        for btn in (self.export_button, self.reverify_button,
                    self.discard_button, self.rollback_button,
                    self.cleanup_button):
            btn.setMinimumWidth(108)
            btn.setMinimumHeight(34)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(10)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.reverify_button)
        buttons.addStretch(1)
        buttons.addWidget(self.discard_button)
        buttons.addWidget(self.rollback_button)
        buttons.addWidget(self.cleanup_button)

        # 状态区：徽章 + 标题，结论文字压在标题下面
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 4, 0, 0)
        title_row.setSpacing(16)
        title_row.addWidget(self.status_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        title_stack = QVBoxLayout()
        title_stack.setContentsMargins(0, 0, 0, 0)
        title_stack.setSpacing(2)
        title_stack.addWidget(self.title_label)
        title_stack.addWidget(self.message_label)
        title_row.addLayout(title_stack, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(14)
        layout.addLayout(title_row)
        layout.addWidget(self.backup_warning)
        layout.addWidget(stats_card)
        layout.addWidget(self.restore_card, 1)
        layout.addWidget(self.log_label)
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
        self._restored = None
        # 从恢复态回到迁移态：`load` 是「这一趟迁移的结果」的入口，
        # 上一次回滚留下的恢复版面必须收掉。
        self.restore_card.hide()
        self.stats_card.show()
        self.table_card.show()
        self.reverify_button.setEnabled(True)
        stats = summarize(outcome)
        ok = bool(outcome.ok)
        self.title_label.setText("迁移成功" if ok else
                                 f"未完成：{phase_text(outcome.phase)}")
        # 成功/未完成用库与 theme 的语义色区分，深浅模式各一个值
        self.title_label.setTextColor(*(theme.SUCCESS if ok else theme.ERROR))
        self.status_badge.set_state(ok)
        for key in self.stat_labels:
            value = stats[key]
            text = human_bytes(value) if key == "copied_bytes" else f"{value:,}"
            label = self.stat_labels[key]
            label.setText(text)
            # 错误计数非零标红提醒；为零或常规项回到主题默认色
            if key in ERROR_KEYS and value > 0:
                label.setTextColor(*theme.ERROR)
            else:
                label.setTextColor()

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
        self.set_backup_warning(getattr(outcome, "backup_path", None))
        log_path = getattr(outcome, "log_path", "") or ""
        self.log_label.setText(f"运行日志：{log_path}" if log_path else "")
        self.log_label.setVisible(bool(log_path))
        has_backup = bool(getattr(outcome, "backup_path", None))
        # 没成功又没备份 = 源目录完好、目标端留着半成品，这正是需要清理出口的现场
        self.set_actions_enabled(has_backup, has_backup,
                                 discard=not ok and not has_backup)

    def load_restored(self, outcome) -> None:
        """把这一页从「迁移结果」切到「恢复结果」。

        回滚成功之后继续顶着「迁移成功」是错的：那件事已经被撤销了。这一页要
        回答的变成了「原路径回来了吗、目标端那份怎么样了」——统计网格里的复制数、
        校验缺失数在那一刻都没有意义，未完成清单也没有可列的东西。

        按钮由调用方（窗口）另行设置：它才知道目标端还在不在。
        """
        ok = bool(getattr(outcome, "ok", False))
        self._restored = outcome
        self.title_label.setText("恢复成功" if ok else "恢复未完成")
        self.title_label.setTextColor(*(theme.SUCCESS if ok else theme.ERROR))
        self.status_badge.set_state(ok)
        self.message_label.setText(getattr(outcome, "message", "") or "")

        # 备份没了（回滚会把它改回原名），那条「请勿删除备份」的警告要连文字一起
        # 清掉，不能只 hide：留着旧文字等于把一份过期的告警藏在版面底下。
        self.set_backup_warning("")
        self.stats_card.hide()
        self.table_card.hide()
        self.table.setRowCount(0)
        self.restore_card.show()

        source = getattr(outcome, "source", "") or ""
        restored = bool(getattr(outcome, "source_restored", False))
        self.restore_values["source"].setText(
            f"{source}（已恢复为真实目录）" if restored and source else source or "-")

        backup = getattr(outcome, "backup_path", "") or ""
        self.restore_values["backup"].setText(
            f"{backup}（已改回原名）" if backup else "无")

        target = getattr(outcome, "target", "") or ""
        state = TARGET_STATE_TEXT.get(getattr(outcome, "target_state", ""), "")
        note = getattr(outcome, "target_note", "") or ""
        detail = "；".join(part for part in (state, note) if part)
        self.restore_values["target"].setText(
            f"{target} —— {detail}" if target and detail
            else (target or detail or "-"))

        acl = getattr(outcome, "acl_note", "") or ""
        self.restore_values["acl"].setText(acl or "未改动（本次没有接管过权限）")
        # 统计标签不能留着上一次的数值：卡片虽然藏起来了，summary_text() 这个
        # 读数口还会读到一份和当前版面无关的旧数据。
        for label in self.stat_labels.values():
            label.setText("-")
        # 恢复之后没有「迁移结果」可校验了：那一刻源是刚还原回来的原件、目标端
        # 是清空或半残的副本，两边早已不是同一次搬迁的两端，比出来的全是假的缺失。
        self.reverify_button.setEnabled(False)

    def is_showing_restore(self) -> bool:
        return self.restore_card.isVisibleTo(self)

    def restore_text(self, key: str) -> str:
        return self.restore_values[key].text()

    def stats_visible(self) -> bool:
        return self.stats_card.isVisibleTo(self)

    def table_visible(self) -> bool:
        return self.table_card.isVisibleTo(self)

    def set_actions_enabled(self, rollback: bool, cleanup: bool,
                            discard: bool = False) -> None:
        self.rollback_button.setEnabled(rollback)
        self.cleanup_button.setEnabled(cleanup)
        self.discard_button.setEnabled(discard)

    def set_discard_enabled(self, enabled: bool) -> None:
        """只动「删除目标」这一个按钮。

        删掉目标端之后要把它关掉：再点一次只会得到「目标目录不存在，无需清理」
        这种自相矛盾的提示。用 set_actions_enabled 会把另外两个按钮一起重置，
        而它们此时的状态是对的，不该被牵连。
        """
        self.discard_button.setEnabled(enabled)

    def summary_text(self, key: str) -> str:
        return self.stat_labels[key].text()

    def rows(self) -> list[list[str]]:
        result: list[list[str]] = []
        for row in range(self.table.rowCount()):
            result.append([self.table.item(row, column).text()
                           for column in range(len(CSV_HEADER))])
        return result

    def export_csv(self, path) -> None:
        """导出当前**屏幕上这份**报告。

        恢复态导出的是恢复信息，不是迁移报告：那一页讲的事已经被撤销了。
        界面写着「恢复成功」却导出一份「迁移报告」，拿到文件的人会以为
        迁移还在生效。
        """
        if self._restored is not None:
            write_csv(path, build_restore_rows(self._restored))
            return
        rows = build_rows(self._outcome) if self._outcome is not None \
            else [list(CSV_HEADER)]
        write_csv(path, rows)

    def title_text(self) -> str:
        return self.title_label.text()

    def message_text(self) -> str:
        return self.message_label.text()

    def backup_warning_text(self) -> str:
        """「别在不确定的情况下删备份」的警告正文；没有备份时为空串。"""
        return "" if self.backup_warning.isHidden() else self.backup_warning.text()

    def set_backup_warning(self, backup_path) -> None:
        """按「现在还有没有备份」刷新警告。

        删除备份成功之后必须再调一次（传空）：否则界面会继续喊「请勿删除备份」，
        而那个目录早就不在了——警告指向一个不存在的东西比没有警告更糟，
        用户会以为删除没生效、或者以为还有退路。
        """
        warning = backup_warning_text(backup_path)
        self.backup_warning.setText(warning)
        self.backup_warning.setVisible(bool(warning))

    def _on_export_clicked(self) -> None:
        chosen, _filter = QFileDialog.getSaveFileName(self, "导出报告",
                                                      "迁移报告.csv",
                                                      "CSV 文件 (*.csv)")
        if not chosen:
            return
        # 必须兜住：写入会因目录不存在、只读、磁盘满、文件正被 Excel 打开而失败。
        # 原来裸调 export_csv，异常逃出 Qt 槽函数——应用不崩（只把 traceback
        # 打到 stderr），但界面上什么都不显示，用户点了「导出 CSV」毫无反应。
        # 这里刻意 catch 宽泛的 Exception：写入路径还可能抛 ValueError（路径含空字节）
        # 之类非 OSError 的异常，槽函数里绝不能让任何东西逃进 Qt 事件循环。
        try:
            self.export_csv(chosen)
        except Exception as exc:
            reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
            self.message_label.setText(f"导出失败：{reason}")
            QMessageBox.warning(self, "导出失败",
                                f"没能写入：\n{chosen}\n\n{reason}\n\n"
                                "换个可写的目录，或先关掉正在打开这个文件的程序。")
            return
        self.message_label.setText(f"报告已导出：{chosen}")



