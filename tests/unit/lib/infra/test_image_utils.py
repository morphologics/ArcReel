# tests/test_image_utils.py
"""image_utils unit tests.

image_utils 单元测试。"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from lib.infra import image_utils
from lib.infra.image_utils import compress_image_bytes, normalize_storyboard_upload, normalize_uploaded_image


class TestCompressImageBytes:
    """compress_image_bytes tests.

    compress_image_bytes 测试。"""

    def _make_png(self, width: int, height: int) -> bytes:
        """Generate PNG bytes of the given size.

        生成指定尺寸的 PNG 字节。"""
        img = Image.new("RGB", (width, height), color="red")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_small_image_unchanged_dimensions(self):
        """Small images (long side < 2048) are not resized but still converted to JPEG.

        小图（长边 < 2048）不缩放，但仍转为 JPEG。"""
        raw = self._make_png(800, 600)
        result = compress_image_bytes(raw)
        img = Image.open(BytesIO(result))
        assert img.format == "JPEG"
        assert img.size == (800, 600)

    def test_large_image_resized(self):
        """Large images (long side > 2048) are resized to a long side of 2048.

        大图（长边 > 2048）缩放到长边 2048。"""
        raw = self._make_png(4096, 3072)
        result = compress_image_bytes(raw)
        img = Image.open(BytesIO(result))
        assert img.format == "JPEG"
        assert max(img.size) == 2048
        assert img.size == (2048, 1536)

    def test_portrait_large_image(self):
        """Large portrait images are also resized correctly.

        竖图大图也正确缩放。"""
        raw = self._make_png(2000, 4000)
        result = compress_image_bytes(raw)
        img = Image.open(BytesIO(result))
        assert max(img.size) == 2048
        assert img.size == (1024, 2048)

    def test_rgba_converted_to_rgb(self):
        """RGBA images are converted to RGB (JPEG does not support alpha).

        RGBA 图片转为 RGB（JPEG 不支持 alpha）。"""
        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        buf = BytesIO()
        img.save(buf, format="PNG")
        result = compress_image_bytes(buf.getvalue())
        out = Image.open(BytesIO(result))
        assert out.mode == "RGB"

    def test_jpeg_input(self):
        """JPEG input is also handled normally.

        JPEG 输入也能正常处理。"""
        img = Image.new("RGB", (500, 500), color="blue")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        result = compress_image_bytes(buf.getvalue())
        out = Image.open(BytesIO(result))
        assert out.format == "JPEG"

    def test_webp_input(self):
        """WebP input is also handled normally.

        WebP 输入也能正常处理。"""
        img = Image.new("RGB", (500, 500), color="green")
        buf = BytesIO()
        img.save(buf, format="WEBP")
        result = compress_image_bytes(buf.getvalue())
        out = Image.open(BytesIO(result))
        assert out.format == "JPEG"

    def test_invalid_input_raises(self):
        """Non-image bytes raise ValueError.

        非图片字节抛出 ValueError。"""
        with pytest.raises(ValueError, match="Invalid image"):
            compress_image_bytes(b"not an image")

    def test_output_smaller_than_input(self):
        """Size should drop significantly after compression.

        压缩后体积应显著减小。"""
        raw = self._make_png(3000, 2000)
        result = compress_image_bytes(raw)
        assert len(result) < len(raw)

    def _make_noise_png(self, width: int, height: int) -> bytes:
        img = Image.effect_noise((width, height), 80).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_default_subsampling_unchanged(self):
        """The default (sentinel -1) passes no subsampling to PIL, matching historical behaviour.

        缺省（sentinel -1）不向 PIL 传 subsampling，与历史行为一致。"""
        raw = self._make_png(800, 600)
        # Default == explicit sentinel == no subsampling passed / 缺省 == 显式 sentinel == 不传 subsampling
        assert compress_image_bytes(raw) == compress_image_bytes(raw, subsampling=-1)

    def test_subsampling_0_valid_and_takes_effect(self):
        """subsampling=0 (4:4:4) is valid and decodable; on a noise image the size is not smaller than the default 4:2:0 (proves the parameter takes effect).

        subsampling=0（4:4:4）合法可解；对噪声图体积不小于默认 4:2:0（证明参数生效）。"""
        raw = self._make_noise_png(1024, 1024)
        out_444 = compress_image_bytes(raw, subsampling=0)
        img = Image.open(BytesIO(out_444))
        assert img.format == "JPEG"
        out_default = compress_image_bytes(raw)
        assert len(out_444) >= len(out_default)

    def test_normalize_uploaded_image_still_default(self):
        """normalize_uploaded_image passes no subsampling and keeps the default (regression).

        normalize_uploaded_image 不传 subsampling，保持默认（回归）。"""
        # Noise PNG, size > 2MB threshold, triggers the compression branch
        big = self._make_noise_png(3000, 2000)  # 噪声 PNG，体积 > 2MB 阈值，触发压缩分支
        assert len(big) > 2 * 1024 * 1024
        processed, suffix = normalize_uploaded_image(big, ".png")
        assert suffix == ".jpg"
        # Byte-identical to the default compress_image_bytes (no subsampling drift introduced)
        # 与默认 compress_image_bytes 字节一致（未引入 subsampling 漂移）
        assert processed == compress_image_bytes(big)

    def test_normalize_uploaded_image_small_passthrough(self):
        """Small images under the threshold pass through unchanged, keeping the original suffix.

        小图不超阈值时原样透传，保留原 suffix。"""
        small = self._make_png(100, 100)
        processed, suffix = normalize_uploaded_image(small, ".png")
        assert processed == small
        assert suffix == ".png"


class TestUploadPixelBudget:
    """Upper limit on the total decoded pixel count: the range that neither the byte limit nor PIL's own bomb check covers.

    解码后像素总数上限：字节上限与 PIL 自带的 bomb 检查都覆盖不到的区间。"""

    def _png(self, width: int, height: int) -> bytes:
        buf = BytesIO()
        Image.new("RGB", (width, height), color="red").save(buf, format="PNG")
        return buf.getvalue()

    def test_rejects_image_over_pixel_budget(self, monkeypatch):
        """Images over the limit are rejected, and not conflated with "cannot be parsed".

        The limit is lowered with monkeypatch: an image that really hits the limit needs hundreds of millions of pixels, which is too costly to build,
        and the check reads the size declared in the file header, independent of the actual threshold.

        超出上限的图片被拒绝，且不与「无法解析」混为一谈。

        用 monkeypatch 压低上限：真正触界的图需上亿像素，构造代价过高，
        而判定读的是文件头声明的尺寸，与具体阈值无关。
        """
        monkeypatch.setattr(image_utils, "MAX_UPLOAD_PIXELS", 100 * 100 - 1)
        with pytest.raises(image_utils.ImagePixelLimitError):
            normalize_storyboard_upload(self._png(100, 100), max_long_edge=None)

    def test_accepts_image_at_budget(self, monkeypatch):
        """An image exactly at the limit is let through (the boundary does not block one step early).

        恰好等于上限的图片放行（边界不多拦一格）。"""
        monkeypatch.setattr(image_utils, "MAX_UPLOAD_PIXELS", 100 * 100)
        assert normalize_storyboard_upload(self._png(100, 100), max_long_edge=None)

    def test_pixel_limit_is_a_value_error(self):
        """Inherits ValueError: callers that do not handle this type separately still treat it as an invalid image.

        继承 ValueError：未单独处理该类型的调用方仍收口为无效图片。"""
        assert issubclass(image_utils.ImagePixelLimitError, ValueError)

    def test_grid_sized_composite_passes_default_budget(self):
        """A 4K square grid composite is within the default limit and is not blocked by mistake.

        4K 见方的联合图在默认上限内，不被误拦。"""
        assert image_utils.MAX_UPLOAD_PIXELS > 4096 * 4096
