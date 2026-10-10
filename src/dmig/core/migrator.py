"""迁移编排：把扫描、复制、校验、改名、建链接、清理/回滚串成状态机。"""
from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import guard, linker, locks, permission, reparse, scanner, verifier
from .copier import run as copy_run
from .fsutil import is_inside, is_link_kind, long_path, reparse_kind, strip_long_prefix
from .models import CopyResult, MigrationPlan, VerifyResult
from .runlog import LOG_SUFFIX, RunLog
from .session import Session

# 改名要独占整棵树，而冲突（资源管理器开着子目录、终端 cwd 在里面、杀软在扫）
# 往往是瞬时的，所以退避重试；仍失败才去查是谁占用。
RENAME_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0)
RENAME_LOCK_SAMPLE = 2000

ABORT_TEXT = {
    "disk_full": "目标卷空间不足，已立即停止（继续重试只会把剩下的文件逐个失败一遍）",
    "symlink_privilege": "创建符号链接需要特权，当前进程没有，已立即停止。"
                         "请以管理员身份重试，或开启 Windows 开发者模式",
}

# 改名已经完成、数据已通过校验，只差源目录那条链接的几种现场
POST_RENAME_PHASES = frozenset({"renamed", "link_failed", "link_verify_failed"})


def is_reparse_path(path) -> bool:
    try:
        return bool(os.lstat(long_path(path)).st_file_attributes
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def awaiting_source_link(sess, source) -> bool:
    """会话是否停在「数据已就位、只差源目录链接」这一步。

    这种现场下源路径既不是目录也不是链接，前置校验必然报「源目录不存在」，
    照着常规流程重跑只会把用户堵死在 guard 上——他明明只差最后一步，
    唯一的出口却是回滚（把备份改回原名，几小时的迁移白做）。

    光看 phase 不够：备份目录必须真的还在，否则现场已经被清理或回滚过，
    这时候凭空建一条链接指向目标是在编造状态。
    """
    if sess is None or sess.phase not in POST_RENAME_PHASES:
        return False
    if not sess.backup_path or not os.path.isdir(long_path(sess.backup_path)):
        return False
    # 源路径又变成了真目录（用户手工把备份改回来了），交给常规流程重跑
    return not (os.path.isdir(long_path(source)) and not is_reparse_path(source))


@dataclass
class MigrateOutcome:
    ok: bool
    phase: str
    copy: CopyResult | None = None
    verify: VerifyResult | None = None
    backup_path: str | None = None
    link_kind: str | None = None
    message: str = ""
    # 会话记下的每条链接：rel / kind / raw_target / new_target / is_dangling。
    # 报告靠它标出断链、给出重写前后的值；不给出来，这些就只躺在会话文件里。
    links: list[dict] = field(default_factory=list)
    # 复制保不住的属性（稀疏 / 硬链接 / ADS），来自计划。这几样复制不会报错，
    # 结果却与原件不同，报告必须把它们和「校验通过」一起摆出来。
    sparse_files: int = 0
    hardlinked_files: int = 0
    ads_files: int = 0
    # 按排除项跳过的条目。它们没复制到目标、迁移后只存在于备份里，
    # 报告要逐条列出：删备份前用户得有据可查。
    excluded: list[str] = field(default_factory=list)
    # 这趟运行的日志文件（`<会话>.log`）。失败现场往往已经变了，
    # 光看结构化数据回答不了「几个小时里走到了哪一步」。
    log_path: str = ""


def _same_path(a, b) -> bool:
    """两条路径是否指向同一处（剥前缀、取绝对、大小写不敏感）。"""
    def norm(p) -> str:
        return os.path.normcase(os.path.abspath(strip_long_prefix(str(p))))
    return norm(a) == norm(b)


def backup_name(source) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{Path(source).name}.bak_{stamp}"


def rmtree_force(path) -> list[str]:
    """删除目录树，返回**没能删掉**的路径清单（空列表代表删干净了）。

    Windows 上只读文件会让 shutil.rmtree 直接抛 PermissionError，
    迁移来的目录里很可能带只读属性，所以先清掉只读位再重试。
    删不掉的项必须如实回报：调用方要据此判断备份是否真的没了，
    否则界面会一边显示「备份已删除」一边把几十 GB 留在盘上。
    """
    leftovers: list[str] = []

    def on_error(func, name, exc):
        try:
            os.chmod(name, stat.S_IWRITE)
            func(name)
        except OSError:
            try:
                # 带未处理标签的目录形态重解析点连 scandir 都失败（WinError 1920），
                # 摘掉标签就能正常删；否则会被误记成一条「删不掉的残留」。
                # 链接类（junction / symlink）绝不能摘：那等于动到外部仓库。
                if not is_link_kind(reparse_kind(os.lstat(long_path(name)))):
                    reparse.delete_reparse_point(name)
                func(name)
            except OSError:
                leftovers.append(str(name))

    try:
        shutil.rmtree(path, onexc=on_error)
    except OSError as exc:
        leftovers.append(f"{path}: {exc}")
    return leftovers


class Migrator:
    """一次迁移任务的完整生命周期。"""

    def __init__(self, source, target, *, mappings=None, deep_verify=False,
                 session_root=None, skip_unknown_reparse=False, excludes=None,
                 probe_ads=True):
        self.source = Path(source)
        self.target = Path(target)
        self.mappings = dict(mappings or {})
        self.deep_verify = deep_verify
        self.session_root = session_root
        self.skip_unknown_reparse = skip_unknown_reparse
        self.excludes = [str(item) for item in (excludes or [])]
        self.probe_ads = probe_ads
        self.plan: MigrationPlan | None = None
        self.session: Session | None = None
        self.backup_path: str | None = None
        self.cleanup_error: str = ""
        self._link_created = False
        self.runlog: RunLog | None = None

    # --- 日志 -------------------------------------------------------
    def _ensure_runlog(self) -> None:
        """会话一旦就位，就把日志挂到它旁边（`<id>.log`，与 `.json`/`.done` 同前缀）。"""
        if self.session is not None and self.runlog is None:
            self.runlog = RunLog(self.session.path.with_suffix(LOG_SUFFIX))

    def _log(self, message: str) -> None:
        if self.runlog is not None:
            self.runlog.write(message)

    def _log_start(self) -> None:
        self._log(f"开始一趟：{self.source} → {self.target}")
        self._log(f"参数：{self.run_options()}")
        plan = self.plan
        if plan is None:
            return
        self._log(f"扫描：files={plan.totals.files} dirs={plan.totals.dirs} "
                  f"bytes={plan.required_bytes} links={len(plan.links)} "
                  f"unknown_reparse={len(plan.unknown_reparse)} "
                  f"permission_issues={len(plan.permission_issues)}")
        self._log(f"属性：sparse={plan.sparse_files} "
                  f"hardlinked={plan.hardlinked_files} ads={plan.ads_files} "
                  f"（ads_probed={plan.ads_probed}）")
        if plan.excluded:
            self._log(f"排除：{len(plan.excluded)} 项，前几项 {plan.excluded[:5]}")
        dangling = [link.rel for link in plan.links if link.is_dangling]
        if dangling:
            self._log(f"断链：{len(dangling)} 条，前几条 {dangling[:5]}")

    def _log_outcome(self, outcome: MigrateOutcome) -> None:
        """把这一趟的结论一次记全（挂在唯一出口上，不必逐个返回点去写）。"""
        log = self.runlog
        if log is None:
            return
        copy = outcome.copy
        if copy is not None:
            log.write(f"复制：copied={copy.copied_files} bytes={copy.copied_bytes} "
                      f"skipped={copy.skipped} cancelled={copy.cancelled} "
                      f"aborted={copy.aborted or '-'}")
            if copy.failures:
                log.failures("复制失败", [f"{f.rel}（{f.reason} {f.process}）".strip()
                                        for f in copy.failures])
            if copy.dir_times_failed:
                log.failures("目录时间戳未写回", copy.dir_times_failed)
        verify = outcome.verify
        if verify is not None:
            log.write(f"校验：ok={verify.ok} expected={verify.expected_files} "
                      f"actual={verify.actual_files} missing={len(verify.missing)} "
                      f"mismatch={len(verify.mismatch)}")
            if verify.missing:
                log.failures("校验缺失", verify.missing)
            if verify.mismatch:
                log.failures("校验不一致", verify.mismatch)
        if outcome.backup_path:
            log.write(f"备份：{outcome.backup_path}")
        if outcome.link_kind:
            log.write(f"源目录链接：{outcome.link_kind}")
        log.write(f"结果：ok={outcome.ok} phase={outcome.phase} {outcome.message}".rstrip())

    # --- 会话 -------------------------------------------------------
    def adopt_session(self, sess: Session | None) -> bool:
        """接管一个既有会话，让 `execute()` 沿用它的 `.done` 记账**真正续传**。

        源或目标对不上就拒绝并返回 False：`.done` 里存的是**相对路径**，
        换一棵目录树沿用，等于把不相干的文件当成「我们已经复制过」而跳过——
        默认校验只看大小与时间戳，看不出来，最终会丢数据。
        """
        if sess is None:
            return False
        if not (_same_path(sess.source, self.source)
                and _same_path(sess.target, self.target)):
            return False
        self.session = sess
        # 上一轮可能已经改过名，备份路径是 discard_target 判断
        # 「数据是否另有副本」的依据之一，必须带过来
        if sess.backup_path:
            self.backup_path = sess.backup_path
        return True

    # --- 计划 -------------------------------------------------------
    def run_options(self) -> dict:
        """本次运行的参数，写进会话供续传照原样重放。

        续传时最容易丢的就是链接重写映射：会话里不记，`resume` 就只能用空映射
        重建 Migrator，目标端几百个 junction 全部指回迁移前的旧位置。
        排除项同样不能丢：丢了就会把用户明确排除的目录重新复制一遍。
        """
        return {"mappings": dict(self.mappings),
                "deep_verify": self.deep_verify,
                "skip_unknown_reparse": self.skip_unknown_reparse,
                "excludes": list(self.excludes),
                "probe_ads": self.probe_ads}

    def plan_it(self, on_progress=None) -> MigrationPlan:
        self.plan = scanner.scan(self.source, self.target,
                                 mappings=self.mappings, on_progress=on_progress,
                                 skip_unknown_reparse=self.skip_unknown_reparse,
                                 excludes=self.excludes,
                                 probe_ads=self.probe_ads)
        return self.plan

    def guard_errors(self) -> list[str]:
        return [i.message for i in
                guard.check(self.source, self.target,
                            required_bytes=self.plan.required_bytes if self.plan else 0,
                            link_count=len(self.plan.links) if self.plan else 0)
                if i.severity == "error"]

    def privilege_blockers(self) -> str:
        """动手前能查出来的权限问题，绝不留到复制几小时之后再暴露。

        只有**文件**符号链接非特权不可（目录符号链接会降级成 junction），
        实测未提权且开发者模式关闭时 CreateSymbolicLinkW 一律 WinError 1314。
        没有这道预检，用户要等整棵树复制完、进入建链阶段才知道要提权。
        """
        if not self.plan or not self.plan.file_symlinks:
            return ""
        if permission.can_create_symlink():
            return ""
        return (f"{self.plan.file_symlinks} 个文件符号链接需要管理员特权才能重建，"
                f"当前进程没有该特权。请以管理员身份重试，"
                f"或开启 Windows 开发者模式后再迁移。")

    # --- 属主接管与 ACL -------------------------------------------------
    def take_over_denied(self) -> tuple[int, str]:
        """接管那些连提权进程也读不到的目录，返回 (接管数, 失败原因)。

        没有这一条，无权限目录就只剩「排除掉」一条路，而排除意味着那些内容
        只留在备份里——想全量迁移的用户就没辙了。

        顺序不能反：必须先把原安全描述符导出到会话，再去改属主。反过来就是
        拿被改过的描述符当原件，回滚也还原不回去（原属主已经换人了）。
        导出失败（连 READ_CONTROL 都没有）照样接管，但记下 sddl 为 None：
        那种路径回滚不会还原权限，报告里会如实说明，总好过把用户堵死在原地。
        """
        if not permission.is_elevated():
            return 0, "接管属主需要管理员权限，请先以管理员身份重启"
        if self.plan is None:
            self.plan_it()
        targets = list(self.plan.permission_issues)
        if not targets:
            return 0, ""

        if self.session is None:
            self.session = Session.create(self.source, self.target,
                                          root=self.session_root)
        recorded = {item.get("path") for item in self.session.acl_backup}
        taken = 0
        failed: list[str] = []
        for path in targets:
            if path not in recorded:
                self.session.acl_backup.append(
                    {"path": path, "sddl": permission.backup_acl(path)})
            if permission.take_ownership(path):
                taken += 1
            else:
                failed.append(path)
        self.session.save()
        self._ensure_runlog()
        self._log(f"接管属主：{taken} 个目录成功、{len(failed)} 个失败"
                  + (f"，失败示例 {failed[:3]}" if failed else ""))
        for item in self.session.acl_backup:
            if not item.get("sddl"):
                self._log(f"接管属主：{item.get('path')} 的原始 ACL 没能读出，"
                          f"回滚时不会还原它的权限")

        # 权限变了，旧计划作废：不重扫的话界面还停在「无权限」那一份上，
        # 用户点「开始迁移」会立刻又被同一条校验拦下。
        self.plan = None
        self.plan_it()
        if not taken:
            preview = "；".join(failed[:3])
            return 0, f"{len(failed)} 个目录接管失败，示例：{preview}"
        return taken, ""

    def _live_path(self, original: str) -> str:
        """把接管时记下的源路径换算成当前实际位置。

        接管发生在源树上，而回滚时源目录可能已经被改名成备份：路径不换算的话，
        还原会打在一条不存在的路径上，权限永远回不去。
        """
        if not original:
            return ""
        if os.path.exists(long_path(original)) or not self.backup_path:
            return original
        source = os.path.abspath(str(self.source))
        text = os.path.abspath(str(original))
        if not is_inside(text, source):
            return original
        if not os.path.isdir(long_path(self.backup_path)):
            return original
        rel = os.path.relpath(text, source)
        return os.path.join(os.path.abspath(self.backup_path), rel)

    def _restore_acl(self) -> list[str]:
        """把接管过的目录的原始安全描述符写回去，返回没能还原的路径。

        没记下原描述符的（接管时连 READ_CONTROL 都没有）无从还原，直接跳过——
        它们在接管时就已经如实告知过用户。
        """
        if self.session is None:
            return []
        failed: list[str] = []
        for item in self.session.acl_backup:
            sddl = item.get("sddl")
            if not sddl:
                continue
            path = self._live_path(str(item.get("path", "")))
            if not path or not os.path.exists(long_path(path)):
                failed.append(path or str(item.get("path", "")))
                continue
            if not permission.restore_acl(path, sddl):
                failed.append(path)
        return failed

    # --- 重新校验 ---------------------------------------------------
    def reverify_baseline(self) -> tuple[str, str]:
        """重新校验该拿什么当原件，返回 (路径, 无法校验的原因)。

        迁移成功后源路径已经变成指向目标的链接：拿它当原件就是拿目标和目标自己比，
        校验必然通过——这是「重新校验」此前最危险的地方，
        用户会据此放心删掉备份，而目标端可能早就被改坏了。
        这时真正的原件只剩备份；备份也没了就只能如实说「没有可比对的原件」，
        绝不能返回一个假的通过。
        """
        if os.path.isdir(long_path(self.source)) and not is_reparse_path(self.source):
            return str(self.source), ""
        if self.backup_path and os.path.isdir(long_path(self.backup_path)):
            return self.backup_path, ""
        return "", (f"源目录 {self.source} 已经是指向目标端的链接，"
                    f"而备份也不在了——没有可比对的原件。"
                    f"要确认目标端是否完整，只能拿别的副本自行比对")

    # --- 执行 -------------------------------------------------------
    def execute(self, *, on_progress=None, should_cancel=None,
                should_pause=None) -> MigrateOutcome:
        outcome = self._execute(on_progress=on_progress,
                                should_cancel=should_cancel,
                                should_pause=should_pause)
        # 日志同样挂在唯一出口上；这一趟才新建的会话要到这时候才能挂。
        self._ensure_runlog()
        self._log_outcome(outcome)
        # 链接清单与属性计数都挂在唯一出口上：_execute 有十来个返回点，逐个补必然漏。
        if self.session is not None:
            outcome.links = list(self.session.links)
        if self.plan is not None:
            outcome.sparse_files = self.plan.sparse_files
            outcome.hardlinked_files = self.plan.hardlinked_files
            outcome.ads_files = self.plan.ads_files
            outcome.excluded = list(self.plan.excluded)
        if self.runlog is not None:
            outcome.log_path = str(self.runlog.path)
        return outcome

    def _execute(self, *, on_progress=None, should_cancel=None,
                 should_pause=None) -> MigrateOutcome:
        # 会话停在改名之后时源目录已经不在了，扫描与前置校验都没有意义，
        # 必须排在它们前面，否则一律失败于「源目录不存在」。
        resumed = self._resume_after_rename()
        if resumed is not None:
            return resumed

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
                        f"请以管理员身份重试；提权后仍读不到的可以接管属主"
                        f"（命令行 --take-ownership / 界面上的「接管这些目录的权限」），"
                        f"也可以直接排除掉（命令行 --exclude，被排除的内容只留在备份里）。"
                        f"示例：{preview}")

        blockers = self.privilege_blockers()
        if blockers:
            return MigrateOutcome(False, "privilege_required", message=blockers)

        if self.session is None:
            self.session = Session.create(self.source, self.target,
                                          root=self.session_root)
            self.session.begin_run("copying")
        else:
            # 续传：沿用既有会话，`.done` 记账就是「哪些文件已经复制过」的依据。
            # 原来这里无条件 Session.create，等于每次都从空白记账重新开始，
            # resume 会把整棵树重复制一遍（结果正确，但白花一次全量时间）。
            self.session.begin_run("resumed")
        self.session.totals = {"files": self.plan.totals.files,
                               "bytes": self.plan.totals.bytes}
        self.session.options = self.run_options()
        self._ensure_runlog()
        self._log_start()

        # ③ 复制
        copy_result = copy_run(self.plan, self.session, on_progress=on_progress,
                               should_cancel=should_cancel, should_pause=should_pause)
        if copy_result.aborted:
            self.session.save()
            return MigrateOutcome(
                False, "aborted", copy=copy_result,
                message=ABORT_TEXT.get(copy_result.aborted, copy_result.aborted))
        if copy_result.cancelled:
            return MigrateOutcome(False, "cancelled", copy=copy_result)
        if copy_result.failures:
            self.session.save()
            return MigrateOutcome(False, "copy_failed", copy=copy_result,
                                  message=f"{len(copy_result.failures)} 个文件未能复制")

        # ④ 校验
        verify_result = verifier.verify(
            self.source, self.target, deep=self.deep_verify,
            on_progress=on_progress, should_cancel=should_cancel,
            totals=self.plan.totals,
            skip_unknown_reparse=self.plan.skip_unknown_reparse,
            excludes=self.plan.excludes)
        if verify_result.cancelled:
            self.session.phase = "verify_cancelled"
            self.session.save()
            return MigrateOutcome(False, "cancelled", copy=copy_result,
                                  verify=verify_result, message="校验被取消，源目录未改动")
        if not verify_result.ok:
            self.session.phase = "verify_failed"
            self.session.save()
            return MigrateOutcome(False, "verify_failed", copy=copy_result,
                                  verify=verify_result,
                                  message="校验未通过，源目录未改动")

        # ⑤ 逐个核对重建出来的链接
        # 必须排在改名之前：此刻目标端已是完整副本，链条完整、可逐个解析；
        # 等源目录变成链接之后再查，INSIDE_SOURCE 那批就全指过来了。
        broken = verifier.verify_links(self.plan, self.target,
                                       should_cancel=should_cancel)
        if should_cancel is not None and should_cancel():
            self.session.phase = "link_check_cancelled"
            self.session.save()
            return MigrateOutcome(False, "cancelled", copy=copy_result,
                                  verify=verify_result,
                                  message="链接核对被取消，源目录未改动")
        if broken:
            preview = "、".join(broken[:5])
            more = f" 等 {len(broken)} 条" if len(broken) > 5 else ""
            self.session.phase = "links_broken"
            self.session.save()
            return MigrateOutcome(
                False, "links_broken", copy=copy_result, verify=verify_result,
                message=f"{len(broken)} 条链接在目标端没有指向计划要求的位置："
                        f"{preview}{more}。源目录未改动，可删掉目标端重来")

        # ⑥ 源目录改名为备份
        self.backup_path, rename_error = self._rename_source_to_backup()
        if rename_error:
            self.backup_path = None
            self.session.phase = "rename_failed"
            self.session.save()
            return MigrateOutcome(False, "rename_failed", copy=copy_result,
                                  verify=verify_result, message=rename_error)
        self.session.phase = "renamed"
        self.session.backup_path = self.backup_path
        self.session.save()

        # ⑦ 建链接并验证
        kind, phase, detail = self._create_source_link()
        if phase != "done":
            self.session.phase = phase
            self.session.save()
            return MigrateOutcome(False, phase, copy=copy_result,
                                  verify=verify_result, backup_path=self.backup_path,
                                  link_kind=kind or None, message=detail)

        self.session.phase = "done"
        self.session.save()
        return MigrateOutcome(True, "done", copy=copy_result, verify=verify_result,
                              backup_path=self.backup_path, link_kind=kind)

    def _create_source_link(self) -> tuple[str, str, str]:
        """在源路径上建指向目标的链接，返回 (链接类型, phase, 详情)。

        phase 为 "done" 表示建好且能访问；否则是 "link_failed" / "link_verify_failed"，
        详情即失败原因。上一趟已经建过（link_verify_failed）时先摘掉再建：
        源路径上留着一个重解析点，os.mkdir 会撞 WinError 183。
        """
        if is_reparse_path(self.source):
            try:
                os.rmdir(long_path(self.source))
            except OSError:
                try:
                    os.unlink(long_path(self.source))
                except OSError as exc:
                    return "", "link_failed", str(exc)
        try:
            kind = linker.create(self.source, self.target, is_dir=True,
                                 prefer_junction=True)
        except OSError as exc:
            return "", "link_failed", str(exc)
        self._link_created = True
        if not linker.verify(self.source):
            return kind, "link_verify_failed", "链接已建但无法访问"
        return kind, "done", ""

    def _resume_after_rename(self) -> MigrateOutcome | None:
        """会话停在改名之后：只补建源目录链接，不再从常规流程头部重来。

        返回 None 表示「不是这种现场，照常执行」。
        """
        if not awaiting_source_link(self.session, self.source):
            return None
        self._ensure_runlog()
        self._log(f"会话停在改名之后：只补建源目录链接（备份 {self.session.backup_path}）")
        self.backup_path = self.session.backup_path or self.backup_path
        kind, phase, detail = self._create_source_link()
        self.session.phase = phase
        self.session.save()
        if phase != "done":
            return MigrateOutcome(False, phase, backup_path=self.backup_path,
                                  link_kind=kind or None, message=detail)
        return MigrateOutcome(
            True, "done", backup_path=self.backup_path, link_kind=kind,
            message="数据在此前那趟已复制并校验完毕，本次只补建了源目录链接")

    # --- 改名与占用诊断 ---------------------------------------------
    def _rename_source_to_backup(self) -> tuple[str, str]:
        """把源目录改名为备份，返回 (备份路径, 失败原因)；成功时原因为空串。

        改名要求整棵树无人持有句柄：资源管理器开着某个子目录、终端 cwd 在里面、
        杀软正在扫，都会让它失败于 ACCESS_DENIED / SHARING_VIOLATION。这类冲突
        多半是瞬时的，所以先退避重试；仍然失败才去查占用——原来只试一次、
        只回一句 WinError 5，用户既不知道该关什么，也没有下一步可走。
        """
        backup = str(self.source.parent / backup_name(self.source))
        last = ""
        for delay in (0.0,) + RENAME_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                os.rename(long_path(self.source), long_path(backup))
                return backup, ""
            except OSError as exc:
                last = str(exc)
        held = self._blocking_processes()
        if held:
            last = f"{last}；正在占用源目录的进程：{held}。关掉它们后重试即可"
        else:
            last = (f"{last}；未查出占用进程（可能是某个进程的当前目录在源目录里，"
                    f"或权限不足）。源目录未改动，目标端已有完整副本")
        return "", last

    def _blocking_processes(self, limit: int = RENAME_LOCK_SAMPLE) -> str:
        """采样源树里的文件，问重启管理器是谁占着。

        RM 实测注册目录查不到任何进程，只能拿文件去问；而诊断要的答案是
        「关掉哪个程序」，采样足够，不值得为此再完整遍历几十万个文件。
        """
        sample: list[str] = []
        for root, dirs, files in os.walk(long_path(self.source)):
            keep = []
            for name in dirs:
                full = os.path.join(root, name)
                try:
                    if reparse_kind(os.lstat(full)) is None:
                        keep.append(name)
                except OSError:
                    continue
            dirs[:] = keep
            for name in files:
                sample.append(os.path.join(root, name))
                if len(sample) >= limit:
                    break
            if len(sample) >= limit:
                break
        if not sample:
            return ""
        held = locks.who_locks_many(sample)
        return ", ".join(f"{p.name} ({p.pid})" for p in held)

    # --- 收尾与回滚 -------------------------------------------------
    def skipped_reparse_count(self) -> int:
        """被显式跳过、因此只存在于备份里的重解析点数量。"""
        if not self.plan or not self.plan.skip_unknown_reparse:
            return 0
        return len(self.plan.unknown_reparse)

    def excluded_count(self) -> int:
        """被排除、因此只存在于备份里的条目数量。

        与跳过的重解析点同理：迁移成功后源目录会改名成备份、原位置变成一条链接，
        这些条目的唯一副本就在备份里，删掉备份等于把它们永久删掉。
        """
        return len(self.plan.excluded) if self.plan else 0

    def cleanup_backup(self, force: bool = False) -> bool:
        """删除备份。返回是否**真的删干净了**，原因写在 cleanup_error。"""
        self.cleanup_error = ""
        self._ensure_runlog()
        if not self.backup_path or not os.path.exists(long_path(self.backup_path)):
            self.cleanup_error = "没有可删除的备份"
            self._log(f"删除备份：被拒（{self.cleanup_error}）")
            return False

        held: list[str] = []
        excluded = self.excluded_count()
        if excluded:
            held.append(f"{excluded} 个目录被排除、没有复制到目标")
        skipped = self.skipped_reparse_count()
        if skipped:
            held.append(f"{skipped} 个非链接类重解析点被跳过、没有复制到目标")
        if held and not force:
            self.cleanup_error = (
                f"备份里有{('；'.join(held))}，它们只存在于备份里，"
                f"删掉备份就会永久消失。确认这些内容不再需要后，再强制删除。")
            self._log(f"删除备份：被拒（{self.cleanup_error}）")
            return False

        leftovers = rmtree_force(long_path(self.backup_path))
        if os.path.exists(long_path(self.backup_path)):
            sample = "；".join(os.path.basename(p.rstrip("\\/")) for p in leftovers[:3])
            self.cleanup_error = (
                f"备份未能完全删除（{len(leftovers)} 项删不掉：{sample}），"
                f"目录仍在：{self.backup_path}")
            self._log(f"删除备份：未删干净，{self.cleanup_error}")
            return False

        if self.session:
            self.session.phase = "cleaned"
            self.session.save()
        self._log(f"删除备份：完成（force={force}）{self.backup_path}")
        return True

    def discard_target(self) -> bool:
        """删掉半成品目标目录。返回是否真的删干净了，原因写在 cleanup_error。

        中途失败（磁盘满、被占用、取消）留下的是「源完好、目标半份」的现场，
        没有出口的话用户只能手工去删几十万个文件。删之前必须先确认数据另有副本：
        源目录已经是指向目标的链接时，目标就是活数据，删掉等于把迁移成果清空。
        """
        self.cleanup_error = ""
        self._ensure_runlog()
        if is_reparse_path(self.source):
            self.cleanup_error = "源目录已经是指向目标的链接，目标端是活数据，不能删"
            self._log(f"删除目标：被拒（{self.cleanup_error}）")
            return False
        backup_ok = bool(self.backup_path) and os.path.exists(long_path(self.backup_path))
        if not os.path.isdir(long_path(self.source)) and not backup_ok:
            self.cleanup_error = "源目录与备份都不在，无法确认数据另有副本，拒绝删除目标"
            self._log(f"删除目标：被拒（{self.cleanup_error}）")
            return False
        if not os.path.exists(long_path(self.target)):
            self.cleanup_error = "目标目录不存在，无需清理"
            self._log(f"删除目标：被拒（{self.cleanup_error}）")
            return False

        leftovers = rmtree_force(long_path(self.target))
        if os.path.exists(long_path(self.target)):
            sample = "；".join(os.path.basename(p.rstrip("\\/")) for p in leftovers[:3])
            self.cleanup_error = (
                f"目标目录未能完全删除（{len(leftovers)} 项删不掉：{sample}），"
                f"目录仍在：{self.target}")
            self._log(f"删除目标：未删干净，{self.cleanup_error}")
            return False

        if self.session:
            self.session.phase = "discarded"
            self.session.save()
        self._log(f"删除目标：完成 {self.target}")
        return True

    def rollback(self) -> bool:
        """幂等：还原被接管的 ACL → 删链接 → 备份改回原名。

        先还原权限再动目录：接管改的是源树上的属主与访问控制，回滚不退回
        等于把用户的权限配置永久改掉了。
        """
        self.cleanup_error = ""
        self._ensure_runlog()
        acl_failed = self._restore_acl()
        if acl_failed:
            preview = "；".join(acl_failed[:3])
            self.cleanup_error = (f"{len(acl_failed)} 个目录的原始权限没能还原"
                                  f"（示例：{preview}），它们仍是接管后的属主。")

        if self._link_created or is_reparse_path(self.source):
            try:
                os.rmdir(long_path(self.source))
            except OSError:
                try:
                    os.unlink(long_path(self.source))
                except OSError:
                    self._log("回滚：摘掉源目录链接失败")
                    return False
        if self.backup_path and os.path.exists(long_path(self.backup_path)):
            try:
                os.rename(long_path(self.backup_path), long_path(self.source))
            except OSError:
                self._log(f"回滚：备份改回原名失败 {self.backup_path}")
                return False
        if self.session:
            self.session.phase = "rolled_back"
            self.session.save()
        self._log(f"回滚：完成 {self.source}" + (f"；{self.cleanup_error}"
                                                if self.cleanup_error else ""))
        return True
