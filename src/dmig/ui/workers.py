"""把耗时的迁移动作搬进 QThread，界面只通过信号接收结果。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from ..core import verifier


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
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, migrator, parent=None):
        super().__init__(parent)
        self._migrator = migrator
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
