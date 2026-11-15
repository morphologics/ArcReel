"""
OpenAI shared utility module

Reused by text_backends / image_backends / video_backends / providers.

Contains:
- OPENAI_RETRYABLE_ERRORS — retryable error types
- create_openai_client — AsyncOpenAI client factory
- OPENAI_IMAGE_QUALITY_MAP — image_size tier → quality mapping, consumed by image_backends.openai.
  Size no longer uses a static (image_size, aspect_ratio) → "WxH" table; it is computed precisely from the ratio by
  arcreel_market_core.aspect_size (ratio first, resolution second), see docs/adr/0011.

OpenAI 共享工具模块

供 text_backends / image_backends / video_backends / providers 复用。

包含：
- OPENAI_RETRYABLE_ERRORS — 可重试错误类型
- create_openai_client — AsyncOpenAI 客户端工厂
- OPENAI_IMAGE_QUALITY_MAP — image_size 档位 → quality 映射，供 image_backends.openai 消费。
  尺寸不再用静态 (image_size, aspect_ratio) → "WxH" 表，改由 arcreel_market_core.aspect_size 按比例精确计算
  （比例优先、清晰度其次），见 docs/adr/0011。
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from lib.backends.transport_layer import TransportLayer, layer_async_client
from lib.config.url_utils import OFFICIAL_OPENAI_BASE_URL

logger = logging.getLogger(__name__)

OPENAI_RETRYABLE_ERRORS: tuple[type[Exception], ...] = ()

OPENAI_IMAGE_QUALITY_MAP: dict[str, str] = {
    "512px": "low",
    "1K": "medium",
    "2K": "high",
    "4K": "high",
}

try:
    from openai import (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    OPENAI_RETRYABLE_ERRORS = (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )
except ImportError:
    pass  # openai is a required dependency; this branch is only defensive; fall back to an empty tuple / openai 是必装依赖，此分支仅作防御性保护；回退到空 tuple


def create_openai_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    max_retries: int | None = None,
    transport_layer: TransportLayer | None = None,
) -> AsyncOpenAI:
    """Create an AsyncOpenAI client, handling api_key and base_url uniformly.

    When base_url is empty (None/blank) the official endpoint is filled in explicitly: AsyncOpenAI falls back to reading
    the OPENAI_BASE_URL env var for an empty base_url, and leftover environment would silently override the DB config.
    The only source of base_url is the DB; pinning an explicit value here cuts off that fallback path. ``transport_layer``
    is layered on top of the SDK's default client.

    创建 AsyncOpenAI 客户端，统一处理 api_key 和 base_url。

    base_url 为空（None/空白）时显式回填官方端点：AsyncOpenAI 对空 base_url
    会回落读取 OPENAI_BASE_URL 环境变量，环境残留将静默覆盖 DB 配置。base_url
    的唯一来源是 DB，此处兜死显式值断掉该回落路径。``transport_layer`` 叠在 SDK 默认客户端之上。
    """
    kwargs: dict = {"base_url": (base_url or "").strip() or OFFICIAL_OPENAI_BASE_URL}
    if api_key:
        kwargs["api_key"] = api_key
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    if transport_layer is not None:
        kwargs["http_client"] = layer_async_client(DefaultAsyncHttpxClient(), transport_layer)
    return AsyncOpenAI(**kwargs)
