"""迁移会话的持久化，支撑断点续传与回滚。

聚合进度与异常项写进 JSON；已完成文件的相对路径另写一个追加式的旁挂文件
（`<id>.done`），避免 50 万个文件把 JSON 撑爆、每 500 个文件还要整体重写一次。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FLUSH_EVERY_FILES = 500
FLUSH_EVERY_SECONDS = 5.0
DONE_SUFFIX = ".done"


def default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DataMigrationTool" / "sessions"


@dataclass
class Session:
    id: str
    source: str
    target: str
    path: Path
    phase: str = "created"
    totals: dict = field(default_factory=lambda: {"files": 0, "bytes": 0})
    done: dict = field(default_factory=lambda: {"files": 0, "bytes": 0})
    links: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    acl_backup: list = field(default_factory=list)
    # 本次运行的参数（链接重写映射、深度校验、跳过重解析点…）。
    # 续传必须照原样重放：映射丢了，目标端重建的几百个链接会全部指回旧位置。
    options: dict = field(default_factory=dict)
    backup_path: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        self.done_log = self.path.with_suffix(DONE_SUFFIX)
        self._pending: list[str] = []
        self._last_flush = time.monotonic()

    # --- 构造与读写 -------------------------------------------------
    @classmethod
    def create(cls, source, target, root=None) -> "Session":
        root = Path(root) if root else default_root()
        root.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        sid = f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        s = cls(id=sid, source=str(source), target=str(target),
                path=root / f"{sid}.json", created_at=now.isoformat(timespec="seconds"))
        s.save()
        return s

    @classmethod
    def load(cls, path) -> "Session | None":
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        required = {"id", "source", "target"}
        if not required.issubset(data):
            return None
        return cls(
            id=data["id"], source=data["source"], target=data["target"], path=p,
            phase=data.get("phase", "created"),
            totals=data.get("totals", {"files": 0, "bytes": 0}),
            done=data.get("done", {"files": 0, "bytes": 0}),
            links=data.get("links", []),
            failed=data.get("failed", []),
            acl_backup=data.get("acl_backup", []),
            options=data.get("options", {}),
            backup_path=data.get("backup_path"),
            created_at=data.get("created_at", ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "created_at": self.created_at,
            "source": self.source, "target": self.target,
            "phase": self.phase, "totals": self.totals, "done": self.done,
            "links": self.links, "failed": self.failed,
            "acl_backup": self.acl_backup, "options": self.options,
            "backup_path": self.backup_path,
        }

    def save(self) -> None:
        self.flush_done()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # --- 进度 -------------------------------------------------------
    def mark_done(self, rel: str, size: int) -> None:
        self.done["files"] += 1
        self.done["bytes"] += int(size)
        self._pending.append(rel)
        if self.done["files"] % FLUSH_EVERY_FILES == 0:
            self.save()                       # save() 会连带落盘已完成清单
        elif time.monotonic() - self._last_flush >= FLUSH_EVERY_SECONDS:
            self.flush_done()                 # 大文件很久才复制完一个，按时间兜底

    def flush_done(self) -> None:
        """把已完成清单追加落盘。

        不长期持有文件句柄：Windows 上留着的句柄会妨碍临时目录清理，
        而追加写的开销可以忽略。
        """
        if not self._pending:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.done_log, "a", encoding="utf-8") as handle:
            handle.write("".join(f"{rel}\n" for rel in self._pending))
        self._pending.clear()
        self._last_flush = time.monotonic()

    def completed_rels(self) -> set[str]:
        """本会话**自己复制完成**过的相对路径（含尚未落盘的那批）。

        判断「目标端这份能不能跳过」只能靠这份记账，不能用大小/时间戳猜：
        实测同一系统时钟节拍（约 15 ms）内写入的两个同大小文件，
        st_mtime_ns 完全相同，启发式会把目标盘上别人的残留当成自己复制过的
        而跳过；默认校验又看不出差别，于是校验通过、备份被删、源数据永久丢失。

        每次都现读、不缓存：复制引擎一趟只取一次快照，而缓存会随 mark_done
        一路长到几十万个字符串（50 万文件约 85 MB 常驻内存），不值得。
        """
        rels: set[str] = set()
        try:
            with open(self.done_log, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        rels.add(line.rstrip("\n"))
        except OSError:
            pass
        rels.update(self._pending)
        return rels

    def mark_failed(self, rel: str, reason: str, process: str = "") -> None:
        self.failed.append({"rel": rel, "reason": reason, "process": process})

    def failed_rels(self) -> set[str]:
        return {f["rel"] for f in self.failed}

    def begin_run(self, phase: str) -> None:
        """开始一趟复制（`phase` 用 "copying" 或续传时的 "resumed"）。

        清掉的是上一轮的 failed / links——它们描述的是**当时的现场**，这一趟会
        重新攒出来，留着只会让会话文件里出现重复与过时的条目。
        `.done` 记账绝不能清：那是续传时「哪些文件是我们自己复制的」的唯一依据，
        清掉就等于把续传退化成从头再复制一遍。
        """
        self.failed = []
        self.links = []
        self.phase = phase

    def pending_files(self, total_files: int) -> int:
        return max(0, total_files - self.done["files"])
