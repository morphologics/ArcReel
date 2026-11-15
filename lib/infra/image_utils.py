"""
Image utility helpers.

Used by WebUI upload endpoints to validate, compress, and normalize uploaded images.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

_COMPRESS_THRESHOLD = 2 * 1024 * 1024  # 2 MB
_MAX_LONG_EDGE = 2048
# Limit on the decoded pixel count (8192×8192). The byte limit can't stop this layer: a low-entropy PNG can pack
# hundreds of millions of pixels into under 30MB; PIL's own MAX_IMAGE_PIXELS only raises DecompressionBombError above
# twice its value, and between one and two times (about 89 to 178 million pixels) it only warns and lets it through,
# while a single RGBA buffer is hundreds of MB. The top grid composite option is 4K square (about 17 million pixels);
# with several times headroom above that, larger images are explicitly rejected here.
# 解码后的像素总数上限（8192×8192）。字节上限拦不住这一层：低熵 PNG 能把上亿像素压进
# 30MB 以内；PIL 自带的 MAX_IMAGE_PIXELS 也只在超过其两倍时才抛 DecompressionBombError，
# 介于一倍与两倍之间（约 8900 万至 1.78 亿像素）仅告警放行，而一份 RGBA 缓冲即数百 MB。
# 宫格联合图最高档位为 4K 见方（约 1700 万像素），此处留出数倍余量后显式拒绝。
MAX_UPLOAD_PIXELS = 8192 * 8192
_JPEG_QUALITY = 85
# sentinel: means "don't pass subsampling to PIL". PIL's subsampling=-1 means "keep source chroma" only for
# JPEG->JPEG, and is invalid when re-encoding decoded PNG/other sources, so this sentinel blocks it by default, keeping
# the default behavior unchanged.
# sentinel：意为「不向 PIL 传 subsampling」。PIL 的 subsampling=-1 仅在 JPEG→JPEG 时表示
# “保持源色度”，对 PNG/其它源解码后再编码不合法，故默认用本 sentinel 拦掉，保证缺省行为不变。
_SUBSAMPLING_KEEP = -1

# EXIF Orientation tag (the field ImageOps.exif_transpose reads) / EXIF Orientation tag（ImageOps.exif_transpose 读取的字段）
_EXIF_ORIENTATION = 0x0112


class ImagePixelLimitError(ValueError):
    """The decoded pixel count of the image exceeds ``MAX_UPLOAD_PIXELS``.

    Subclasses ValueError: when callers don't handle it separately, it ends as a 400 like other invalid images.

    图片解码后的像素总数超过 ``MAX_UPLOAD_PIXELS``。

    继承 ValueError：调用方不单独处理时，与其它无效图片一样收口为 400。
    """


def _ensure_pixel_budget(size: tuple[int, int]) -> None:
    """Stop oversized images before decoding allocates the pixel buffer. / 在解码分配像素缓冲之前拦掉超大图。"""
    if size[0] * size[1] > MAX_UPLOAD_PIXELS:
        raise ImagePixelLimitError(f"image has {size[0]}x{size[1]} pixels, over the {MAX_UPLOAD_PIXELS} limit")


def _open_oriented(content: bytes, *, target_modes: tuple[str, ...], fallback_mode: str) -> Image.Image:
    """Decode + EXIF orientation correction + convert to the target color mode. Returns an image object decoupled from the source decoder.

    解码 + EXIF 方向矫正 + 收敛到目标颜色模式。返回与源解码器解耦的图像对象。"""
    with Image.open(BytesIO(content)) as src:
        img = ImageOps.exif_transpose(src)
        if img.mode not in target_modes:
            img = img.convert(fallback_mode)
        return img.copy() if img is src else img


def _fit_long_edge(img: Image.Image, max_long_edge: int) -> Image.Image:
    """Scale proportionally when the long edge exceeds the limit; at extreme aspect ratios the short edge is clamped to at least 1 pixel to avoid a resize(…, 0) error.

    长边超限时等比缩放；极端宽高比下短边钳到至少 1 像素，避免 resize(…, 0) 报错。"""
    w, h = img.size
    long_edge = max(w, h)
    if long_edge <= max_long_edge:
        return img
    scale = max_long_edge / long_edge
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)


def convert_image_bytes_to_png(content: bytes) -> bytes:
    """
    Convert arbitrary image bytes (jpg/png/webp/...) into PNG bytes.

    Raises:
        ValueError: if the input bytes are not a valid image.
    """
    try:
        with _open_oriented(content, target_modes=("RGB", "RGBA"), fallback_mode="RGBA") as img:
            out = BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
    except Exception as e:
        raise ValueError("Invalid image") from e


def normalize_storyboard_upload(content: bytes, *, max_long_edge: int | None = _MAX_LONG_EDGE) -> bytes:
    """
    Normalize an uploaded storyboard image to PNG bytes: fix orientation via exif, scale proportionally when the long edge
    exceeds the limit.

    The canonical storyboard image path is always .png (resource_paths / VersionManager / restore all work with this
    extension), so any input format is converted to PNG. A PNG that already complies (mode/orientation/size all OK) is
    returned as is, with no pointless decode and re-encode.

    ``max_long_edge=None`` means no scaling, only format/orientation normalization -- uploads where resolution is
    information, such as grid composites, use this, so a 4K grid composite isn't silently downsampled and distorted at
    grid split.

    Raises:
        ImagePixelLimitError: if the decoded pixel count exceeds MAX_UPLOAD_PIXELS.
        ValueError: if the input bytes are not a valid image.

    将上传的分镜图归一化为 PNG 字节：exif 矫正方向、长边超限时等比缩放。

    分镜图 canonical 路径固定为 .png（resource_paths / VersionManager / restore
    均按此扩展名工作），因此无论输入格式一律转 PNG。
    已合规的 PNG（模式/方向/尺寸均达标）原样返回，不做无谓的解码重编码。

    ``max_long_edge=None`` 表示不缩放，只做格式/方向归一化——宫格联合图等
    分辨率即信息量的上传走此档，避免 4K 联合图被静默降采样后切格失真。

    Raises:
        ImagePixelLimitError: if the decoded pixel count exceeds MAX_UPLOAD_PIXELS.
        ValueError: if the input bytes are not a valid image.
    """
    try:
        with Image.open(BytesIO(content)) as probe:
            # size comes from the file header; no pixel buffer is allocated yet / 尺寸来自文件头，此时尚未分配像素缓冲
            _ensure_pixel_budget(probe.size)
            if (
                probe.format == "PNG"
                and probe.mode in ("RGB", "RGBA")
                and (max_long_edge is None or max(probe.size) <= max_long_edge)
                and probe.getexif().get(_EXIF_ORIENTATION, 1) == 1
            ):
                return content

        with _open_oriented(content, target_modes=("RGB", "RGBA"), fallback_mode="RGBA") as img:
            if max_long_edge is not None:
                img = _fit_long_edge(img, max_long_edge)
            out = BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
    except ImagePixelLimitError:
        # the image itself parses, it is just too large -- keep this type so the caller can give an actionable hint
        # 图片本身可解析，只是过大——保留该类型，让调用方给出可操作的提示
        raise
    except Exception as e:
        raise ValueError("Invalid image") from e


def validate_image_bytes(content: bytes) -> None:
    """Validate that *content* is a decodable image.

    Raises:
        ValueError: if the input bytes are not a valid image.
    """
    try:
        with Image.open(BytesIO(content)) as img:
            img.verify()
    except Exception as e:
        raise ValueError("Invalid image") from e


def compress_image_bytes(
    content: bytes,
    *,
    max_long_edge: int = _MAX_LONG_EDGE,
    quality: int = _JPEG_QUALITY,
    subsampling: int = _SUBSAMPLING_KEEP,
) -> bytes:
    """
    Compress any image bytes to JPEG: scale proportionally so the long edge is at most max_long_edge; quality controls
    JPEG compression quality.

    subsampling controls JPEG chroma subsampling (0=4:4:4 visually lossless, 2=4:2:0 default); it defaults to
    _SUBSAMPLING_KEEP, in which case the parameter is not passed to PIL at all, keeping PIL's default (same as historical
    behavior).

    Raises:
        ValueError: if the input bytes are not a valid image.

    将任意图片字节压缩为 JPEG：等比缩放到长边不超过 max_long_edge，
    quality 控制 JPEG 压缩质量。

    subsampling 控制 JPEG 色度抽样（0=4:4:4 视觉无损，2=4:2:0 默认）；缺省为
    _SUBSAMPLING_KEEP，此时完全不向 PIL 传该参数，保持 PIL 默认（与历史行为一致）。

    Raises:
        ValueError: if the input bytes are not a valid image.
    """
    try:
        with _open_oriented(content, target_modes=("RGB",), fallback_mode="RGB") as img:
            img = _fit_long_edge(img, max_long_edge)
            extra: dict[str, int] = {"subsampling": subsampling} if subsampling >= 0 else {}
            out = BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True, **extra)
            return out.getvalue()
    except Exception as e:
        raise ValueError("Invalid image") from e


def normalize_uploaded_image(
    content: bytes,
    original_suffix: str,
    *,
    compress_threshold: int = _COMPRESS_THRESHOLD,
) -> tuple[bytes, str]:
    """Validate (and optionally compress) an uploaded image.

    If *content* exceeds *compress_threshold* bytes the image is compressed to
    JPEG and ``".jpg"`` is returned as the suffix.  Otherwise the original
    bytes are returned after validation, together with *original_suffix* (or
    ``".png"`` when empty).

    Returns:
        ``(processed_content, final_suffix)``

    Raises:
        ValueError: if the input bytes are not a valid image.
    """
    if len(content) > compress_threshold:
        return compress_image_bytes(content), ".jpg"
    validate_image_bytes(content)
    return content, original_suffix or ".png"
