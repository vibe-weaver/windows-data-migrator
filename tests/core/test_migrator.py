import os
import sys
import stat as stat_mod

import pytest

from dmig.core import linker, reparse
from dmig.core.migrator import Migrator

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


def test_rollback_restores_source(tmp_path, monkeypatch):
    src, ext = _tree(tmp_path)
    m = _migrator(tmp_path, src)

    # 只让「给源路径建链接」这一步失败。
    # 不能把 linker.create 整体打桩：copier 复用的是同一个 linker 模块，
    # 整体打桩会连复制阶段的链接重建一起打掉，结果变成 copy_failed 而不是 link_failed。
    real_create = linker.create

    def fail_only_for_source(link_path, target, is_dir, prefer_junction=True):
        if os.path.normcase(str(link_path)) == os.path.normcase(str(src)):
            raise OSError("boom")
        return real_create(link_path, target, is_dir, prefer_junction)

    monkeypatch.setattr("dmig.core.linker.create", fail_only_for_source)
    out = m.execute()
    assert out.ok is False
    assert out.phase == "link_failed"
    assert m.rollback() is True
    assert os.path.isdir(src)
    assert not (src and os.lstat(src).st_file_attributes & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT)
    assert (src / "two.bin").read_bytes() == b"2" * 500


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
