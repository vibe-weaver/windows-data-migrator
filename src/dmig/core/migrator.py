"""迁移编排：把扫描、复制、校验、改名、建链接、清理/回滚串成状态机。"""
from __future__ import annotations

import ctypes
import os
import shutil
import stat
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import guard, linker, locks, permission, reparse, scanner, verifier
from .copier import run as copy_run
from .fsutil import (is_inside, is_link_kind, long_path, rel_to, reparse_kind,
                     strip_long_prefix)
from .models import CopyResult, MigrationPlan, VerifyResult
from .runlog import LOG_SUFFIX, RunLog
from .session import Session

# 改名要独占整棵树，而冲突（资源管理器开着子目录、杀软在扫）往往是瞬时的，
# 所以退避重试；仍失败才去查是谁占用。
# （注：曾经写「终端 cwd 在里面」也算一种，实测 cwd 并不阻塞改名，已删。）
RENAME_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0)
RENAME_LOCK_SAMPLE = 2000

# 改名失败的常见 Windows 错误码 → 人话。
# 直接用 str(exc) 会把 Python 的异常格式连同 `\\?\` 长路径前缀一起甩到界面上
# （真实反馈：标题下面挂着 `[WinError 5] 拒绝访问。: '\\?\C:\wwwwwww' -> ...`）。
RENAME_ERROR_TEXT = {
    5: "拒绝访问（被其他程序占用，或权限不足）",
    32: "文件正被其他程序使用",
    33: "文件的一部分被锁定",
    183: "目标位置已经存在同名目录",
}


def _readable_rename_error(exc: OSError) -> str:
    """把改名失败说成人话；没收录的错误码退回原文，免得把信息吞掉。"""
    code = getattr(exc, "winerror", None)
    known = RENAME_ERROR_TEXT.get(code)
    if known:
        return f"{known}（WinError {code}）"
    return str(exc)

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
    # 整趟卡在某一步时的结构化信息（改名失败、建链失败……）。
    # 只有一句 message 的话，报告页的清单就没东西可列，用户看到的是
    # 刺眼的红标题配一张空表。
    blocked_path: str = ""
    blocked_reason: str = ""
    blocked_process: str = ""


@dataclass
class RestoreOutcome:
    """回滚的结果，供报告页把「迁移结果」那一套换成「恢复结果」。

    和 MigrateOutcome 是两回事：那一页回答的是「数据搬过去了吗」，回滚之后
    该回答的是「原路径回来了吗、目标端那份怎么样了」。统计网格里的
    复制数、校验缺失数在那一刻都没有意义——那件事已经被撤销了。
    """

    ok: bool = False
    source: str = ""
    source_restored: bool = False
    backup_path: str = ""
    # 权限还原的情况，已经是人话（来自 cleanup_error 里那一段）；没接管过就是空串
    acl_note: str = ""
    target: str = ""
    # cleared / kept / skipped / missing / failed，由界面映射成文案
    target_state: str = ""
    target_note: str = ""
    message: str = ""


def _same_path(a, b) -> bool:
    """两条路径是否指向同一处（剥前缀、取绝对、大小写不敏感）。"""
    def norm(p) -> str:
        return os.path.normcase(os.path.abspath(strip_long_prefix(str(p))))
    return norm(a) == norm(b)


def backup_name(source) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{Path(source).name}.bak_{stamp}"


# kernel32 句柄：rmtree_force 直接用 DeleteFileW / RemoveDirectoryW，
# 绕开被运行环境 sitecustomize 劫持成「丢回收站」的 shutil.rmtree。
try:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
except OSError:  # pragma: no cover - 非 Windows
    _k32 = None


def _delete_file_win32(path) -> bool:
    """直接调 DeleteFileW 删文件，绕开被 sitecustomize 劫持的 shutil/os.unlink。

    返回是否删掉。失败不抛——调用方按 leftovers 流程处理。
    """
    try:
        return bool(_k32.DeleteFileW(path))
    except OSError:
        return False


