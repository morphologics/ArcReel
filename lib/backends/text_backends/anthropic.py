"""AnthropicTextBackend — text generation backend for the Anthropic Messages protocol.

Called via the Anthropic-protocol entry points of Chinese vendors and relays. Structured output uses Messages'
native ``output_config.format``, not the OpenAI-compatible layer (which ignores ``response_format``); images use
image content blocks.

AnthropicTextBackend — Anthropic Messages 协议的文本生成后端。

经国内厂商与中转站的 Anthropic 协议入口调用。结构化输出走 Messages 原生的
``output_config.format``，不经 OpenAI 兼容层（后者忽略 ``response_format``）；图片走图片内容块。
"""

from __future__ import annotations

import asyncio
import logging

from anthropic import (
    APIConnectionError,
    APITimeoutError,
    AsyncAnthropic,
    BadRequestError,
    DefaultAsyncHttpxClient,
    InternalServerError,
    RateLimitError,
    transform_schema,
)

from lib.backends.image_backends.base import image_to_base64_data_uri
from lib.backends.providers import PROVIDER_ANTHROPIC
from lib.backends.text_backends.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    TextCapability,
    TextGenerationRequest,
    TextGenerationResult,
    check_truncation,
    merge_billed_usage,
    prompt_json_fallback,
    resolve_schema,
    structured_fallback_reason,
    truncate_for_log,
)
from lib.backends.transport_layer import TransportLayer, layer_async_client
from lib.config.url_utils import OFFICIAL_ANTHROPIC_BASE_URL
from lib.infra.logging_utils import format_kwargs_for_log
from lib.infra.retry import with_retry_async

logger = logging.getLogger(__name__)

ANTHROPIC_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)


