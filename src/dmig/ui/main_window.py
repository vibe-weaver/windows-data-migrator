"""主窗口：四步向导的装配与线程调度。

窗口基类换成 `FluentWindow`，外壳（品牌栏 / 步骤条 / 内容区 / 操作栏）交给
`chrome`。三条实测结论直接决定了这里的写法：

1. `theme.apply_theme()` **必须在任何页面/控件构造之前**调用。该库的主题是
   逐个控件注册样式的（`common/style_sheet.py` 遍历 `styleSheetManager` 重刷），
   `app.styleSheet()` 始终为空；顺序错了就会出现「控件文字变白、背景还是白的」。
2. 必须 `setMicaEffectEnabled(False)`：Mica 开着时窗口背景跟随**操作系统**主题
   而不是库的主题，强制主题与系统不符时就是白底白字，浅色机器上也没法验证深色。
3. 背景用 `setCustomBackgroundColor` 显式指定库自带的浅/深背景色
   （`theme.WINDOW_BACKGROUND`），不自造配色。
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QElapsedTimer, Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QMessageBox,
                               QStackedWidget, QVBoxLayout, QWidget)

import qfluentwidgets as qf

from ..core import guard
from ..core.migrator import Migrator, RestoreOutcome, awaiting_source_link
from ..core.session import Session, default_root
from . import chrome, logo, theme
from .holders_page import HoldersPage
from .plan_page import PlanPage, mapping_summary
from .progress_page import ProgressPage
from .report_page import ReportPage
from .select_page import SelectPage
from .shell import parse_process_label, process_image_path, relaunch_elevated
from .workers import (HolderScanWorker, MigrateWorker, PlanWorker,
                      ShutdownWorker, TakeoverWorker, VerifyWorker)

PAGE_SELECT = 0
PAGE_PLAN = 1
PAGE_PROGRESS = 2
PAGE_REPORT = 3
# 第一步的子流程页面，不是向导的第 5 步（步骤条仍高亮第 1 步）
PAGE_HOLDERS = 4

# 会话停在改名之后时，下一步做的事已经不是「扫描」而是补建源目录链接
LINK_ONLY_BUTTON = "补建源目录链接"


def _confirmed(answer) -> bool:
    """用户在确认框里是否点了「是」。

    **必须用 `==` 比较，绝不能用 `is`。** 真实 `QMessageBox` 的静态方法返回的是
    普通 Python `int`（「是」是 16384），不是 `StandardButton.Yes` 枚举成员；
    `answer is StandardButton.Yes` 因此**恒为假**。

    这个坑的实际后果是：删备份、一键回滚、删除目标、迁移中关窗确认——
    用户点了「是」也一律被当成「否」，按钮点了毫无反应，而且**测试抓不到**：
    测试替身返回的是枚举成员，`is` 在替身上成立、在真货上失效。
    """
    return answer == QMessageBox.StandardButton.Yes


def _confirm_with_option(parent, title: str, text: str, option_text: str,
                         *, default: bool = False) -> tuple[bool, bool]:
    """带一个勾选项的确认框，返回 (是否确认, 是否勾选)。

    必须**构造** QMessageBox 实例：静态方法（warning/information）返回的是答复，
    挂不上勾选框（`setCheckBox` 只有实例方法）。注意实例的 `exec()` 同样返回
    普通 int，所以仍然要走 `_confirmed` 比较。

    默认不勾：勾选项代表一个不可逆的销毁动作，默认勾上等于替用户做了决定。
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Yes
                           | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    check = QCheckBox(option_text)
    check.setChecked(default)
    box.setCheckBox(check)
    answer = box.exec()
    return _confirmed(answer), bool(check.isChecked())


def default_migrator_factory(source, target, mappings, deep_verify, session_root,
                             skip_unknown_reparse=False, excludes=()):
    return Migrator(source, target, mappings=mappings, deep_verify=deep_verify,
                    session_root=session_root,
                    skip_unknown_reparse=skip_unknown_reparse,
                    excludes=excludes)


