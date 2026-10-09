import os
import sys
from pathlib import Path

import pytest

from dmig.core import verifier

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _pair(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    for base in (src, dst):
        (base / "a").mkdir(parents=True)
        (base / "a" / "one.txt").write_text("one", encoding="utf-8")
        (base / "two.bin").write_bytes(b"2" * 500)
    return src, dst


def test_identical_trees_pass(tmp_path):
    src, dst = _pair(tmp_path)
    res = verifier.verify(src, dst)
    assert res.ok is True
    assert res.expected_files == res.actual_files == 2
    assert res.expected_bytes == res.actual_bytes == 503


def test_missing_file_is_reported(tmp_path):
    src, dst = _pair(tmp_path)
    (dst / "two.bin").unlink()
    res = verifier.verify(src, dst)
    assert res.ok is False
    assert "two.bin" in res.missing


def test_size_mismatch_is_reported(tmp_path):
    src, dst = _pair(tmp_path)
    (dst / "two.bin").write_bytes(b"2" * 100)
    res = verifier.verify(src, dst)
    assert res.ok is False
    assert "two.bin" in res.mismatch


def test_deep_hash_detects_content_difference(tmp_path):
    src, dst = _pair(tmp_path)
    (dst / "two.bin").write_bytes(b"9" * 500)   # 同大小不同内容
    assert verifier.verify(src, dst).ok is True
    deep = verifier.verify(src, dst, deep=True)
    assert deep.ok is False
    assert "two.bin" in deep.mismatch


def test_sha256_of_matches_hashlib(tmp_path):
    import hashlib
    f = tmp_path / "x.bin"
    payload = os.urandom(4096)
    f.write_bytes(payload)
    assert verifier.sha256_of(f) == hashlib.sha256(payload).hexdigest()


def test_unreadable_subdirectory_is_reported_not_raised(tmp_path, monkeypatch):
    """ACL 拒绝的子目录必须被记录为校验不通过，而不是让整趟校验抛异常。"""
    src, dst = _pair(tmp_path)
    locked = src / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("s", encoding="utf-8")

    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        if os.path.basename(str(path)) == "locked":
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    res = verifier.verify(src, dst)          # 不得抛异常
    assert res.ok is False
    assert "locked" in res.mismatch
