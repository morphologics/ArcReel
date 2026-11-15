"""OpenAIChatImageBackend — image backend that generates images via Chat Completions (``modalities`` includes ``image``).

Hits the same API as the text ``openai-chat``: the prompt and reference images are sent as one multi-part user
message, with reference images as ``image_url`` content parts (data URI). Where the image comes back differs across
relays: OpenRouter uses ``choices[0].message.images[].image_url.url``, while some relays inline the image as
Markdown in ``content``; both are accepted. The image itself may be a data URI or a downloadable URL.

OpenAIChatImageBackend — 经 Chat Completions 出图（``modalities`` 含 ``image``）的图片后端。

与文本的 ``openai-chat`` 打同一个 API：提示词与参考图作为一条多段 user 消息发出，参考图是
``image_url`` 内容段（data URI）。出图位置各中转站不一：OpenRouter 约定在
``choices[0].message.images[].image_url.url``，也有中转站把图以 Markdown 内联在 ``content`` 里，
两处都认；图片本身可以是 data URI 或可下载的 URL。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any

from openai.types.chat import ChatCompletion, ChatCompletionContentPartImageParam, ChatCompletionContentPartParam

from arcreel_market_core.aspect_size import IMAGE_TIER_SHORT_EDGE
from lib.backends.backend_runtime import with_artifact_retry
from lib.backends.image_backends.base import (
    ImageCapability,
    ImageCapabilityError,
    ImageGenerationRequest,
    ImageGenerationResult,
    download_image_to_path,
    image_to_base64_data_uri,
)
from lib.backends.openai_shared import OPENAI_RETRYABLE_ERRORS, create_openai_client
from lib.backends.providers import PROVIDER_OPENAI
from lib.backends.transport_layer import TransportLayer
from lib.infra.async_thread import run_noninterruptible_sync
from lib.infra.retry import with_retry_async

logger = logging.getLogger(__name__)

#: image_config.image_size only sends tier words (1K / 2K / 4K …); a custom WxH has no matching value and is not sent.
#: image_config.image_size 只下传档位词（1K / 2K / 4K …）；自定义 WxH 没有对应取值，不下传。
_IMAGE_SIZE_TIERS = {k.lower(): k for k in IMAGE_TIER_SHORT_EDGE}
#: Markdown image inlined in content: ``![alt](url)``, where url may be a data URI or an http(s) address.
#: content 里内联的 Markdown 图片：``![alt](url)``，url 可以是 data URI 或 http(s) 地址。
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)\s*\)")


def _image_config(request: ImageGenerationRequest) -> dict[str, str]:
    config = {"aspect_ratio": request.aspect_ratio}
    tier = _IMAGE_SIZE_TIERS.get((request.image_size or "").strip().lower())
    if tier:
        config["image_size"] = tier
    return config


def _extract_image_url(message: Any) -> str | None:
    """Take the address of the first image from the assistant message: look at ``images`` first, then Markdown images in ``content``.

    从 assistant 消息里取第一张图的地址：先看 ``images``，再看 ``content`` 里的 Markdown 图片。"""
    for item in getattr(message, "images", None) or []:
        image_url = item.get("image_url") if isinstance(item, dict) else getattr(item, "image_url", None)
        url = image_url.get("url") if isinstance(image_url, dict) else getattr(image_url, "url", None)
        if url:
            return url
    content = getattr(message, "content", None)
    if isinstance(content, str):
        match = _MARKDOWN_IMAGE.search(content)
        if match:
            return match.group(1)
    return None


async def _save_image(url: str, output_path: Path) -> None:
    if not url.startswith("data:"):
        # Fetch failures are retried only at the download layer, never falling back to rerunning the already billed image request
        # 取件失败只在下载层重试，不回退到重跑已计费的出图请求
        await with_artifact_retry(lambda: download_image_to_path(url, output_path), label="Chat Completions 图像")
        return

    def _decode_and_save() -> None:
        _, _, payload = url.partition(",")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(payload))

    await run_noninterruptible_sync(_decode_and_save)


class OpenAIChatImageBackend:
    """Generates images via Chat Completions; supports text-to-image and image-to-image. / 经 Chat Completions 出图，支持文生图与图生图。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str,
        base_url: str | None = None,
        transport_layer: TransportLayer | None = None,
        provider_name: str = PROVIDER_OPENAI,
    ):
        self._client = create_openai_client(api_key=api_key, base_url=base_url, transport_layer=transport_layer)
        self._model = model
        self._provider_name = provider_name

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ImageCapability]:
        return {ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}

    async def reference_image_limit(self) -> int:
        # The same protocol fronts models from many vendors; the reference image limit varies by model and is left to the upstream to decide.
        # 同一个协议背后挂的是各家模型，参考图上限随模型而异，交由上游裁决。
        return 0

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        content: list[ChatCompletionContentPartParam] = [{"type": "text", "text": request.prompt}]
        content.extend(await asyncio.to_thread(self._reference_parts, request))
        response = await self._complete(content, request)
        message = response.choices[0].message if response.choices else None
        url = _extract_image_url(message) if message is not None else None
        if not url:
            raise RuntimeError(
                f"Chat Completions 出图响应中没有图片 (model={self._model})，可能触发内容安全过滤或模型不支持出图"
            )
        await _save_image(url, request.output_path)
        logger.info("Chat Completions 出图完成: %s", request.output_path)

        # Report total input and total output with the existing fields (the read side adds the two kinds together, see usage_repo).
        # 按现有字段上报总输入与总输出（读侧把两类相加，见 usage_repo）。
        usage = response.usage
        return ImageGenerationResult(
            image_path=request.output_path,
            provider=self._provider_name,
            model=self._model,
            text_input_tokens=usage.prompt_tokens if usage is not None else None,
            image_output_tokens=usage.completion_tokens if usage is not None else None,
        )

    @with_retry_async(retryable_errors=OPENAI_RETRYABLE_ERRORS)
    async def _complete(
        self, content: list[ChatCompletionContentPartParam], request: ImageGenerationRequest
    ) -> ChatCompletion:
        return await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": content}],
            modalities=["image", "text"],  # type: ignore[list-item]  # SDK types only list text / audio / SDK 类型只列 text / audio
            extra_body={"image_config": _image_config(request)},
        )

    def _reference_parts(self, request: ImageGenerationRequest) -> list[ChatCompletionContentPartImageParam]:
        parts: list[ChatCompletionContentPartImageParam] = []
        for ref in request.reference_images:
            ref_path = Path(ref.path)
            try:
                data_uri = image_to_base64_data_uri(ref_path)
            except FileNotFoundError:
                logger.warning("参考图不存在，跳过: %s", ref_path)
                continue
            parts.append({"type": "image_url", "image_url": {"url": data_uri}})
        if request.reference_images and not parts:
            # The user submitted image-to-image but no material is usable: raise instead of silently degrading to text-to-image
            # 用户提交的是图生图却没有一张可用素材：报错而不是悄悄降级成文生图
            raise ImageCapabilityError(
                "image_endpoint_mismatch_no_i2i",
                model=self._model,
                detail="all reference images failed to open",
            )
        return parts
