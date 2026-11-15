"""URL normalization utility functions. / URL 归一化工具函数。"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

# Official OpenAI endpoint: both the baseline for is_official_openai_base_url and the default that the upper-layer
# client factory must explicitly fill in when base_url is empty — AsyncOpenAI falls back to reading the OPENAI_BASE_URL
# environment variable for an empty base_url, and passing the official value explicitly cuts that fallback. Single
# source, do not scatter the literal.
# 官方 OpenAI 端点：既是 is_official_openai_base_url 的判定基准，也是上层
# 客户端工厂在 base_url 为空时须显式回填的默认值——AsyncOpenAI 对空 base_url
# 会回落读取 OPENAI_BASE_URL 环境变量，显式传入官方值即断掉该回落。单一来源，
# 勿散落字面量。
OFFICIAL_OPENAI_HOSTNAME = "api.openai.com"
OFFICIAL_OPENAI_BASE_URL = f"https://{OFFICIAL_OPENAI_HOSTNAME}/v1"

# The Claude CLI's bundled SDK always appends this segment after base_url; probing, preview and runtime share the same constant.
# It is also the only suffix form when users copy the whole Messages endpoint from provider docs, and the only one that can be stripped unambiguously.
# Claude CLI 内置 SDK 固定在 base_url 后拼这一段；探测、预览、运行时共用同一个常量。
# 也是用户从供应商文档复制整条 Messages 端点时的唯一后缀形态，只有它能被无歧义地剥掉。
ANTHROPIC_MESSAGES_PATH = "/v1/messages"

# Official Anthropic endpoint: the anthropic SDK falls back to reading the ANTHROPIC_BASE_URL environment variable for an empty base_url;
# fill it in explicitly when base_url is empty to cut that fallback (same as OFFICIAL_OPENAI_BASE_URL).
# 官方 Anthropic 端点：anthropic SDK 对空 base_url 会回落读取 ANTHROPIC_BASE_URL 环境变量，
# base_url 为空时显式回填它以断掉该回落（同 OFFICIAL_OPENAI_BASE_URL）。
OFFICIAL_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


def is_official_openai_base_url(base_url: str | None) -> bool:
    """Whether an OpenAI-compatible base_url points at the official api.openai.com.

    On the official endpoint max_tokens is deprecated and rejected by reasoning models (o series / gpt-5 etc.), and
    max_completion_tokens should be used instead; third-party compatible endpoints (vLLM, various relays) vary in support
    for the new parameter, so conservatively keep max_tokens.

    base_url only comes from the DB config (the single source); when empty (None/blank string) it is judged official.

    Known limitation: base_urls pointing at relays/proxies are always judged non-official; if a relay forwards max_tokens
    as is to an official reasoning model it will still be rejected (an explicit 400 with a self-describing error message).

    判断 OpenAI 兼容 base_url 是否指向官方 api.openai.com。

    官方端点上 max_tokens 已弃用且被推理模型（o 系列 / gpt-5 等）拒绝，
    应改用 max_completion_tokens；第三方兼容端点（vLLM、各类中转）对新
    参数支持情况不一，须保守沿用 max_tokens。

    base_url 只来自 DB 配置（唯一来源），为空（None/空白串）时判定为官方端点。

    已知限制：指向中转/代理的 base_url 一律判非官方，若中转将 max_tokens
    原样转发给官方推理模型仍会被拒（显式 400，报错信息自描述）。
    """
    effective = (base_url or "").strip()
    if not effective:
        return True
    # hostname lowercases and drops the port itself; without a scheme hostname is None → conservatively judged non-official
    # hostname 自带小写化与去端口；无 scheme 时 hostname 为 None → 保守判非官方
    return urlparse(effective).hostname == OFFICIAL_OPENAI_HOSTNAME


def ensure_openai_base_url(url: str | None) -> str | None:
    """Automatically append the /v1 path suffix for OpenAI-compatible APIs.

    Users may enter only ``https://api.example.com``, but the OpenAI SDK expects ``https://api.example.com/v1``. This
    function appends it automatically when the version path is missing.

    自动补全 OpenAI 兼容 API 的 /v1 路径后缀。

    用户可能只填了 ``https://api.example.com``，但 OpenAI SDK 期望
    ``https://api.example.com/v1``。本函数在缺少版本路径时自动追加。
    """
    if not url:
        return url
    stripped = url.strip().rstrip("/")
    if not re.search(r"/v\d+$", stripped):
        stripped += "/v1"
    return stripped


def normalize_base_url(url: str | None) -> str | None:
    """Make sure base_url ends with /.

    The Google genai SDK's http_options.base_url requires a trailing /, otherwise request path joining fails. The preset
    Gemini backend uses this function.

    确保 base_url 以 / 结尾。

    Google genai SDK 的 http_options.base_url 要求尾部带 /，
    否则请求路径拼接会失败。预置 Gemini 后端使用此函数。
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not url.endswith("/"):
        url += "/"
    return url


