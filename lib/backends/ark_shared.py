"""
Ark (Volcano Engine Ark) shared utility module

Reused by text_backends / image_backends / video_backends / providers.

Contains:
- ARK_BASE_URL — Volcano Engine Ark API base URL
- resolve_ark_api_key — API Key resolution (raises if missing, no longer uses env fallback)
- ark_base_url — normalizes user input (strip + remove trailing slash), falling back to ARK_BASE_URL
- create_ark_client — Ark client factory

Ark (火山方舟) 共享工具模块

供 text_backends / image_backends / video_backends / providers 复用。

包含：
- ARK_BASE_URL — 火山方舟 API 基础 URL
- resolve_ark_api_key — API Key 解析（缺失即 raise，不再走 env fallback）
- ark_base_url — 归一化用户输入（strip + 去尾斜杠），缺省回落 ARK_BASE_URL
- create_ark_client — Ark 客户端工厂
"""

from __future__ import annotations

import httpx

from lib.backends.transport_layer import TransportLayer, layer_sync_client

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def resolve_ark_api_key(api_key: str | None = None) -> str:
    if api_key is None or not api_key.strip():
        raise ValueError("Enter the Ark API Key on the settings page")
    return api_key.strip()


def ark_base_url(configured: str | None = None) -> str:
    """Normalize the user-entered base_url: strip + remove trailing slash, falling back to ARK_BASE_URL.

    Unlike dashscope/minimax/agnes, it does not derive the host by known suffixes and rebuild -- ark backends pass in
    non-standard variant paths (such as /api/plan/v3 used by ark-agent-plan), and rebuilding by suffix would break such
    paths, so only conservative whitespace/trailing-slash normalization is done without rewriting the path structure.

    归一化用户填入的 base_url：strip + 去尾斜杠，缺省回落 ARK_BASE_URL。

    不像 dashscope/minimax/agnes 那样按已知后缀做 host 派生再重建——ark 的 backend 会传入
    非标准变体路径（如 ark-agent-plan 用的 /api/plan/v3），按后缀重建会把这类路径拼坏，
    所以只做保守的空白/尾斜杠归一化，不改写路径结构。
    """
    # Strip before checking for empty: a whitespace-only string ("   ") is truthy and bypasses `or`, so the fallback must come after strip.
    # 先 strip 再判空：纯空白串（"   "）是真值会绕过 or，回落必须在 strip 之后。
    base = (configured or "").strip()
    return base.rstrip("/") if base else ARK_BASE_URL


def create_ark_client(
    *, api_key: str | None = None, base_url: str | None = None, transport_layer: TransportLayer | None = None
):
    """Create an Ark client; base_url defaults to ARK_BASE_URL (i.e. /api/v3), normalized via ark_base_url.

    The SDK is a synchronous client; ``transport_layer`` is layered on top of the synchronous httpx client.

    创建 Ark 客户端；base_url 缺省走 ARK_BASE_URL（即 /api/v3），经 ark_base_url 归一化。

    SDK 是同步客户端，``transport_layer`` 叠在同步 httpx 客户端之上。
    """
    from volcenginesdkarkruntime import Ark

    base = ark_base_url(base_url)
    key = resolve_ark_api_key(api_key)
    if transport_layer is None:
        return Ark(base_url=base, api_key=key)
    return Ark(
        base_url=base, api_key=key, http_client=layer_sync_client(httpx.Client(follow_redirects=True), transport_layer)
    )
