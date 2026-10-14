"""把耗时的迁移动作搬进 QThread，界面只通过信号接收结果。"""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QThread, Signal

from ..core import guard, locks, verifier


class SelectCheckWorker(QThread):
    """「开始检验」线程：前置校验 + 试探源目录能否改名。

    为什么要 worker：试探改名原以为是 O(1) 元数据操作，实测**与文件数线性相关**
    （0.46 微秒/文件：1 万文件 3 ms、10 万文件 47 ms——大概率是杀软过滤驱动在
    目录改名后重新评估整棵子树）。10 万个文件就已经接近半秒，慢盘或防护更严时
    到秒级，放在主线程就是界面冻死。

    副作用与 `guard.probe_rename` 同源（源路径会消失几毫秒），这一步本来就是
    用户显式点「开始检验」才触发的。

    `source`/`target` 随结果一起回传：检验期间用户可能改了路径，调用方据此
    判断这份结论是否过期。
    """

    done = Signal(object, str, str)          # issues, source, target
    failed = Signal(str)

    def __init__(self, source, target, parent=None):
        super().__init__(parent)
        self._source = str(source)
        self._target = str(target)

    def run(self) -> None:
        try:
            issues = list(guard.check(self._source, self._target))
            if not guard.has_error(issues):
                # 前置校验已经拦住了就没有必要试探——源可能根本不是目录，
                # 而且这一跳会真的给源目录改名（系统目录尤其不能碰）。
                issues.extend(guard.probe_rename(self._source))
        except Exception as exc:                     # 线程里必须兜住所有异常
            self.failed.emit(str(exc))
            return
        self.done.emit(issues, self._source, self._target)


class PlanWorker(QThread):
    """预扫描线程：migrator 只需实现 plan_it(on_progress=...)。"""

    planned = Signal(object)
    progressed = Signal(str)
    failed = Signal(str)

    def __init__(self, migrator, parent=None):
        super().__init__(parent)
        self._migrator = migrator

    def run(self) -> None:
        try:
            plan = self._migrator.plan_it(on_progress=self.progressed.emit)
        except Exception as exc:                     # 线程里必须兜住所有异常
            self.failed.emit(str(exc))
            return
        self.planned.emit(plan)


class TakeoverWorker(QThread):
    """接管无权限目录的属主：takeown /R 与 icacls /T 可能要跑几分钟。

    跑完会重扫一遍（权限变了，旧计划作废），所以耗时和预扫描同量级，
    同样不能占着界面线程。
    """

    done = Signal(int, str)          # 接管数, 失败原因
    failed = Signal(str)

    def __init__(self, migrator, parent=None):
        super().__init__(parent)
        self._migrator = migrator

    def run(self) -> None:
        try:
            taken, error = self._migrator.take_over_denied()
        except Exception as exc:                     # 线程里必须兜住所有异常
            self.failed.emit(str(exc))
            return
        self.done.emit(taken, error)


class MigrateWorker(QThread):
    """执行线程：支持暂停、继续、取消。"""

    progressed = Signal(object)
    logged = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, migrator, parent=None):
        super().__init__(parent)
        self._migrator = migrator
        # 让 migrator 写的每一行日志同时流到界面。界面上的「迁移日志」原来只写
        # 三个事件，一趟干净成功的迁移在界面上只剩一行校验提示、磁盘上却有十条；
        # 接上这个信号之后，界面显示的就是磁盘那份日志本身，不会再各走各的。
        # 走信号而不是直接改控件：日志是在这个线程里写的。
        migrator.log_sink = self.logged.emit
        self._cancel = threading.Event()
        self._pause = threading.Event()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()

    def run(self) -> None:
        try:
            outcome = self._migrator.execute(
                on_progress=self.progressed.emit,
                should_cancel=self._cancel.is_set,
                should_pause=self._pause.is_set)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(outcome)


# 顶层条目超过这个数就跳过「逐个试探」那一级。实测试探 0.67 毫秒/条目，
# 2000 条大约 1.3 秒；再多就不如直接扫整棵树（RM 约 0.13 毫秒/文件）。
NARROW_LIMIT = 2000


