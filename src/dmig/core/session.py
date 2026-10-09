"""迁移会话的持久化，支撑断点续传与回滚。

只记录聚合进度与异常项，普通文件不逐条落盘。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FLUSH_EVERY_FILES = 500
FLUSH_EVERY_SECONDS = 5.0


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
    backup_path: str | None = None
    created_at: str = ""

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
            backup_path=data.get("backup_path"),
            created_at=data.get("created_at", ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "created_at": self.created_at,
            "source": self.source, "target": self.target,
            "phase": self.phase, "totals": self.totals, "done": self.done,
            "links": self.links, "failed": self.failed,
            "acl_backup": self.acl_backup, "backup_path": self.backup_path,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # --- 进度 -------------------------------------------------------
    def mark_done(self, rel: str, size: int) -> None:
        self.done["files"] += 1
        self.done["bytes"] += int(size)
        if self.done["files"] % FLUSH_EVERY_FILES == 0:
            self.save()

    def mark_failed(self, rel: str, reason: str, process: str = "") -> None:
        self.failed.append({"rel": rel, "reason": reason, "process": process})

    def failed_rels(self) -> set[str]:
        return {f["rel"] for f in self.failed}

    def reset_failures(self) -> None:
        self.failed = []

    def pending_files(self, total_files: int) -> int:
        return max(0, total_files - self.done["files"])
