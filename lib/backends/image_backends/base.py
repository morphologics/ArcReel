"""Core interface definitions of the image generation service layer. / 图片生成服务层核心接口定义。"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from arcreel_market_core.video_backend_contract import IMAGE_MIME_TYPES, ProviderResponseStage
from lib.backends.artifact_download_guard import IMAGE_ARTIFACT_MAX_BYTES, artifact_http_client
from lib.backends.backend_runtime import stream_to_file
from lib.backends.data_uri import image_to_data_uri as _image_to_data_uri
from lib.billing.metering import Usage
from lib.infra.async_thread import run_noninterruptible_sync


def image_to_base64_data_uri(image_path: Path) -> str:
    """Convert a local image to a base64 data URI. / 将本地图片转为 base64 data URI。"""
    return _image_to_data_uri(image_path, IMAGE_MIME_TYPES)


# passed to httpx as the I/O timeout config, not an async cancellation deadline
async def download_image_to_path(url: str, output_path: Path, *, timeout: int = 60) -> None:  # noqa: ASYNC109 -- 转交 httpx 的 I/O 超时配置，非 async 取消 deadline
    """Asynchronously stream an image from a URL into a local file; destination and size are constrained by the artifact download entry.

    从 URL 异步流式下载图片到本地文件；目的地与体积受产物下载入口约束。"""
    async with artifact_http_client() as client:
        await stream_to_file(client, url, output_path, max_bytes=IMAGE_ARTIFACT_MAX_BYTES, timeout=timeout)


async def save_image_from_response_item(item, output_path: Path) -> None:
    """Extract the image from an OpenAI-compatible SDK's ``response.data[i]`` and write it to a local path.

    Prefer ``b64_json``; if empty degrade to downloading ``url``; raise ``ValueError`` if both are empty.

    The provider response is in hand, so decoding and writing fall under docs/standards/backend.md "the post-provider-return
    accounting section is non-interruptible": if cancellation lands while writing, the thread finishes writing and the
    cancellation is dropped, and the caller's subsequent transcoding and artifact commit proceed as usual. Reusing this
    helper satisfies that rule. The network fetch of the URL path is still interruptible, the final rename isn't; see the
    note in the standard.

    从 OpenAI 兼容 SDK 的 ``response.data[i]`` 提取图片并写入本地路径。

    优先 ``b64_json``；为空降级到 ``url`` 下载；两者皆空抛 ``ValueError``。

    供应商响应已到手，解码与写盘属 docs/standards/backend.md「供应商返回之后的落账段不可中断」：
    取消落在写盘期间时线程照常写完，取消随之消失，调用方后续的转码与产物提交照常执行。
    复用本助手即满足该规则。URL 路径的网络取件仍可中断，最终改名不可中断，见规范中的说明。
    """
    b64 = getattr(item, "b64_json", None)
    if b64:

        def _decode_and_save() -> None:
            # Decoding + writing are offloaded to a thread together, to avoid CPU-heavy base64 decoding in the event loop
            # 解码 + 写盘统一 offload 到线程，避免在事件循环内做 CPU 密集 base64 解码
            image_bytes = base64.b64decode(b64)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(image_bytes)

        await run_noninterruptible_sync(_decode_and_save)
        return
    url = getattr(item, "url", None)
    if url:
        await download_image_to_path(url, output_path)
        return
    raise ValueError("图片生成响应既无 b64_json 也无 url")


class ImageCapability(StrEnum):
    """图片后端支持的能力枚举。"""

    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"


#: Backend facts of a backend implementing both text-to-image and image-to-image. Built-in backends declare what they implement in the class attribute
#: ``CHANNEL_CAPABILITIES`` (readable without constructing an instance, see ``registry.channel_capabilities``); whether the model accepts it is stated by the
#: model entry, and the effective capabilities are the part satisfied by both (see ``lib.backends.image_capabilities``).
#: 文生图与图生图都实现了的调用通道的通道事实。内置调用通道在类属性 ``CHANNEL_CAPABILITIES`` 上声明
#: 自己实现了哪些（不构造实例即可读，见 ``registry.channel_capabilities``）；模型接不接受由模型条目说明，
#: 生效能力取两者都满足的部分（见 ``lib.backends.image_capabilities``）。
BOTH_IMAGE_CAPABILITIES: frozenset[ImageCapability] = frozenset(
    {ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}
)


@dataclass
class ReferenceImage:
    """Reference image. Reference images are referred to in the prompt as "image N" by array position; the transport chain only carries paths.

    参考图片。参考图在 prompt 里按数组序位以「图N」指认，传输链只携带路径。"""

    path: str


@dataclass
class ImageGenerationRequest:
    """Generic image generation request. Each Backend ignores fields it doesn't support. / 通用图片生成请求。各 Backend 忽略不支持的字段。"""

    prompt: str
    output_path: Path
    reference_images: list[ReferenceImage] = field(default_factory=list)
    aspect_ratio: str = "9:16"
    image_size: str | None = None
    project_name: str | None = None
    seed: int | None = None
    #: The only sizes the model accepts ((width, height), …), for fixed-size models; the backend sends the one
    #: closest to the size derived from ``aspect_ratio`` and ``image_size``. ``None`` = any size.
    sizes: tuple[tuple[int, int], ...] | None = None
    #: Diagnostic callback for provider responses, same shape as in ``VideoGenerationRequest``. Only callers needing per-stage recording
    #: (endpoint tests) provide it; the production image path doesn't collect recordings and keeps it ``None``.
    #: 供应商响应的诊断回调，与 ``VideoGenerationRequest`` 同形。只有需要逐阶段留痕的调用方
    #: （端点测试）会给它；生产的出图路径不收集留痕，保持 ``None``。
    on_provider_response: Callable[[ProviderResponseStage, object], Awaitable[None]] | None = None


