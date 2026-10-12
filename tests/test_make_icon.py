import struct
import sys
import zipfile

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]
                       / "packaging"))

import make_icon


def test_build_ico_header_and_first_entry():
    data = make_icon.build_ico({16: b"a" * 10, 32: b"b" * 20})
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 2)

    width, height, colors, pad, planes, bits, length, offset = struct.unpack(
        "<BBBBHHII", data[6:22])
    assert (width, height, colors, pad) == (16, 16, 0, 0)
    assert (planes, bits) == (1, 32)
    assert length == 10
    assert offset == 6 + 16 * 2
    assert data[offset:offset + 10] == b"a" * 10


def test_build_ico_encodes_256_as_zero():
    data = make_icon.build_ico({256: b"x"})
    assert data[6] == 0 and data[7] == 0


def test_build_ico_payload_order_is_sorted():
    data = make_icon.build_ico({256: b"big", 16: b"small"})
    first_offset = struct.unpack("<I", data[6 + 12:6 + 16])[0]
    assert data[first_offset:first_offset + 5] == b"small"


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_render_png_returns_png_bytes(qapp):
    blob = make_icon.render_png(32)
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    # PNG 头部里的宽高必须是请求的尺寸
    width, height = struct.unpack(">II", blob[16:24])
    assert (width, height) == (32, 32)


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_main_writes_ico_with_all_sizes(tmp_path, qapp):
    """图标档位来自 `dmig.ui.logo`——那是品牌几何的唯一事实来源。

    64 与 128 是本次补上的：原来只有 16/32/48/256，资源管理器「中等图标」
    是 64px，系统只能把 48 硬拉大，看着发虚。
    """
    from dmig.ui import logo

    target = tmp_path / "app.ico"
    assert make_icon.main(target) == 0
    data = target.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"
    count = struct.unpack("<H", data[4:6])[0]
    assert count == len(logo.SIZES)
    assert count == 6
    assert {64, 128} <= set(logo.SIZES)
    assert set(logo.SIZES) == {16, 32, 48, 64, 128, 256}


# --- make_zip：分发单位是整个文件夹 ---------------------------------------

import make_zip  # noqa: E402


def test_zip_keeps_the_top_level_folder(tmp_path):
    """解压出来必须是一个文件夹，而不是把几百个文件摊在下载目录里。"""
    app = tmp_path / "dist" / "Windows数据迁移助手"
    (app / "_internal").mkdir(parents=True)
    (app / "Windows数据迁移助手.exe").write_bytes(b"MZ" + b"\0" * 100)
    (app / "_internal" / "python313.dll").write_bytes(b"\0" * 500)
    out = tmp_path / "Windows数据迁移助手-1.0.0-win64.zip"

    assert make_zip.main(app, out) == 0

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert names == {"Windows数据迁移助手/Windows数据迁移助手.exe", "Windows数据迁移助手/_internal/python313.dll"}


def test_zip_reports_missing_directory_instead_of_crashing(tmp_path, capsys):
    assert make_zip.main(tmp_path / "nope", tmp_path / "x.zip") == 1
    assert "找不到目录" in capsys.readouterr().err


def test_zip_roundtrips_file_content(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    payload = bytes(range(256)) * 40
    (app / "blob.bin").write_bytes(payload)
    out = tmp_path / "out.zip"

    make_zip.main(app, out)

    with zipfile.ZipFile(out) as zf:
        assert zf.read("app/blob.bin") == payload


