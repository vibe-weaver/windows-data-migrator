"""品牌标记：几何、档位与分尺寸画法。

这个模块是 exe 图标与界面品牌栏的**唯一事实来源**，两处走样会立刻显形，
所以几何本身也值得钉几条测试。
"""
import pytest
from PySide6.QtWidgets import QApplication

from dmig.ui import chrome, logo


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_brand_copy_is_defined():
    assert logo.NAME == "Windows数据迁移助手"
    assert logo.TAGLINE


def test_sizes_cover_the_ones_windows_asks_for(qapp):
    """64 与 128 是必需的。

    资源管理器「中等图标」是 64px，缺了这一档系统只能把 48 硬拉大，看着发虚。
    """
    assert set(logo.SIZES) == {16, 32, 48, 64, 128, 256}


def test_the_asset_is_the_single_source_of_the_mark(qapp):
    """界面与 exe 图标必须取同一张主图。

    原来这里是「代码画几何」，两处（品牌栏与 make_icon）各画一遍就会走样；
    换成主图之后，走样的风险变成「有一处忘了走主图」——所以钉住这条。
    """
    assert logo.asset_path(), "找不到 assets/logo.png"
    assert not logo.master_image().isNull()


def test_every_size_scales_from_the_one_master(qapp, monkeypatch):
    """每个档位都是从同一张主图缩出来的，没有第二套画法。"""
    drawn = []
    original = logo.draw_mark

    def spy(painter, size):
        drawn.append(size)
        original(painter, size)

    monkeypatch.setattr(logo, "draw_mark", spy)
    for size in (16, 32, 48, 64, 128, 256):
        logo.mark_pixmap(size)

    assert drawn == [16, 32, 48, 64, 128, 256]


def test_small_sizes_use_smooth_scaling(qapp):
    """缩到小尺寸必须用平滑插值。

    1024 的图缩到 16 靠默认的最近邻会碎成格子——图标越小越明显。
    """
    import inspect

    source = inspect.getsource(logo.mark_pixmap)
    assert "SmoothPixmapTransform" in source


def test_transparent_pixels_are_not_white(qapp):
    """透明区域的 RGB 不能是白的。

    缩放按非预乘 alpha 插值，白色会混进边缘像素，深色背景下就是一圈毛边。
    这条盯住的是主图本身——它是在图像处理阶段修好的，很容易被重新导出的
    图覆盖回去。
    """
    image = logo.master_image()
    size = image.width()
    for x, y in ((0, 0), (size - 1, 0), (0, size - 1), (size // 2, 2)):
        color = image.pixelColor(x, y)
        if color.alpha() < 8:
            assert not (color.red() > 200 and color.green() > 200
                        and color.blue() > 200), \
                f"({x},{y}) 透明但 RGB 是白的 {color.getRgb()}"



def test_every_size_renders_something(qapp):
    """每个档位都要画出东西；空白图标会静默地什么也不显示。"""
    for size in logo.SIZES:
        image = logo.mark_pixmap(size).toImage()
        assert image.width() == size and image.height() == size
        opaque = sum(1 for y in range(size) for x in range(size)
                     if image.pixelColor(x, y).alpha() > 128)
        assert opaque > size * size * 0.5, f"{size}px 几乎没画出内容"


def test_mark_icon_carries_every_size(qapp):
    icon = logo.mark_icon()
    assert not icon.isNull()
    for size in logo.SIZES:
        assert not icon.pixmap(size, size).isNull(), f"{size}px 档位缺失"


def test_app_icon_falls_back_to_the_drawn_mark(qapp, monkeypatch):
    """拿不到 app.ico 时要现画，而不是返回空图标（品牌栏会因此空白）。"""
    monkeypatch.setattr(logo.os.path, "exists", lambda _p: False)
    assert not logo.app_icon().isNull()


def test_brand_bar_uses_the_brand_name(qapp):
    """品牌栏的名字来自 logo，避免界面与图标各叫一个名字。"""
    assert chrome.APP_NAME == logo.NAME
    assert chrome.APP_SUBTITLE == logo.TAGLINE
