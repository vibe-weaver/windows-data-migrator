import json
import os
import sys

import pytest

from dmig import cli

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


def test_migrate_dry_run_does_not_touch_sources(tmp_path, capsys):
    src = _tree(tmp_path)
    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst"),
                   "--dry-run"])
    assert rc == 0
    assert (src / "two.bin").exists()
    assert not (tmp_path / "dst").exists()


def test_migrate_requires_yes_without_tty(tmp_path):
    src = _tree(tmp_path)
    rc = cli.main(["migrate", "--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 2


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
