"""Container detection of bytes on disk: reads only the file header, not the extension.

Artifact file names are assigned by the persisting layer (the final cut's canonical ``.mp4`` resource path, trial run
``artifact.mp4`` / ``artifact.png``), while the real container is decided by the provider's export settings; when the
two don't match, declaring MIME by extension would make the browser decode with the wrong type. So "what container are
these bytes" is answered in one place by this module: there is only one copy of the magic numbers, referenced by both
the post-write container check and the MIME of the artifact read endpoint, so there aren't two tables where adding a
container updates only one side.

Division of labour with :mod:`lib.backends.data_uri`: that one answers "what MIME should this asset declare in an
outbound request body", hence its GIF / HEIC and extension-fallback compatibility rules; this module only recognizes the
few containers of final cuts and storyboard images, and says so when it can't recognize something.

落盘字节的容器识别：只读文件头，不读扩展名。

产物文件名是落盘那一层起的（成片的规范 ``.mp4`` 资源路径、测试连接的 ``artifact.mp4`` /
``artifact.png``），而真实容器由供应商那一侧的导出设置决定，两者对不上时按扩展名声明 MIME 会让
浏览器按一个错误的类型去解。故「这段字节是什么容器」由本模块一处回答：魔数只有这一份，落盘后的
容器核对与产物读接口的 MIME 都引用它，不会两处各存一张表、新增容器时只改一边。

与 :mod:`lib.backends.data_uri` 的分工：那边答的是「出站请求体里这张素材该声明什么 MIME」，
故带 GIF / HEIC 与按扩展名回落的兼容口径；本模块只认成片与分镜图这几种容器，认不出就说认不出。
"""

from __future__ import annotations

from collections.abc import Mapping

#: Number of header bytes read to determine the container: enough to reach the second magic of ``RIFF....WEBP`` and the major brand of ``ftyp``.
#: 判定容器要读的文件头字节数：够读到 ``RIFF....WEBP`` 的第二段魔数，也够读到 ``ftyp`` 的 major brand。
CONTAINER_HEAD_BYTES = 12

#: ISO BMFF major brands that are still images, not videos.
#:
#: It recognizes this side rather than a "video brand allowlist": video brands are an open set (``isom`` / ``iso2`` /
#: ``mp41`` / ``mp42`` / ``avc1`` / ``qt`` / ``M4V`` / ``dash`` …, with exporters still adding more), and an allowlist would
#: judge legitimate videos as unrecognized -- worse than missing a HEIC. The brand-to-MIME mapping reuses the existing one in
#: ``data_uri._image_mime_from_bytes``.
#: ISO BMFF 里属于静态图、不是成片的 major brand。
#:
#: 认的是这一边而不是「视频 brand 白名单」：视频侧的 brand 是开放集（``isom`` / ``iso2`` /
#: ``mp41`` / ``mp42`` / ``avc1`` / ``qt`` / ``M4V`` / ``dash`` …，各家导出器还在加），白名单
#: 会把合法成片判成认不出——那比漏认一张 HEIC 更糟。brand 与 MIME 的对应沿用
#: ``data_uri._image_mime_from_bytes`` 已有的那一份。
_ISO_BMFF_IMAGE_BRANDS: Mapping[bytes, str] = {
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"hevc": "image/heic",
    b"hevx": "image/heic",
    b"mif1": "image/heif",
    b"msf1": "image/heif",
    b"avif": "image/avif",
    b"avis": "image/avif",
}


def sniff_container(head: bytes) -> str | None:
    """The container MIME for this file header; ``None`` if unrecognized.

    ``ftyp`` (at byte offset 4–8) only says "this is the ISO BMFF family", which still images like HEIC / AVIF share, so
    they must be picked out by major brand -- otherwise a HEIC under a ``.mp4`` name would pass as a video. All other brands
    give ``video/mp4``: the brands of ``.mov`` and ``.m4v`` are written differently by different vendors, and on the playback
    side they go the same way as ``.mp4``; an allowlist would only judge legitimate videos as unrecognized.

    这段文件头对应的容器 MIME；认不出给 ``None``。

    ``ftyp``（落在偏移 4–8 字节）只说「这是 ISO BMFF 家族」，HEIC / AVIF 这类静态图同用这一层，
    故要按 major brand 把它们摘出来——否则一张 HEIC 装进 ``.mp4`` 的名字会被当成成片放行。其余
    brand 一律给 ``video/mp4``：``.mov`` 与 ``.m4v`` 的 brand 各家写法不一，而它们在播放侧与
    ``.mp4`` 同路，细分到白名单只会把合法成片判成认不出。
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return _ISO_BMFF_IMAGE_BRANDS.get(head[8:12], "video/mp4")
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None
