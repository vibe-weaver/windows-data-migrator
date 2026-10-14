import os
import sys
from pathlib import Path

import pytest

from dmig.core import fsutil, guard

ALLOWANCE = 512 * 1024 * 1024
MARGIN = 1.05


def _codes(issues):
    return {i.code for i in issues}


def test_source_must_exist(tmp_path):
    issues = guard.check(tmp_path / "nope", tmp_path / "dst")
    assert "source_missing" in _codes(issues)
    assert guard.has_error(issues)


def test_source_must_be_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    assert "source_not_dir" in _codes(guard.check(f, tmp_path / "dst"))


def test_source_must_not_equal_target(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    assert "same_path" in _codes(guard.check(src, src))


def test_target_must_not_be_inside_source(tmp_path):
    src = tmp_path / "a"
    (src / "inner").mkdir(parents=True)
    assert "target_inside_source" in _codes(guard.check(src, src / "inner"))


def test_source_must_not_be_inside_target(tmp_path):
    dst = tmp_path / "b"
    (dst / "inner").mkdir(parents=True)
    assert "source_inside_target" in _codes(guard.check(dst / "inner", dst))


def test_source_must_not_be_drive_root():
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    drive = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep
    assert "source_is_root" in _codes(guard.check(drive, r"E:\anywhere"))


# --- 系统目录：不许当源，且**连试探都不许做** -------------------------
#
# 实测来源：用户选了 C:\Program Files (x86) 当源，点「开始检验」后界面冻死。
# 根因是 probe_rename 会真的把这个系统目录改名再改回；在提权进程里，
# WOW64 重定向 / AppCompat / 杀软过滤驱动会同时重新评估整棵子树。
# 这里直接测那个纯函数（不碰真实文件系统），并逐条钉住「只拦本身、不拦子目录」。

@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
@pytest.mark.parametrize("path", [
    r"C:\Windows",
    r"C:\Windows\System32",              # SystemRoot 子树里的任何一层
    r"C:\Windows\System32\drivers",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\Users",
    r"C:\Users\Someone",                 # 用户配置文件根
    r"D:\Program Files",                 # 别的卷上同样拦
])
def test_system_directories_are_refused_as_source(path):
    issue = guard._protected_source(Path(path))
    assert issue is not None, f"{path} 应当被拦下"
    assert issue.severity == "error"
    assert issue.code in ("source_is_system_dir", "source_in_windows",
                          "source_is_profile")


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
@pytest.mark.parametrize("path", [
    r"C:\Program Files (x86)\SomeGame",          # 装在系统目录下的具体程序
    r"C:\Program Files\MyApp\data",
    r"C:\Users\Someone\AppData\Local\SomeCache",  # 本工具最正当的用法之一
    r"C:\Users\Someone\Documents\stuff",
    r"C:\ProgramData\SomeApp\cache",
    r"E:\data\mine",
])
def test_subdirectories_of_system_dirs_are_allowed(path):
    """只拦「系统目录本身」——它们的子目录正是这个工具要服务的对象。"""
    assert guard._protected_source(Path(path)) is None, f"{path} 不该被拦"


def test_protected_source_short_circuits_before_any_probe(tmp_path, monkeypatch):
    """命中系统目录时 check() 立刻返回，调用方据此跳过试探改名。

    这是那条安全性质的**契约来源**：SelectCheckWorker 只在
    `not has_error(issues)` 时才调 probe_rename（见 test_workers.py 里的同名用例）。
    """
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    monkeypatch.setattr(guard, "volume_root", lambda p: "C:\\")
    issues = guard.check(r"C:\Program Files (x86)", str(tmp_path / "dst"))
    assert guard.has_error(issues)
    assert "source_is_system_dir" in {i.code for i in issues}


def test_fails_when_target_is_inside_its_own_drive_subpath(tmp_path):
    """目标在源子树的兄弟目录里是允许的，不应报错。"""
    src = tmp_path / "a"
    src.mkdir()
    dst = tmp_path / "b"
    issues = guard.check(src, dst)
    assert not guard.has_error(issues)


def test_not_enough_space_is_error(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    (src / "f.bin").write_text("data", encoding="utf-8")
    huge = guard.volume_free_bytes(str(tmp_path)) * 10
    assert "insufficient_space" in _codes(guard.check(src, tmp_path / "b", required_bytes=huge))


def test_enough_space_passes(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    assert not guard.has_error(guard.check(src, tmp_path / "b", required_bytes=1024))


def test_space_check_works_when_target_chain_missing(tmp_path):
    r"""目标是全新的嵌套路径时不能误判空间不足。

    目标整条链都不存在时，若 disk_usage 直接抛错被吞成 0，就会报
    「目标盘剩余 0 MB」并把迁移拒掉——这是真实场景里最常见的目标形态。
    """
    src = tmp_path / "a"
    src.mkdir()
    (src / "f.bin").write_text("data", encoding="utf-8")
    deep_target = tmp_path / "no" / "such" / "nested" / "target"
    assert not guard.has_error(guard.check(src, deep_target, required_bytes=1024))


def test_non_empty_target_is_warning_only(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    dst = tmp_path / "b"
    dst.mkdir()
    (dst / "existing.txt").write_text("x", encoding="utf-8")
    issues = guard.check(src, dst)
    assert "target_not_empty" in _codes(issues)
    assert not guard.has_error(issues)
    assert next(i for i in issues if i.code == "target_not_empty").severity == "warning"


def test_source_being_reparse_point_is_error(tmp_path):
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(str(real), str(link), target_is_directory=True)
    except OSError:
        pytest.skip("无符号链接权限")
    assert "source_is_reparse" in _codes(guard.check(link, tmp_path / "dst"))


# --- 卷层面：同卷 / 文件系统 / 盘类型（C3） -------------------------

def _stub_volumes(monkeypatch, *, src_root="C:\\", dst_root="D:\\",
                  src_reparse=True, dst_reparse=True,
                  src_fs="NTFS", dst_fs="NTFS",
                  src_kind=fsutil.DRIVE_FIXED, dst_kind=fsutil.DRIVE_FIXED):
    r"""把 guard 眼里的卷信息换成假数据；被测函数只认 "SRC" / "DST" 两个哨兵。

    真实机器上凑不出 exFAT 卷、可移动盘和网络盘，而这三类恰恰是前置校验
    最容易漏报的：迁移会一路跑到几小时后的建链阶段才失败。
    """
    roots = {"SRC": src_root, "DST": dst_root}
    reparse = {"SRC": src_reparse, "DST": dst_reparse}
    filesystems = {"SRC": src_fs, "DST": dst_fs}
    infos = {src_root: (src_kind, src_fs), dst_root: (dst_kind, dst_fs)}
    monkeypatch.setattr(guard, "volume_root", lambda p: roots[p])
    monkeypatch.setattr(guard, "supports_reparse_points", lambda p: reparse[p])
    monkeypatch.setattr(guard, "volume_filesystem", lambda p: filesystems[p])
    monkeypatch.setattr(guard, "volume_info", lambda root: infos[root])


def test_same_volume_is_a_warning_not_a_block(tmp_path):
    """搬到同一个盘根本不释放空间，备份还与原件同盘；要说清楚，但不能拦死。"""
    src = tmp_path / "a"
    src.mkdir()
    issues = guard.check(src, tmp_path / "b")
    assert "same_volume" in _codes(issues)
    assert not guard.has_error(issues)
    assert next(i for i in issues if i.code == "same_volume").severity == "warning"


def test_volume_checks_stay_quiet_for_two_fixed_ntfs_volumes(monkeypatch):
    _stub_volumes(monkeypatch)
    assert guard._volume_checks("SRC", "DST", 0) == []
    assert guard._volume_checks("SRC", "DST", 500) == []


def test_same_volume_warning_mentions_the_root(monkeypatch):
    _stub_volumes(monkeypatch, src_root="E:\\", dst_root="e:\\")
    issues = guard._volume_checks("SRC", "DST", 0)
    assert _codes(issues) == {"same_volume"}
    assert "E:\\" in issues[0].message


def test_source_volume_without_reparse_support_blocks(monkeypatch):
    """源卷存不下 junction：数据搬走了却留不下入口，比复制失败难收拾得多。"""
    _stub_volumes(monkeypatch, src_reparse=False, src_fs="exFAT")
    issues = guard._volume_checks("SRC", "DST", 0)
    assert _codes(issues) == {"source_no_reparse"}
    assert guard.has_error(issues)
    assert "exFAT" in issues[0].message


def test_target_volume_blocks_only_when_links_must_be_rebuilt(monkeypatch):
    """扫描前不知道树内有没有链接，那时只能提示；扫描出链接后就是致命错误。"""
    _stub_volumes(monkeypatch, dst_reparse=False, dst_fs="FAT32")
    soft = guard._volume_checks("SRC", "DST", 0)
    assert "target_no_reparse" in _codes(soft)
    assert not guard.has_error(soft)

    hard = guard._volume_checks("SRC", "DST", 12)
    assert guard.has_error(hard)
    issue = next(i for i in hard if i.code == "target_no_reparse")
    assert "12 条链接" in issue.message
    assert "FAT32" in issue.message


@pytest.mark.parametrize("kind,text", [
    (fsutil.DRIVE_REMOVABLE, "可移动盘"),
    (fsutil.DRIVE_REMOTE, "网络位置"),
    (fsutil.DRIVE_CDROM, "光驱"),
    (fsutil.DRIVE_RAMDISK, "内存盘"),
])
def test_unstable_target_volume_is_a_warning(monkeypatch, kind, text):
    _stub_volumes(monkeypatch, dst_kind=kind)
    issues = guard._volume_checks("SRC", "DST", 0)
    assert not guard.has_error(issues)
    issue = next(i for i in issues if i.code == "target_volume_kind")
    assert text in issue.message


def test_unstable_source_volume_is_a_warning(monkeypatch):
    _stub_volumes(monkeypatch, src_kind=fsutil.DRIVE_REMOTE)
    issues = guard._volume_checks("SRC", "DST", 0)
    assert "source_volume_kind" in _codes(issues)
    assert not guard.has_error(issues)


def test_check_passes_link_count_to_the_volume_checks(monkeypatch, tmp_path):
    """link_count 是判定目标卷致命与否的唯一依据，断在这里等于永远只提示。"""
    seen: list[int] = []
    real = guard._volume_checks

    def spy(src, dst, link_count):
        seen.append(link_count)
        return real(src, dst, link_count)

    monkeypatch.setattr(guard, "_volume_checks", spy)
    src = tmp_path / "a"
    src.mkdir()
    guard.check(src, tmp_path / "b")
    guard.check(src, tmp_path / "b", link_count=7)
    assert seen == [0, 7]
