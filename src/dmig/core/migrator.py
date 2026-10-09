"""迁移编排：把扫描、复制、校验、改名、建链接、清理/回滚串成状态机。"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import guard, linker, scanner, verifier
from .copier import run as copy_run
from .fsutil import long_path
from .models import CopyResult, MigrationPlan, VerifyResult
from .session import Session


@dataclass
class MigrateOutcome:
    ok: bool
    phase: str
    copy: CopyResult | None = None
    verify: VerifyResult | None = None
    backup_path: str | None = None
    link_kind: str | None = None
    message: str = ""


def backup_name(source) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{Path(source).name}.bak_{stamp}"


def rmtree_force(path) -> None:
    """删除目录树。

    Windows 上只读文件会让 shutil.rmtree 直接抛 PermissionError，
    迁移来的目录里很可能带只读属性，所以先清掉只读位再重试。
    """
    def on_error(func, name, exc):
        try:
            os.chmod(name, stat.S_IWRITE)
            func(name)
        except OSError:
            pass

    shutil.rmtree(path, onexc=on_error)


class Migrator:
    """一次迁移任务的完整生命周期。"""

    def __init__(self, source, target, *, mappings=None, deep_verify=False,
                 session_root=None):
        self.source = Path(source)
        self.target = Path(target)
        self.mappings = dict(mappings or {})
        self.deep_verify = deep_verify
        self.session_root = session_root
        self.plan: MigrationPlan | None = None
        self.session: Session | None = None
        self.backup_path: str | None = None
        self._link_created = False

    # --- 计划 -------------------------------------------------------
    def plan_it(self, on_progress=None) -> MigrationPlan:
        self.plan = scanner.scan(self.source, self.target,
                                 mappings=self.mappings, on_progress=on_progress)
        return self.plan

    def guard_errors(self) -> list[str]:
        return [i.message for i in
                guard.check(self.source, self.target,
                            required_bytes=self.plan.required_bytes if self.plan else 0)
                if i.severity == "error"]

    # --- 执行 -------------------------------------------------------
    def execute(self, *, on_progress=None, should_cancel=None,
                should_pause=None) -> MigrateOutcome:
        if self.plan is None:
            self.plan_it()
        errors = self.guard_errors()
        if errors:
            return MigrateOutcome(False, "guard_failed", message="; ".join(errors))

        if self.plan.permission_issues:
            preview = "; ".join(self.plan.permission_issues[:5])
            return MigrateOutcome(
                False, "permission_denied",
                message=f"{len(self.plan.permission_issues)} 个目录无权限访问，"
                        f"请以管理员身份重试。示例：{preview}")

        self.session = Session.create(self.source, self.target, root=self.session_root)
        self.session.totals = {"files": self.plan.totals.files,
                               "bytes": self.plan.totals.bytes}

        # ③ 复制
        copy_result = copy_run(self.plan, self.session, on_progress=on_progress,
                               should_cancel=should_cancel, should_pause=should_pause)
        if copy_result.cancelled:
            return MigrateOutcome(False, "cancelled", copy=copy_result)
        if copy_result.failures:
            self.session.save()
            return MigrateOutcome(False, "copy_failed", copy=copy_result,
                                  message=f"{len(copy_result.failures)} 个文件未能复制")

        # ④ 校验
        verify_result = verifier.verify(self.source, self.target, deep=self.deep_verify)
        if not verify_result.ok:
            self.session.phase = "verify_failed"
            self.session.save()
            return MigrateOutcome(False, "verify_failed", copy=copy_result,
                                  verify=verify_result,
                                  message="校验未通过，源目录未改动")

        # ⑤ 源目录改名为备份
        self.backup_path = str(self.source.parent / backup_name(self.source))
        try:
            os.rename(long_path(self.source), long_path(self.backup_path))
        except OSError as exc:
            return MigrateOutcome(False, "rename_failed", copy=copy_result,
                                  verify=verify_result, message=str(exc))
        self.session.phase = "renamed"
        self.session.backup_path = self.backup_path
        self.session.save()

        # ⑥ 建链接并验证
        try:
            kind = linker.create(self.source, self.target, is_dir=True,
                                 prefer_junction=True)
            self._link_created = True
        except OSError as exc:
            self.session.phase = "link_failed"
            self.session.save()
            return MigrateOutcome(False, "link_failed", copy=copy_result,
                                  verify=verify_result, backup_path=self.backup_path,
                                  message=str(exc))

        if not linker.verify(self.source):
            self.session.phase = "link_verify_failed"
            self.session.save()
            return MigrateOutcome(False, "link_verify_failed", copy=copy_result,
                                  verify=verify_result, backup_path=self.backup_path,
                                  link_kind=kind, message="链接已建但无法访问")

        self.session.phase = "done"
        self.session.save()
        return MigrateOutcome(True, "done", copy=copy_result, verify=verify_result,
                              backup_path=self.backup_path, link_kind=kind)

    # --- 收尾与回滚 -------------------------------------------------
    def cleanup_backup(self) -> bool:
        if not self.backup_path or not os.path.exists(long_path(self.backup_path)):
            return False
        rmtree_force(long_path(self.backup_path))
        if self.session:
            self.session.phase = "cleaned"
            self.session.save()
        return True

    def rollback(self) -> bool:
        """幂等：删链接 → 备份改回原名。"""
        if self._link_created or self._is_link(self.source):
            try:
                os.rmdir(long_path(self.source))
            except OSError:
                try:
                    os.unlink(long_path(self.source))
                except OSError:
                    return False
        if self.backup_path and os.path.exists(long_path(self.backup_path)):
            try:
                os.rename(long_path(self.backup_path), long_path(self.source))
            except OSError:
                return False
        if self.session:
            self.session.phase = "rolled_back"
            self.session.save()
        return True

    @staticmethod
    def _is_link(path) -> bool:
        import stat as _stat
        try:
            return bool(os.lstat(long_path(path)).st_file_attributes
                        & _stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False
