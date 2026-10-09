"""命令行入口：scan / migrate / resume / rollback。"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .core import guard, scanner
from .core.migrator import Migrator
from .core.session import Session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dmig", description="Windows 数据目录迁移工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="只预扫描，输出迁移计划")
    p_scan.add_argument("--source", required=True)
    p_scan.add_argument("--target", required=True)
    p_scan.add_argument("--map", action="append", default=[],
                        help="链接目标前缀映射，格式 源=目标，可重复")

    p_mig = sub.add_parser("migrate", help="执行迁移")
    p_mig.add_argument("--source", required=True)
    p_mig.add_argument("--target", required=True)
    p_mig.add_argument("--map", action="append", default=[])
    p_mig.add_argument("--yes", action="store_true", help="跳过交互确认")
    p_mig.add_argument("--dry-run", action="store_true", help="只出计划，不改动任何东西")
    p_mig.add_argument("--sha256", action="store_true", help="启用逐文件哈希校验")
    p_mig.add_argument("--keep-backup", action="store_true", help="结束后保留备份目录")
    p_mig.add_argument("--session-root", default=None)

    p_res = sub.add_parser("resume", help="从会话继续未完成的迁移")
    p_res.add_argument("session")
    p_res.add_argument("--yes", action="store_true")

    p_rb = sub.add_parser("rollback", help="回滚一次迁移")
    p_rb.add_argument("session")

    return parser


def _parse_maps(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--map 需要 源=目标 格式：{item}")
        src, dst = item.split("=", 1)
        result[src.strip()] = dst.strip()
    return result


def _print_plan(plan) -> None:
    payload = {
        "source": str(plan.source),
        "target": str(plan.target),
        "totals": asdict(plan.totals),
        "required_bytes": plan.required_bytes,
        "free_bytes": plan.free_bytes,
        "link_count": len(plan.links),
        "link_groups": [asdict(g) for g in plan.link_groups],
        "permission_issues": plan.permission_issues,
        "unknown_reparse": plan.unknown_reparse,
        "target_not_empty": plan.target_not_empty,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "scan":
        plan = scanner.scan(args.source, args.target,
                            mappings=_parse_maps(args.map))
        _print_plan(plan)
        errors = [i for i in guard.check(args.source, args.target,
                                         required_bytes=plan.required_bytes)
                  if i.severity == "error"]
        if errors:
            for issue in errors:
                print(f"错误：{issue.message}", file=sys.stderr)
            return 1
        return 0

    if args.command == "migrate":
        mappings = _parse_maps(args.map)
        migrator = Migrator(args.source, args.target, mappings=mappings,
                            deep_verify=args.sha256,
                            session_root=args.session_root)
        plan = migrator.plan_it()
        _print_plan(plan)
        if args.dry_run:
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                print("非交互环境必须显式传 --yes", file=sys.stderr)
                return 2
            reply = input(f"确认把 {args.source} 迁移到 {args.target}？(yes/no) ")
            if reply.strip().lower() not in ("yes", "y"):
                print("已取消", file=sys.stderr)
                return 1

        outcome = migrator.execute()
        if not outcome.ok:
            print(f"失败于 {outcome.phase}：{outcome.message}", file=sys.stderr)
            if outcome.phase in ("link_failed", "link_verify_failed"):
                print("提示：可执行 dmig rollback <会话文件> 回滚", file=sys.stderr)
            return 1
        print(f"迁移完成，链接类型 {outcome.link_kind}，备份 {outcome.backup_path}")
        if not args.keep_backup:
            migrator.cleanup_backup()
            print("备份已清理")
        return 0

    if args.command == "rollback":
        sess = Session.load(Path(args.session))
        if sess is None:
            print("会话文件无效", file=sys.stderr)
            return 1
        migrator = Migrator(sess.source, sess.target)
        migrator.backup_path = sess.backup_path
        return 0 if migrator.rollback() else 1

    if args.command == "resume":
        sess = Session.load(Path(args.session))
        if sess is None:
            print("会话文件无效", file=sys.stderr)
            return 1
        migrator = Migrator(sess.source, sess.target, session_root=Path(args.session).parent)
        migrator.plan_it()
        outcome = migrator.execute()
        if outcome.ok:
            migrator.cleanup_backup()
        return 0 if outcome.ok else 1

    return 2
