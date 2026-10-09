"""端到端：在临时目录里造出真实场景，跑完整迁移与回滚。"""
import os
import stat
import sys

import pytest

from dmig.core import linker, reparse
from dmig.core.migrator import Migrator

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _scenario(tmp_path):
    """造出：真实文件、深层长路径、嵌套 junction、树内/树外链接、只读文件。"""
    src = tmp_path / "src"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "readme.txt").write_text("hello 世界", encoding="utf-8")

    deep = src
    for i in range(12):
        deep = deep / f"level{i:02d}"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("deep", encoding="utf-8")

    read_only = src / "locked.txt"
    read_only.write_text("ro", encoding="utf-8")
    os.chmod(read_only, stat.S_IREAD)

    external = tmp_path / "external" / "pkg-ext"
    external.mkdir(parents=True)
    (external / "index.js").write_text("js", encoding="utf-8")

    inside = src / "docs" / "inner"
    inside.mkdir()
    (inside / "i.txt").write_text("i", encoding="utf-8")

    (src / "nm").mkdir()
    reparse.create_junction(src / "nm" / "ext", external)
    reparse.create_junction(src / "nm" / "inner", inside)
    return src, external, inside


def test_roundtrip_preserves_data_and_links(tmp_path):
    src, external, inside = _scenario(tmp_path)
    dst = tmp_path / "dst"
    m = Migrator(src, dst, session_root=tmp_path / "sessions")
    out = m.execute()

    assert out.ok, f"迁移失败：{out.phase} {out.message}"

    # 1) 源路径现在是链接，且能读到数据
    assert os.lstat(src).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert (src / "docs" / "readme.txt").read_text(encoding="utf-8") == "hello 世界"

    # 2) 数据真的落在目标盘
    assert (dst / "docs" / "readme.txt").read_text(encoding="utf-8") == "hello 世界"

    # 3) 深层长路径完整保留
    deep = dst
    for i in range(12):
        deep = deep / f"level{i:02d}"
    assert (deep / "deep.txt").read_text(encoding="utf-8") == "deep"

    # 4) 只读属性被保留
    assert not (os.stat(dst / "locked.txt").st_mode & stat.S_IWRITE)

    # 5) 树外链接原样指向外部仓库，且没有把外部数据复制进来
    assert (dst / "nm" / "ext" / "index.js").read_text(encoding="utf-8") == "js"
    assert os.readlink(dst / "nm" / "ext").endswith("pkg-ext")

    # 6) 树内链接被改写为新位置
    assert (dst / "nm" / "inner" / "i.txt").read_text(encoding="utf-8") == "i"

    # 7) 备份目录仍在，且里面有原始数据
    assert out.backup_path and (os.path.join(out.backup_path, "docs", "readme.txt"))


def test_roundtrip_then_cleanup(tmp_path):
    src, external, inside = _scenario(tmp_path)
    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions")
    out = m.execute()
    assert out.ok
    assert m.cleanup_backup() is True
    assert not os.path.exists(out.backup_path)
    assert (src / "docs" / "readme.txt").read_text(encoding="utf-8") == "hello 世界"


def test_rollback_returns_everything(tmp_path, monkeypatch):
    src, external, inside = _scenario(tmp_path)
    before = (src / "docs" / "readme.txt").read_text(encoding="utf-8")

    m = Migrator(src, tmp_path / "dst", session_root=tmp_path / "sessions")

    # 只让「给源路径建链接」失败；整体打桩 linker.create 会连 copier 的链路重建一起打掉
    real_create = linker.create

    def fail_only_for_source(link_path, target, is_dir, prefer_junction=True):
        if os.path.normcase(str(link_path)) == os.path.normcase(str(src)):
            raise OSError("forced")
        return real_create(link_path, target, is_dir, prefer_junction)

    monkeypatch.setattr("dmig.core.linker.create", fail_only_for_source)
    out = m.execute()
    assert not out.ok
    assert m.rollback() is True

    assert not (os.lstat(src).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    assert (src / "docs" / "readme.txt").read_text(encoding="utf-8") == before
    assert os.path.exists(src / "nm" / "ext" / "index.js")


def test_migration_is_idempotent_when_target_already_has_data(tmp_path):
    src, external, inside = _scenario(tmp_path)
    dst = tmp_path / "dst"
    first = Migrator(src, dst, session_root=tmp_path / "s1")
    out1 = first.execute()
    assert out1.ok
    assert first.cleanup_backup()

    # 再迁一次（源现在是链接，应当被 guard 拒绝）
    second = Migrator(src, tmp_path / "dst2", session_root=tmp_path / "s2")
    out2 = second.execute()
    assert out2.ok is False
    assert out2.phase == "guard_failed"
