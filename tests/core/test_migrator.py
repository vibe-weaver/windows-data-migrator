import os
import subprocess
import sys
import stat as stat_mod
import time
from pathlib import Path

import pytest

from dmig.core import copier, linker, migrator as migrator_mod, permission, reparse
from dmig.core.migrator import Migrator
from dmig.core.session import Session

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _tree(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "one.txt").write_text("one", encoding="utf-8")
    (src / "two.bin").write_bytes(b"2" * 500)
    ext = tmp_path / "ext" / "pkg"
    ext.mkdir(parents=True)
    (ext / "index.js").write_text("js", encoding="utf-8")
    reparse.create_junction(src / "link-pkg", ext)
    return src, ext


def _migrator(tmp_path, src):
    return Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions")


# --- 回滚后自动清目标端：先判断「目标端有没有迁移之外的内容」--------------
#
# 会话有一本复制记账（<id>.done），记的是本会话自己复制过哪些相对路径。
# 目标端多出来的条目就是迁移之后被写进去的（用户在那儿跑构建、下载文件），
# 回滚时若顺手删掉，等于静默销毁他的数据。

def test_clean_migration_leaves_no_extras(tmp_path):
    src, _ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    assert m.execute().ok is True

    assert m.target_extras() == []
    ok, reason = m.can_clear_target()
    assert ok is True
    assert reason == ""


def test_files_written_after_the_migration_are_extras(tmp_path):
    src, _ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.execute()

    dst = tmp_path / "dst"
    (dst / "新下载的电影.mp4").write_bytes(b"x" * 10)
    (dst / "a" / "跑构建生成的.log").write_text("build", encoding="utf-8")

    extras = m.target_extras()
    assert sorted(extras) == ["a\\跑构建生成的.log", "新下载的电影.mp4"]

    ok, reason = m.can_clear_target()
    assert ok is False
    assert "不属于这次迁移" in reason
    assert "2 项" in reason


def test_rebuilt_links_are_not_extras(tmp_path):
    """迁移会在目标端重建链接，那是迁移的正常产物，不是「多出来的内容」。

    链接不进复制记账（它们在第 2 趟单独重建），所以拿记账比对时，
    遍历目标端必须同样排除链接——否则每条重建出来的链接都会被误判成
    用户后写入的数据，自动清理永远被拒。
    """
    src, ext = _tree(tmp_path)          # _tree 里有一个指向树外的 junction
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    assert m.execute().ok is True
    assert (tmp_path / "dst" / "link-pkg").exists()

    assert m.target_extras() == []
    assert m.can_clear_target()[0] is True


def test_without_a_ledger_we_refuse_to_clear(tmp_path):
    """没有记账就确认不了，宁可拒绝自动清理。"""
    src, _ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.execute()
    m.session = None

    ok, reason = m.can_clear_target()
    assert ok is False
    assert "记账" in reason


def test_clearing_target_removes_the_migrated_copy(tmp_path):
    """确认可以清时，清完目标端目录不再存在。"""
    src, _ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.execute()
    m.rollback()

    assert m.can_clear_target()[0] is True
    assert m.discard_target() is True
    assert not (tmp_path / "dst").exists()


# --- 回滚时清目标端：只清内容，保留目录本身 -----------------------------
#
# discard_target 连目录一起删（那是「删除目标」按钮的语义，CLI 的 discard 也是）。
# 回滚要的不是这个：用户要的是「把同步过去的内容去掉」，而目标目录很可能是他
# 自己先建好的空目录（合入已有目录是被支持的场景，guard 里就有 target_not_empty）。
# 删掉整个目录等于顺手清掉了一个用户自己建的文件夹。

def test_clear_target_keeps_the_directory_itself(tmp_path):
    src, _ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.execute()
    m.rollback()

    assert m.clear_target() is True
    assert (tmp_path / "dst").is_dir(), "目录本身必须留着"
    assert list((tmp_path / "dst").iterdir()) == [], "里面应当清空"


def test_clear_target_unlinks_rebuilt_links_without_following(tmp_path):
    """目标端重建出来的链接要摘掉，但绝不能跟随删到外部仓库的数据。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.execute()
    m.rollback()
    assert (tmp_path / "dst" / "link-pkg").exists()

    assert m.clear_target() is True

    assert not (tmp_path / "dst" / "link-pkg").exists()
    assert (ext / "index.js").exists(), "外部仓库的数据一个字节都不能动"


def test_clear_target_does_not_clobber_the_rolled_back_phase(tmp_path):
    """清空目标端不该改写会话 phase。

    它是回滚之后的收尾动作，不是迁移本身的新结局。写成 "discarded" 会把回滚刚
    记下的 "rolled_back" 盖掉，会话文件里就再也看不出「回滚发生过」了。
    """
    src, _ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.execute()
    assert m.rollback() is True
    assert m.session.phase == "rolled_back"

    assert m.clear_target() is True

    assert m.session.phase == "rolled_back"


def test_clear_target_refuses_when_the_source_is_a_link(tmp_path):
    """源还是链接时目标端是活数据，不能清——和 discard_target 同一条底线。"""
    src, _ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.execute()                       # 成功后源就是链接了

    assert m.clear_target() is False
    assert (tmp_path / "dst" / "two.bin").exists()
    assert "活数据" in m.cleanup_error


def test_clear_target_without_a_copy_to_fall_back_on(tmp_path):
    src, _ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.execute()
    m.rollback()
    m.backup_path = None
    src.rename(tmp_path / "gone")

    assert m.clear_target() is False
    assert "无法确认" in m.cleanup_error


def test_rmtree_force_removes_top_level_junction_without_following(tmp_path):
    """rmtree_force 直接删一个**顶层** junction 时，要摘掉链接本身、不跟随删目标数据。

    原来 shutil.rmtree 跟随 junction 进目标仓库、又删不掉链接本身，最后还误报空清单
    （leftovers=[] 但链接仍在、目标数据险被删）。入口先判顶层是链接类重解析点：
    是则直接 rmdir/unlink 摘链接，绝不进 rmtree。生产路径（备份/目标都是真实目录）
    本来不触达顶层链接，但契约「空清单=删干净了」必须名副其实。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "precious.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link_to_repo"
    reparse.create_junction(link, repo)
    leftovers = migrator_mod.rmtree_force(link)
    assert not os.path.lexists(link), "顶层 junction 应被摘掉"
    assert (repo / "precious.txt").read_text(encoding="utf-8") == "keep", \
        "目标数据绝不能被跟随删除"
    assert leftovers == [], "摘掉了就该报空清单，不能误报成功"


