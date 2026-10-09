import struct
import sys

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
    target = tmp_path / "app.ico"
    assert make_icon.main(target) == 0
    data = target.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"
    count = struct.unpack("<H", data[4:6])[0]
    assert count == len(make_icon.SIZES)
