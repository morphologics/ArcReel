"""Agnes shared utility module.

Reused by image_backends / video_backends / text_backends / config / connection tests. Agnes provides OpenAI-style
endpoints through the apihub gateway, with a single `/v1` base and single-key Bearer auth:
- AGNES_BASE_URL — default base (including `/v1`)
- resolve_agnes_api_key — Bearer API Key resolution (raises if missing, no env fallback)
- agnes_base_url — normalized to {host}/v1, tolerating the user entering a host or one with a `/v1` suffix
- agnes_host — normalized to {host} (without `/v1`), for building non-OpenAI-style endpoints mounted at the gateway root
- agnes_headers — Bearer auth headers

Agnes 共享工具模块。

供 image_backends / video_backends / text_backends / config / 连接测试复用。Agnes 经
apihub 网关提供 OpenAI 风格端点，单 `/v1` base，Bearer 单 key 鉴权：
- AGNES_BASE_URL — 默认 base（含 `/v1`）
- resolve_agnes_api_key — Bearer API Key 解析（缺失即 raise，不走 env fallback）
- agnes_base_url — 归一化为 {host}/v1，容忍用户填 host 或带 `/v1` 后缀
- agnes_host — 归一化为 {host}（不含 `/v1`），供挂在网关根下的非 OpenAI 风格端点拼接
- agnes_headers — Bearer 鉴权头
"""

from __future__ import annotations

# Default base (including /v1); users can override base_url in config to point to a self-hosted relay.
# 默认 base（含 /v1）；用户可经配置覆盖 base_url 指向自建中转。
AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"

# The single known path suffix, stripped when normalizing the host to tolerate users entering a full base.
# 单一已知路径后缀，归一化 host 时剥除以容忍用户填入完整 base。
_V1_SUFFIX = "/v1"


def resolve_agnes_api_key(api_key: str | None = None) -> str:
    if api_key is None or not api_key.strip():
        raise ValueError("请到系统配置页填写 Agnes API Key")
    return api_key.strip()


def agnes_host(configured: str | None = None) -> str:
    """Extract the host part from the configured base_url (stripping the `/v1` suffix), falling back to the default host.

    Not all gateway endpoints are under `/v1` -- the final video query `/agnesapi` is mounted directly at the host root
    and needs this base without the version segment.

    从配置的 base_url 提取 host 段（剥除 `/v1` 后缀），缺省回落默认 host。

    网关并非所有端点都挂在 `/v1` 下——成片查询 `/agnesapi` 直接挂在 host 根，需要这个不带
    版本段的 base。
    """
    # Strip before checking for empty: a whitespace-only string ("   ") is truthy and bypasses `or`, so the fallback must come after strip,
    # otherwise base becomes an empty string and derives invalid relative URLs like "/v1".
    # 先 strip 再判空：纯空白串（"   "）是真值会绕过 or，回落必须在 strip 之后，
    # 否则 base 变空串、派生出 "/v1" 这类非法相对 URL。
    base = ((configured or "").strip() or AGNES_BASE_URL).rstrip("/")
    if base.endswith(_V1_SUFFIX):
        return base[: -len(_V1_SUFFIX)]
    return base


def agnes_base_url(configured: str | None = None) -> str:
    """OpenAI-compatible base: {host}/v1. / OpenAI 兼容 base：{host}/v1。"""
    return f"{agnes_host(configured)}{_V1_SUFFIX}"


def agnes_headers(api_key: str) -> dict[str, str]:
    """Bearer auth headers.

    Reuses resolve_agnes_api_key validation: empty / whitespace-only raises locally, avoiding building
    ``Authorization: Bearer `` and only getting an upstream 401 at request time for a missing key.

    Bearer 鉴权头。

    复用 resolve_agnes_api_key 校验：空串 / 纯空白即本地 raise，避免拼出
    ``Authorization: Bearer `` 把缺失 key 拖到请求期才收上游 401。
    """
    api_key = resolve_agnes_api_key(api_key)
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