def test_full_migration_creates_link_and_backup(tmp_path):
    src, ext = _tree(tmp_path)
    dst = tmp_path / "dst"
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok is True
    assert out.phase == "done"
    # 源路径变成了指向目标的链接
    st = os.lstat(src)
    assert st.st_file_attributes & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT
    assert os.path.exists(src / "two.bin")
    assert (src / "a" / "one.txt").read_text(encoding="utf-8") == "one"
    # 数据真的在新的位置
    assert (dst / "two.bin").read_bytes() == b"2" * 500
    # 备份目录存在且保留了原数据
    assert out.backup_path and os.path.isdir(out.backup_path)
    assert (os.path.join(out.backup_path, "two.bin"))
    # 链接也被重建
    assert os.path.exists(dst / "link-pkg" / "index.js")


def test_cleanup_backup_removes_it(tmp_path):
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert m.cleanup_backup() is True
    assert not os.path.exists(out.backup_path)


def _break_source_link(monkeypatch, src):
    """只让「给源路径建链接」这一步失败，其余链接照常建。

    不能把 linker.create 整体打桩：copier 复用的是同一个 linker 模块，
    整体打桩会连复制阶段的链接重建一起打掉，结果变成 copy_failed 而不是 link_failed。
    """
    real_create = linker.create

    def fail_only_for_source(link_path, target, is_dir, prefer_junction=True):
        if os.path.normcase(str(link_path)) == os.path.normcase(str(src)):
            raise OSError("boom")
        return real_create(link_path, target, is_dir, prefer_junction)

    monkeypatch.setattr("dmig.core.linker.create", fail_only_for_source)
    return real_create


def test_rollback_restores_source(tmp_path, monkeypatch):
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    _break_source_link(monkeypatch, src)
    out = m.execute()
    assert out.ok is False
    assert out.phase == "link_failed"
    assert m.rollback() is True
    assert os.path.isdir(src)
    assert not (src and os.lstat(src).st_file_attributes & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT)
    assert (src / "two.bin").read_bytes() == b"2" * 500


def test_rollback_refuses_when_backup_is_gone(tmp_path):
    """备份已不存在时回滚必须**拒绝**，绝不能把源路径上的链接摘掉。

    这是**默认流程**：CLI 迁移成功后会自动删除备份。此时源路径上那条链接
    是用户程序唯一的入口。原来 rollback 摘链接是无条件的、还原备份是有条件的，
    又无条件 return True —— 备份不在时就成了「报成功 + 原路径消失」。
    """
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok is True
    # 模拟迁移成功后的自动清理（CLI 不加 --keep-backup 就是这个结果）
    migrator_mod.rmtree_force(m.backup_path)
    assert not os.path.exists(m.backup_path)

    assert m.rollback() is False, "没有可还原的原件时必须拒绝"
    assert m.cleanup_error, "拒绝时要给出原因，不能默默返回 False"
    assert os.path.exists(src), "源路径绝不能被摘掉"
    assert os.lstat(src).st_file_attributes & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT, \
        "源路径应仍是那条链接"
    assert (src / "two.bin").read_bytes() == b"2" * 500, "经源路径仍要能读到数据"


def test_rollback_restores_backup_when_source_link_is_gone(tmp_path):
    """源路径上的链接已经被外部删掉、备份还在时，必须把备份还原回去。

    回滚是这种现场**唯一**的恢复手段：数据锁在备份里、原路径空着。
    原来只要 `_link_created` 为真就去摘链接，摘不到就直接 return False，
    备份永远不会被还原；而且那条失败分支没设 cleanup_error，
    界面/命令行只能显示「未知原因」。
    """
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok is True
    backup = m.backup_path
    os.rmdir(str(src))                     # 模拟外部删掉源路径上的链接
    assert not os.path.exists(src)

    assert m.rollback() is True, f"应还原备份，实际被拒：{m.cleanup_error!r}"
    assert os.path.isdir(src)
    assert not (os.lstat(src).st_file_attributes
                & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT)
    assert (src / "two.bin").read_bytes() == b"2" * 500
    assert not os.path.exists(backup), "备份应已被改回原名"


def test_rollback_failure_always_explains_itself(tmp_path):
    """源被换成了真实目录、备份还在：拒绝覆盖是对的，但必须说明原因。

    没有原因时界面与命令行只能显示「未知原因」，用户完全不知道该做什么。
    """
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok is True
    os.rmdir(str(src))
    (src / "a").mkdir(parents=True)        # 源位置冒出一个真实目录
    (src / "marker.txt").write_text("mine", encoding="utf-8")

    assert m.rollback() is False
    assert m.cleanup_error, "失败时必须给出原因"
    assert (src / "marker.txt").read_text(encoding="utf-8") == "mine", \
        "拒绝时不能动源里已有的东西"


def test_verify_failure_keeps_source_intact(tmp_path, monkeypatch):
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    monkeypatch.setattr("dmig.core.migrator.verifier.verify",
                        lambda *a, **k: __import__("dmig.core.models", fromlist=["VerifyResult"])
                        .VerifyResult(ok=False, missing=["two.bin"]))
    out = m.execute()
    assert out.ok is False
    assert out.phase == "verify_failed"
    # 源目录必须原封不动
    assert not (os.lstat(src).st_file_attributes & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT)
    assert (src / "two.bin").read_bytes() == b"2" * 500


def test_backup_name_contains_timestamp(tmp_path):
    from dmig.core.migrator import backup_name
    name = backup_name(tmp_path / "src")
    assert name.startswith("src.bak_")
    assert len(name) > len("src.bak_") + 8


def test_execute_refuses_when_guard_fails(tmp_path):
    m = Migrator(tmp_path / "nope", tmp_path / "dst",
                 session_root=tmp_path / "sessions")
    out = m.execute()
    assert out.ok is False
    assert out.phase == "guard_failed"


