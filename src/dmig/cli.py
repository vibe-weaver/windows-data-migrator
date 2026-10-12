"""命令行入口：scan / migrate / resume / rollback / discard。"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .core import guard, models, permission, scanner
from .core.migrator import Migrator, awaiting_source_link
from .core.session import Session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dmig", description="Windows 数据目录迁移工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="只预扫描，输出迁移计划")
    p_scan.add_argument("--source", required=True)
    p_scan.add_argument("--target", required=True)
    p_scan.add_argument("--map", action="append", default=[],
                        help="链接目标前缀映射，格式 源=目标，可重复")
    p_scan.add_argument("--exclude", action="append", default=[],
                        help="整棵跳过的目录（可重复）。跳过的内容不会复制到目标，"
                             "只保留在备份里")
    p_scan.add_argument("--no-ads-scan", action="store_true",
                        help="不探测附加数据流（ADS）。探测要对每个文件问一次文件系统，"
                             "几十万文件下是可测的常数开销；关掉后计划里不再报 ADS 条数")
    p_scan.add_argument("--skip-unknown-reparse", action="store_true",
                        help="跳过非链接类重解析点（默认按真实内容复制）")

    p_mig = sub.add_parser("migrate", help="执行迁移")
    p_mig.add_argument("--source", required=True)
    p_mig.add_argument("--target", required=True)
    p_mig.add_argument("--map", action="append", default=[])
    p_mig.add_argument("--exclude", action="append", default=[],
                       help="整棵跳过的目录（可重复）。删备份前请确认不再需要它们")
    p_mig.add_argument("--take-ownership", action="store_true",
                       help="以管理员身份运行时，接管无权限目录的属主"
                            "（会先把原始 ACL 记进会话，回滚时还原）")
    p_mig.add_argument("--no-ads-scan", action="store_true",
                       help="不探测附加数据流（ADS），扫描更快；详见 scan 的同名参数")
    p_mig.add_argument("--yes", action="store_true", help="跳过交互确认")
    p_mig.add_argument("--dry-run", action="store_true", help="只出计划，不改动任何东西")
    p_mig.add_argument("--sha256", action="store_true", help="启用逐文件哈希校验")
    p_mig.add_argument("--keep-backup", action="store_true", help="结束后保留备份目录")
    p_mig.add_argument("--skip-unknown-reparse", action="store_true",
                       help="跳过非链接类重解析点（默认按真实内容复制）")
    p_mig.add_argument("--session-root", default=None)

    p_res = sub.add_parser("resume", help="从会话继续未完成的迁移")
    p_res.add_argument("session")
    p_res.add_argument("--yes", action="store_true")

    p_rb = sub.add_parser("rollback", help="回滚一次迁移")
    p_rb.add_argument("session")
    p_rb.add_argument("--yes", action="store_true", help="跳过交互确认")

    p_dc = sub.add_parser("discard", help="删除目标端的半成品（源目录不受影响）")
    p_dc.add_argument("session")
    p_dc.add_argument("--yes", action="store_true", help="跳过交互确认")

    return parser


def _parse_maps(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--map 需要 源=目标 格式：{item}")
        src, dst = item.split("=", 1)
        result[src.strip()] = dst.strip()
    return result


def _migrator_from_session(sess: Session) -> Migrator:
    """按会话里记录的运行参数重建 Migrator，用于续传。

    链接重写映射必须照原样重放：会话文件里不记、续传时用空映射重建，
    目标端那几百个 junction 就会全部指回迁移前的旧位置（而那些旧位置
    马上要被改名成备份，链接随之变成断链）。排除项同理：丢了它，用户
    明确排除的目录会被重新复制一遍，空间也白占。
    会话根目录取会话文件所在目录，续传写的还是同一个 .json 与 .done。
    """
    opts = sess.options or {}
    return Migrator(sess.source, sess.target,
                    mappings=dict(opts.get("mappings") or {}),
                    deep_verify=bool(opts.get("deep_verify")),
                    session_root=sess.path.parent,
                    skip_unknown_reparse=bool(opts.get("skip_unknown_reparse")),
                    excludes=list(opts.get("excludes") or []),
                    probe_ads=bool(opts.get("probe_ads", True)))


def _warn_symlink_privilege(plan) -> None:
    """文件符号链接建不起来就现在说，别等到复制完几十万个文件才失败。"""
    if not plan.file_symlinks:
        return
    if permission.can_create_symlink():
        return
    print(f"警告：{plan.file_symlinks} 个文件符号链接需要管理员特权才能重建，"
          f"当前进程没有该特权。请以管理员身份运行，或开启 Windows 开发者模式。",
          file=sys.stderr)


def _warn_dangling_links(plan, limit: int = 5) -> None:
    """断链要说出来：工具照样原样重建，但它在源里就已经指着空气。

    不说的话用户迁移完发现链接点不开，只会以为是工具弄坏的。
    """
    dangling = [link for link in plan.links if link.is_dangling]
    if not dangling:
        return
    shown = "、".join(link.rel for link in dangling[:limit])
    if len(dangling) > limit:
        shown += "…"
    print(f"警告：{len(dangling)} 条链接的目标在迁移前就不存在（断链）：{shown}。"
          f"仍会原样重建，迁移后照样指不到东西；"
          f"完整清单见计划 JSON 的 dangling_links 字段。", file=sys.stderr)


def _warn_lossy_attributes(plan) -> None:
    """复制保不住的属性：不报的话「校验通过」会被当成「一个字节都没变」。"""
    text = models.lossy_attribute_text(plan.sparse_files, plan.hardlinked_files,
                                       plan.ads_files)
    if text:
        print(f"警告：{text}。这些不会让复制失败，但结果与原件不再逐字节相同。",
              file=sys.stderr)
    if not getattr(plan, "ads_probed", True):
        print("提示：本次没有探测附加数据流（--no-ads-scan），"
              "上面的 ADS 条数不代表实际数量。", file=sys.stderr)


def _guard_issues(source, target, plan) -> list:
    """跑一遍前置校验，把不致命但影响决策的提示说出来，返回全部 Issue。

    error 由调用方决定退出码；warning 此前在 CLI 里完全不可见——
    「同盘迁移不释放空间」「目标在网络盘上」这类话，用户只有在确认前看到才有用。
    """
    issues = guard.check(source, target, required_bytes=plan.required_bytes,
                         link_count=len(plan.links))
    for issue in issues:
        if issue.severity != "error":
            print(f"提示：{issue.message}", file=sys.stderr)
    return issues


def _confirm(prompt: str) -> bool | None:
    """交互确认。True=同意，False=明确拒绝，None=根本没能问出口。

    「没能问出口」必须与「拒绝」分开：非交互环境（脚本调用、重定向、
    打包后 --windowed 没有控制台）与 stdin 到 EOF / Ctrl+C 都属于前者，
    退出码用 2 告诉调用方「你漏了 --yes」，而不是让它以为用户点了否。
    """
    if not sys.stdin.isatty():
        print("非交互环境必须显式传 --yes", file=sys.stderr)
        return None
    try:
        reply = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("未获得确认，已取消。无人值守请显式传 --yes", file=sys.stderr)
        return None
    return reply.strip().lower() in ("yes", "y")


def _ask_confirmation(prompt: str) -> int | None:
    """返回 None 表示已获确认；否则返回调用方应直接使用的退出码。"""
    reply = _confirm(prompt)
    if reply is None:
        return 2
    if not reply:
        print("已取消", file=sys.stderr)
        return 1
    return None


def _print_plan(plan) -> None:
    payload = {
        "source": str(plan.source),
        "target": str(plan.target),
        "totals": asdict(plan.totals),
        "required_bytes": plan.required_bytes,
        "free_bytes": plan.free_bytes,
        "link_count": len(plan.links),
        "file_symlinks": plan.file_symlinks,
        "link_groups": [asdict(g) for g in plan.link_groups],
        "dangling_links": [
            {"rel": link.rel, "raw_target": link.raw_target,
             "new_target": link.new_target}
            for link in plan.links if link.is_dangling],
        "permission_issues": plan.permission_issues,
        "unknown_reparse": [
            {"rel": u.rel, "tag": f"0x{u.tag:08X}", "is_dir": u.is_dir}
            for u in plan.unknown_reparse],
        "skip_unknown_reparse": plan.skip_unknown_reparse,
        "target_not_empty": plan.target_not_empty,
        "excludes": list(plan.excludes),
        "excluded": list(plan.excluded),
        "lossy_attributes": {
            "sparse_files": plan.sparse_files,
            "hardlinked_files": plan.hardlinked_files,
            "ads_files": plan.ads_files,
            "ads_probed": getattr(plan, "ads_probed", True),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_log_path(outcome, stream=None) -> None:
    """把运行日志的位置告诉用户。"""
    path = getattr(outcome, "log_path", "")
    if path:
        print(f"运行日志：{path}", file=stream or sys.stdout)


def _report_failure(outcome, migrator) -> None:
    """失败原因与下一步出路一起给出，别让用户对着一句 WinError 猜。"""
    print(f"失败于 {outcome.phase}：{outcome.message}", file=sys.stderr)
    _print_log_path(outcome, sys.stderr)
    if outcome.phase in ("link_failed", "link_verify_failed"):
        print("提示：数据已复制到目标并通过校验，源目录已改名为备份。"
              "解决原因后 dmig resume <会话文件> 只补建源目录链接；"
              "要退回迁移前的状态可 dmig rollback <会话文件>", file=sys.stderr)
        return
    session = getattr(migrator, "session", None)
    path = getattr(session, "path", None)
    if path is None:
        return
    print(f"提示：源目录未改动。解决原因后可 dmig resume {path} 续传；"
          f"要清掉目标端的半成品可 dmig discard {path} --yes", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "scan":
        plan = scanner.scan(args.source, args.target,
                            mappings=_parse_maps(args.map),
                            skip_unknown_reparse=args.skip_unknown_reparse,
                            excludes=args.exclude,
                            probe_ads=not args.no_ads_scan)
        _print_plan(plan)
        _warn_symlink_privilege(plan)
        _warn_dangling_links(plan)
        _warn_lossy_attributes(plan)
        errors = [i for i in _guard_issues(args.source, args.target, plan)
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
                            session_root=args.session_root,
                            skip_unknown_reparse=args.skip_unknown_reparse,
                            excludes=args.exclude,
                            probe_ads=not args.no_ads_scan)
        plan = migrator.plan_it()
        if args.take_ownership and plan.permission_issues:
            if not permission.is_elevated():
                print("接管属主需要管理员权限，请以管理员身份重新运行",
                      file=sys.stderr)
                return 1
            taken, error = migrator.take_over_denied()
            if error:
                print(f"接管未完成：{error}", file=sys.stderr)
                return 1
            print(f"已接管 {taken} 个目录的属主，原始 ACL 已记入会话，回滚时会还原")
            plan = migrator.plan
        _print_plan(plan)
        _warn_symlink_privilege(plan)
        _warn_dangling_links(plan)
        _warn_lossy_attributes(plan)
        # 提示必须在确认之前出来：同盘迁移根本不释放空间，用户答 yes 前有权知道。
        # error 不在这里拦，execute() 会统一给出失败阶段与出路。
        _guard_issues(args.source, args.target, plan)
        if args.dry_run:
            return 0
        if not args.yes:
            code = _ask_confirmation(
                f"确认把 {args.source} 迁移到 {args.target}？(yes/no) ")
            if code is not None:
                return code

        outcome = migrator.execute()
        if not outcome.ok:
            _report_failure(outcome, migrator)
            return 1
        print(f"迁移完成，链接类型 {outcome.link_kind}，备份 {outcome.backup_path}")
        _print_log_path(outcome)
        if not args.keep_backup:
            if migrator.cleanup_backup():
                print("备份已清理")
            else:
                # 迁移本身已成功，备份留着是安全状态，因此不改退出码
                print(f"备份未删除：{migrator.cleanup_error}", file=sys.stderr)
        return 0

    if args.command == "rollback":
        sess = Session.load(Path(args.session))
        if sess is None:
            print("会话文件无效", file=sys.stderr)
            return 1
        if not args.yes:
            code = _ask_confirmation(
                f"确认把 {sess.source} 退回迁移前的状态？"
                f"迁移后写进 {sess.target} 的新数据会变成孤儿 (yes/no) ")
            if code is not None:
                return code
        migrator = Migrator(sess.source, sess.target)
        # 不接管会话，rollback() 里那句 phase="rolled_back" 就写不回文件
        migrator.adopt_session(sess)
        if migrator.rollback():
            return 0
        # 拒绝/失败的原因必须打印出来。回滚会被拒的最常见情形是备份已被删除，
        # 此时只说「退出码 1」用户完全不知道发生了什么、也不知道原路径还安不安全。
        print(f"回滚未执行：{migrator.cleanup_error or '未知原因'}", file=sys.stderr)
        return 1

    if args.command == "discard":
        sess = Session.load(Path(args.session))
        if sess is None:
            print("会话文件无效", file=sys.stderr)
            return 1
        migrator = Migrator(sess.source, sess.target,
                            session_root=Path(args.session).parent)
        migrator.adopt_session(sess)
        if not args.yes:
            code = _ask_confirmation(
                f"确认删除目标目录里的半成品？源目录不受影响：{sess.target} (yes/no) ")
            if code is not None:
                return code
        if migrator.discard_target():
            print("目标目录已删除")
            return 0
        print(f"目标目录未删除：{migrator.cleanup_error}", file=sys.stderr)
        return 1

    if args.command == "resume":
        sess = Session.load(Path(args.session))
        if sess is None:
            print("会话文件无效", file=sys.stderr)
            return 1
        # 接管旧会话才会沿用它的 .done 记账；不接管就等于从空白记账重头复制一遍
        migrator = _migrator_from_session(sess)
        migrator.adopt_session(sess)
        link_only = awaiting_source_link(sess, sess.source)
        if link_only:
            print(f"会话停在改名之后：数据已复制到 {sess.target} 并通过校验，"
                  f"本次只补建源目录链接")
        else:
            done = int(sess.done.get("files", 0))
            if done:
                print(f"续传会话 {sess.id}：已记录 {done:,} 个文件，"
                      f"与源完全一致的将直接跳过")
        if migrator.mappings:
            shown = "，".join(f"{src} → {dst}"
                              for src, dst in migrator.mappings.items())
            print(f"沿用会话记录的链接映射：{shown}")
        if not args.yes:
            prompt = (f"确认补建 {sess.source} → {sess.target} 的链接？(yes/no) "
                      if link_only else
                      f"确认继续把 {sess.source} 迁移到 {sess.target}？(yes/no) ")
            code = _ask_confirmation(prompt)
            if code is not None:
                return code
        # 不在这里 plan_it()：execute() 需要时会自己扫；而会话停在改名之后时
        # 源目录已经不在了，扫一遍只会得到一棵空树。
        outcome = migrator.execute()
        if not outcome.ok:
            _report_failure(outcome, migrator)
            return 1
        print(f"迁移完成，链接类型 {outcome.link_kind}，备份 {outcome.backup_path}")
        _print_log_path(outcome)
        if not migrator.cleanup_backup():
            print(f"备份未删除：{migrator.cleanup_error}", file=sys.stderr)
        return 0

    return 2
