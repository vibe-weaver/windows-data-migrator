"""迁移流程共用的数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class EntryKind(str, Enum):
    FILE = "file"
    DIR = "dir"
    JUNCTION = "junction"
    SYMLINK_DIR = "symlink_dir"
    SYMLINK_FILE = "symlink_file"
    UNKNOWN_REPARSE = "unknown_reparse"


class LinkTargetClass(str, Enum):
    """链接目标与源树的关系。

    「关系」与「目标是否存在」是正交的两件事——树外的目标同样可能断链，
    因此断链由 is_dangling 单独表达，不塞进这个枚举。
    """
    RELATIVE = "relative"
    INSIDE_SOURCE = "inside_source"
    OUTSIDE_SOURCE = "outside_source"


@dataclass
class PlanTotals:
    files: int = 0
    bytes: int = 0
    dirs: int = 0


@dataclass(frozen=True)
class ResolvedTarget:
    """resolve_target 的返回值。

    origin_target 是**应用映射之前**的绝对路径。归属表要按它分组：
    按映射后的路径分组会让用户填一次映射、重扫一次就换了行，
    「重写为」列永远对不上自己刚填的那一行。
    """
    new_target: str
    target_class: LinkTargetClass
    is_dangling: bool = False
    origin_target: str = ""


@dataclass(frozen=True)
class UnknownReparse:
    """非链接类的重解析点（AppExecLink、云占位符、Dedup、WCI…）。

    默认按真实内容复制：能直读的读出内容（云占位符会触发下载），
    读不动的原样搬重解析数据。`is_dir` 决定 verbatim 搬运时建目录还是建文件。
    """
    rel: str
    tag: int
    is_dir: bool = False


@dataclass(frozen=True)
class LinkPlan:
    rel: str
    kind: EntryKind
    raw_target: str
    new_target: str
    target_class: LinkTargetClass
    is_dir: bool
    is_dangling: bool = False


@dataclass
class TargetGroup:
    prefix: str
    count: int = 0
    mapped_to: str = ""


@dataclass
class MigrationPlan:
    source: Path
    target: Path
    totals: PlanTotals = field(default_factory=PlanTotals)
    links: list[LinkPlan] = field(default_factory=list)
    link_groups: list[TargetGroup] = field(default_factory=list)
    permission_issues: list[str] = field(default_factory=list)
    unknown_reparse: list[UnknownReparse] = field(default_factory=list)
    skip_unknown_reparse: bool = False
    needs_elevation: bool = False
    file_symlinks: int = 0
    required_bytes: int = 0
    free_bytes: int = 0
    target_not_empty: bool = False
    # 本次生效的排除目录（绝对路径）。扫描、复制、校验三条遍历共用它；
    # excluded 是实际跳过的条目（相对路径），界面与报告靠它告诉用户
    # 「哪些内容只留在备份里」——删备份之前必须知道这件事。
    excludes: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    # 复制会成功、但结果与原件不同的三类属性。它们不进失败清单，
    # 却也绝不能沉默：用户看到「校验通过」会以为一个字节都没变。
    sparse_files: int = 0
    hardlinked_files: int = 0
    ads_files: int = 0


def lossy_attribute_text(sparse: int, hardlinks: int, ads: int) -> str:
    """把三类保不住的属性说成一句话；都没有时返回空串。

    计划页（事前预告）、命令行与报告（事后留档）共用同一份措辞，
    否则同一个事实在三处会有三种说法。
    """
    parts: list[str] = []
    if sparse:
        parts.append(f"{sparse} 个稀疏文件会被实化（占用空间变大）")
    if hardlinks:
        parts.append(f"{hardlinks} 个硬链接会变成各自独立的副本（不再共享同一份数据）")
    if ads:
        parts.append(f"{ads} 个文件带附加数据流（ADS），复制后不保留")
    return "；".join(parts)


@dataclass
class Progress:
    phase: str = ""
    done_files: int = 0
    done_bytes: int = 0
    total_files: int = 0
    total_bytes: int = 0
    current: str = ""
    speed_bps: float = 0.0
    eta_seconds: float = 0.0


@dataclass
class CopyFailure:
    rel: str
    reason: str
    process: str = ""


@dataclass
class CopyResult:
    copied_files: int = 0
    copied_bytes: int = 0
    skipped: int = 0
    skipped_bytes: int = 0
    failures: list[CopyFailure] = field(default_factory=list)
    cancelled: bool = False
    aborted: str = ""       # 不可恢复错误导致整趟中止，如 "disk_full"
    # 目录时间戳写不回去的相对路径。收尾步骤是尽力而为：失败不该让整趟作废，
    # 但也不能沉默——依赖目录时间戳做增量同步的工具会因此整棵重扫。
    dir_times_failed: list[str] = field(default_factory=list)

    @property
    def accounted_bytes(self) -> int:
        """已处理字节 = 真复制的 + 续传时跳过的。

        进度条与 ETA 要用这个口径：跳过的文件确实已经就位，不算进「已处理」的话
        一次全量续传跑完进度条也只走到一半。报告里的「成功复制」仍只用 copied_bytes，
        否则实际一个文件都没复制也会显示「成功复制 N」。
        """
        return self.copied_bytes + self.skipped_bytes


@dataclass
class VerifyResult:
    ok: bool = False
    cancelled: bool = False
    expected_files: int = 0
    actual_files: int = 0
    expected_bytes: int = 0
    actual_bytes: int = 0
    missing: list[str] = field(default_factory=list)
    mismatch: list[str] = field(default_factory=list)
