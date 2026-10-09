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
    RELATIVE = "relative"
    INSIDE_SOURCE = "inside_source"
    OUTSIDE_SOURCE = "outside_source"
    DANGLING = "dangling"


@dataclass
class PlanTotals:
    files: int = 0
    bytes: int = 0
    dirs: int = 0


@dataclass(frozen=True)
class LinkPlan:
    rel: str
    kind: EntryKind
    raw_target: str
    new_target: str
    target_class: LinkTargetClass
    is_dir: bool


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
    unknown_reparse: list[str] = field(default_factory=list)
    needs_elevation: bool = False
    required_bytes: int = 0
    free_bytes: int = 0
    target_not_empty: bool = False


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
    failures: list[CopyFailure] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class VerifyResult:
    ok: bool = False
    expected_files: int = 0
    actual_files: int = 0
    expected_bytes: int = 0
    actual_bytes: int = 0
    missing: list[str] = field(default_factory=list)
    mismatch: list[str] = field(default_factory=list)