def test_execute_refuses_on_permission_issues(tmp_path):
    """预扫描发现无权限时不得进入复制阶段。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan_it()
    m.plan.permission_issues.append(str(src / "denied"))
    out = m.execute()
    assert out.ok is False
    assert out.phase == "permission_denied"
    assert not (tmp_path / "dst").exists()


def test_a_refused_run_still_leaves_a_log(tmp_path):
    """前置校验拒绝时也要留一份可交的现场。

    这三处（guard / 无权限 / 缺特权）都在会话建立之前 return，原来 runlog 一直
    是 None、outcome.log_path 是空串——用户被拒之后手里什么都没有。
    同时**不能为此建会话**：「前置校验失败时磁盘零改动」是实测确认过的性质。
    """
    src, ext = _tree(tmp_path)
    sessions = tmp_path / "sessions"
    m = Migrator(src, tmp_path / "dst", session_root=sessions)
    m.plan_it()
    m.plan.permission_issues.append(str(src / "denied"))

    out = m.execute()

    assert out.ok is False
    assert out.log_path, "拒绝路径应当留下日志路径"
    log = Path(out.log_path)
    assert log.exists()
    assert log.name.endswith("-refused.log")
    body = log.read_text(encoding="utf-8")
    assert "参数" in body, "日志里要有这一趟的参数，否则没法复盘"
    assert out.phase in body
    # 零改动的性质不能被破坏：只多了一条日志，没有 .json / .done
    assert list(sessions.glob("*.json")) == []
    assert list(sessions.glob("*.done")) == []


def test_cleanup_backup_does_not_follow_junctions(tmp_path):
    r"""删备份时只能删链接本身，绝不能跟进目标。

    备份目录就是改名后的源目录，里面含有指向外部仓库的 junction
    （真实场景里是 482 个指向 C 盘 npm 缓存的链接）。如果 rmtree 跟进了，
    删备份就会连外部仓库的真实数据一起删掉——这是不可挽回的数据损坏。
    """
    external = tmp_path / "external_store"
    external.mkdir()
    (external / "precious.txt").write_text("不能丢的数据", encoding="utf-8")
    (external / "sub").mkdir()
    (external / "sub" / "more.txt").write_text("更多数据", encoding="utf-8")

    src = tmp_path / "src"
    (src / "own").mkdir(parents=True)
    (src / "own" / "a.txt").write_text("自己的数据", encoding="utf-8")
    reparse.create_junction(src / "link-ext", external)

    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok is True

    # 备份里确实保留了那个链接
    assert os.path.isdir(os.path.join(out.backup_path, "link-ext"))
    assert m.cleanup_backup() is True

    # 外部仓库的真实数据必须分毫未动
    assert (external / "precious.txt").read_text(encoding="utf-8") == "不能丢的数据"
    assert (external / "sub" / "more.txt").read_text(encoding="utf-8") == "更多数据"


def test_mapping_reaches_the_created_link(tmp_path):
    """A1 端到端：带映射执行迁移，目标端链接必须指向映射后的位置。

    修复前实测：计划阶段显示的重写目标是对的，执行时映射被丢掉，
    junction 仍指向原始位置，用户以为搬走了其实没搬。
    """
    from dmig.core.fsutil import strip_long_prefix

    src = tmp_path / "src"
    src.mkdir()
    outside = tmp_path / "outside_store"
    outside.mkdir()
    (outside / "pkg.dat").write_text("repo data", encoding="utf-8")
    moved = tmp_path / "moved_store"
    reparse.create_junction(src / "store", outside)

    m = Migrator(src, tmp_path / "dst",
                 mappings={str(outside): str(moved)},
                 session_root=tmp_path / "sessions")
    plan = m.plan_it()
    assert plan.links[0].new_target == str(moved)
    assert plan.link_groups[0].mapped_to == str(moved)

    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"
    actual = strip_long_prefix(os.readlink(str(tmp_path / "dst" / "store")))
    assert os.path.normcase(actual.rstrip("\\")) == os.path.normcase(str(moved))


def test_non_link_reparse_survives_full_migration(tmp_path, make_reparse_file,
                                                  raw_reparse_buffer):
    """A3 端到端：读不动的重解析点要跟着迁移走到目标端，而不是只留在备份里。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_text("data", encoding="utf-8")
    make_reparse_file(src / "app.exe")

    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions")
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"
    assert reparse.read_raw(tmp_path / "dst" / "app.exe") == raw_reparse_buffer(
        0x8000001B)
    # 备份里那份也在，删备份才是安全的
    assert os.path.exists(os.path.join(out.backup_path, "app.exe"))
    assert m.cleanup_backup() is True


def test_cleanup_backup_reports_leftovers(tmp_path, monkeypatch):
    """A4：删不掉就必须如实回报，不能一边留着数据一边说「已删除」。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok

    # rmtree_force 现在用 Win32 API 直删（绕开被运行环境劫持的 shutil.rmtree），
    # 让删除函数失败来制造「删不掉」场景，验证残留清单如实回报、cleanup_backup
    # 返回 False、备份仍在、会话不被标记成 cleaned。
    from dmig.core import migrator as _mig

    monkeypatch.setattr(_mig, "_delete_file_win32", lambda p: False)
    monkeypatch.setattr(_mig, "_remove_dir_win32", lambda p: False)
    assert m.cleanup_backup() is False
    assert m.cleanup_error
    assert os.path.exists(out.backup_path)
    assert m.session.phase != "cleaned"


def test_cleanup_backup_refuses_when_reparse_skipped(tmp_path, make_reparse_file):
    """A3/A4：被跳过的重解析点只存在于备份中，删备份前必须明确强制。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_text("x", encoding="utf-8")
    make_reparse_file(src / "app.exe")

    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions",
                 skip_unknown_reparse=True)
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"
    assert m.skipped_reparse_count() == 1

    assert m.cleanup_backup() is False
    assert "重解析点" in m.cleanup_error
    assert os.path.exists(out.backup_path)
    assert not os.path.exists(tmp_path / "dst" / "app.exe")

    assert m.cleanup_backup(force=True) is True
    assert not os.path.exists(out.backup_path)


