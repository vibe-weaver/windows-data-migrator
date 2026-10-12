import json
import os
import sys

import pytest

from dmig import cli
from dmig.core import copier, linker, reparse
from dmig.core.fsutil import reparse_kind
from dmig.core.migrator import Migrator
from dmig.core.session import Session

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _tree(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "one.txt").write_text("one", encoding="utf-8")
    (src / "two.bin").write_bytes(b"2" * 500)
    return src


def test_scan_subcommand_prints_plan_json(tmp_path, capsys):
    src = _tree(tmp_path)
    rc = cli.main(["scan", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["files"] == 2
    assert payload["totals"]["bytes"] == 503


def test_scan_prints_guard_warnings_to_stderr(tmp_path, capsys):
    """同盘迁移根本不释放空间，这类提示此前在 CLI 里一个字都看不见。"""
    src = _tree(tmp_path)
    rc = cli.main(["scan", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 0
    captured = capsys.readouterr()
    assert "提示：源与目标在同一个卷" in captured.err
    # 计划 JSON 走 stdout，提示走 stderr，脚本仍然能直接 json.loads
    assert json.loads(captured.out)["totals"]["files"] == 2


def test_migrate_shows_volume_warning_before_confirming(tmp_path, capsys):
    """提示必须排在确认之前：用户答 yes 前有权知道这是同盘迁移。"""
    src = _tree(tmp_path)
    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 2                      # 没有 tty 又没给 --yes，确认问不出口
    err = capsys.readouterr().err
    assert "提示：源与目标在同一个卷" in err
    assert err.index("同一个卷") < err.index("非交互环境")
    assert not (tmp_path / "dst").exists()


def test_scan_reports_dangling_links(tmp_path, capsys):
    """断链此前只在 scanner 里被算出来，CLI 一个字都不提。"""
    src = _tree(tmp_path)
    reparse.create_junction(src / "gone", tmp_path / "vanished")

    rc = cli.main(["scan", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert [item["rel"] for item in payload["dangling_links"]] == ["gone"]
    assert payload["dangling_links"][0]["new_target"] == str(tmp_path / "vanished")
    assert "1 条链接的目标在迁移前就不存在（断链）：gone" in captured.err


def test_scan_discloses_attributes_copy_cannot_preserve(tmp_path, capsys):
    """C9：硬链接复制后变成彼此独立的副本，此前一个字都不提。"""
    src = _tree(tmp_path)
    os.link(str(src / "two.bin"), str(src / "two-copy.bin"))

    rc = cli.main(["scan", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["lossy_attributes"]["hardlinked_files"] == 2
    assert "2 个硬链接会变成各自独立的副本" in captured.err


def test_scan_can_skip_the_ads_probe(tmp_path, capsys):
    """D1：ADS 探测是每文件一次额外系统调用，要能关，且不能把「没问过」说成 0。"""
    src = _tree(tmp_path)
    rc = cli.main(["scan", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--no-ads-scan"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["lossy_attributes"]["ads_files"] == 0
    assert payload["lossy_attributes"]["ads_probed"] is False
    assert "没有探测附加数据流" in captured.err


def test_scan_probes_ads_by_default(tmp_path, capsys):
    src = _tree(tmp_path)
    rc = cli.main(["scan", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["lossy_attributes"]["ads_probed"] is True


def test_migrate_prints_the_run_log_path(tmp_path, capsys):
    """D8：失败现场往往已经变了，日志路径要直接给出来。"""
    src = _tree(tmp_path)
    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--yes", "--keep-backup"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "运行日志：" in captured.out
    log_path = [line for line in captured.out.splitlines()
                if line.startswith("运行日志：")][0].split("：", 1)[1]
    assert os.path.exists(log_path)
    with open(log_path, encoding="utf-8") as handle:
        assert "结果：ok=True phase=done" in handle.read()


def test_migrate_dry_run_does_not_touch_sources(tmp_path, capsys):
    src = _tree(tmp_path)
    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--dry-run"])
    assert rc == 0
    assert (src / "two.bin").exists()
    assert not (tmp_path / "dst").exists()


def test_scan_can_exclude_a_directory(tmp_path, capsys):
    """无权限目录此前只能硬拒绝执行；--exclude 就是那条出路。"""
    src = _tree(tmp_path)
    junk = src / "junk"
    junk.mkdir()
    (junk / "big.bin").write_bytes(b"x" * 5000)

    rc = cli.main(["scan", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--exclude", str(junk)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["excluded"] == ["junk"]
    assert payload["excludes"] == [str(junk)]
    assert payload["totals"]["files"] == 2      # junk/big.bin 没算进来


def test_migrate_with_exclude_keeps_the_backup(tmp_path, capsys):
    """被排除的内容只留在备份里，自动清理备份时必须拒绝。"""
    src = _tree(tmp_path)
    junk = src / "junk"
    junk.mkdir()
    (junk / "big.bin").write_bytes(b"x" * 5000)

    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--exclude", str(junk), "--yes"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "迁移完成" in captured.out
    assert not (tmp_path / "dst" / "junk").exists()
    assert "1 个目录被排除" in captured.err
    assert "备份未删除" in captured.err


def _denied_source(tmp_path, monkeypatch):
    """造一个「扫描读不到、接管之后就能读」的目录，返回状态字典。

    用假的 os.scandir 而不是真去改 ACL：真改需要管理员权限，测试机不一定有，
    而这里要验的是「接管前后扫描结果的差异」怎么被处理。
    """

    def fake_scandir(path):
        if str(path).endswith("locked") and not state["taken"]:
            raise PermissionError(13, "denied")
        return real_scandir(path)

    src = _tree(tmp_path)
    (src / "locked").mkdir()
    (src / "locked" / "secret.bin").write_bytes(b"s" * 10)
    real_scandir = os.scandir
    state = {"taken": False}
    monkeypatch.setattr(os, "scandir", fake_scandir)
    return src, state


def test_take_ownership_requires_elevation(tmp_path, capsys, monkeypatch):
    """接管属主必须提权；不讲清楚的话用户只看到一句「无权限访问」。"""
    src, state = _denied_source(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.permission, "is_elevated", lambda: False)

    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--take-ownership", "--yes"])
    assert rc == 1
    assert "需要管理员权限" in capsys.readouterr().err
    assert state["taken"] is False


def test_take_ownership_migrates_the_whole_tree(tmp_path, capsys, monkeypatch):
    """接管成功后重扫，原本读不到的目录也一并迁移——这是「无损」的那条出路。"""
    src, state = _denied_source(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.permission, "is_elevated", lambda: True)
    monkeypatch.setattr(cli.permission, "backup_acl", lambda p: "SDDL")

    def take(path):
        state["taken"] = True
        return True

    monkeypatch.setattr(cli.permission, "take_ownership", take)

    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--take-ownership", "--yes", "--keep-backup"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "已接管 1 个目录的属主" in out
    assert "回滚时会还原" in out
    assert (tmp_path / "dst" / "locked" / "secret.bin").exists()


def test_take_ownership_is_skipped_when_nothing_is_denied(tmp_path, capsys,
                                                          monkeypatch):
    """没有无权限目录时不该调用任何接管动作，也不该多印一行。"""
    src = _tree(tmp_path)
    monkeypatch.setattr(cli.permission, "is_elevated", lambda: True)

    def boom(path):
        raise AssertionError("没有无权限目录时不该接管")

    monkeypatch.setattr(cli.permission, "take_ownership", boom)

    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--take-ownership", "--yes", "--keep-backup"])
    assert rc == 0
    assert "已接管" not in capsys.readouterr().out


def test_rollback_requires_yes_without_tty(tmp_path, capsys):
    """回滚是破坏性动作，非交互环境必须显式 --yes 表态。"""
    src = _tree(tmp_path)
    sess = Session.create(src, tmp_path / "dst", root=tmp_path / "sessions")
    rc = cli.main(["rollback", str(sess.path)])
    assert rc == 2
    assert "非交互环境" in capsys.readouterr().err


def test_rollback_restores_the_source_with_yes(tmp_path):
    """回滚真的把源目录退回来：摘掉源路径上的链接、把备份改回原名。"""
    src = _tree(tmp_path)
    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions")
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"
    # 迁移成功后源是一条重解析点（junction）；注意 os.path.islink 对 junction
    # 实测返回 False，它只认真正的符号链接
    assert reparse_kind(os.lstat(str(src))) is not None

    rc = cli.main(["rollback", str(m.session.path), "--yes"])
    assert rc == 0
    assert reparse_kind(os.lstat(str(src))) is None
    assert (src / "two.bin").read_bytes() == b"2" * 500
    assert not list(tmp_path.glob("src.bak_*"))


def test_rollback_says_why_when_it_refuses(tmp_path, capsys):
    """回滚被拒时必须说明原因。

    最常见的拒绝情形是备份已被删除（迁移成功后命令行会自动删掉备份），
    此时源路径上那条链接是唯一入口。原来 CLI 只写 `return 0 if rollback() else 1`，
    用户得到一个退出码 1，既不知道发生了什么，也不知道原路径还安不安全。
    """
    src = _tree(tmp_path)
    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions")
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"
    m.cleanup_backup()                     # 模拟迁移成功后的自动清理

    rc = cli.main(["rollback", str(m.session.path), "--yes"])

    assert rc == 1
    assert "备份" in capsys.readouterr().err, "必须说明拒绝原因"
    assert reparse_kind(os.lstat(str(src))) is not None, "源路径必须完好"
    assert (src / "two.bin").read_bytes() == b"2" * 500


def test_migrate_requires_yes_without_tty(tmp_path):
    src = _tree(tmp_path)
    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 2

def test_migrate_handles_eof_at_confirmation_prompt(tmp_path, monkeypatch, capsys):
    r"""交互确认时 stdin 到达 EOF，必须干净退出而不是抛 Traceback。

    真实场景：脚本调用、stdin 重定向、打包后 --windowed 的 exe 没有控制台。
    pytest 下 sys.stdin.isatty() 为 False 会在到达 input() 之前就返回，
    所以必须把 isatty 打桩为 True，才能真的覆盖这条路径。
    """
    src = _tree(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def raise_eof(prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)
    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 2
    assert "已取消" in capsys.readouterr().err
    assert not (tmp_path / "dst").exists()


def test_migrate_with_yes_completes(tmp_path, capsys):
    src = _tree(tmp_path)
    dst = tmp_path / "dst"
    rc = cli.main(["migrate", "--source", str(src), "--target", str(dst), "--yes",
                   "--session-root", str(tmp_path / "sessions"),
                   "--keep-backup"])
    assert rc == 0
    assert (dst / "two.bin").read_bytes() == b"2" * 500
    assert os.path.islink(src) or os.path.exists(src)


def test_scan_reports_guard_error(tmp_path, capsys):
    rc = cli.main(["scan", "--source", str(tmp_path / "nope"),
                   "--target", str(tmp_path / "dst")])
    assert rc == 1
    assert "不存在" in capsys.readouterr().err


def test_discard_refuses_when_source_is_already_a_link(tmp_path, capsys):
    """迁移成功后源目录就是指向目标的链接，此时删目标等于删活数据。"""
    src = _tree(tmp_path)
    dst = tmp_path / "dst"
    root = tmp_path / "sessions"
    assert cli.main(["migrate", "--source", str(src), "--target", str(dst), "--yes",
                     "--session-root", str(root), "--keep-backup"]) == 0
    capsys.readouterr()
    session = next(root.glob("*.json"))

    assert cli.main(["discard", str(session), "--yes"]) == 1
    assert "活数据" in capsys.readouterr().err
    assert (dst / "two.bin").read_bytes() == b"2" * 500


def test_discard_removes_partial_target(tmp_path, capsys):
    """B1 的出口：复制失败留下的半成品目标要能一键清掉，源目录不受影响。"""
    src = _tree(tmp_path)
    dst = tmp_path / "dst"
    (dst / "a").mkdir(parents=True)
    (dst / "a" / "one.txt").write_text("one", encoding="utf-8")   # 只复制了一半
    session = Session.create(src, dst, root=tmp_path / "sessions")
    session.phase = "copy_failed"
    session.save()

    assert cli.main(["discard", str(session.path), "--yes"]) == 0
    assert capsys.readouterr().out.strip() == "目标目录已删除"
    assert not dst.exists()
    assert (src / "two.bin").read_bytes() == b"2" * 500
    assert json.loads(session.path.read_text(encoding="utf-8"))["phase"] == "discarded"


def test_discard_requires_yes_without_tty(tmp_path, capsys):
    """非交互环境没传 --yes 就用退出码 2 说清楚，绝不擅自删东西。"""
    src = _tree(tmp_path)
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "half.bin").write_bytes(b"x" * 10)
    session = Session.create(src, dst, root=tmp_path / "sessions")

    assert cli.main(["discard", str(session.path)]) == 2
    assert "非交互环境" in capsys.readouterr().err
    assert (dst / "half.bin").exists()


def test_failure_output_points_at_the_way_out(tmp_path, monkeypatch, capsys):
    """失败时直接给出下一步命令，别让用户对着一句 WinError 猜。"""
    src = _tree(tmp_path)
    monkeypatch.setattr(copier, "RETRY_DELAYS", (0.01, 0.01))

    def denied(*args, **kwargs):
        raise PermissionError(13, "拒绝访问", None, 5)

    monkeypatch.setattr(copier, "copy_one", denied)
    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--yes", "--session-root", str(tmp_path / "sessions")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "失败于 copy_failed" in err
    assert "dmig resume" in err
    assert "dmig discard" in err
    assert (src / "two.bin").read_bytes() == b"2" * 500


def test_resume_continues_from_the_session_file(tmp_path, monkeypatch, capsys):
    """C1：resume 要接着上一次的记账跑，而不是换个新会话从头复制一遍。"""
    src = _tree(tmp_path)
    dst = tmp_path / "dst"
    root = tmp_path / "sessions"
    real_copy_one = copier.copy_one

    def denied_for_two_bin(s, d):
        if str(s).endswith("two.bin"):
            raise PermissionError(13, "拒绝访问", str(s), 5)
        return real_copy_one(s, d)

    monkeypatch.setattr(copier, "copy_one", denied_for_two_bin)
    monkeypatch.setattr(copier, "RETRY_DELAYS", (0.01,))
    assert cli.main(["migrate", "--source", str(src), "--target", str(dst), "--yes",
                     "--session-root", str(root)]) == 1
    capsys.readouterr()
    session = next(root.glob("*.json"))

    monkeypatch.setattr(copier, "copy_one", real_copy_one)
    assert cli.main(["resume", str(session), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "续传会话" in out
    assert "已记录 1 个文件" in out           # one.txt 已经落盘
    assert "迁移完成" in out
    assert (dst / "two.bin").read_bytes() == b"2" * 500
    assert list(root.glob("*.json")) == [session]


def test_resume_replays_the_recorded_link_mappings(tmp_path, monkeypatch, capsys):
    """C6：续传必须照原样重放会话里的链接重写映射。

    修复前实测：`resume` 用空映射重建 Migrator，第二趟把目标端已经指对的
    junction 删掉重建，指回迁移前的旧位置——而那个位置马上就要变成备份。
    """
    src = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pkg.dat").write_text("repo", encoding="utf-8")
    reparse.create_junction(src / "store", outside)
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "pkg.dat").write_text("repo", encoding="utf-8")
    dst = tmp_path / "dst"
    root = tmp_path / "sessions"

    real_copy_one = copier.copy_one

    def denied_for_two_bin(s, d):
        if str(s).endswith("two.bin"):
            raise PermissionError(13, "拒绝访问", str(s), 5)
        return real_copy_one(s, d)

    monkeypatch.setattr(copier, "copy_one", denied_for_two_bin)
    monkeypatch.setattr(copier, "RETRY_DELAYS", (0.01,))
    assert cli.main(["migrate", "--source", str(src), "--target", str(dst), "--yes",
                     "--map", f"{outside}={moved}",
                     "--session-root", str(root)]) == 1
    session = next(root.glob("*.json"))
    recorded = json.loads(session.read_text(encoding="utf-8"))["options"]
    assert recorded["mappings"] == {str(outside): str(moved)}
    capsys.readouterr()

    monkeypatch.setattr(copier, "copy_one", real_copy_one)
    # 故意不传 --map：映射只能从会话文件里来
    assert cli.main(["resume", str(session), "--yes"]) == 0
    assert "沿用会话记录的链接映射" in capsys.readouterr().out
    assert os.path.normcase(os.path.realpath(dst / "store")) == \
        os.path.normcase(os.path.realpath(moved))
    assert (dst / "store" / "pkg.dat").read_text(encoding="utf-8") == "repo"


def test_resume_finishes_a_run_that_stopped_after_the_rename(tmp_path, monkeypatch,
                                                             capsys):
    """C6：会话停在改名之后，源目录已经不在，resume 要能把最后一步走完。

    原来一律从 execute() 头部重来，guard 报「源目录不存在」直接失败，
    用户明明只差一条链接，唯一的出口却是回滚。
    """
    src = _tree(tmp_path)
    dst = tmp_path / "dst"
    root = tmp_path / "sessions"
    real_create = linker.create

    def fail_only_for_source(link_path, target, is_dir, prefer_junction=True):
        if os.path.normcase(str(link_path)) == os.path.normcase(str(src)):
            raise OSError("boom")
        return real_create(link_path, target, is_dir, prefer_junction)

    monkeypatch.setattr("dmig.core.linker.create", fail_only_for_source)
    assert cli.main(["migrate", "--source", str(src), "--target", str(dst), "--yes",
                     "--session-root", str(root)]) == 1
    session = next(root.glob("*.json"))
    assert not src.exists()
    err = capsys.readouterr().err
    assert "dmig resume" in err                 # 失败提示要指出续传这条路

    monkeypatch.setattr("dmig.core.linker.create", real_create)
    assert cli.main(["resume", str(session), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "只补建源目录链接" in out
    assert "迁移完成" in out
    assert (src / "two.bin").read_bytes() == b"2" * 500
    assert (src / "a" / "one.txt").read_text(encoding="utf-8") == "one"


def test_resume_requires_yes_without_tty(tmp_path, capsys):
    """非交互环境没传 --yes 就退出码 2，别在脚本里悄悄动数据。"""
    src = _tree(tmp_path)
    session = Session.create(src, tmp_path / "dst", root=tmp_path / "sessions")
    session.save()

    assert cli.main(["resume", str(session.path)]) == 2
    assert "非交互环境" in capsys.readouterr().err
    assert not (tmp_path / "dst").exists()