def ensure_google_base_url(url: str | None) -> str | None:
    """Normalize the base_url for the Google genai SDK.

    The Google genai SDK automatically appends ``api_version`` (default ``v1beta``) after base_url. If the user mistakenly
    enters ``https://example.com/v1beta``, the SDK builds ``https://example.com/v1beta/v1beta/models`` and the request fails.

    This function strips a trailing version path (e.g. ``/v1beta``, ``/v1``) and ensures a trailing ``/``.

    规范化 Google genai SDK 的 base_url。

    Google genai SDK 会自动在 base_url 后拼接 ``api_version``（默认 ``v1beta``）。
    如果用户误填了 ``https://example.com/v1beta``，SDK 会拼出
    ``https://example.com/v1beta/v1beta/models``，导致请求失败。

    本函数剥离末尾的版本路径（如 ``/v1beta``、``/v1``），并确保尾部带 ``/``。
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    url = url.rstrip("/")
    # Strip a trailing version path (/v1, /v1beta, /v1alpha etc.)
    # Use [a-zA-Z] instead of \w: the overlap of \d+\w* triggers a CodeQL polynomial regex warning
    # 剥离末尾的版本路径（/v1, /v1beta, /v1alpha 等）
    # 用 [a-zA-Z] 代替 \w：\d+\w* 的重叠会触发 CodeQL polynomial regex 警告
    url = re.sub(r"/v\d+[a-zA-Z]*$", "", url)
    if not url.endswith("/"):
        url += "/"
    return url


class InvalidAnthropicBaseUrlError(ValueError):
    """The Agent credential base_url contains unsupported parts (query / fragment / userinfo) or is not an absolute http(s) address.

    Agent 凭证 base_url 含不支持的成分（query / fragment / userinfo）或不是绝对 http(s) 地址。"""


def normalize_anthropic_base_url(raw: str) -> str:
    """Normalize the user's Anthropic base_url into the "stored value is the call value" form.

    Does only three things: trim whitespace; strip trailing slashes; strip the segment when it ends exactly with
    ``/v1/messages`` (the only unambiguous form of a user copying the full endpoint from docs).

    Does not strip ``/vN``, does not strip a lone ``/messages``, and neither recognizes nor appends ``/anthropic``-style
    subpaths: the Claude CLI's bundled SDK joins base_url and ``/v1/messages`` by string concatenation, and any guessing
    would make "test connection passed" and "404 at runtime" diverge.

    把用户填的 Anthropic base_url 归一到「存储值即调用值」形态。

    只做三件事：去首尾空白；去尾部斜杠；末尾恰好是 ``/v1/messages`` 时去掉这一段
    （用户从文档复制完整端点的唯一无歧义形态）。

    不剥 ``/vN``、不剥单独的 ``/messages``、不识别也不追加 ``/anthropic`` 类子路径：
    Claude CLI 内置 SDK 以字符串拼接 base_url 与 ``/v1/messages``，任何猜测都会让
    「测试连接通过」与「运行时 404」分叉。
    """
    stripped = raw.strip().rstrip("/")
    if stripped.endswith(ANTHROPIC_MESSAGES_PATH):
        # Strip trailing slashes once more after stripping the endpoint: the stored value of ``https://x//v1/messages`` must be ``https://x``.
        # 剥掉端点后再去一次尾斜杠：``https://x//v1/messages`` 的存储值须是 ``https://x``。
        stripped = stripped[: -len(ANTHROPIC_MESSAGES_PATH)].rstrip("/")
    return stripped


def validate_anthropic_base_url(raw: str) -> str:
    """Normalize and validate the Agent credential base_url, returning the canonical value shared by storage / calls.

    Raises:
        InvalidAnthropicBaseUrlError: contains query / fragment / userinfo, or lacks scheme / host.
            The message is fixed text without echoing the input, to avoid writing credentials in the address into logs and responses.

    归一化并校验 Agent 凭证 base_url，返回入库 / 调用共用的规范值。

    Raises:
        InvalidAnthropicBaseUrlError: 含 query / fragment / userinfo，或缺 scheme / host。
            消息为固定文案，不回显输入，避免把地址里的凭证写进日志与响应。
    """
    normalized = normalize_anthropic_base_url(raw)
    if not normalized:
        raise InvalidAnthropicBaseUrlError("base_url is empty")
    # Block by character instead of checking whether urlparse's query / fragment is non-empty: ``https://x/a?`` with an empty query
    # likewise makes the CLI build ``https://x/a?/v1/messages``; identical to the frontend's blocking rule.
    # 按字符拦而不看 urlparse 的 query / fragment 是否非空：空 query 的 ``https://x/a?``
    # 同样会让 CLI 拼出 ``https://x/a?/v1/messages``；与前端拦截规则逐字一致。
    if "?" in normalized or "#" in normalized:
        raise InvalidAnthropicBaseUrlError("base_url must not carry query or fragment")
    parsed = urlparse(normalized)
    try:
        has_userinfo = bool(parsed.username or parsed.password)
        host = parsed.hostname
        _ = parsed.port  # netloc is parsed lazily; an illegal port only raises ValueError at this step / netloc 惰性解析，非法端口在这一步才抛 ValueError
        httpx.URL(normalized)
    except (ValueError, httpx.InvalidURL) as exc:
        raise InvalidAnthropicBaseUrlError("base_url is not a parsable URL") from exc
    if has_userinfo:
        raise InvalidAnthropicBaseUrlError("base_url must not carry userinfo")
    if parsed.scheme not in ("http", "https") or not host:
        raise InvalidAnthropicBaseUrlError("base_url must be an absolute http(s) URL")
    return normalized


def anthropic_endpoint_url(base_url: str, path_suffix: str) -> str:
    """Append ``path_suffix`` (e.g. ``/v1/messages``) to the end of ``base_url``.

    Joined by string concatenation like the Claude CLI's bundled SDK: ``base_url`` has passed
    :func:`validate_anthropic_base_url` (no query / fragment, no trailing slash), and only verbatim concatenation makes
    probing, model discovery and runtime request the same string; decoding via ``httpx.URL`` and writing the path back
    would turn escaped separators like ``%2F`` into real separators.

    在 ``base_url`` 末尾追加 ``path_suffix``（如 ``/v1/messages``）。

    与 Claude CLI 内置 SDK 同样按字符串拼接：``base_url`` 已经过 :func:`validate_anthropic_base_url`
    （无 query / fragment、无尾斜杠），逐字拼接才能让探测、模型发现与运行时请求的是同一个
    字符串；经 ``httpx.URL`` 解码再回写 path 会把 ``%2F`` 这类已转义的分隔符还原成真实分隔符。
    """
    return base_url.rstrip("/") + path_suffix