def test_cleanup_backup_handles_dir_shaped_reparse(tmp_path, make_reparse_file):
    """A4：目录形态的重解析点会让 scandir 失败于 WinError 1920。

    删备份必须照样删干净，并且不能把它误报成「删不掉的残留」。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_text("x", encoding="utf-8")
    make_reparse_file(src / "container", is_dir=True)

    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions")
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"
    assert m.cleanup_backup() is True
    assert m.cleanup_error == ""
    assert not os.path.exists(out.backup_path)


_LOCK_CHILD = """
import sys, time
handle = open(sys.argv[1], 'rb+')
print('HELD', flush=True)
time.sleep(30)
"""


def test_rename_retries_transient_failure(tmp_path, monkeypatch):
    """B1：改名冲突多半是瞬时的，退避重试就能过，不该一次失败就整趟报废。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    monkeypatch.setattr(migrator_mod, "RENAME_DELAYS", (0.0, 0.0))
    real_rename = os.rename
    attempts = {"n": 0}

    def flaky_rename(a, b, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(13, "拒绝访问", str(a))
        return real_rename(a, b, **kwargs)

    monkeypatch.setattr(os, "rename", flaky_rename)
    out = m.execute()
    assert attempts["n"] == 3
    assert out.ok is True, f"{out.phase} {out.message}"
    assert out.backup_path and os.path.isdir(out.backup_path)


def test_rename_failure_names_the_blocking_process(tmp_path, monkeypatch):
    """B1：改名失败要告诉用户关掉哪个程序，而不是只甩一句 WinError 5。

    子进程用普通 open() 持有 src/a/one.txt——共享读，复制照样成功，
    但祖先目录改名会被拒（实测 WinError 5），正是真实现场。
    """
    src, ext = _tree(tmp_path)
    (src / "a" / "one.txt").write_text("one", encoding="utf-8")
    m = _migrator(tmp_path, src)
    monkeypatch.setattr(migrator_mod, "RENAME_DELAYS", (0.01, 0.01))
    child = subprocess.Popen([sys.executable, "-c", _LOCK_CHILD,
                              str(src / "a" / "one.txt")],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "HELD"
        time.sleep(0.6)
        out = m.execute()
    finally:
        child.kill()
        child.wait(timeout=10)

    assert out.ok is False
    assert out.phase == "rename_failed"
    assert str(child.pid) in out.message, f"未点出占用进程：{out.message}"
    # 源目录完好、目标端已有完整副本：这正是需要出口的那个现场
    assert os.path.isdir(src)
    assert (tmp_path / "dst" / "two.bin").read_bytes() == b"2" * 500
    assert m.backup_path is None


def test_rename_failure_without_diagnosis_still_explains(tmp_path, monkeypatch):
    """查不出占用者时也要给出可操作的说明，不能只剩一句系统错误。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    monkeypatch.setattr(migrator_mod, "RENAME_DELAYS", (0.0,))
    monkeypatch.setattr(migrator_mod.locks, "who_locks_many", lambda paths, **kw: [])

    def deny(a, b, **kwargs):
        raise PermissionError(13, "拒绝访问", str(a))

    monkeypatch.setattr(os, "rename", deny)
    out = m.execute()
    assert out.phase == "rename_failed"
    assert "未查出占用进程" in out.message
    assert "源目录未改动" in out.message


def test_disk_full_stops_before_verify(tmp_path, monkeypatch):
    """B2：磁盘写满要立刻停在校验之前，并给出人能看懂的原因。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)

    def full(*args, **kwargs):
        raise OSError(28, "磁盘空间不足", None, 112)

    monkeypatch.setattr(copier, "copy_one", full)
    out = m.execute()
    assert out.ok is False
    assert out.phase == "aborted"
    assert "空间不足" in out.message
    assert out.verify is None, "磁盘都满了还去校验，纯属浪费时间"
    assert out.copy.aborted == "disk_full"
    assert os.path.isdir(src)


def test_missing_symlink_privilege_blocks_before_copying(tmp_path, monkeypatch):
    """B4：缺特权就一个字节都别复制，几秒内给出结论。

    原来这个错误要等到几十万个文件复制完、进入建链阶段才暴露。
    """
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan_it()
    m.plan.file_symlinks = 1               # 本机造不出真身，见 test_scanner 的说明
    monkeypatch.setattr(permission, "can_create_symlink", lambda: False)
    calls = {"n": 0}

    def spy(plan, sess, **kwargs):
        calls["n"] += 1
        raise AssertionError("不该走到复制")

    monkeypatch.setattr(migrator_mod, "copy_run", spy)
    out = m.execute()
    assert out.phase == "privilege_required"
    assert calls["n"] == 0
    assert "文件符号链接" in out.message
    assert not (tmp_path / "dst").exists()


def test_symlink_privilege_preflight_passes_when_capable(tmp_path, monkeypatch):
    """预检不能误伤：真能建符号链接时照常迁移。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan_it()
    m.plan.file_symlinks = 1
    monkeypatch.setattr(permission, "can_create_symlink", lambda: True)
    out = m.execute()
    assert out.ok is True, f"{out.phase} {out.message}"


def test_no_preflight_cost_without_file_symlinks(tmp_path, monkeypatch):
    """计划里没有文件符号链接时不该去做建链实测（那要建临时目录）。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.plan_it()

    def boom():
        raise AssertionError("没有文件符号链接却去实测建链")

    monkeypatch.setattr(permission, "can_create_symlink", boom)
    assert m.execute().ok is True


def test_cancel_during_verify_leaves_source_untouched(tmp_path, monkeypatch):
    """B6：校验阶段可以取消，取消后绝不能继续去改名源目录。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    state = {"copied": False}
    real_run = copier.run

    def run_then_flag(plan, sess, **kwargs):
        result = real_run(plan, sess, **kwargs)
        state["copied"] = True
        return result

    monkeypatch.setattr(migrator_mod, "copy_run", run_then_flag)
    out = m.execute(should_cancel=lambda: state["copied"])

    assert out.phase == "cancelled"
    assert out.verify is not None and out.verify.cancelled is True
    assert "源目录未改动" in out.message
    assert os.path.isdir(src)
    assert not migrator_mod.is_reparse_path(src)
    assert out.backup_path is None
    # 目标端已有完整副本，用户可以续传或清掉
    assert (tmp_path / "dst" / "two.bin").read_bytes() == b"2" * 500


def test_verify_progress_reaches_the_caller(tmp_path):
    """B6：校验进度要能传到界面，阶段标记为 verifying。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    phases = []
    out = m.execute(on_progress=lambda p: phases.append(p.phase))
    assert out.ok is True
    assert "copying" in phases
    assert "verifying" in phases, "校验阶段没有发出任何进度"


def test_discard_target_removes_partial_copy(tmp_path, monkeypatch):
    """B1 的出口：复制失败留下的半成品目标要能一键清掉，源目录不受影响。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)

    def denied(*args, **kwargs):
        raise PermissionError(13, "拒绝访问", None, 5)

    monkeypatch.setattr(copier, "copy_one", denied)
    monkeypatch.setattr(copier, "RETRY_DELAYS", (0.01, 0.01))
    out = m.execute()
    assert out.phase == "copy_failed"
    assert (tmp_path / "dst").exists()

    assert m.discard_target() is True
    assert m.cleanup_error == ""
    assert not (tmp_path / "dst").exists()
    assert (src / "two.bin").read_bytes() == b"2" * 500
    assert m.session.phase == "discarded"


def test_discard_target_refuses_when_source_is_link(tmp_path):
    """迁移成功后源目录已是指向目标的链接，此时删目标等于删活数据。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok is True

    assert m.discard_target() is False
    assert "链接" in m.cleanup_error
    assert (tmp_path / "dst").exists()
    assert (src / "two.bin").read_bytes() == b"2" * 500


def test_discard_target_refuses_without_any_other_copy(tmp_path):
    """源目录和备份都不在时无法确认数据另有副本，必须拒绝。"""
    src, ext = _tree(tmp_path)
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "two.bin").write_bytes(b"2" * 500)
    m = Migrator(tmp_path / "gone", dst, session_root=tmp_path / "sessions")
    assert m.discard_target() is False
    assert "拒绝删除目标" in m.cleanup_error
    assert dst.exists()