@dataclass(frozen=True)
class ImageRequestFacts:
    """Request facts known before submit, for the backend to derive estimated usage (see each built-in backend's ``estimate_usage``).

    ``reference_count`` is the number of reference images; ``None`` when a batch estimate only knows it's image-to-image but not how many.

    提交前可知的请求事实，供调用通道推出预估用量（见各内置调用通道的 ``estimate_usage``）。

    ``reference_count`` 是参考图张数；整批预估只知道走图生图、不知道几张时为 ``None``。
    """

    model: str
    aspect_ratio: str
    image_size: str | None = None
    reference_count: int | None = 0


#: A built-in backend's estimation function: derives the request fields to be sent and the usage from request facts, with the same transforms ``generate`` uses to build the request.
#: 内置调用通道的预估函数：按请求事实推出会发送的请求字段与用量，与 ``generate`` 构造请求用同一套变换。
ImageUsageEstimator = Callable[[ImageRequestFacts], Usage]


@dataclass
class ImageGenerationResult:
    """Generic image generation result.

    ``usage`` is this request's meters and selection dimensions reported by the backend per the usage contract
    (docs/adr/0100): selection dimensions come from the request fields actually sent to the provider, and only meters that
    actually occurred are reported. All built-in backends report it; the custom provider wrapper fills it in by the generic rules when the wrapped backend doesn't (see :mod:`lib.custom_provider.metering`).

    通用图片生成结果。

    ``usage`` 是调用通道按用量契约上报的这次请求的计量项与选择维度（docs/adr/0100）：选择维度取自真正发给供应商
    的请求字段，计量项只报实际发生的。内置调用通道都上报；自定义供应商的包装层在被包装的通道不报时按通用口径补上（见 :mod:`lib.custom_provider.metering`）。
    """

    image_path: Path
    provider: str
    model: str
    image_uri: str | None = None
    seed: int | None = None
    usage_tokens: int | None = None
    # Token usage breakdown for the OpenAI GPT Image series (None by default for other backends)
    # OpenAI GPT Image 系列的 token 用量拆分（其它 backend 默认 None）
    image_input_tokens: int | None = None
    image_output_tokens: int | None = None
    text_input_tokens: int | None = None
    text_output_tokens: int | None = None
    usage: Usage | None = None