class StatusLine(QWidget):
    """底部状态行，替代 `QStatusBar`。

    `QStatusBar` 是原生 Qt 控件，不参与 Fluent 的逐控件主题注册，深色模式下
    它仍是默认的黑色文字，落在窗口的深色背景上就是一行看不见的字。这里换成
    Fluent 的 `CaptionLabel`，颜色由库的主题负责。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 0, 24, 10)
        row.setSpacing(0)
        self._label = qf.CaptionLabel("")
        row.addWidget(self._label)
        row.addStretch(1)

    def showMessage(self, message: str) -> None:      # noqa: N802 - 对齐 QStatusBar
        self._label.setText(message)

    def text(self) -> str:
        return self._label.text()


class MainWindow(qf.FluentWindow):
    """四步向导：选择 → 计划 → 执行 → 报告。"""

    def __init__(self, migrator_factory=None, session_id=None, parent=None):
        # 主题必须在任何控件之前落定：控件是在构造时注册样式的。
        # 深色已移除，apply_theme 内部恒定应用浅色。
        theme.apply_theme("light")
        super().__init__(parent)

        # 关掉 Mica，并显式指定背景色，让窗口背景跟随库的主题而非系统主题。
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(*theme.WINDOW_BACKGROUND)

        # 窗口标题带上副标题：应用名本身看不出是干什么的，
        # 而任务栏悬停与 Alt-Tab 只显示这一行。
        self.setWindowTitle(f"{logo.NAME} · {logo.TAGLINE}")
        # 任务栏与 Alt-Tab 的图标：能拿到打包的 app.ico 就用它，否则现画一枚
        self.setWindowIcon(logo.app_icon())
        self.resize(1000, 740)

        # 顶部自定义标题栏已经显示窗口标题，品牌栏里又有一遍应用名——
        # 藏掉标题栏文字只留窗控按钮，避免应用名出现两次。
        try:
            self.titleBar.titleLabel.hide()
        except Exception:
            pass

        # 窗口背景与控件主题强制同步。库的背景层走 120ms QPropertyAnimation
        # 过渡，主题切换时控件样式立即重刷、背景却要等动画跑完——动画一旦
        # 没有正常完成，就是「深色控件 + 浅色窗口」的混搭，浅底上全是看不清
        # 的白字。themeChangedFinished 在样式表全部刷完后发出，这里直接赋值
        # （不走动画），任何时刻背景都与控件一致。
        qf.qconfig.themeChangedFinished.connect(self._sync_background)
        self._title_hidden = True

        # 本应用是线性向导，不需要左侧导航栏。
        self.navigationInterface.hide()

        self._factory = migrator_factory or default_migrator_factory
        self._migrator = None
        self._worker = None
        self._outcome = None
        # 重新校验的比对基准与结果；基准在迁移成功后是备份而不是源
        self._reverify_base = ""
        self._reverify_result = None
        self._elevate_session = None
        self._restored_session = None
        # 迁移耗时在界面侧计时：core 里没有耗时字段，也不该为一个显示值去改它
        self._migration_timer = QElapsedTimer()
        # 迁移是否已跑完。用来让步骤条把「执行迁移」打成已完成，
        # 否则完成后步骤条还停在「进行中」，和页面上的结论自相矛盾。
        self._migration_finished = False
        # 这一趟扫描是不是用户按「应用映射并重新扫描」触发的。
        # 重扫快得看不见（实测 0.03 秒），所以结束后要主动说明应用了什么。
        self._replan_pending = False

        self.select_page = SelectPage()
        self.plan_page = PlanPage()
        self.progress_page = ProgressPage()
        self.report_page = ReportPage()
        # 占用进程页不是向导的第 5 步，而是第一步的子流程：从「改名被挡住」
        # 那条红线跳过去，办完再回来。所以它排在最后，步骤条仍高亮第 1 步。
        self.holders_page = HoldersPage()

        self.stack = QStackedWidget()
        # 四个页面统一套透明滚动容器：内容超出视口时上下滚动，
        # 卡片不会被压成细条、复选框与按钮也不会溢出到窗口外。
        for page in (self.select_page, self.plan_page,
                     self.progress_page, self.report_page, self.holders_page):
            self.stack.addWidget(self._wrap_scroll(page))

        self.brand_bar = chrome.BrandBar()
        self.step_bar = chrome.StepBar(["选择目录", "确认计划", "执行迁移", "完成"])

        self.back_button = qf.PushButton("上一步")
        self.next_button = qf.PrimaryPushButton("下一步")
        for btn in (self.back_button, self.next_button):
            btn.setMinimumWidth(108)
            btn.setMinimumHeight(34)
        self.action_bar = chrome.ActionBar()
        self.action_bar.add_left(self.back_button)
        self.action_bar.add_right(self.next_button)
        self.status_line = StatusLine()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.brand_bar)
        layout.addWidget(self.step_bar)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.action_bar)
        layout.addWidget(self.status_line)

        # 库自带的 stackedWidget 是 QFrame 而不是 QStackedWidget，用不上；
        # 隐藏后把自建容器放进同一个 widgetLayout（它已经预留了标题栏的 48px）。
        self.stackedWidget.hide()
        self.widgetLayout.addWidget(central, 1)

        self.next_button.clicked.connect(self.go_next)
        self.back_button.clicked.connect(self.go_back)
        self.select_page.changed.connect(self._update_buttons)
        # 「开始检验」：前置校验 + 试探改名。试探失败是 error 级，
        # 会经 has_blocking_error() 把「开始扫描」按住。
        self.select_page.check_requested.connect(self.run_select_check)
        # 改名被挡住时的专用通道 -> 占用进程页
        self.select_page.holders_requested.connect(self.show_holders_page)
        self.holders_page.back_requested.connect(self.leave_holders_page)
        self.holders_page.scan_requested.connect(self.scan_holders)
        self.holders_page.close_requested.connect(
            lambda: self.shutdown_holders(force=False))
        self.holders_page.force_requested.connect(
            lambda: self.shutdown_holders(force=True))
        self.plan_page.replan_requested.connect(self.replan)
        self.plan_page.elevate_requested.connect(self.restart_elevated)
        self.plan_page.takeover_requested.connect(self.take_over_denied)
        self.progress_page.pause_toggled.connect(self._on_pause_toggled)
        self.progress_page.cancel_requested.connect(self.cancel_migration)
        self.progress_page.locate_requested.connect(self.locate_process)
        self.report_page.rollback_requested.connect(self.rollback)
        self.report_page.cleanup_requested.connect(self.cleanup_backup)
        self.report_page.discard_requested.connect(self.discard_target)
        self.report_page.reverify_requested.connect(self.reverify)

        if session_id:
            self.restore_session(session_id)
        self._update_buttons()

    def statusBar(self) -> StatusLine:                # noqa: N802 - 对齐 QMainWindow
        """保留原来的 `statusBar().showMessage(...)` 调用点。"""
        return self.status_line

    def _sync_background(self) -> None:
        """主题切换完成后把窗口背景直接设为目标色（跳过动画）。"""
        try:
            self.setBackgroundColor(self._normalBackgroundColor())
        except Exception:
            pass

    def showEvent(self, event) -> None:              # noqa: N802 - Qt 命名
        # 首次/再次显示时兜底同步一次：构造期若主题判定与显示期不一致，
        # 背景层可能停在旧色，这里保证可见时背景永远与控件同主题。
        super().showEvent(event)
        self._sync_background()

    @staticmethod
    def _wrap_scroll(page: QWidget) -> qf.ScrollArea:
        """把页面套进透明背景的滚动容器，内容超出视口时可上下滚动。

        `enableTransparentBackground` 让滚动区继承窗口/卡片背景，否则会出现
        一块独立的面板色，与页面其它部分的留白割裂。
        """
        scroll = qf.ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        try:
            scroll.enableTransparentBackground()
        except Exception:
            scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                  "{ background: transparent; border: none; }")
        return scroll

    # --- 导航 -------------------------------------------------------
    def current_index(self) -> int:
        return self.stack.currentIndex()

    def is_busy(self) -> bool:
        return self._worker is not None

    def run_select_check(self) -> None:
        """「开始检验」：前置校验 + 试探源目录能否改名。

        试探有副作用（源路径会消失几毫秒），所以**只在这一步做**，不挂在实时刷新上。
        失败是 error 级，`_update_buttons` 会把「开始扫描」按住不让点。
        """
        if self.is_busy():
            return
        self.select_page.run_probe()
        self._update_buttons()
        blocking = self.select_page.has_blocking_error()
        self.statusBar().showMessage(
            "检验未通过：按上面的红色错误处理后再继续" if blocking
            else "检验通过，可以继续下一步")

    # --- 占用进程页（第一步的子流程）---------------------------------

    def show_holders_page(self) -> None:
        """从「改名被挡住」那条红线跳到占用进程页，并立刻开始扫描。"""
        if self.is_busy():
            return
        self.stack.setCurrentIndex(PAGE_HOLDERS)
        self._sync_step()
        self._update_buttons()
        self.scan_holders()

    def leave_holders_page(self) -> None:
        """办完回来：重跑一次检验，让第一步的红线/绿字如实反映现在的状态。"""
        if self.is_busy():
            return
        self.stack.setCurrentIndex(PAGE_SELECT)
        self.run_select_check()

    def scan_holders(self) -> None:
        """两级扫描：先缩范围（毫秒），再对嫌疑子树做 RestartManager 全扫。"""
        if self.is_busy():
            return
        source = self.select_page.source()
        if not source:
            return
        page = self.holders_page
        page.set_scanning("正在缩小范围：逐个试探源目录的顶层条目…")
        worker = HolderScanWorker(source)
        worker.progressed.connect(self._on_holder_progress)
        worker.found.connect(self._on_holders_found)
        worker.failed.connect(self._on_holder_failed)
        self._begin_worker(worker)
        worker.start()

    def _on_holder_progress(self, stage: str, done: int, total: int) -> None:
        page = self.holders_page
        if stage == "probe":
            page.set_probe_progress(done, total)
        else:
            page.set_scan_progress(done, total)

    def _on_holders_found(self, holders, blocked, note, files) -> None:
        page = self.holders_page
        # 关闭时要拿**同一批**文件去注册。清单由 worker 一起交回来，这里不再自己
        # 收一遍——同一棵树走两遍本就是浪费，而且在主线程上走第二遍会把界面冻住
        # （实测 4 万文件 2.4 秒）。
        self._holder_paths = list(files or [])
        if holders:
            page.show_holders(holders, names=blocked, note=note)
        elif blocked or note:
            page.show_no_holder(names=blocked, note=note)
        else:
            page.show_result(True, "没有发现占用：源目录现在可以改名了。")
        self._update_buttons()

    def _on_holder_failed(self, message: str) -> None:
        self.holders_page.show_no_holder(note=f"扫描没做成：{message}")
        self.statusBar().showMessage(f"扫描占用失败：{message}")

    def shutdown_holders(self, *, force: bool) -> None:
        """关闭占用进程。force 那一步必须由界面单独确认（会丢未保存内容）。"""
        paths = getattr(self, "_holder_paths", None)
        if not paths:
            return
        if force:
            answer = QMessageBox.warning(
                self, "强制结束程序",
                "将强制结束占用源目录的程序。\n\n"
                "注意：这些程序里未保存的内容会直接丢失，"
                "而且正在写盘的数据可能损坏。\n"
                "请先确认里面没有未保存的东西。\n\n确认强制结束吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if not _confirmed(answer):
                self.statusBar().showMessage("已取消强制结束")
                return
        closing = ("正在强制结束占用程序…" if force
                   else "正在请求占用程序关闭…（有的程序会弹「是否保存」，请留意）")
        self.holders_page.show_closing(closing)
        worker = ShutdownWorker(paths, force=force)
        worker.done_signal.connect(self._on_shutdown_done)
        self._begin_worker(worker)
        worker.start()

    def _on_shutdown_done(self, result) -> None:
        """关闭之后立刻重新试探：**能改名才算解决了**。

        `result.ok` 只是 RestartManager 的返回码判断，**不是结论**。实测 RmShutdown
        会返回 351，而微软对它的定义是「有些程序关掉了、有些没有」——部分成功，
        目标程序其实已经放开了文件。原来这里写的是 `if not ok: return`，把复验整个
        跳过，于是用户看到「请求关闭失败」，非得再点一次「重新扫描」才发现其实成了。

        真正的结论只能由改名试探给出。返回码只决定**没解决时怎么措辞**，以及要不要
        给「强制结束」——被护栏拦下时强制走的是同一条路，给了也是死按钮。

        **判定用 `probe_rename` 而不是 `probe_entries`**：前者是整棵树改一次名，
        耗时与文件数无关（实测 25 毫秒）；后者要逐个顶层条目试，0.67 毫秒/条目，
        平铺目录上能到几秒——而这里是主线程，那几秒就是界面冻住。只有**真的没
        解决**时，才需要后者来点名是哪些条目还挡着。
        """
        page = self.holders_page
        try:
            still_blocked = bool(guard.probe_rename(self.select_page.source()))
            # 只有确定没解决时才去点名（这一步才需要逐个试探，见上面的说明）
            blocked = [] if not still_blocked else \
                guard.probe_entries(self.select_page.source())
        except Exception as exc:
            # 复验本身出问题时，也**必须**把页面从「正在关闭」里放出来：
            # 卡在那一页、重新扫描还灰着，用户就彻底动不了了——那正是上一次的
            # 教训（结论出来了界面却卡住）。槽函数里绝不能让异常逃出去。
            page.show_result(False, f"没能确认占用是否解除：{exc}\n请点「重新扫描」。")
            self.statusBar().showMessage("复验失败，请重新扫描")
            return
        if not still_blocked:
            page.show_result(True, "占用已解除，源目录现在可以改名了。"
                                   "点「返回选择目录」继续。")
            self.statusBar().showMessage("占用已解除")
            return

        tail = (f"还在挡着的顶层条目：{'、'.join(blocked)}" if blocked
                else "仍然改不了名，但试探查不出是哪个条目挡着——"
                     "多半是目录句柄占用（资源管理器开着它、有终端 cd 在里面）")
        if result.ok:
            # 关闭动作没报错却仍改不了名：多半还有另一个占用者，或者挡着的是
            # 目录句柄（那种查不到进程）。给强制按钮没意义，要的是重新扫描。
            text = (f"关闭了程序，但源目录仍然无法改名。\n{tail}\n"
                    "可能是另一个进程占着，也可能是目录句柄占用——请重新扫描。")
        else:
            text = f"{result.message}\n{tail}"
            if result.force_may_help:
                text += "\n有的程序可能拒绝了关闭，可以再试一次，或强制结束。"
        page.show_closed(False, text, can_force=result.force_may_help)
        self.statusBar().showMessage("仍有占用")

    def go_next(self) -> None:
        index = self.current_index()
        if index == PAGE_SELECT:
            if self._awaiting_link_only():
                # 源目录已经不在了，扫描只能得到一棵空树；跳过计划页，
                # 直接进执行页把最后一步（补建源目录链接）走完。
                self.start_migration()
            else:
                self.start_plan()
        elif index == PAGE_PLAN:
            self.start_migration()
        elif index == PAGE_PROGRESS:
            self.stack.setCurrentIndex(PAGE_REPORT)
            self._update_buttons()
        self._sync_step()

    def _awaiting_link_only(self) -> bool:
        """恢复的会话是否停在「数据已就位、只差源目录链接」。

        这种现场下前置校验必然报「源目录不存在」，照常走会让下一步永远灰着，
        用户明明只差最后一步，却只能去命令行或手工建链接。
        """
        return awaiting_source_link(self._restored_session, self.select_page.source())

    def go_back(self) -> None:
        if self.is_busy():
            return
        if self.current_index() == PAGE_PLAN:
            self.stack.setCurrentIndex(PAGE_SELECT)
            self._update_buttons()
        self._sync_step()

    def _sync_step(self) -> None:
        """步骤条与内容区始终同步（切换可能发生在异步回调里）。

        迁移跑完后仍停在执行页（等用户点「查看报告」），但此时第 3 步本身
        已经完成——照旧显示成「进行中」会和页面上的「迁移完成」自相矛盾。
        所以完成后把指针推到第 4 步：前三步打勾、第四步高亮。
        """
        index = self.stack.currentIndex()
        if self._migration_finished and index == PAGE_PROGRESS:
            index = PAGE_REPORT
        if index == PAGE_HOLDERS:
            # 占用进程页是第一步的子流程，步骤条仍停在「选择目录」。
            # 不映射的话 set_current(4) 会越界（步骤条只有 4 项）。
            index = PAGE_SELECT
        self.step_bar.set_current(index)

    def _update_buttons(self) -> None:
        index = self.current_index()
        busy = self.is_busy()
        self._sync_step()
        self.back_button.setEnabled(not busy and index == PAGE_PLAN)
        if index == PAGE_SELECT:
            link_only = self._awaiting_link_only()
            self.next_button.setText(LINK_ONLY_BUTTON if link_only else "开始扫描")
            # 必须先检验过：校验只由「开始检验」触发（那里才含试探改名），
            # 没检验就没有结论，也就不该放行——否则用户直接跳过最容易白跑的那一步。
            ready = (bool(self.select_page.source())
                     and bool(self.select_page.target())
                     and (link_only or (self.select_page.is_checked()
                                        and not self.select_page.has_blocking_error())))
            self.next_button.setEnabled(ready and not busy)
        elif index == PAGE_PLAN:
            self.next_button.setText("开始迁移")
            self.next_button.setEnabled(not busy)
        elif index == PAGE_PROGRESS:
            self.next_button.setText("查看报告")
            self.next_button.setEnabled(not busy)
        elif index == PAGE_HOLDERS:
            # 这一页有自己那颗「← 返回选择目录」，向导的上一/下一步在这儿
            # 没有意义，摆一个「完成」在上面只会让人以为点了就结束。
            self.next_button.setText("下一步")
            self.next_button.setEnabled(False)
        else:
            self.next_button.setText("完成")
            self.next_button.setEnabled(False)

    # --- 线程调度 ---------------------------------------------------
    def _begin_worker(self, worker) -> None:
        self._worker = worker
        worker.finished.connect(self._on_worker_finished)
        self._update_buttons()

    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.wait(5000)
            # 迁移日志的接收方挂在这个 worker 的信号上（见 MigrateWorker），
            # worker 一被释放那个信号就是死对象。这里**显式**摘掉，别让行为
            # 依赖 GC 时机：不摘的话「日志进不进界面面板」要看 worker 有没有
            # 恰好被回收，同一操作两种结果。摘掉之后，迁移结束后那些收尾动作
            # （删除备份、回滚）写的日志只进文件、不再进界面面板——这是确定的。
            if isinstance(worker, MigrateWorker) and self._migrator is not None:
                self._migrator.log_sink = None
        self._update_buttons()

    def _on_worker_failed(self, message: str) -> None:
        # 扫描失败时不会有 _on_planned，标志要在这里清掉，
        # 否则下一次成功扫描会被误当成「重扫」而报出不相干的结论
        self._replan_pending = False
        self.statusBar().showMessage(f"出错：{message}")
        QMessageBox.warning(self, "操作失败", message)

    # --- ① → ② ------------------------------------------------------
    def _make_migrator(self, mappings, deep_verify, skip_reparse, excludes):
        migrator = self._factory(self.select_page.source(),
                                 self.select_page.target(),
                                 mappings, deep_verify, None, skip_reparse,
                                 excludes=excludes)
        if self._restored_session is None:
            return migrator
        if migrator.adopt_session(self._restored_session):
            return migrator
        # 用户改了源或目标：旧会话记的是**另一棵树**的相对路径，沿用它
        # 会把不相干的文件当成「已复制」而跳过，只能丢弃、按新任务处理
        self._restored_session = None
        return migrator

    def start_plan(self) -> None:
        if self.is_busy():
            return
        self._migrator = self._make_migrator(self.plan_page.mappings(),
                                             self.plan_page.deep_verify(),
                                             self.plan_page.skip_unknown_reparse(),
                                             self.plan_page.excludes())
        worker = PlanWorker(self._migrator)
        worker.planned.connect(self._on_planned)
        worker.progressed.connect(
            lambda rel: self.statusBar().showMessage(f"扫描：{rel}"))
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        self.statusBar().showMessage("正在扫描…")
        worker.start()

    def _on_planned(self, plan) -> None:
        from ..core import permission
        # 建链实测要创建临时目录，只在真有文件符号链接时才做；
        # 目录符号链接能降级成 junction，不需要这个特权。
        capable = (permission.can_create_symlink()
                   if getattr(plan, "file_symlinks", 0) else True)
        self.plan_page.load(plan, already_elevated=permission.is_elevated(),
                            symlink_capable=capable)
        self.stack.setCurrentIndex(PAGE_PLAN)
        self._update_buttons()
        if self._replan_pending:
            self._replan_pending = False
            text = mapping_summary(plan, self.plan_page.mappings())
            self.statusBar().showMessage(text)
            try:
                # 只写状态栏不够：状态栏在窗口最底部、字又小，
                # 而重扫快得根本看不见过程。给一个气泡提示。
                qf.InfoBar.success(
                    title="重新扫描完成", content=text, duration=4000,
                    position=qf.InfoBarPosition.TOP_RIGHT, parent=self)
            except Exception:
                pass      # 库版本差异不该让重扫本身失败
        else:
            self.statusBar().showMessage("计划已生成")

    def replan(self) -> None:
        """映射或重解析点策略变了：用新参数重建 Migrator 并重新扫描。"""
        if self.is_busy():
            return
        self._replan_pending = True
        self.start_plan()

    # --- ② → ③ ------------------------------------------------------
    def start_migration(self) -> None:
        if self.is_busy():
            return
        mappings = self.plan_page.mappings()
        deep_verify = self.plan_page.deep_verify()
        skip_reparse = self.plan_page.skip_unknown_reparse()
        excludes = self.plan_page.excludes()
        # 复用旧 Migrator 的前提是四个开关都没变：映射、深度校验、重解析点策略、
        # 排除项都会改变 scanner/copier/verifier 的行为，漏比任何一个都会让新设置被悄悄丢弃。
        if (self._migrator is None
                or mappings != getattr(self._migrator, "mappings", {})
                or deep_verify != getattr(self._migrator, "deep_verify", False)
                or skip_reparse != getattr(self._migrator, "skip_unknown_reparse", False)
                or excludes != getattr(self._migrator, "excludes", [])):
            self._migrator = self._make_migrator(mappings, deep_verify, skip_reparse,
                                                excludes)
        self.progress_page.reset()
        self._migration_finished = False
        self._migration_timer.start()
        worker = MigrateWorker(self._migrator)
        worker.progressed.connect(self.progress_page.update_progress)
        # 界面上的日志面板直接显示会话日志本身，不再自己另写一套
        worker.logged.connect(self.progress_page.append_log)
        worker.done.connect(self._on_migrated)
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        self.stack.setCurrentIndex(PAGE_PROGRESS)
        self.progress_page.set_running(True)
        self._update_buttons()
        self.statusBar().showMessage("正在迁移…")
        worker.start()

    def _on_migrated(self, outcome) -> None:
        self._outcome = outcome
        if getattr(outcome, "copy", None):
            self.progress_page.set_failures(outcome.copy.failures)
        self.report_page.load(outcome)
        # 把「进行中」形态收成结论：只调 set_running(False) 的话，
        # 页面会停在最后那一帧（校验中 100% / 当前文件 - / 速度是陈旧值），
        # 看起来像卡住而不是跑完。
        elapsed = (self._migration_timer.elapsed() / 1000.0
                   if self._migration_timer.isValid() else None)
        self._migration_finished = True
        self.progress_page.finish(outcome, elapsed)
        self._update_buttons()
        self.statusBar().showMessage(
            "迁移完成" if outcome.ok
            else f"未完成：{getattr(outcome, 'message', '') or outcome.phase}")

    def _on_pause_toggled(self, paused: bool) -> None:
        worker = self._worker
        if isinstance(worker, MigrateWorker):
            worker.pause() if paused else worker.resume()

    def cancel_migration(self) -> None:
        worker = self._worker
        if isinstance(worker, MigrateWorker):
            worker.cancel()
            self.statusBar().showMessage("正在取消…")

    # --- 提权 -------------------------------------------------------
    def _ensure_session_id(self) -> str:
        if self._migrator is not None and getattr(self._migrator, "session", None):
            return self._migrator.session.id
        if self._restored_session is not None:
            return self._restored_session.id
        if self._elevate_session is None:
            self._elevate_session = Session.create(self.select_page.source(),
                                                   self.select_page.target())
        return self._elevate_session.id

    def restart_elevated(self) -> None:
        session_id = self._ensure_session_id()
        if relaunch_elevated(session_id):
            self.statusBar().showMessage(
                f"已请求以管理员身份重启，会话 {session_id}")
        else:
            QMessageBox.warning(self, "提权失败",
                                "用户取消了 UAC 授权，或系统拒绝了提权请求。")

    def take_over_denied(self) -> None:
        """接管无权限目录的属主：先存 ACL 再改属主，然后重扫。

        没有这一步，无权限目录只剩「排除掉」一条路，而排除意味着那些内容
        只留在备份里——想全量迁移的用户就没辙了。
        """
        if self.is_busy():
            return
        if self._migrator is None:
            # 还没有计划就没有「哪些目录读不到」这份清单，先扫一遍
            self.start_plan()
            return
        worker = TakeoverWorker(self._migrator)
        worker.done.connect(self._on_taken_over)
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        self.statusBar().showMessage("正在接管目录权限（takeown 可能要跑一会儿）…")
        worker.start()

    def _on_taken_over(self, taken: int, error: str) -> None:
        plan = getattr(self._migrator, "plan", None)
        if plan is not None:
            # 权限变了，界面要换成重扫后的那一份
            self._on_planned(plan)
        if error:
            self.statusBar().showMessage(error)
            QMessageBox.warning(self, "接管未完成", error)
            return
        # 排在 _on_planned 之后：它会把状态行改成「计划已生成」，
        # 顺序反了用户就看不到「接管了几个目录」这个结论。
        self.statusBar().showMessage(
            f"已接管 {taken} 个目录的属主；原始 ACL 已记入会话，回滚时会还原")

    def restore_session(self, session_id: str) -> None:
        session = Session.load(default_root() / f"{session_id}.json")
        if session is None:
            self.statusBar().showMessage(f"未找到会话 {session_id}，按新任务处理")
            return
        self.select_page.set_source(session.source)
        self.select_page.set_target(session.target)
        # 记住它：新建 Migrator 时要接管这个会话，才能沿用 .done 记账续传。
        # 只回填源/目标的话，execute() 会另起一个新会话，已复制的文件全部重复制一遍。
        self._restored_session = session
        # 链接重写映射也要一并回填，否则续传会用空映射重建 Migrator，
        # 目标端那几百个 junction 全部指回迁移前的旧位置（马上要变成断链）。
        self.plan_page.set_options(getattr(session, "options", None))
        if self._awaiting_link_only():
            self.statusBar().showMessage(
                f"已恢复会话 {session.id}：数据已复制到目标并通过校验，"
                f"源目录已改名为备份，只差源目录链接——点「{LINK_ONLY_BUTTON}」完成最后一步")
            return
        done = int(session.done.get("files", 0))
        detail = (f"，已记录 {done:,} 个文件，与源完全一致的将直接跳过" if done
                  else "，尚无可续传的复制记录")
        mapped = (session.options or {}).get("mappings") or {}
        if mapped:
            detail += f"，沿用 {len(mapped)} 条链接映射"
        self.statusBar().showMessage(f"已恢复会话 {session.id}{detail}")

    # --- 失败清单与收尾 ---------------------------------------------
    def locate_process(self, label: str) -> None:
        name, pid = parse_process_label(label)
        if pid is None:
            QMessageBox.information(self, "占用进程",
                                    f"{label or '未知进程'}\n未能解析出 PID。")
            return
        path = process_image_path(pid)
        text = f"进程：{name}\nPID：{pid}"
        if path:
            text += f"\n路径：{path}"
        else:
            text += "\n（拿不到可执行文件路径，可能权限不足或进程已退出）"
        QMessageBox.information(self, "占用进程", text)

    def rollback(self) -> None:
        if self._migrator is None:
            return
        phase = getattr(self._outcome, "phase", "") if self._outcome else ""
        note = ""
        if phase == "done":
            note = ("\n\n迁移已经完成：回滚会摘掉源路径上那条链接、把备份改回原名。"
                    "迁移之后写进目标端的新数据（跑过的构建、新下载的文件）"
                    "会变成孤儿——它们还留在目标目录里，但从此没人指向它们。")
        delete_target, also_delete = _confirm_with_option(
            self, "回滚",
            f"将退回迁移前的状态：\n{self._migrator.source}{note}\n\n确认回滚吗？",
            "同时删除目标端里同步过去的内容（迁移之后写进去的东西不会被删）")
        if not delete_target:
            self.statusBar().showMessage("已取消回滚")
            return

        ok = self._migrator.rollback()
        if not ok:
            reason = getattr(self._migrator, "cleanup_error", "") or "原因未知"
            self.statusBar().showMessage(f"回滚未执行：{reason}")
            QMessageBox.warning(
                self, "回滚未执行",
                f"回滚未执行：{reason}\n\n源目录未做任何改动，数据仍在原处可访问。")
            return

        detail = getattr(self._migrator, "cleanup_error", "")
        self.statusBar().showMessage(
            f"已回滚到原目录；{detail}" if detail else "已回滚到原目录")
        target_state, target_note = self._handle_target_after_rollback(also_delete)
        note2 = f"\n\n{detail}" if detail else ""
        tail = self._restore_target_tail(target_state, target_note)
        # 界面上与备份有关的三样都要跟着变：警告（`load_restored` 会连文字一起清）、
        # 按钮（备份没了就退不回去了）、以及目标端（成了孤儿，该能清）。
        self.report_page.set_actions_enabled(
            False, False, discard=self._migrator.has_target())
        # 这一页必须从「迁移结果」切成「恢复结果」：迁移已经被撤销了，
        # 继续顶着「迁移成功」会让人以为它还生效着。
        self.report_page.load_restored(RestoreOutcome(
            ok=True, source=str(self._migrator.source), source_restored=True,
            backup_path=self._migrator.backup_path or "",
            acl_note=detail, target=str(self._migrator.target),
            target_state=target_state, target_note=target_note))
        QMessageBox.information(
            self, "回滚完成",
            f"已退回迁移前的状态。源路径恢复为真实目录（不再是链接）：\n"
            f"{self._migrator.source}{note2}{tail}")

    def _handle_target_after_rollback(self, also_delete: bool) -> tuple[str, str]:
        """按用户是否勾选处理目标端，返回 (状态键, 补充说明)。

        状态键由界面映射成文案（report_page.TARGET_STATE_TEXT）。
        """
        target = self._migrator.target
        if not also_delete:
            return "kept", ""
        if not self._migrator.has_target():
            self.statusBar().showMessage("已回滚，目标端目录本就不存在")
            return "missing", ""
        ok, reason = self._migrator.can_clear_target()
        if not ok:
            # 只跳过清空这一步，回滚本身照做：用户要的是回滚，
            # 不该被目标端的情况挡回去。
            self.statusBar().showMessage(f"已回滚；目标端未清理：{reason}")
            return "skipped", reason
        if self._migrator.clear_target():
            self.statusBar().showMessage("已回滚，目标端内容已清空")
            return "cleared", ""
        fail = getattr(self._migrator, "cleanup_error", "") or ""
        self.statusBar().showMessage(f"已回滚；目标端未能清空：{fail}")
        return "failed", fail

    def _restore_target_tail(self, state: str, note: str) -> str:
        """结果弹窗里那句关于目标端的说明。"""
        target = self._migrator.target
        if state == "cleared":
            return f"\n\n目标端里同步过去的内容已清空（目录本身保留）：\n{target}"
        if state == "missing":
            return f"\n\n目标端目录本就不存在，没有需要清理的内容：\n{target}"
        if state == "skipped":
            return (f"\n\n目标端未清理：{note}。\n"
                    f"确认那些内容不再需要后，可用本页的「删除目标」手动处理：\n"
                    f"{target}")
        if state == "failed":
            return f"\n\n目标端未能清空：{note}\n{target}"
        return (f"\n\n迁移期间复制到目标端的那份数据仍在：\n{target}\n"
                f"它现在没有任何入口指向，可按需保留，或用本页的「删除目标」清掉。")

    def cleanup_backup(self) -> None:
        if self._migrator is None:
            return
        # 删备份是彻底不可逆的一步：删掉之后就没有任何退回迁移前状态的手段了
        answer = QMessageBox.warning(
            self, "删除备份",
            f"将删除源数据的最后一份副本：\n{self._migrator.backup_path}\n\n"
            "目标端已经通过校验，但删掉之后就无法再回滚了。\n\n确认删除吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if not _confirmed(answer):
            self.statusBar().showMessage("已保留备份")
            return
        if self._migrator.cleanup_backup():
            self.statusBar().showMessage("备份已删除")
            # 备份没了，那条「请勿删除备份」的警告必须同时撤掉：
            # 让警告指向一个已经不存在的目录，比没有警告更糟。
            self.report_page.set_backup_warning("")
            # 回滚必须依靠备份，没有备份就再也退不回去了，两个按钮都该关掉。
            # （能走到这里说明源路径已是链接，所以回滚确实不可能成功。）
            self.report_page.set_actions_enabled(False, False)
            # 这一步之后就没有退路了，成功也要明确告知，不能只在状态栏留一行字
            QMessageBox.information(
                self, "备份已删除",
                f"备份已删除：\n{self._migrator.backup_path}\n\n"
                f"此后无法再回滚到迁移前的状态。目标端的数据不受影响，"
                f"本次迁移的结果保持不变。")
            return

        reason = getattr(self._migrator, "cleanup_error", "") or "备份未能删除"
        # 备份里有「只存在于备份里」的内容时（跳过的重解析点、被排除的目录），
        # 拒绝删除是正确的，但必须给出强制删除的出口，否则用户被堵死在这里。
        counters = [getattr(self._migrator, name, None) for name in
                    ("skipped_reparse_count", "excluded_count")]
        has_only_in_backup = any(callable(c) and c() > 0 for c in counters)

        # 删除失败的两种情形都要明确弹窗告知，不能只在状态栏留一行字——
        # 否则用户点了按钮、备份还在盘上，却以为「没反应」。
        if has_only_in_backup:
            answer = QMessageBox.warning(
                self, "备份未删除", f"{reason}\n\n仍要强制删除吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if not _confirmed(answer):
                self.statusBar().showMessage("已保留备份")
                return
            if self._migrator.cleanup_backup(force=True):
                self.statusBar().showMessage("备份已删除")
            else:
                fail = getattr(self._migrator, "cleanup_error", "") or "备份未能删除"
                self.statusBar().showMessage(fail)
                QMessageBox.warning(
                    self, "备份未删除",
                    f"{fail}\n\n备份目录仍在：{self._migrator.backup_path}\n"
                    "可关掉占用它的程序后重试，或手动删除。")
        else:
            self.statusBar().showMessage(reason)
            QMessageBox.warning(
                self, "备份未删除",
                f"{reason}\n\n备份目录仍在：{self._migrator.backup_path}\n"
                "可关掉占用它的程序后重试，或手动删除。")

    def discard_target(self) -> None:
        """删掉目标端的半成品（源目录完好、迁移没走完时的清理出口）。"""
        if self._migrator is None:
            return
        phase = getattr(self._outcome, "phase", "") if self._outcome else ""
        note = ""
        if phase == "rename_failed":
            note = ("\n\n注意：目标端已经通过校验，改名失败通常只是某个程序占着源目录。"
                    "关掉那个程序后重试迁移更划算——删除等于丢掉这份已复制好的数据。")
        answer = QMessageBox.warning(
            self, "删除目标目录",
            f"将删除目标目录里已复制的内容：\n{self._migrator.target}\n"
            f"源目录不受影响。{note}\n\n确认删除吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if not _confirmed(answer):
            self.statusBar().showMessage("已保留目标目录")
            return
        if self._migrator.discard_target():
            self.statusBar().showMessage("目标目录已删除")
            # 目标端没了，这个按钮不该还能点：再点只会得到
            # 「目标目录不存在，无需清理」这种自相矛盾的提示。
            self.report_page.set_discard_enabled(False)
        else:
            self.statusBar().showMessage(
                getattr(self._migrator, "cleanup_error", "") or "目标目录未能删除")

    def reverify(self) -> None:
        if self.is_busy() or self._migrator is None:
            return
        # 基准必须问 migrator：迁移成功后源路径已经变成指向目标的链接，
        # 拿它当原件就是拿目标和目标自己比，校验永远通过。
        baseline, error = self._migrator.reverify_baseline()
        if error:
            self.statusBar().showMessage(error)
            QMessageBox.information(self, "无法重新校验", error)
            return
        plan = getattr(self._migrator, "plan", None)
        worker = VerifyWorker(
            baseline, self._migrator.target,
            deep=self.plan_page.deep_verify(), totals=getattr(plan, "totals", None),
            skip_unknown_reparse=bool(getattr(plan, "skip_unknown_reparse", False)),
            excludes=getattr(plan, "excludes", ()) or ())
        self._reverify_base = baseline
        worker.progressed.connect(
            lambda p: self.statusBar().showMessage(
                f"正在校验：{p.done_files:,} / {p.total_files:,} 个文件"))
        worker.verified.connect(self._on_reverified)
        worker.failed.connect(self._on_worker_failed)
        self._begin_worker(worker)
        worker.start()

    def _on_reverified(self, result) -> None:
        self._reverify_result = result
        if getattr(result, "cancelled", False):
            self.statusBar().showMessage("校验已取消")
            return
        if result.ok:
            base = getattr(self, "_reverify_base", "")
            suffix = "" if base == str(self._migrator.source) \
                else f"（比对基准：{base}）"
            self.statusBar().showMessage(f"重新校验通过{suffix}")
            return

        problems = list(result.missing) + list(result.mismatch)
        self.statusBar().showMessage(
            f"重新校验未通过：{len(result.missing)} 个缺失、"
            f"{len(result.mismatch)} 个不一致")
        preview = "\n".join(problems[:5])
        more = f"\n…共 {len(problems)} 项" if len(problems) > 5 else ""
        QMessageBox.warning(
            self, "重新校验未通过",
            f"目标端与原件不一致：{len(result.missing)} 个缺失、"
            f"{len(result.mismatch)} 个不一致。\n\n{preview}{more}")

    # --- 退出 -------------------------------------------------------
    def closeEvent(self, event) -> None:      # noqa: N802 - Qt 命名
        worker = self._worker
        if worker is not None and worker.isRunning():
            # 迁移中关窗 = 中途放弃。已经复制的内容不会退回、源目录也还没改名，
            # 所以必须问一句，并把「可以续传」这条退路讲清楚。
            answer = QMessageBox.warning(
                self, "迁移尚未完成",
                "迁移还在进行中，现在关闭会中断它：\n"
                "· 已经复制到目标端的内容会保留，不会被撤销\n"
                "· 源目录不受影响\n"
                "· 之后可以用会话文件续传，已复制的文件会直接跳过\n\n"
                "确认关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if not _confirmed(answer):
                event.ignore()
                return
            if hasattr(worker, "cancel"):
                worker.cancel()
            worker.wait(5000)
        event.accept()


def run_gui(argv=None) -> int:
    """创建 QApplication 并显示主窗口；返回进程退出码。"""
    from ..entry import parse_launch_args

    argv = list(sys.argv if argv is None else argv)
    app = QApplication.instance() or QApplication(argv)
    args = parse_launch_args(argv)
    window = MainWindow(session_id=args["session"])
    window.show()
    return app.exec()