# --- C1：续传 ------------------------------------------------------
def test_adopt_session_refuses_a_different_tree(tmp_path):
    """`.done` 记的是相对路径，换一棵树沿用等于把不相干的文件当成已复制而跳过。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    foreign = Session.create(r"D:\other\src", r"E:\other\dst",
                             root=tmp_path / "sessions")
    assert m.adopt_session(foreign) is False
    assert m.session is None
    assert m.adopt_session(None) is False


def test_adopt_session_accepts_the_same_paths_regardless_of_form(tmp_path):
    """会话文件里的大小写、正斜杠、结尾反斜杠都不该让续传失效。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    sess = Session.create(str(src).upper().replace("\\", "/"),
                          str(tmp_path / "dst") + "\\",
                          root=tmp_path / "sessions")
    sess.backup_path = str(tmp_path / "bak")
    assert m.adopt_session(sess) is True
    assert m.session is sess
    # 上一轮可能已经改过名，备份路径是 discard_target 判断数据是否另有副本的依据
    assert m.backup_path == str(tmp_path / "bak")


def test_resume_skips_files_the_previous_run_copied(tmp_path, monkeypatch):
    """续传的意义：上一轮已经落盘且没被动过的文件直接跳过，不再重复制一遍。"""
    src, ext = _tree(tmp_path)
    real_copy_one = copier.copy_one

    def denied_for_two_bin(s, d):
        if str(s).endswith("two.bin"):
            raise PermissionError(13, "拒绝访问", str(s), 5)
        return real_copy_one(s, d)

    monkeypatch.setattr(copier, "copy_one", denied_for_two_bin)
    monkeypatch.setattr(copier, "RETRY_DELAYS", (0.0,))
    first = _migrator(tmp_path, src)
    out = first.execute()
    assert out.phase == "copy_failed"
    assert (tmp_path / "dst" / "a" / "one.txt").read_text(encoding="utf-8") == "one"
    session_path = first.session.path
    assert not (tmp_path / "dst" / "two.bin").exists()

    monkeypatch.setattr(copier, "copy_one", real_copy_one)
    resumed = _migrator(tmp_path, src)
    assert resumed.adopt_session(Session.load(session_path)) is True
    resumed.plan_it()
    out2 = resumed.execute()
    assert out2.ok is True, out2.message
    assert out2.copy.skipped == 1              # one.txt 被跳过，two.bin 真的复制了
    assert (tmp_path / "dst" / "two.bin").read_bytes() == b"2" * 500
    # 沿用同一个会话文件，而不是又新建一个孤儿会话
    assert resumed.session.path == session_path
    assert len(list((tmp_path / "sessions").glob("*.json"))) == 1
    assert resumed.session.phase == "done"


def test_execute_records_the_run_options(tmp_path):
    """C6：运行参数要落进会话文件，续传才有得可重放。

    映射丢了最要命：目标端那几百个 junction 会在续传时被重新指回迁移前的旧位置。
    """
    src, ext = _tree(tmp_path)
    moved = tmp_path / "moved"
    m = Migrator(src, tmp_path / "dst", mappings={str(ext): str(moved)},
                 deep_verify=True, session_root=tmp_path / "sessions")
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"

    reloaded = Session.load(m.session.path)
    assert reloaded.options == {"mappings": {str(ext): str(moved)},
                                "deep_verify": True,
                                "skip_unknown_reparse": False,
                                "excludes": [],
                                "probe_ads": True}


# --- C6：会话停在改名之后 ---------------------------------------------
def _session_at(tmp_path, phase, backup=None):
    sess = Session.create(tmp_path / "src", tmp_path / "dst",
                          root=tmp_path / "sessions")
    sess.phase = phase
    sess.backup_path = str(backup) if backup else None
    return sess


def test_awaiting_source_link_needs_a_real_backup(tmp_path):
    """备份不在就不能凭 phase 硬建链接：那等于凭空断言「数据已就位」。"""
    src = tmp_path / "src"
    assert migrator_mod.awaiting_source_link(None, src) is False
    assert migrator_mod.awaiting_source_link(_session_at(tmp_path, "created"), src) is False

    sess = _session_at(tmp_path, "link_failed")
    assert migrator_mod.awaiting_source_link(sess, src) is False      # 没记备份路径
    sess.backup_path = str(tmp_path / "gone")
    assert migrator_mod.awaiting_source_link(sess, src) is False      # 备份目录不在

    backup = tmp_path / "src.bak_20261010"
    backup.mkdir()
    sess.backup_path = str(backup)
    for phase in ("renamed", "link_failed", "link_verify_failed"):
        sess.phase = phase
        assert migrator_mod.awaiting_source_link(sess, src) is True


def test_awaiting_source_link_steps_aside_when_the_source_is_back(tmp_path):
    """用户手工把备份改回原名之后就该走常规流程重扫重复制，不能只补一条链接。"""
    src = tmp_path / "src"
    src.mkdir()
    backup = tmp_path / "src.bak_20261010"
    backup.mkdir()
    sess = _session_at(tmp_path, "renamed", backup)
    assert migrator_mod.awaiting_source_link(sess, src) is False


def test_resume_finishes_a_run_that_stopped_after_the_rename(tmp_path, monkeypatch):
    """C6：会话停在改名之后，源目录已经不在了。

    原来 resume 一律从 execute() 头部重来：扫到一棵空树、guard 报「源目录不存在」，
    用户明明只差最后一步，唯一的出口却是回滚——几小时的迁移白做。
    """
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    real_create = _break_source_link(monkeypatch, src)
    out = m.execute()
    assert out.phase == "link_failed"
    backup = out.backup_path
    assert os.path.isdir(backup)
    assert not os.path.exists(src)              # 源路径此刻什么都不是
    assert (tmp_path / "dst" / "two.bin").read_bytes() == b"2" * 500
    session_path = m.session.path

    monkeypatch.setattr("dmig.core.linker.create", real_create)
    resumed = _migrator(tmp_path, src)
    assert resumed.adopt_session(Session.load(session_path)) is True
    out2 = resumed.execute()

    assert out2.ok is True, f"{out2.phase} {out2.message}"
    assert out2.link_kind == "junction"
    assert out2.copy is None                    # 这一趟没有复制任何东西
    assert "只补建了源目录链接" in out2.message
    # 源路径重新可访问，内容来自目标端
    assert (src / "two.bin").read_bytes() == b"2" * 500
    assert (src / "a" / "one.txt").read_text(encoding="utf-8") == "one"
    # 备份原封不动，仍然可以清理或回滚
    assert os.path.isdir(backup)
    assert out2.backup_path == backup
    assert resumed.session.phase == "done"
    assert len(list((tmp_path / "sessions").glob("*.json"))) == 1


