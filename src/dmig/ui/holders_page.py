"""占用进程页：找出占着源目录、导致改名失败的程序，并（在护栏内）关掉它们。

**为什么单独一页**：它是个有副作用的排查流程（试探改名会让条目一闪、关闭程序
可能丢未保存的内容），塞在第一步的提示行里说不清楚，也放不下「谁占着、能不能
关、为什么不能关」这些必须交代的信息。

**入口只在第一步检验因改名失败时才出现**——检验通过时用不到这一页。

**这里有个不能绕的约束**：`RmShutdown` 是整批语义，它关掉的是「所有正在使用
已注册资源的进程」，RestartManager 也不提供「哪个文件被哪个进程占」的归属。
所以「勾选要关哪几个」是个做不出来的承诺，这一页**不摆勾选框**，只列出将关闭
的清单；想少关几个，就自己先手动关掉再重新扫描。护栏那一层在 core 里：批次中
只要混进一个受保护进程，就一个都不关。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import qfluentwidgets as qf

from . import chrome
from .theme import ERROR, MUTED, SUCCESS, error_color, warning_color

HOLDER_HEADERS = ["进程", "PID", "可执行文件", "状态"]

# 页面的几个状态。测试与窗口都靠它判断现在该显示什么。
SCANNING, HOLDERS, NONE, CLOSING, DONE = (
    "scanning", "holders", "none", "closing", "done")

INTRO = ("这一页找出占着源目录、导致「源目录无法改名」的程序。\n"
         "改名是迁移的最后一步，任何句柄都会挡住它——所以要在开始之前解决。")
PROBE_NOTE = ("接下来会逐个试探源目录的顶层条目（每个都会改名再立刻改回，"
              "因此开着这些目录的程序可能看到它们一闪，属正常现象）。")
CLOSE_WARNING = ("关闭程序会让其中未保存的内容丢失。关闭前请确认那些程序里"
                 "没有没保存的东西。")


class HoldersPage(QWidget):
    """占用进程页。只发意图，实际的扫描/关闭由窗口去跑（和别的页面一致）。"""

    back_requested = Signal()
    scan_requested = Signal()
    close_requested = Signal()
    force_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = SCANNING
        self._note_color = warning_color()

        title = qf.TitleLabel("找出占用进程")
        intro = qf.BodyLabel(INTRO)
        intro.setWordWrap(True)
        intro.setTextColor(*MUTED)

        # ① 扫描区
        self.stage_label = qf.BodyLabel("")
        self.stage_label.setWordWrap(True)
        self.bar = qf.ProgressBar(None, useAni=False)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.probe_note = qf.CaptionLabel(PROBE_NOTE)
        self.probe_note.setWordWrap(True)
        self.probe_note.setTextColor(*MUTED)
        self.rescan_button = qf.PushButton("重新扫描")
        self.rescan_button.setMinimumHeight(32)
        self.rescan_button.clicked.connect(self.scan_requested.emit)

        scan_header = QHBoxLayout()
        scan_header.setContentsMargins(0, 0, 0, 0)
        scan_header.addWidget(chrome.make_section_header(
            "扫描", chrome.fluent_icon("SEARCH")))
        scan_header.addStretch(1)
        scan_header.addWidget(self.rescan_button)

        self.scan_card = chrome.Card()
        scan_layout = QVBoxLayout(self.scan_card)
        scan_layout.setContentsMargins(20, 14, 20, 16)
        scan_layout.setSpacing(10)
        scan_layout.addLayout(scan_header)
        scan_layout.addWidget(self.stage_label)
        scan_layout.addWidget(self.bar)
        scan_layout.addWidget(self.probe_note)

        # ② 结论 + 占用者清单
        self.verdict = qf.BodyLabel("")
        self.verdict.setWordWrap(True)
        self.verdict.hide()

        self.table = qf.TableWidget()
        self.table.setColumnCount(len(HOLDER_HEADERS))
        self.table.setRowCount(0)
        self.table.setHorizontalHeaderLabels(HOLDER_HEADERS)
        # 这张表是只读的。qf.TableWidget 默认带 AnyKeyPressed：敲任意一个键就进
        # 编辑态（格子变成一个带清空按钮的输入框），看着像坏了。
        self.table.setEditTriggers(qf.TableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(36)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        # 不能关的那些，理由单独成段说清楚——不能只把表格里标灰了事
        self.note = qf.BodyLabel("")
        self.note.setWordWrap(True)
        self.note.setTextColor(*warning_color())
        self.note.hide()

        self.close_button = qf.PrimaryPushButton("关闭这些程序")
        self.close_button.setMinimumHeight(34)
        self.close_button.clicked.connect(self.close_requested.emit)
        self.force_button = qf.PushButton("强制结束")
        self.force_button.setMinimumHeight(34)
        self.force_button.clicked.connect(self.force_requested.emit)
        self.force_button.hide()
        self.close_warning = qf.CaptionLabel(CLOSE_WARNING)
        self.close_warning.setWordWrap(True)
        self.close_warning.setTextColor(*MUTED)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        actions.addWidget(self.close_button)
        actions.addWidget(self.force_button)
        actions.addWidget(self.close_warning, 1)

        self.holders_card = chrome.Card()
        holders_layout = QVBoxLayout(self.holders_card)
        holders_layout.setContentsMargins(20, 14, 20, 16)
        holders_layout.setSpacing(10)
        holders_layout.addWidget(chrome.make_section_header(
            "占用者", chrome.fluent_icon("PEOPLE")))
        holders_layout.addWidget(self.verdict)
        holders_layout.addWidget(self.table, 1)
        holders_layout.addWidget(self.note)
        holders_layout.addLayout(actions)

        self.back_button = qf.PushButton("← 返回选择目录")
        self.back_button.setMinimumHeight(34)
        self.back_button.clicked.connect(self.back_requested.emit)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self.back_button)
        bottom.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addWidget(self.scan_card)
        layout.addWidget(self.holders_card, 1)
        layout.addLayout(bottom)

        self.set_scanning("准备扫描…")

    # --- 状态切换 ---------------------------------------------------

    def set_scanning(self, stage: str) -> None:
        self._state = SCANNING
        self._note_color = warning_color()
        self.stage_label.setText(stage)
        self.bar.show()
        self.probe_note.show()
        self.rescan_button.setEnabled(False)
        self.verdict.hide()
        self.table.setRowCount(0)
        self.table.hide()
        self.note.hide()
        self.close_button.hide()
        self.force_button.hide()
        self.close_warning.hide()

    def set_scan_progress(self, done: int, total: int) -> None:
        """第二级（RestartManager 全扫）的进度。"""
        self.bar.setValue(int(done * 100 / total) if total else 100)
        self.stage_label.setText(f"正在扫描占用：{done:,} / {total:,} 个文件")

    def set_probe_progress(self, done: int, total: int) -> None:
        """第一级（逐个顶层条目试探）的进度。"""
        self.bar.setValue(int(done * 100 / total) if total else 100)
        self.stage_label.setText(f"正在缩小范围：{done} / {total} 个顶层条目")

    def show_holders(self, holders, names=(), note: str = "") -> None:
        """找到占用者。受保护的那些照列，但标出来并写明理由。

        `names` 为空可能是两种情况：整棵树扫的兜底（试探没定位到条目），
        或者顶层条目太多、跳过了试探那一级——后者由 `note` 说明，不能让用户
        以为扫描没做成。
        """
        self._state = HOLDERS
        self.bar.hide()
        self.probe_note.hide()
        self.rescan_button.setEnabled(True)
        self.verdict.setText(
            f"在这 {len(holders)} 个程序里发现了占用源目录的句柄。"
            if holders else "")
        self.verdict.setTextColor(*MUTED)
        self.verdict.setVisible(bool(holders))

        closeable = [h for h in holders if h.can_close]
        protected = [h for h in holders if not h.can_close]

        self.table.setRowCount(0)
        self.table.show()
        for holder in holders:
            row = self.table.rowCount()
            self.table.insertRow(row)
            cells = [holder.name, str(holder.pid), holder.exe or "（取不到）",
                     "可由本工具关闭" if holder.can_close else "不关闭"]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if not holder.can_close:
                    item.setForeground(QBrush(QColor(error_color())))
                self.table.setItem(row, column, item)

        # 说明行承担两件事，可能同时出现：
        #   · note —— 扫描本身的交代（例如「顶层条目太多，跳过了逐个试探」）
        #   · 受保护进程的警告 —— 这条是警告色，不能和普通说明混成一种
        # 两者都要说清，顺序上先交代扫描、再讲哪些不能关。
        blocks = []
        if note:
            blocks.append(note)
        if protected:
            lines = [f"{h.name} ({h.pid})：{h.protected_reason}"
                     for h in protected]
            blocks.append("下面这些程序本工具不会关闭——请自己处理，然后重新扫描：\n"
                          + "\n".join(lines))
        if blocks:
            self.note.setText("\n\n".join(blocks))
            # 颜色必须在这里重设：`show_no_holder` 把它改成了 MUTED，不设的话
            # 「不会关闭」这段警告会以灰色显示，看不出是警告。
            color = warning_color() if protected else MUTED[0]
            self.note.setTextColor(*warning_color() if protected else MUTED)
            self._note_color = color
            self.note.show()
        else:
            self.note.hide()

        self.close_button.setVisible(bool(closeable))
        self.close_button.setText(f"关闭这 {len(closeable)} 个程序"
                                 if closeable else "关闭这些程序")
        self.close_button.setEnabled(bool(closeable))
        self.force_button.hide()
        self.close_warning.setVisible(bool(closeable))
        # 阶段文字**无条件**重设，而且必须是**短状态行**：只在 names 非空时才写，
        # 会让「正在扫描占用：412/5000 个文件」那种中间态留在卡片上（names 为空
        # 是可能的：试探没定位到条目，或顶层条目太多跳过了试探那一级）。
        # 跳过试探的**原因**是句子，归说明行，不塞进这里。
        self.stage_label.setText(
            "被占用的顶层条目：" + "、".join(names) if names else "扫描完成")

    def show_no_holder(self, names=(), note: str = "") -> None:
        """试探改名失败，但 RM 查不出任何进程——如实说，并点名最可能的原因。"""
        self._state = NONE
        self.bar.hide()
        self.probe_note.hide()
        self.rescan_button.setEnabled(True)
        self.table.hide()
        self.close_button.hide()
        self.force_button.hide()
        self.close_warning.hide()
        self.verdict.show()
        self.verdict.setTextColor(*error_color())
        self.verdict.setText(
            "源目录改不了名，但没有查出任何占用它的进程。")
        detail = note or (
            "最可能的原因是：有程序把「源目录本身」当作工作目录——\n"
            "· 资源管理器正开着这个目录（或它的父目录）\n"
            "· 有个终端 cd 在里面\n"
            "这类占用拿不到具体进程，请手动关掉它们，再点「重新扫描」。")
        if names:
            detail = f"被挡住的顶层条目：{'、'.join(names)}\n\n{detail}"
        self.note.setText(detail)
        self.note.setTextColor(*MUTED)
        self._note_color = MUTED[0]
        self.note.show()
        # 阶段文字只陈述**两种情况都成立**的事实。写「范围已缩小」在「顶层条目
        # 太多、跳过试探直接扫整棵树」那一支上就是假话——而那正是 note 要解释的
        # 情况，两张卡片会自相矛盾。缩没缩范围、扫到哪一步，note 里说。
        self.stage_label.setText("没查到具体进程")

    def show_closing(self, text: str) -> None:
        self._state = CLOSING
        self.stage_label.setText(text)
        self.bar.show()
        self.bar.setValue(0)
        self.rescan_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.force_button.setEnabled(False)

    def show_closed(self, ok: bool, text: str, *, can_force: bool = False) -> None:
        """关闭动作结束（结果另行复验）后的结论。"""
        self._state = DONE
        self.bar.hide()
        self.probe_note.hide()
        self.rescan_button.setEnabled(True)
        self.close_button.setEnabled(True)
        # 阶段文字必须换掉：「正在强制结束占用程序…」留在那儿，用户会以为还没完
        self.stage_label.setText("占用已解除" if ok else "仍有占用，见下方说明")
        self.verdict.show()
        self.verdict.setTextColor(*(SUCCESS if ok else ERROR))
        self.verdict.setText(text)
        self.note.setText("")
        self.note.hide()
        # 优雅关闭没成，才给强制这条路——它是单独一步、单独确认
        self.force_button.setVisible(can_force)
        self.force_button.setEnabled(can_force)
        self.close_warning.setVisible(can_force)
        if ok:
            # 与 show_result 保持一致：成功了就不能再留着已死进程的清单。
            # 窗口目前只传 False，但留这条分叉就是把同一个坑埋在另一个入口——
            # 迟早有人从这儿走一次，然后看到「已解除」底下还列着刚关掉的程序。
            self.table.setRowCount(0)

    def show_result(self, ok: bool, text: str) -> None:
        """重新检验之后的结论：占用到底解除了没有。

        这里必须把**关闭阶段留下的痕迹全部收掉**——进度条、阶段文字、被禁用的
        「重新扫描」、以及已经被关掉那些进程的旧清单。原来只设了一句结论，界面
        就卡在「正在强制结束占用程序…」上、重新扫描还是灰的：用户既看不出结果
        也动不了（实测踩到，反馈很差）。
        """
        self._state = DONE
        self.bar.hide()
        self.probe_note.hide()
        self.stage_label.setText("占用已解除" if ok else "仍有占用，见下方说明")
        self.rescan_button.setEnabled(True)
        self.verdict.show()
        self.verdict.setTextColor(*(SUCCESS if ok else ERROR))
        self.verdict.setText(text)
        self.force_button.hide()
        self.close_warning.hide()
        self.close_button.setVisible(not ok)
        self.close_button.setEnabled(not ok)
        if ok:
            # 已经关掉的进程再列一遍，用户会以为还要再关一次
            self.table.setRowCount(0)
            self.note.setText("")
            self.note.hide()

    # --- 读取口（测试与窗口用）--------------------------------------

    def state(self) -> str:
        return self._state

    def stage_text(self) -> str:
        return self.stage_label.text()

    def verdict_text(self) -> str:
        return "" if self.verdict.isHidden() else self.verdict.text()

    def note_text(self) -> str:
        return "" if self.note.isHidden() else self.note.text()

    def note_color(self) -> str:
        """说明行当前用的颜色。

        `BodyLabel` 只有 `setTextColor` 没有读取口，所以这里自己记一份——
        这条断言盯的是「警告别被上一屏的灰色带过去」，没有读取口就测不了。
        """
        return self._note_color

    def holder_rows(self) -> list[list[str]]:
        return [[self.table.item(row, column).text()
                 for column in range(self.table.columnCount())]
                for row in range(self.table.rowCount())]

    def close_label(self) -> str:
        return self.close_button.text()

    def can_force(self) -> bool:
        return not self.force_button.isHidden()