def per_image_usage(
    *,
    output_images: int | None,
    input_images: int | None,
    dimensions: dict[str, object] | None = None,
) -> Usage:
    """Usage reported by per-image-priced backends: output image count, reference image count and selection dimensions.

    A reference image count of 0 is reported too: an estimate can't be made if any usage item is missing. ``None`` means the count is unknown (only in estimates) and isn't reported.

    按张计价的调用通道上报的用量：输出张数、参考图张数与选择维度。

    参考图张数为 0 也照报：预估时缺一项用量就无从估算。``None`` 表示不知道几张（只出现在预估中），不报。
    """
    quantities: dict[str, float] = {}
    if output_images is not None:
        quantities["output_images"] = output_images
    if input_images is not None:
        quantities["input_images"] = input_images
    return Usage(quantities=quantities, dimensions=dict(dimensions or {}))


class ImageBackend(Protocol):
    """Image generation backend protocol. / 图片生成后端协议。"""

    @property
    def name(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def capabilities(self) -> set[ImageCapability]:
        """Whether this backend implements text-to-image or image-to-image (backend facts, excluding model facts).

        这条调用通道实现了文生图还是图生图（通道事实，不含模型事实）。"""
        ...

    async def reference_image_limit(self) -> int:
        """Max reference images this model can carry per request; ``0`` = this backend doesn't trim by count.

        Same source as the truncation in ``generate``. The orchestration layer reads it once when resolving the image backend and
        trims the reference image sequence before rendering "image N" numbers (see
        ``lib.prompts.reference_image_numbering.clamp_reference_images``); the truncation inside the backend becomes a fallback.
        Async: the limit may only be known by querying the provider's model catalog (e.g. OpenRouter's per-model ``input_references``).

        这个模型单请求可携带的参考图上限；``0`` = 该后端不按数量裁剪。

        与 ``generate`` 内的截断同源。编排层解析图片通道时读一次，据此在渲染「图N」编号前裁剪参考图序列
        （见 ``lib.prompts.reference_image_numbering.clamp_reference_images``），后端内的截断退为兜底。
        异步：上限可能要向供应商的模型目录查询才知道（如 OpenRouter 按模型声明的 ``input_references``）。
        """
        ...

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult: ...


class ImageOutputMissingError(RuntimeError):
    """The provider returned a successful response without an image (common with content safety filter hits or upstream anomalies).

    Like :class:`ImageCapabilityError` it only carries a stable code and params; copy is rendered by the read side per language.

    供应商成功响应却没有带回图片（常见于内容安全过滤命中或上游异常）。

    与 :class:`ImageCapabilityError` 一样只带稳定 code 与 params，文案由读侧按语言渲染。
    """

    code = "image_output_missing"

    def __init__(self, *, model: str) -> None:
        self.params = {"model": model}
        super().__init__(f"{self.code}: model={model}")


class ImageOutputFormatError(RuntimeError):
    """The image format returned by the provider can't be used as a bitmap artifact (e.g. vector SVG); carries a stable code and params.

    供应商返回的图片格式无法作为位图产物使用（如矢量 SVG），携带稳定 code 与 params。"""

    code = "image_output_format_unsupported"

    def __init__(self, *, model: str, media_type: str) -> None:
        self.params = {"model": model, "media_type": media_type}
        super().__init__(f"{self.code}: model={model} media_type={media_type}")


class ImageCapabilityError(RuntimeError):
    """Image backend capability mismatch (shared by endpoint mismatch / generator gating).

    Carries no localized strings, only a stable code + context params;
    the route layer renders it directly with _t(code, **params), the Worker stores code + params in task.error_message,
    and copy is rendered on the read side per Accept-Language.

    图像后端能力不匹配（endpoint mismatch / generator gating 共用）。

    不携带本地化字符串，只带稳定 code + 上下文 params；
    路由层直接 _t(code, **params) 渲染，Worker 则按 code + params 落 task.error_message，
    文案留到读侧按 Accept-Language 渲染。
    """

    def __init__(self, code: str, **params) -> None:
        self.code = code
        self.params = params
        super().__init__(code)