def test_resume_after_rename_reports_a_link_that_cannot_be_created(
        tmp_path, monkeypatch):
    """补建这一步自己也可能失败：要如实回报，不能假装迁移完成。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    _break_source_link(monkeypatch, src)
    assert m.execute().phase == "link_failed"
    session_path = m.session.path

    resumed = _migrator(tmp_path, src)
    assert resumed.adopt_session(Session.load(session_path)) is True
    out = resumed.execute()

    assert out.ok is False
    assert out.phase == "link_failed"
    assert "boom" in out.message
    assert resumed.session.phase == "link_failed"
    # 目标端的数据与备份都没被动过
    assert (tmp_path / "dst" / "two.bin").read_bytes() == b"2" * 500
    assert os.path.isdir(out.backup_path)


# --- C3：卷层面的前置校验 ---------------------------------------------
def test_guard_errors_passes_the_link_count(tmp_path, monkeypatch):
    """目标卷存不下链接时，guard 只有拿到链接条数才能判成致命错误。

    少传这一个参数，前置校验就永远只给一句提示，迁移照跑，
    最后死在复制完几小时之后的建链阶段。
    """
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    plan = m.plan_it()
    assert plan.links

    seen: dict = {}
    real = migrator_mod.guard.check

    def spy(source, target, **kwargs):
        seen.update(kwargs)
        return real(source, target, **kwargs)

    monkeypatch.setattr(migrator_mod.guard, "check", spy)
    assert m.guard_errors() == []
    assert seen == {"required_bytes": plan.required_bytes,
                    "link_count": len(plan.links)}


def test_guard_errors_without_a_plan_assumes_no_links(tmp_path, monkeypatch):
    seen: dict = {}
    real = migrator_mod.guard.check

    def spy(source, target, **kwargs):
        seen.update(kwargs)
        return real(source, target, **kwargs)

    src = tmp_path / "src"
    src.mkdir()
    m = _migrator(tmp_path, src)
    monkeypatch.setattr(migrator_mod.guard, "check", spy)
    m.guard_errors()
    assert seen == {"required_bytes": 0, "link_count": 0}


# --- C5：断链全链路可见 -----------------------------------------------
def test_dangling_link_is_recorded_in_the_outcome(tmp_path):
    """断链在源里就已经指着空气，下游（计划页、报告）要靠这条记录标出来。"""
    src, ext = _tree(tmp_path)
    reparse.create_junction(src / "gone", tmp_path / "vanished")

    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"

    by_rel = {link["rel"]: link for link in out.links}
    assert by_rel["gone"]["is_dangling"] is True
    assert by_rel["link-pkg"]["is_dangling"] is False
    # 照样原样重建了：目标端仍然是一条链接，只是依旧指不到东西
    assert migrator_mod.is_reparse_path(tmp_path / "dst" / "gone")
    # 会话文件里也留着，导出报告与事后排查都靠它
    assert Session.load(m.session.path).links == out.links


def test_links_record_what_was_rebuilt_even_if_a_later_step_fails(tmp_path, monkeypatch):
    """清单来自会话记录的实际动作，不是计划里的打算。

    建链刻意排在批量复制之前（否则要等几小时才发现权限错误），所以后面的校验
    失败时链接已经在目标端建好了；清单里如实反映这一点，用户才知道现场是什么样。
    """
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    monkeypatch.setattr("dmig.core.migrator.verifier.verify",
                        lambda *a, **k: __import__("dmig.core.models", fromlist=["VerifyResult"])
                        .VerifyResult(ok=False, missing=["two.bin"]))
    out = m.execute()
    assert out.phase == "verify_failed"
    assert [link["rel"] for link in out.links] == ["link-pkg"]
    assert migrator_mod.is_reparse_path(tmp_path / "dst" / "link-pkg")


# --- C8：目标端重建出来的每个链接都要核对 -----------------------------
def test_misdirected_rebuilt_link_stops_before_the_rename(tmp_path, monkeypatch):
    """映射填错（少一个反斜杠、指到别的盘）会让目标端链接指到别处。

    此前只验顶层那条源目录链接，成百上千个嵌套 junction 建错位置没人知道；
    而顶层链接照样能打开，校验也照样通过——错误就一路留到用户某天点开它。
    """
    src, ext = _tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real_create = linker.create

    def misdirect(link_path, target, is_dir, prefer_junction=True):
        if os.path.basename(str(link_path)) == "link-pkg":
            target = elsewhere         # 模拟映射错了
        return real_create(link_path, target, is_dir,
                           prefer_junction=prefer_junction)

    monkeypatch.setattr("dmig.core.linker.create", misdirect)
    m = _migrator(tmp_path, src)
    out = m.execute()

    assert out.ok is False
    assert out.phase == "links_broken"
    assert "link-pkg" in out.message
    # 失败发生在改名之前：源目录原封不动，也没有留下备份
    assert not migrator_mod.is_reparse_path(src)
    assert (src / "two.bin").read_bytes() == b"2" * 500
    assert not list(tmp_path.glob("src.bak_*"))
    # 源完好的现场留给用户的就是这条出路
    assert m.discard_target() is True
    assert not (tmp_path / "dst").exists()


# --- C9：复制保不住的属性 ---------------------------------------------
def test_outcome_discloses_attributes_copy_cannot_preserve(tmp_path):
    """硬链接复制后成了两份独立数据：报告要看得到，否则「校验通过」被当成没变。"""
    src, ext = _tree(tmp_path)
    os.link(str(src / "two.bin"), str(src / "two-copy.bin"))

    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"
    assert out.hardlinked_files == 2
    assert out.sparse_files == 0
    assert out.ads_files == 0
    # 两份副本确实各自独立了：删掉目标端其中一份，另一份必须还在
    (tmp_path / "dst" / "two-copy.bin").unlink()
    assert (tmp_path / "dst" / "two.bin").read_bytes() == b"2" * 500


# --- C4：排除目录 -----------------------------------------------------
def _tree_with_locked(tmp_path):
    src, ext = _tree(tmp_path)
    locked = src / "locked"
    locked.mkdir()
    (locked / "secret.bin").write_bytes(b"s" * 100)
    return src, ext, locked


def test_excluded_directory_migrates_and_is_disclosed(tmp_path):
    """无权限目录此前是死路：扫描报错、迁移直接拒绝执行，用户没有出口。"""
    src, ext, locked = _tree_with_locked(tmp_path)
    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions",
                 excludes=[str(locked)])

    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"
    assert out.excluded == ["locked"]
    assert not (tmp_path / "dst" / "locked").exists()
    # 被排除的内容只存在于备份里，删备份前必须拦住
    assert m.excluded_count() == 1
    assert m.cleanup_backup() is False
    assert "1 个目录被排除" in m.cleanup_error
    assert m.cleanup_backup(force=True) is True


def test_excludes_are_written_to_the_session(tmp_path):
    """排除项丢了，续传会把用户明确排除的目录重新复制一遍、空间也白占。"""
    src, ext, locked = _tree_with_locked(tmp_path)
    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions",
                 excludes=[str(locked)])
    assert m.execute().ok

    assert Session.load(m.session.path).options["excludes"] == [str(locked)]


def test_options_are_on_disk_before_copying_starts(tmp_path, monkeypatch):
    """P1 回归：复制开始之前，运行参数必须已经落盘。

    原来要等复制失败或全部成功才 save()，进程在复制中途被杀时盘上还是
    options={}——续传读回来 mappings/excludes/deep_verify 全没了。
    用户明确排除的目录被重新扫回复制、SHA256 静默降级，最后照样报成功。
    这里模拟「杀在复制刚开始」：让 copier.run 一进门就抛 SystemExit，
    然后从磁盘 reload 会话，检查 options 是否完整。
    """
    src, ext = _tree(tmp_path)
    moved = tmp_path / "moved"

    def kill_mid_copy(*a, **kw):
        raise SystemExit("模拟进程被杀")

    monkeypatch.setattr(migrator_mod, "copy_run", kill_mid_copy)

    m = Migrator(src, tmp_path / "dst", mappings={str(ext): str(moved)},
                 deep_verify=True, session_root=tmp_path / "sessions")
    with pytest.raises(SystemExit):
        m.execute()

    reloaded = Session.load(m.session.path)
    assert reloaded.options["mappings"] == {str(ext): str(moved)}
    assert reloaded.options["deep_verify"] is True
    assert reloaded.totals["files"] > 0


def test_nothing_excluded_leaves_cleanup_unblocked(tmp_path):
    """没有排除项时，删备份不该被这条新规则平白拦下。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    assert m.execute().ok
    assert m.excluded_count() == 0
    assert m.cleanup_backup() is True


