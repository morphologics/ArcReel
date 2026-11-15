"""Base64 data URI encoding on the outbound request side.

Providers' image/video endpoints commonly accept inline assets of the form `data:<mime>;base64,<content>`; using a data
URI saves a layer of file serving. The encoding itself is the same for everyone; the only difference is "which
extensions map to which MIME" -- the table is held and passed in by each provider module; this module holds no
provider-specific rules.

Division of labour with `lib/infra/image_utils.py` / `lib/speech/audio_utils.py`: those two guard the inbound upload
side (size compression, duration and format validation); this module only encodes assets already on disk into fields
of the outbound request body.

出站请求侧的 base64 data URI 编码。

各供应商的图像/视频接口普遍接受 `data:<mime>;base64,<内容>` 形态的内联素材，走 data URI
可以免掉一层文件服务。编码动作本身各家一致，差异只在「哪些扩展名映射到哪个 MIME」——
表由各供应商模块自己持有并传入，本模块不持有任何供应商口径。

与 `lib/infra/image_utils.py` / `lib/speech/audio_utils.py` 的分工：那两个模块守的是入站上传侧
（尺寸压缩、时长与格式校验），本模块只负责把已落盘的素材编成出站请求体里的字段。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path

_DEFAULT_IMAGE_MIME = "image/png"


def _image_mime_from_bytes(content: bytes) -> str | None:
    """Identify the MIME from the file header of common image formats; when unrecognized, defer to the extension compatibility fallback.

    从常见图片格式的文件头识别 MIME，无法识别时交由扩展名兼容回退。"""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        brand = content[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx"}:
            return "image/heic"
        if brand in {b"mif1", b"msf1"}:
            return "image/heif"
    return None


def file_to_data_uri(path: Path, mime: str) -> str:
    """Local file → base64 data URI; if unreadable, OSError bubbles up and the caller decides the semantics.

    本地文件 → base64 data URI；读不到时 OSError 向上冒泡由调用方决定语义。"""
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def image_to_data_uri(image_path: Path, mime_types: Mapping[str, str]) -> str:
    """Local image → base64 data URI, preferring MIME identified from the actual bytes.

    The file name may not have been updated to match the actual encoding during upload, generation or compression; if MIME
    were declared by extension, providers would reject such a Data URL as format spoofing. When the file header can't be
    identified, the existing compatible behaviour is kept: look up the extension in the provider's `mime_types`, falling
    back to PNG if unregistered.

    本地图片 → base64 data URI，优先按实际字节识别 MIME。

    文件名可能在上传、生成或压缩过程中未随实际编码格式同步更新；若按扩展名声明 MIME，
    供应商会将这种 Data URL 作为格式伪造而拒绝。无法从文件头识别时，保留按供应商
    `mime_types` 查扩展名、未登记回落 PNG 的既有兼容行为。
    """
    content = image_path.read_bytes()
    mime = _image_mime_from_bytes(content) or mime_types.get(image_path.suffix.lower(), _DEFAULT_IMAGE_MIME)
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
