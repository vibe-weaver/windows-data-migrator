import ctypes
import os
import stat
import subprocess
import sys

import pytest

from dmig.core import reparse

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _is_reparse(path) -> bool:
    return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def test_build_mount_point_buffer_layout():
    """锁死三个易错点：无 Flags 字段、两个名字带 NUL、PrintNameOffset 跨终止符。"""
    buf = reparse.build_mount_point("\\??\\E:\\b", "E:\\b")
    tag, dlen, reserved = (int.from_bytes(buf[0:4], "little"),
                           int.from_bytes(buf[4:6], "little"),
                           int.from_bytes(buf[6:8], "little"))
    assert tag == 0xA0000003
    assert reserved == 0
    sub_off = int.from_bytes(buf[8:10], "little")
    sub_len = int.from_bytes(buf[10:12], "little")
    prt_off = int.from_bytes(buf[12:14], "little")
    prt_len = int.from_bytes(buf[14:16], "little")
    assert sub_off == 0
    assert sub_len == len("\\??\\E:\\b".encode("utf-16-le"))
    assert prt_off == sub_len + 2
    assert prt_len == len("E:\\b".encode("utf-16-le"))
    # 路径区 = Substitute + NUL + PrintName + NUL，两个终止符都必须计入数据区长度
    expected_pathbuf = (len("\\??\\E:\\b".encode("utf-16-le")) + 2
                        + len("E:\\b".encode("utf-16-le")) + 2)
    assert dlen == 8 + expected_pathbuf
    assert len(buf) == 8 + dlen


def test_created_junction_is_byte_identical_to_mklink(tmp_path):
    store = tmp_path / "store" / "pkg-a"
    store.mkdir(parents=True)
    (store / "index.js").write_text("payload\n", encoding="utf-8")

    mine = tmp_path / "nm" / "pkg-a"
    mine.parent.mkdir(parents=True)
    reparse.create_junction(mine, store)

    ref = tmp_path / "nm_ref" / "pkg-a"
    ref.parent.mkdir(parents=True)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(ref), str(store)],
                       capture_output=True, text=True, encoding="mbcs")
    if r.returncode != 0:
        pytest.skip(f"mklink 不可用: {r.stdout}{r.stderr}")

    assert reparse.read_raw(mine) == reparse.read_raw(ref)


def test_junction_is_readable_and_removable(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "a.txt").write_text("hello", encoding="utf-8")
    link = tmp_path / "link"
    reparse.create_junction(link, store)

    assert _is_reparse(link)
    assert (link / "a.txt").read_text(encoding="utf-8") == "hello"
    assert "\\\\?\\" in os.readlink(link)

    os.rmdir(link)
    assert not os.path.exists(link)
    assert (store / "a.txt").exists()          # 删链接不能删数据


def test_create_symlink_reports_privilege_error(tmp_path):
    """未提权且开发者模式关闭时应抛出 WinError 1314；已提权则跳过。"""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "sym"
    try:
        reparse.create_symlink(link, target, is_dir=True)
    except OSError as exc:
        assert exc.winerror in (1314,), f"预期 1314，实际 {exc.winerror}"
        return
    assert _is_reparse(link)
    os.rmdir(link)
    pytest.skip("当前环境有符号链接权限")


def test_copy_reparse_data_is_byte_identical(tmp_path, make_reparse_file,
                                             raw_reparse_buffer):
    """读不动的重解析点要能原样搬走（AppExecLink 连 open 都被拒）。"""
    src = make_reparse_file(tmp_path / "src.exe")
    dst = tmp_path / "dst.exe"
    reparse.copy_reparse_data(src, dst)
    assert reparse.read_raw(dst) == raw_reparse_buffer(0x8000001B)


def test_copy_reparse_data_handles_dir_shape(tmp_path, make_reparse_file):
    src = make_reparse_file(tmp_path / "srcdir", is_dir=True)
    dst = tmp_path / "dstdir"
    reparse.copy_reparse_data(src, dst)
    assert _is_reparse(dst)
    assert reparse.read_raw(dst) == reparse.read_raw(src)


def test_copy_reparse_data_can_rerun_onto_the_same_destination(
        tmp_path, make_reparse_file):
    """续传的第二趟会把已经搬过的条目再走一遍，不能因此失败。

    目标端那时已经带着同一个未处理标签：open() 打不开它（Invalid argument），
    FSCTL_SET_REPARSE_POINT 也不接受覆盖，实测整趟会栽在一个早已复制好的文件上。
    """
    for name, is_dir in (("app.exe", False), ("wci", True)):
        src = make_reparse_file(tmp_path / f"src-{name}", is_dir=is_dir)
        dst = tmp_path / f"dst-{name}"
        reparse.copy_reparse_data(src, dst)
        reparse.copy_reparse_data(src, dst)
        assert _is_reparse(dst)
        assert reparse.read_raw(dst) == reparse.read_raw(src)


def test_delete_reparse_point_restores_plain_file(tmp_path, make_reparse_file):
    path = make_reparse_file(tmp_path / "app.exe")
    assert _is_reparse(path)
    reparse.delete_reparse_point(path)
    assert not _is_reparse(path)
    # 摘掉标签后就是普通文件，能正常读写与删除
    with open(path, "wb") as handle:
        handle.write(b"plain")
    os.unlink(path)


def test_delete_reparse_point_on_plain_file_is_noop(tmp_path):
    path = tmp_path / "plain.txt"
    path.write_text("x", encoding="utf-8")
    reparse.delete_reparse_point(path)
    assert path.read_text(encoding="utf-8") == "x"