def _remove_dir_win32(path) -> bool:
    """直接调 RemoveDirectoryW 删空目录，绕开被劫持的 os.rmdir。"""
    try:
        return bool(_k32.RemoveDirectoryW(path))
    except OSError:
        return False


def _force_clear_readonly(path) -> None:
    """清掉只读位（迁移来的目录常带只读属性，DeleteFileW 删不掉）。"""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass


def rmtree_force(path) -> list[str]:
    """删除目录树，返回**没能删掉**的路径清单（空列表代表删干净了）。

    直接用 Win32 API（DeleteFileW / RemoveDirectoryW）递归删除，**不走
    shutil.rmtree**——运行环境（如桌面应用的 sitecustomize）会把
    shutil.rmtree 劫持成「丢回收站」版，回收站一失败整棵树就删不掉，
    界面却报不出原因。Win32 直删只对真正被占用句柄锁住的文件失败，
    那种情况由调用方查占用进程提示用户关闭。

    删不掉的项必须如实回报：调用方要据此判断备份是否真的没了，
    否则界面会一边显示「备份已删除」一边把几十 GB 留在盘上。

    顶层本身就是链接类重解析点（junction / 符号链接）时不能跟随：
    直接摘掉链接本身（RemoveDirectoryW / DeleteFileW），目标数据绝不动。
    """
    leftovers: list[str] = []
    lp = long_path(path)

    try:
        st = os.lstat(lp)
    except OSError:
        return leftovers                       # 不存在 = 删干净了
    if is_link_kind(reparse_kind(st)):
        # 链接本身：摘链接，不跟随目标
        ok = (_remove_dir_win32(lp) if os.path.isdir(lp)
              else _delete_file_win32(lp))
        if not ok and os.path.exists(lp):
            leftovers.append(str(path))
        return leftovers

    def _recurse(dir_path: str) -> None:
        try:
            entries = list(os.scandir(dir_path))
        except OSError as exc:
            # 带未处理标签的目录形态重解析点连 scandir 都失败（WinError 1920），
            # 摘掉标签就能正常删；链接类绝不能摘：那等于动到外部仓库。
            try:
                kind = reparse_kind(os.lstat(dir_path))
                if kind is not None and not is_link_kind(kind):
                    reparse.delete_reparse_point(dir_path)
                    entries = list(os.scandir(dir_path))
                else:
                    leftovers.append(f"{dir_path}: {exc}")
                    return
            except OSError:
                leftovers.append(f"{dir_path}: {exc}")
                return
        for entry in entries:
            full = long_path(entry.path)
            try:
                st_entry = os.lstat(full)
            except OSError:
                continue
            if is_link_kind(reparse_kind(st_entry)):
                # 子项是链接：摘链接本身，不跟随
                ok = (_remove_dir_win32(full) if entry.is_dir(follow_symlinks=False)
                      else _delete_file_win32(full))
                if not ok and os.path.exists(full):
                    leftovers.append(str(entry.path))
                continue
            if entry.is_dir(follow_symlinks=False):
                _recurse(full)
                # 子项删完后再删这个目录本身
                if not _remove_dir_win32(full):
                    _force_clear_readonly(full)
                    if not _remove_dir_win32(full) and os.path.exists(full):
                        leftovers.append(str(entry.path))
            else:
                if not _delete_file_win32(full):
                    _force_clear_readonly(full)
                    if not _delete_file_win32(full) and os.path.exists(full):
                        leftovers.append(str(entry.path))

    _recurse(lp)
    # 顶层目录
    if os.path.exists(lp) and not _remove_dir_win32(lp):
        _force_clear_readonly(lp)
        if not _remove_dir_win32(lp) and os.path.exists(lp):
            leftovers.append(str(path))
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
        # 卡住时的结构化信息，由 _rename_source_to_backup 之类填，再带给报告页
        self.blocked_reason = ""
        self.blocked_process = ""
        # 逐行日志的界面接收方。可以由界面在构造之后再挂上（worker 就是这么做的），
        # 所以 _ensure_runlog 里传的是 self._emit_log 这个间接层、而不是当时的取值。
        self.log_sink = None

    # --- 日志 -------------------------------------------------------
    def _ensure_runlog(self) -> None:
        """会话一旦就位，就把日志挂到它旁边（`<id>.log`，与 `.json`/`.done` 同前缀）。"""
        if self.session is not None and self.runlog is None:
            self.runlog = RunLog(self.session.path.with_suffix(LOG_SUFFIX),
                                 on_line=self._emit_log)

    def _emit_log(self, line: str) -> None:
        """把一行日志转给界面。界面侧出问题绝不能影响迁移本身。

        try/except 在这里是**载荷**而不是装饰：接收方是界面挂在 worker 信号上的
        方法，worker 一旦被释放，那个信号就成了死对象，调用会抛 RuntimeError。
        界面会显式摘掉它（main_window._on_worker_finished），但这里仍要兜住——
        日志本身必须照写不误。
        """
        sink = self.log_sink
        if sink is None:
            return
        try:
            sink(line)
        except Exception:
            pass

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
                                  verify=verify_result, message=rename_error,
                                  blocked_path=str(self.source),
                                  blocked_reason=self.blocked_reason,
                                  blocked_process=self.blocked_process)
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

        改名要求整棵树无人持有句柄：资源管理器开着某个子目录、杀软正在扫，
        都会让它失败于 ACCESS_DENIED / SHARING_VIOLATION。这类冲突多半是瞬时的，
        所以先退避重试；仍然失败才去查占用——原来只试一次、只回一句 WinError 5，
        用户既不知道该关什么，也没有下一步可走。
        """
        backup = str(self.source.parent / backup_name(self.source))
        last = ""
        for delay in (0.0,) + RENAME_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                os.rename(long_path(self.source), long_path(backup))
                self.blocked_reason = ""
                self.blocked_process = ""
                return backup, ""
            except OSError as exc:
                last = _readable_rename_error(exc)
        held = self._blocking_processes()
        # 结构化留存：报告页的「未完成清单」靠它出一条**可读**的记录，
        # 而不是只把整段文字塞进 message 就没了。
        self.blocked_reason = last
        self.blocked_process = held
        if held:
            last = (f"改名失败：{last}。正在占用源目录的进程：{held}。"
                    f"关掉它们后重试即可；源目录未改动，目标端已有完整副本")
        else:
            last = (f"改名失败：{last}；未查出占用进程（可能是权限不足，"
                    f"或有进程正把源目录当作工作目录）。"
                    f"源目录未改动，目标端已有完整副本")
        return "", last

    def _blocking_processes(self, limit: int = RENAME_LOCK_SAMPLE) -> str:
        """采样源树里的文件，问重启管理器是谁占着。

        采样逻辑挪到了 `locks.blockers_in_tree`——改名前的试探（guard.probe_rename）
        也要用同一份，两处各写一遍迟早分叉。
        """
        return locks.describe(locks.blockers_in_tree(self.source, limit))

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

    def target_extras(self, limit: int | None = None,
                      ledger: set[str] | None = None) -> list[str]:
        """目标端里「不属于这次迁移」的文件（相对路径）。

        依据是本会话的复制记账：目标端只该有记账里的那些条目，多出来的就是迁移
        之后被写进去的（用户在那儿跑构建、下载文件）。回滚时顺手删掉它们等于
        静默销毁用户的数据，所以删之前先问一遍。

        遍历规则必须和记账对齐：**只取普通文件、不跟进任何重解析点**。
        迁移在目标端重建的链接不进记账（它们在第 2 趟单独建），所以这里也要
        排除，否则每条重建出来的链接都会被误判成用户后写入的数据。
        未知重解析点是**进**记账的（它们走复制路径），但这里跳过它们只是不参与
        比对——不会被算成 extras，不影响结论。

        `limit` 只影响返回条数（界面展示用），判定「有没有」不受影响。
        `ledger` 允许调用方把已经读好的记账传进来：几十万行时读两遍纯属浪费。
        """
        if self.session is None:
            return []
        if ledger is None:
            ledger = self.session.completed_rels()
        if not ledger:
            return []
        target = long_path(self.target)
        if not os.path.isdir(target):
            return []
        extras: list[str] = []
        for dirpath, dirs, files in os.walk(target):
            keep = []
            for name in dirs:
                full = os.path.join(dirpath, name)
                try:
                    if reparse_kind(os.lstat(full)) is None:
                        keep.append(name)
                except OSError:
                    continue
            dirs[:] = keep
            for name in files:
                full = os.path.join(dirpath, name)
                try:
                    if reparse_kind(os.lstat(full)) is not None:
                        continue        # 链接 / 未知重解析点：不在比对范围
                except OSError:
                    continue
                rel = rel_to(full, self.target)
                if rel not in ledger:
                    extras.append(rel)
                    if limit is not None and len(extras) >= limit:
                        return extras
        return extras

    def has_target(self) -> bool:
        """目标目录现在还在不在。

        回滚后要判断「还有没有东西可清」：目标端本来就不存在时，
        再走一遍删除只会得到「目标目录不存在，无需清理」这种自相矛盾的结论
        （说「未能删除」又说「无需清理」）。
        """
        return os.path.exists(long_path(self.target))

    def can_clear_target(self) -> tuple[bool, str]:
        """回滚后能不能安全地自动清掉目标端。返回 (能否, 不能的原因)。

        判据只有一条：目标端里除了这次复制过去的东西，没有别的。
        没有记账时一律拒绝——确认不了就不动手，和 discard_target 同一个原则。
        """
        if self.session is None:
            return False, "没有会话记账，无法确认目标端里有没有迁移之后写入的数据"
        ledger = self.session.completed_rels()
        if not ledger:
            return False, "没有会话记账，无法确认目标端里有没有迁移之后写入的数据"
        if not os.path.exists(long_path(self.target)):
            return True, ""
        extras = self.target_extras(limit=21, ledger=ledger)
        if extras:
            shown = "、".join(extras[:3])
            more = f" 等 {len(extras)} 项" if len(extras) > 3 else ""
            return False, (f"目标端有 {len(extras)} 项不属于这次迁移的内容"
                           f"（{shown}{more}），删掉会一并销毁它们")
        return True, ""

    def clear_target(self) -> bool:
        """清空目标目录**里面的东西**，但保留目录本身。

        和 discard_target 只差这一点：那个连目录一起删（「删除目标」按钮与 CLI 的
        discard 就是这个语义）。回滚要的不是这个——用户要的是「把同步过去的内容
        去掉」，而目标目录完全可能是他自己先建好的空目录（合入已有目录是被支持的
        场景，guard 里专门有 target_not_empty 这一条）。把整个目录删掉，等于顺手
        清掉了一个用户自己建的文件夹。

        安全性判断与 discard_target 完全一致：确认数据另有副本才动手。
        """
        self.cleanup_error = ""
        self._ensure_runlog()
        if is_reparse_path(self.source):
            self.cleanup_error = "源目录已经是指向目标的链接，目标端是活数据，不能清"
            self._log(f"清空目标：被拒（{self.cleanup_error}）")
            return False
        backup_ok = bool(self.backup_path) and os.path.exists(
            long_path(self.backup_path))
        if not os.path.isdir(long_path(self.source)) and not backup_ok:
            self.cleanup_error = "源目录与备份都不在，无法确认数据另有副本，拒绝清空目标"
            self._log(f"清空目标：被拒（{self.cleanup_error}）")
            return False
        target = long_path(self.target)
        if not os.path.isdir(target):
            self.cleanup_error = "目标目录不存在，无需清理"
            self._log(f"清空目标：被拒（{self.cleanup_error}）")
            return False

        leftovers: list[str] = []
        try:
            entries = list(os.scandir(target))
        except OSError as exc:
            self.cleanup_error = f"目标目录读不出来：{exc}"
            self._log(f"清空目标：被拒（{self.cleanup_error}）")
            return False
        for entry in entries:
            full = long_path(entry.path)
            try:
                st_entry = os.lstat(full)
            except OSError:
                continue
            # 目录与链接都交给 rmtree_force：它认链接、只摘链接本身。
            # 普通文件不能交给它——它内部走 scandir，对文件会直接报「删不掉」。
            if is_link_kind(reparse_kind(st_entry)) or entry.is_dir(
                    follow_symlinks=False):
                leftovers.extend(rmtree_force(full))
            else:
                if not _delete_file_win32(full):
                    _force_clear_readonly(full)
                    if not _delete_file_win32(full) and os.path.exists(full):
                        leftovers.append(str(entry.path))

        remaining = []
        try:
            remaining = [e.path for e in os.scandir(target)]
        except OSError:
            pass
        if remaining:
            sample = "；".join(os.path.basename(p.rstrip("\\/"))
                              for p in remaining[:3])
            self.cleanup_error = (
                f"目标目录里有 {len(remaining)} 项没能清掉（{sample}），"
                f"目录仍在：{self.target}")
            self._log(f"清空目标：未清干净，{self.cleanup_error}")
            return False

        if self.session:
            # 清空目标端不写会话 phase：这是回滚之后的收尾动作，不是迁移本身的新
            # 结局。写成 "discarded" 会把回滚刚记下的 "rolled_back" 盖掉，会话文件
            # 里就再也看不出「回滚发生过」了。清了什么已经落在运行日志里。
            self.session.save()
        self._log(f"清空目标：完成（目录保留）{self.target}")
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

        **唯一会拒绝的情形：源路径是链接、却没有可还原的备份。**
        迁移成功后命令行默认会删掉备份，此时源路径上那条链接是用户程序
        唯一的入口；摘掉它而不还原，等于把原路径抹掉——用户看到「回滚成功」，
        实际所有引用原路径的程序、快捷方式、脚本全部失效。这种组合一律拒绝，
        且什么都不动（连 ACL 也不碰，被拒的操作不留副作用）。

        其余情况照常执行：ACL 还原不依赖备份，源是普通目录时也没有结构可还原。
        """
        self.cleanup_error = ""
        self._ensure_runlog()

        has_backup = bool(self.backup_path) and os.path.exists(
            long_path(self.backup_path))
        source_is_link = is_reparse_path(self.source)
        if source_is_link and not has_backup:
            self.cleanup_error = (
                "没有可还原的备份（备份已被删除）。源路径上的链接是当前唯一入口，"
                "摘掉它会让这个路径彻底消失，因此拒绝回滚；数据在目标端完好。")
            self._log(f"回滚：被拒（{self.cleanup_error}）")
            return False

        acl_failed = self._restore_acl()
        if acl_failed:
            preview = "；".join(acl_failed[:3])
            self.cleanup_error = (f"{len(acl_failed)} 个目录的原始权限没能还原"
                                  f"（示例：{preview}），它们仍是接管后的属主。")

        if source_is_link:
            # 只在**确实存在一条链接**时才摘。链接已经不在了（被外部删掉、
            # 或上一趟回滚已经摘过）就直接去还原备份——那种现场下回滚是
            # 唯一的恢复手段，卡在摘链接这一步会让数据永远锁在备份里。
            try:
                os.rmdir(long_path(self.source))
            except OSError:
                try:
                    os.unlink(long_path(self.source))
                except OSError:
                    self.cleanup_error = (
                        f"摘掉源路径上的链接失败：{self.source}，回滚中止")
                    self._log(f"回滚：{self.cleanup_error}")
                    return False
        if has_backup:
            try:
                os.rename(long_path(self.backup_path), long_path(self.source))
            except OSError as exc:
                reason = getattr(exc, "strerror", None) or str(exc)
                self.cleanup_error = (
                    f"备份改回原名失败（{reason}）。备份仍在 {self.backup_path}，"
                    f"{self.source} 未被改动")
                self._log(f"回滚：{self.cleanup_error}")
                return False
        if self.session:
            self.session.phase = "rolled_back"
            self.session.save()
        self._log(f"回滚：完成 {self.source}" + (f"；{self.cleanup_error}"
                                                if self.cleanup_error else ""))
        return True