class AnthropicTextBackend:
    """Anthropic Messages text generation backend, serving only custom providers' ``anthropic-messages`` endpoints.

    Credentials come only from the custom provider's own api_key and base_url, isolated from the Agent's Anthropic
    credentials (ADR 0017).

    Anthropic Messages 文本生成后端，只服务自定义供应商的 ``anthropic-messages`` 端点。

    凭证只取自定义供应商自己的 api_key 与 base_url，与 Agent 的 Anthropic 凭证隔离（ADR 0017）。
    """

    def __init__(
        self, *, api_key: str, model: str, base_url: str | None = None, transport_layer: TransportLayer | None = None
    ):
        # When base_url is empty, fill in the official address explicitly to cut off the SDK's fallback to the
        # ANTHROPIC_BASE_URL environment variable; disable the SDK's built-in retries, managed by _generate_native.
        # base_url 为空时显式回填官方地址，断掉 SDK 对 ANTHROPIC_BASE_URL 的环境回落；
        # 禁用 SDK 内置重试，由 _generate_native 统一管理。
        self._client = AsyncAnthropic(
            api_key=api_key,
            base_url=(base_url or "").strip() or OFFICIAL_ANTHROPIC_BASE_URL,
            max_retries=0,
            http_client=(
                layer_async_client(DefaultAsyncHttpxClient(), transport_layer) if transport_layer is not None else None
            ),
        )
        self._model = model

    @property
    def name(self) -> str:
        return PROVIDER_ANTHROPIC

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[TextCapability]:
        return {
            TextCapability.TEXT_GENERATION,
            TextCapability.STRUCTURED_OUTPUT,
            TextCapability.VISION,
        }

    async def generate(self, request: TextGenerationRequest) -> TextGenerationResult:
        """Generate a text reply.

        This method has no retry decorator: transient error retries happen at the :meth:`_generate_native` layer;
        re-validation and fallback are outside the retry scope, so a failed fallback does not also replay the already
        successful native call.

        生成文本回复。

        本方法不带重试装饰器：瞬态错误重试在 :meth:`_generate_native` 层完成，复验与降级
        不进重试范围，免得降级失败连带重放已成功的原生调用。
        """
        # Requests with images need to read local images for base64; build them in a thread to avoid blocking the event loop
        # 带图请求要读本地图片做 base64，放线程里构建，避免阻塞事件循环
        content = await asyncio.to_thread(_build_user_content, request)
        structured = bool(request.response_schema)
        native = await self._generate_native(request, content, structured=structured)

        if native is None:
            # The gateway does not accept output_config (schema error); structured output falls back as a whole
            # 网关不接受 output_config（schema 错误），结构化输出整体降级
            return await self._prompt_json_fallback(request)

        if request.response_schema:
            # Relays may silently ignore output_config and return prose or non-conforming JSON. transform_schema moves
            # unsupported constraints into description, so the native request is equivalent to not declaring strict; re-validation uses strict=False.
            # 中转站可能静默忽略 output_config 返回散文或违例 JSON。transform_schema 会把
            # 不支持的约束挪进 description，原生请求等同未声明 strict，复验用 strict=False。
            fallback_reason = structured_fallback_reason(native.text, request.response_schema, strict=False)
            if fallback_reason:
                logger.warning(
                    "原生 output_config %s，降级到 prompt 注入路径；模型原始输出：%s",
                    fallback_reason,
                    truncate_for_log(native.text),
                )
                result = await self._prompt_json_fallback(request)
                # This native 200 call has already been billed; merge its tokens into the fallback result.
                # 这次原生 200 调用已被计费，把它的 token 并入降级结果。
                merge_billed_usage(result, native)
                return result

        return native

    async def _prompt_json_fallback(self, request: TextGenerationRequest) -> TextGenerationResult:
        async def _once(fb_request: TextGenerationRequest) -> TextGenerationResult:
            content = await asyncio.to_thread(_build_user_content, fb_request)
            # The fallback request no longer carries output_config, so it will not return the schema-error fallback signal again
            # 降级请求已不带 output_config，不会再返回 schema 错误的降级信号
            result = await self._generate_native(fb_request, content, structured=True)
            assert result is not None
            return result

        return await prompt_json_fallback(request, _once, provider=PROVIDER_ANTHROPIC, model=self._model)

    @with_retry_async(max_attempts=4, backoff_seconds=(2, 4, 8), retryable_errors=ANTHROPIC_RETRYABLE_ERRORS)
    async def _generate_native(
        self, request: TextGenerationRequest, content: list[dict], *, structured: bool
    ) -> TextGenerationResult | None:
        """A single Messages call: transient errors are retried; returns ``None`` as the fallback signal when output_config is rejected.

        The output limit can reach tens of thousands of tokens and the SDK rejects non-streaming requests as a timeout risk,
        so it always streams and then takes the complete message.

        单次 Messages 调用：瞬态错误重试；output_config 被拒时返回 ``None`` 作为降级信号。

        输出上限可达数万 token，非流式请求会被 SDK 以超时风险拒绝，固定走流式再取完整消息。
        """
        kwargs: dict = {
            "model": self._model,
            "max_tokens": request.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": content}],
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt
        if request.response_schema:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": transform_schema(resolve_schema(request.response_schema))}
            }

        logger.info("调用 %s 文本 SDK kwargs=%s", self.name, format_kwargs_for_log(kwargs))
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                message = await stream.get_final_message()
        except BadRequestError as exc:
            if request.response_schema:
                logger.warning("原生 output_config 失败 (%s)，降级到 prompt 注入路径", exc)
                return None
            raise

        usage = message.usage
        # Anthropic's input_tokens excludes cache reads and writes: total input must add cache reads and cache writes back.
        # Cache writes are reported in two tiers by TTL; without a tier breakdown, the default 5-minute tier is used.
        # Anthropic 的 input_tokens 不含缓存读写：总输入须加回缓存读与缓存写。缓存写按 TTL 分两档上报；
        # 没有分档明细时按默认的 5 分钟档计。
        cache_read = usage.cache_read_input_tokens or 0
        cache_write = usage.cache_creation_input_tokens or 0
        breakdown = usage.cache_creation
        cache_write_1h = breakdown.ephemeral_1h_input_tokens if breakdown is not None else 0
        cache_write_5m = breakdown.ephemeral_5m_input_tokens if breakdown is not None else cache_write
        input_tokens = usage.input_tokens + cache_read + cache_write
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        truncated = check_truncation(
            message.stop_reason,
            provider=PROVIDER_ANTHROPIC,
            model=self._model,
            output_tokens=usage.output_tokens,
            structured=structured,
        )
        return TextGenerationResult(
            text=text,
            provider=PROVIDER_ANTHROPIC,
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
            truncated=truncated,
            cache_read_tokens=cache_read,
            cache_write_5m_tokens=cache_write_5m,
            cache_write_1h_tokens=cache_write_1h,
        )


def _build_user_content(request: TextGenerationRequest) -> list[dict]:
    """Image content blocks first, text after, forming the user message content. / 图片内容块在前、文本在后，组成 user 消息的 content。"""
    content: list[dict] = []
    for img in request.images or []:
        if img.path:
            header, data = image_to_base64_data_uri(img.path).split(",", 1)
            media_type = header.removeprefix("data:").split(";", 1)[0]
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        elif img.url:
            content.append({"type": "image", "source": {"type": "url", "url": img.url}})
    content.append({"type": "text", "text": request.prompt})
    return content