class HolderScanWorker(QThread):
    """找出占着源目录的进程：先缩范围，再对嫌疑子树做 RestartManager 全扫。

    两级都要上报进度：第二级在几十万文件时是几十秒，没有进度用户会以为卡死。

    **收好的文件清单要随结果一起交回去**（`found` 的第四个参数）。调用方关闭时
    要拿同一批文件去注册；让它自己去收一遍，等于把整棵树的遍历又跑一次、而且
    是在主线程上跑——实测 4 万文件就能把界面冻住 2.4 秒。
    """

    progressed = Signal(str, int, int)      # 阶段标签 / 已完成 / 总数
    # holders / 被挡住的顶层条目 / 说明 / 已收好的文件清单
    found = Signal(object, object, object, object)
    failed = Signal(str)

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self._source = source
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            roots, blocked, note = self._suspect_roots()
            if self._cancel.is_set():
                self.failed.emit("已取消")
                return

            self.progressed.emit("scan", 0, 0)
            files = collect_files(roots)
            if not files:
                self.found.emit([], blocked, note, [])
                return
            holders = locks.find_holders(
                files,
                on_progress=lambda done, total: self.progressed.emit(
                    "scan", done, total),
                should_cancel=self._cancel.is_set)
        except Exception as exc:
            self.failed.emit(str(exc))
            return

        if not holders and blocked and not note:
            note = ("试探改名确实失败了，但 RestartManager 查不到具体进程。"
                    "多半是程序把源目录本身当作工作目录（资源管理器开着它、"
                    "终端 cd 在里面）——这类占用拿不到进程。")
        self.found.emit(holders, blocked, note, files)

    def _suspect_roots(self) -> tuple[list[str], list[str], str]:
        """要交给 RestartManager 的根清单，以及怎么得到它的说明。

        **只对子目录做试探，普通文件一律直接交给 RM。** 试探一个条目 0.67 毫秒，
        而 RM 扫一个文件只要 0.13 毫秒——试一个文件永远是净亏：没被占用时省下的
        还不到成本的三分之一，被占用时照样得交给 RM。只有目录才可能赚回来：
        它的子树里可能有成百上千个文件。

        **先看顶层规模再决定要不要缩范围。** 目录数本身就有几万时（极端平铺），
        逐个试探比它要省的那步还贵——外推 10 万条目要 67 秒。这时直接对整棵树扫
        反而更快。
        """
        dirs, total = _top_level_dirs(self._source)
        if total > NARROW_LIMIT:
            return ([self._source], [],
                    f"源目录顶层有 {total:,} 个条目，逐个试探比直接扫描更慢，"
                    f"已跳过这一步直接扫整棵树。")

        blocked = guard.probe_entries(
            self._source, names=dirs,
            on_progress=lambda done, tot: self.progressed.emit("probe", done, tot),
            should_cancel=self._cancel.is_set)
        # blocked 为空但根改名也失败时（目录级句柄，试探查不出具体条目），
        # 只能退回整棵树——那种情况本来就查不到进程，代价是等一次全扫。
        roots = [os.path.join(self._source, name) for name in blocked] or \
            [self._source]
        return roots, blocked, ""


def _top_level_dirs(source) -> tuple[list[str], int]:
    """一次 scandir 拿到：顶层**子目录**名 + 顶层条目总数。

    分两件事是有意的：目录名是**要试探的对象**，总数只用来判断值不值得试探。
    读不出来就当 0 个条目（走「不试探、整棵树扫」那条路，安全）。
    """
    from ..core.fsutil import long_path

    dirs: list[str] = []
    total = 0
    try:
        with os.scandir(long_path(source)) as it:
            for entry in it:
                total += 1
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return [], 0
    return dirs, total


def collect_files(roots, limit: int = 400000) -> list[str]:
    """收集这些目录树里的普通文件路径，供 RestartManager 注册。

    只收普通文件：链接不进 RM（它们指向别处），目录注册了也查不到东西（实测）。
    `limit` 是防呆上限，正常不会碰到。

    扫描与关闭**共用这一个**——两处各写一套走法，迟早会分叉成「扫出来了却关不掉」。
    """
    import os

    from ..core.fsutil import is_link_kind, long_path, reparse_kind

    files: list[str] = []
    for root in roots:
        if os.path.isfile(root):
            files.append(root)
            continue
        for dirpath, dirs, names in os.walk(long_path(root)):
            keep = []
            for name in dirs:
                full = os.path.join(dirpath, name)
                try:
                    if reparse_kind(os.lstat(full)) is None:
                        keep.append(name)
                except OSError:
                    continue
            dirs[:] = keep
            for name in names:
                full = os.path.join(dirpath, name)
                try:
                    if is_link_kind(reparse_kind(os.lstat(full))):
                        continue
                except OSError:
                    continue
                files.append(full)
                if len(files) >= limit:
                    return files
    return files


class ShutdownWorker(QThread):
    """关闭占用进程。force=False 是优雅关闭（程序可以提示保存、也可以拒绝）。"""

    done_signal = Signal(object)

    def __init__(self, paths, *, force: bool = False, parent=None):
        super().__init__(parent)
        self._paths = list(paths)
        self._force = force

    def run(self) -> None:
        try:
            result = locks.shutdown_holders(self._paths, force=self._force)
        except Exception as exc:
            self.done_signal.emit(locks.ShutdownResult(False, False, str(exc)))
            return
        self.done_signal.emit(result)


class VerifyWorker(QThread):
    """单独重新校验，供报告页使用。"""

    verified = Signal(object)
    progressed = Signal(object)
    failed = Signal(str)

    def __init__(self, source, target, *, deep: bool = False,
                 skip_unknown_reparse: bool = False, totals=None, excludes=(),
                 parent=None):
        super().__init__(parent)
        self._source = source
        self._target = target
        self._deep = deep
        self._skip_unknown_reparse = skip_unknown_reparse
        self._totals = totals
        self._excludes = list(excludes or [])
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            result = verifier.verify(
                self._source, self._target, deep=self._deep,
                on_progress=self.progressed.emit,
                should_cancel=self._cancel.is_set, totals=self._totals,
                skip_unknown_reparse=self._skip_unknown_reparse,
                excludes=self._excludes)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.verified.emit(result)