# --- C2：接管属主与 ACL 还原 -----------------------------------------
def test_take_over_requires_elevation(tmp_path, monkeypatch):
    """接管属主必须提权；不提权时要给出明确原因，而不是静默返回 0。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    monkeypatch.setattr(migrator_mod.permission, "is_elevated", lambda: False)
    taken, error = m.take_over_denied()
    assert taken == 0
    assert "管理员权限" in error


def test_take_over_records_the_acl_before_touching_the_owner(tmp_path, monkeypatch):
    """顺序不能反：必须先导出原 ACL 再改属主。

    反过来就是拿被改过的描述符当原件——原属主已经换人，回滚也还原不回去。
    """
    src, ext = _tree(tmp_path)
    locked = src / "locked"
    locked.mkdir()
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.plan.permission_issues = [str(locked)]      # 模拟扫描到的无权限目录

    order: list[tuple[str, str]] = []
    monkeypatch.setattr(migrator_mod.permission, "is_elevated", lambda: True)
    monkeypatch.setattr(
        migrator_mod.permission, "backup_acl",
        lambda p: order.append(("backup", p)) or "D:AI(A;OICI;FA;;;SY)")
    monkeypatch.setattr(
        migrator_mod.permission, "take_ownership",
        lambda p: order.append(("take", p)) or True)

    assert m.take_over_denied() == (1, "")
    assert order == [("backup", str(locked)), ("take", str(locked))]
    assert m.session.acl_backup == [{"path": str(locked),
                                     "sddl": "D:AI(A;OICI;FA;;;SY)"}]
    # 权限变了，旧计划作废，必须重扫（这里权限是替身，因此仍会报同样的目录）
    assert m.plan is not None


def test_take_over_keeps_going_when_the_acl_cannot_be_read(tmp_path, monkeypatch):
    """连 READ_CONTROL 都没有时记下 sddl=None 照样接管，但不会假装能还原。"""
    src, ext = _tree(tmp_path)
    locked = src / "locked"
    locked.mkdir()
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.plan.permission_issues = [str(locked)]

    monkeypatch.setattr(migrator_mod.permission, "is_elevated", lambda: True)
    monkeypatch.setattr(migrator_mod.permission, "backup_acl", lambda p: None)
    monkeypatch.setattr(migrator_mod.permission, "take_ownership", lambda p: True)

    assert m.take_over_denied() == (1, "")
    assert m.session.acl_backup == [{"path": str(locked), "sddl": None}]


def test_take_over_reports_total_failure(tmp_path, monkeypatch):
    src, ext = _tree(tmp_path)
    locked = src / "locked"
    locked.mkdir()
    m = _migrator(tmp_path, src)
    m.plan = m.plan_it()
    m.plan.permission_issues = [str(locked)]

    monkeypatch.setattr(migrator_mod.permission, "is_elevated", lambda: True)
    monkeypatch.setattr(migrator_mod.permission, "backup_acl", lambda p: "SDDL")
    monkeypatch.setattr(migrator_mod.permission, "take_ownership", lambda p: False)

    taken, error = m.take_over_denied()
    assert taken == 0
    assert "接管失败" in error and "locked" in error


def _migrator_with_acl(tmp_path, sddl="SDDL"):
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.session = Session.create(src, tmp_path / "dst", root=tmp_path / "sessions")
    m.session.acl_backup = [{"path": str(src / "a"), "sddl": sddl}]
    m.session.save()
    return m


def test_rollback_restores_the_taken_over_acl(tmp_path, monkeypatch):
    """接管把源树上的属主改掉了；回滚不还原等于永久改掉用户的权限配置。"""
    m = _migrator_with_acl(tmp_path)
    restored: list[tuple[str, str]] = []
    monkeypatch.setattr(migrator_mod.permission, "restore_acl",
                        lambda path, sddl: restored.append((path, sddl)) or True)

    assert m.rollback() is True
    assert restored == [(str(m.source / "a"), "SDDL")]
    assert m.cleanup_error == ""


def test_acl_restore_follows_the_renamed_backup(tmp_path, monkeypatch):
    """接管发生在源树上，回滚时源目录可能已改名成备份，路径要跟着换算。

    不换算就会打在一条不存在的路径上，权限永远回不去。
    """
    m = _migrator_with_acl(tmp_path)
    backup = tmp_path / "src.bak_20261009120000"
    os.rename(m.source, backup)
    m.backup_path = str(backup)

    seen: list[str] = []
    monkeypatch.setattr(migrator_mod.permission, "restore_acl",
                        lambda path, sddl: seen.append(path) or True)

    assert m._restore_acl() == []
    assert seen == [str(backup / "a")]


def test_acl_restore_failures_are_reported(tmp_path, monkeypatch):
    """还原失败必须说出来，否则用户以为权限已经退回去了。"""
    m = _migrator_with_acl(tmp_path)
    monkeypatch.setattr(migrator_mod.permission, "restore_acl", lambda p, s: False)

    assert m.rollback() is True
    assert "1 个目录的原始权限没能还原" in m.cleanup_error


def test_acl_entry_without_a_recorded_sddl_is_not_a_failure(tmp_path, monkeypatch):
    """接管时就告知过「这个路径的原 ACL 没记下来」，回滚时不该再算失败。"""
    m = _migrator_with_acl(tmp_path, sddl=None)
    monkeypatch.setattr(migrator_mod.permission, "restore_acl", lambda p, s: False)
    assert m._restore_acl() == []


def test_acl_restore_is_a_noop_without_records(tmp_path, monkeypatch):
    """没接管过就不该碰任何权限：GetNamedSecurityInfo 之外的写操作都有风险。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    m.session = Session.create(src, tmp_path / "dst", root=tmp_path / "sessions")

    def boom(path, sddl):
        raise AssertionError("没有接管记录时不该调用 restore_acl")

    monkeypatch.setattr(migrator_mod.permission, "restore_acl", boom)
    assert m._restore_acl() == []


# --- C1：重新校验的比对基准 -------------------------------------------
def test_reverify_baseline_is_the_source_before_migration(tmp_path):
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    assert m.reverify_baseline() == (str(src), "")


def test_reverify_baseline_switches_to_the_backup_after_migration(tmp_path):
    """迁移成功后源已经是指向目标的链接：拿它当原件就是拿目标和目标自己比。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"

    baseline, error = m.reverify_baseline()
    assert error == ""
    assert baseline == out.backup_path
    assert os.path.normcase(baseline) != os.path.normcase(str(src))


def test_reverify_baseline_is_honest_when_the_backup_is_gone(tmp_path):
    """原件都没了就如实说，绝不能返回一个假的通过。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    assert m.execute().ok
    assert m.cleanup_backup() is True            # 备份删了，源又已是链接

    baseline, error = m.reverify_baseline()
    assert baseline == ""
    assert "没有可比对的原件" in error


def test_reverify_detects_a_corrupted_target_after_migration(tmp_path):
    """实证：迁移后把目标文件改坏，「重新校验」必须报出不一致。

    修复前它比的是目标和目标自己，改坏也一样通过。
    """
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    assert m.execute().ok

    (tmp_path / "dst" / "two.bin").write_bytes(b"CORRUPTED!!")   # 大小也变了
    baseline, error = m.reverify_baseline()
    assert error == ""

    result = migrator_mod.verifier.verify(baseline, m.target)
    assert result.ok is False
    assert "two.bin" in result.mismatch


def test_reverify_catches_a_same_size_change(tmp_path):
    """同大小、只改了内容与时间戳的改动同样要查得出来。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"

    target_file = tmp_path / "dst" / "two.bin"
    target_file.write_bytes(b"9" * 500)
    stamp = os.stat(os.path.join(out.backup_path, "two.bin")).st_mtime + 60
    os.utime(target_file, (stamp, stamp))

    baseline, _ = m.reverify_baseline()
    assert migrator_mod.verifier.verify(baseline, m.target).ok is False


# --- D8：运行日志 -----------------------------------------------------
def _log_text(outcome) -> str:
    with open(outcome.log_path, encoding="utf-8") as handle:
        return handle.read()


def test_run_log_records_the_whole_run(tmp_path):
    """失败现场往往已经变了（源被改名成备份），复盘全过程只能靠这份日志。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok, f"{out.phase} {out.message}"

    assert out.log_path == str(m.session.path.with_suffix(".log"))
    assert os.path.exists(out.log_path)
    text = _log_text(out)
    for needle in ("开始一趟", "参数：", "扫描：files=2", "复制：copied=2",
                   "校验：ok=True", "备份：", "源目录链接：junction",
                   "结果：ok=True phase=done"):
        assert needle in text, f"日志缺 {needle}：\n{text}"


def test_run_log_names_the_failed_items(tmp_path, monkeypatch):
    """失败项要落进日志，否则只能去翻会话 JSON 才知道是哪个文件。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    monkeypatch.setattr("dmig.core.migrator.verifier.verify",
                        lambda *a, **k: __import__("dmig.core.models", fromlist=["VerifyResult"])
                        .VerifyResult(ok=False, missing=["two.bin"]))
    out = m.execute()
    assert out.phase == "verify_failed"

    text = _log_text(out)
    assert "校验：ok=False" in text
    assert "校验缺失：1 项" in text
    assert "two.bin" in text
    assert "结果：ok=False phase=verify_failed" in text


def test_run_log_accumulates_instead_of_overwriting(tmp_path):
    """续传沿用同一个会话，日志也接着写——抹掉上一趟等于丢掉现场。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    assert m.execute().ok

    second = _migrator(tmp_path, src)
    assert second.adopt_session(Session.load(m.session.path)) is True
    out = second.execute()
    assert out.ok is False                       # 源已是指向目标的链接，前置校验会拒绝

    text = _log_text(out)
    assert text.count("结果：") >= 2


def test_rollback_and_cleanup_leave_a_log_line(tmp_path):
    """回滚与删备份都是破坏性动作，事后必须查得到「谁做的、做成没有」。

    两件事各跑一趟。原来这条测试是「先 cleanup_backup() 删掉备份，再 rollback()
    并期望成功」——它恰好把「备份没了还能回滚」这个缺陷当成了正确行为，
    而那个组合会让源路径彻底消失。现在那种组合会被拒绝（见下一条测试）。
    """
    # 第一趟：删备份
    case_a = tmp_path / "case_a"
    case_a.mkdir()
    src_a, _ext_a = _tree(case_a)
    m_a = _migrator(case_a, src_a)
    out_a = m_a.execute()
    assert out_a.ok, f"{out_a.phase} {out_a.message}"
    assert m_a.cleanup_backup() is True
    with open(out_a.log_path, encoding="utf-8") as handle:
        assert "删除备份：完成" in handle.read()

    # 第二趟：回滚（备份还在，才走得通）
    case_b = tmp_path / "case_b"
    case_b.mkdir()
    src_b, _ext_b = _tree(case_b)
    m_b = _migrator(case_b, src_b)
    out_b = m_b.execute()
    assert out_b.ok, f"{out_b.phase} {out_b.message}"
    assert m_b.rollback() is True
    with open(out_b.log_path, encoding="utf-8") as handle:
        assert "回滚：完成" in handle.read()


def test_refused_rollback_is_also_logged(tmp_path):
    """被拒的回滚同样要留痕，否则用户事后查不到「为什么没回滚」。"""
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)
    out = m.execute()
    assert out.ok
    migrator_mod.rmtree_force(m.backup_path)

    assert m.rollback() is False
    with open(out.log_path, encoding="utf-8") as handle:
        assert "回滚：被拒" in handle.read()

