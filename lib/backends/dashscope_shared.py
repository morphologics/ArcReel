"""Alibaba Bailian DashScope (Model Studio) shared utility module.

Reused by image_backends / video_backends / text_backends factory / custom_provider / config. Contains:
- DASHSCOPE_BASE_URL — Bailian host part (without path suffix), Beijing region as the starting point
- resolve_dashscope_api_key — API Key resolution (raises if missing, no env fallback)
- dashscope_text_base_url / dashscope_native_base_url — two bases derived from the host
  (text uses /compatible-mode/v1, native image/video uses /api/v1), tolerating with/without suffix
- dashscope_headers — Bearer auth headers; async video additionally carries X-DashScope-Async: enable
- async video task tools — status checks / failure reason / task_id / video_url / billed duration extraction
- extract_image_url — URL extraction from synchronous image responses (multimodal choices)
- safe_body_for_log — allowlisted log view (keeps base64 images or long prompts out of logs)

阿里百炼 DashScope (Model Studio) 共享工具模块。

供 image_backends / video_backends / text_backends factory / custom_provider / config
复用。包含：
- DASHSCOPE_BASE_URL — 百炼 host 段（不含路径后缀），北京地域起点
- resolve_dashscope_api_key — API Key 解析（缺失即 raise，不走 env fallback）
- dashscope_text_base_url / dashscope_native_base_url — 由 host 派生双 base
  （文本走 /compatible-mode/v1，原生图像/视频走 /api/v1），容忍带/不带后缀
- dashscope_headers — Bearer 鉴权头，视频异步额外带 X-DashScope-Async: enable
- 视频异步任务工具 — 状态判定 / 失败原因 / task_id / video_url / 计费时长提取
- extract_image_url — 同步图像响应（multimodal choices）URL 提取
- safe_body_for_log — 日志白名单视图（避免 base64 图片或长 prompt 进日志）
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from lib.backends.data_uri import image_to_data_uri as _image_to_data_uri
from lib.db.repositories.usage_repo import MAX_BILLED_DURATION_SECONDS

logger = logging.getLogger(__name__)

# Host part (scheme://host), without the /api/v1 or /compatible-mode/v1 suffix; both bases are derived from it.
# host 段（scheme://host），不含 /api/v1 或 /compatible-mode/v1 后缀；两 base 由此派生。
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com"

# Retry decisions use the response status code rather than the exception string: str() of HTTPStatusError carries the URL/task_id, whose
# "503"/"timeout" substrings could make a 4xx business error be misjudged as retryable. DashScope backends use the status code predicates of
# lib.backends.backend_runtime -- create/submit (non-idempotent POST) uses should_retry_submit (4xx fail-fast, 5xx/429 retried, ambiguous
# transport errors converted by submit_post into AmbiguousSubmitError and not retried), polling (idempotent GET) uses should_retry_poll (404
# treated as not ready and retried), downloading an issued result URL uses should_retry_download (4xx including 404 always fail-fast).
# HTTPStatusError is always judged explicitly by response.status_code, which also preserves status code semantics so the choke point can
# recognize 413 for downgrade fallback.
# 重试判定按响应状态码而非异常字符串：HTTPStatusError 的 str() 携带 URL/task_id，其中的
# "503"/"timeout" 子串可能让 4xx 业务错误被误判为可重试。各 DashScope 后端使用
# lib.backends.backend_runtime 的状态码谓词——创建/提交（非幂等 POST）走 should_retry_submit（4xx
# fail-fast、5xx/429 重试、歧义传输错误经 submit_post 转 AmbiguousSubmitError 不重试），轮询
# （幂等 GET）走 should_retry_poll（404 视为未就绪重试），下载已签发结果 URL 走 should_retry_download
# （4xx 含 404 一律 fail-fast）。HTTPStatusError 一律按 response.status_code 显式闸门判定，状态码
# 语义也因此被保留，供咽喉层识别 413 做降档兜底。

# Task state machine (two-step async image/video) / 任务状态机（图像/视频异步两步式）
DASHSCOPE_STATUS_PENDING = "PENDING"
DASHSCOPE_STATUS_RUNNING = "RUNNING"
DASHSCOPE_STATUS_SUCCEEDED = "SUCCEEDED"
DASHSCOPE_STATUS_FAILED = "FAILED"
DASHSCOPE_STATUS_CANCELED = "CANCELED"
# After a task_id passes its 24h validity, queries return UNKNOWN (the resume path uses this to detect expiry)
# task_id 超过 24h 有效期后查询返回 UNKNOWN（resume 路径据此判过期）
DASHSCOPE_STATUS_UNKNOWN = "UNKNOWN"

_TERMINAL_STATES = frozenset(
    {
        DASHSCOPE_STATUS_SUCCEEDED,
        DASHSCOPE_STATUS_FAILED,
        DASHSCOPE_STATUS_CANCELED,
        DASHSCOPE_STATUS_UNKNOWN,
    }
)
_FAILURE_STATES = frozenset({DASHSCOPE_STATUS_FAILED, DASHSCOPE_STATUS_CANCELED})

_IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# Bailian recommends a 15-second polling interval; task_id / video_url both expire after 24h. / 百炼建议轮询间隔 15 秒；task_id / video_url 均 24h 过期。
DASHSCOPE_POLL_INTERVAL_SECONDS = 15.0

# Known path suffixes, stripped when deriving the host to tolerate users entering a full base (including region switching).
# 已知路径后缀，派生 host 时剥除以容忍用户填入完整 base（含地域切换）。
_KNOWN_SUFFIXES = ("/compatible-mode/v1", "/api/v1")


def resolve_dashscope_api_key(api_key: str | None = None) -> str:
    if api_key is None or not api_key.strip():
        raise ValueError("请到系统配置页填写 DashScope API Key")
    return api_key.strip()


def _dashscope_host(configured: str | None) -> str:
    """Extract the host part from the configured base_url (stripping known path suffixes), falling back to the Beijing host.

    从配置的 base_url 提取 host 段（剥除已知路径后缀），缺省回落北京 host。"""
    # Strip before checking for empty: a whitespace-only string ("   ") is truthy and bypasses `or`, so the fallback must come after strip,
    # otherwise base becomes an empty string and derives invalid relative URLs like "/api/v1".
    # 先 strip 再判空：纯空白串（"   "）是真值会绕过 or，回落必须在 strip 之后，
    # 否则 base 变空串、派生出 "/api/v1" 这类非法相对 URL。
    base = ((configured or "").strip() or DASHSCOPE_BASE_URL).rstrip("/")
    for suffix in _KNOWN_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def dashscope_text_base_url(configured: str | None = None) -> str:
    """Text (OpenAI-compatible mode) base: {host}/compatible-mode/v1. / 文本（OpenAI 兼容模式）base：{host}/compatible-mode/v1。"""
    return f"{_dashscope_host(configured)}/compatible-mode/v1"


def dashscope_native_base_url(configured: str | None = None) -> str:
    """Native (image/video) base: {host}/api/v1. / 原生（图像/视频）base：{host}/api/v1。"""
    return f"{_dashscope_host(configured)}/api/v1"


def dashscope_headers(api_key: str, *, async_mode: bool = False) -> dict[str, str]:
    """Bearer auth headers; video/async image must carry X-DashScope-Async: enable, synchronous image does not.

    Bearer 鉴权头；视频/异步图像须带 X-DashScope-Async: enable，同步图像不带。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if async_mode:
        headers["X-DashScope-Async"] = "enable"
    return headers


def image_to_data_uri(image_path: Path) -> str:
    """Local image → base64 data URI (Bailian media/image accepts a URL or data URI).

    本地图片 → base64 data URI（百炼 media/image 接受 URL 或 data URI）。"""
    return _image_to_data_uri(image_path, _IMAGE_MIME_TYPES)


# ── async video task status tools ──────────────────────────────────────────────────────
# ── 视频异步任务状态工具 ──────────────────────────────────────────────────────


def _as_dict(value: object) -> dict:
    """Normalize any value to a dict: non-dicts (including None / list / str and other abnormal upstream structures) all return an empty dict.

    The DashScope docs guarantee output/usage are objects, but relays or error responses may return truthy non-dicts;
    this helper is a uniform fallback to avoid AttributeError from calling .get on them.

    把任意值归一化为 dict：非 dict（含 None / list / str 等异常上游结构）一律回空 dict。

    DashScope 文档保证 output/usage 为对象，但代理中转或错误响应可能给出非 dict 真值，
    用此 helper 统一兜底，避免对其调用 .get 抛 AttributeError。
    """
    return value if isinstance(value, dict) else {}


def _task_status(payload: dict) -> str | None:
    return _as_dict(payload.get("output")).get("task_status")


def is_dashscope_succeeded(payload: dict) -> bool:
    return _task_status(payload) == DASHSCOPE_STATUS_SUCCEEDED


def is_dashscope_terminal(payload: dict) -> bool:
    """SUCCEEDED / FAILED / CANCELED / UNKNOWN are all terminal states; polling stops.

    SUCCEEDED / FAILED / CANCELED / UNKNOWN 均视为终态，停止轮询。"""
    return _task_status(payload) in _TERMINAL_STATES


def is_dashscope_expired(payload: dict) -> bool:
    """An expired task_id (24h) returns UNKNOWN on query; the resume path raises ResumeExpiredError based on it.

    task_id 过期（24h）查询返回 UNKNOWN；resume 路径据此抛 ResumeExpiredError。"""
    return _task_status(payload) == DASHSCOPE_STATUS_UNKNOWN


def dashscope_failure_reason(payload: dict) -> str | None:
    """FAILED/CANCELED return the error description; UNKNOWN is not a failure (handled separately as expired).

    Also covers top-level error responses from the submit phase (``{code, message, request_id}`` without output).

    FAILED/CANCELED 返回错误描述；UNKNOWN 不算失败（由 expired 单独处理）。

    同时兜底提交阶段的顶层错误响应（``{code, message, request_id}`` 无 output）。
    """
    output = _as_dict(payload.get("output"))
    status = output.get("task_status")
    if status in _FAILURE_STATES:
        code = output.get("code") or "unknown"
        message = output.get("message") or ""
        return f"DashScope 任务失败 status={status} code={code}: {message}".strip()
    # Top-level error from the submit phase (e.g. InvalidApiKey), no output.task_status / 提交阶段顶层错误（如 InvalidApiKey），无 output.task_status
    if status is None and payload.get("code"):
        return f"DashScope 提交失败 code={payload.get('code')}: {payload.get('message', '')}".strip()
    return None


def extract_task_id(submit_payload: dict) -> str:
    """Extract output.task_id from the submit response. / 从提交响应提取 output.task_id。"""
    task_id = _as_dict(submit_payload.get("output")).get("task_id")
    if not task_id:
        reason = dashscope_failure_reason(submit_payload)
        raise RuntimeError(reason or f"DashScope 提交响应缺少 task_id: {submit_payload}")
    return task_id


def extract_video_url(payload: dict) -> str:
    """Extract output.video_url from a SUCCEEDED poll response. / 从 SUCCEEDED 轮询响应提取 output.video_url。"""
    url = _as_dict(payload.get("output")).get("video_url")
    if not url:
        raise RuntimeError(f"DashScope 任务完成但缺少 video_url: {payload}")
    return url


def extract_billing_duration(payload: dict) -> int | None:
    """Take the real billed duration from usage.duration (wan2.7-r2v includes the input video duration).

    Tolerates int / float / numeric strings; rounds half-up (4.5→5) rather than truncating or banker's rounding, to avoid
    under-billing seconds. Non-positive values (0 / negative / unparseable) and values beyond a reasonable cap (24h, to
    prevent huge values overflowing the DB Integer column) all return None; the caller falls back to the requested
    duration rather than billing 0 seconds.

    从 usage.duration 取真实计费时长（wan2.7-r2v 含输入视频时长）。

    容忍 int / float / 数字字符串；按 half-up 取整（4.5→5）而非截断或银行家舍入，避免少计费秒数。
    非正值（0 / 负 / 无法解析）与超出合理上限（24h，防超大数值写入 DB Integer 列溢出）
    一律回 None，由 caller 回落请求时长，不记 0 秒账。
    """
    raw = _as_dict(payload.get("usage")).get("duration")
    if raw is None:
        return None
    try:
        decimal_value = Decimal(str(raw))
        # The cap is checked on the raw value before rounding: 86400.4 already exceeds 24h and must not be accepted by half-up landing back within the cap
        # (NaN in a comparison raises InvalidOperation and returns None via the same path as a parse failure)
        # 上限基于取整前的原始数值判断：86400.4 已超 24h，不得因 half-up 落回上限内被接受
        # （NaN 参与比较抛 InvalidOperation，与解析失败同路径回 None）
        if not 0 < decimal_value <= MAX_BILLED_DURATION_SECONDS:
            return None
        value = int(decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value > 0 else None


# ── synchronous image response tools ──────────────────────────────────────────────────────────
# ── 同步图像响应工具 ──────────────────────────────────────────────────────────


def extract_image_url(payload: dict) -> str:
    """Extract the first URL from a synchronous image response's output.choices[0].message.content[*].image.

    从同步图像响应 output.choices[0].message.content[*].image 提取首个 URL。"""
    choices = _as_dict(payload.get("output")).get("choices")
    if not isinstance(choices, list) or not choices:
        reason = dashscope_failure_reason(payload)
        raise RuntimeError(reason or f"DashScope 图像响应缺少 choices: {payload}")
    # Abnormal upstream structures (choices[0]/message not a dict) are normalized to empty dicts to avoid AttributeError from .get
    # 上游异常结构（choices[0]/message 非 dict）归一化为空 dict，避免 .get 抛 AttributeError
    content = _as_dict(_as_dict(choices[0]).get("message")).get("content")
    # Check for list explicitly: when content is a truthy non-list (e.g. int / bool / dict) `or []` can't catch it,
    # and iterating directly would raise TypeError; the isinstance guard routes everything to the RuntimeError below.
    # 显式校验 list：content 为 truthy 非 list（如 int / bool / dict）时 `or []` 兜不住，
    # 直接 for 会抛 TypeError；isinstance 守卫统一落到下方 RuntimeError。
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and (url := item.get("image")):
                return url
    raise RuntimeError(f"DashScope 图像响应 content 无 image 字段: {payload}")


# ── synchronous audio response tools ──────────────────────────────────────────────────────────
# ── 同步音频响应工具 ──────────────────────────────────────────────────────────


def extract_audio_url(payload: dict) -> str:
    """Extract the audio file URL (wav, valid 24h) from a synchronous speech synthesis response's output.audio.url.

    从同步语音合成响应 output.audio.url 提取音频文件 URL（wav，24h 有效）。"""
    url = _as_dict(_as_dict(payload.get("output")).get("audio")).get("url")
    if not url:
        reason = dashscope_failure_reason(payload)
        raise RuntimeError(reason or f"DashScope 语音合成响应缺少 audio.url: {payload}")
    return url


# ── log redaction ──────────────────────────────────────────────────────────────────
# ── 日志脱敏 ──────────────────────────────────────────────────────────────────

# Allowlist of scalar fields allowed into logs; everything else (including base64 images / media urls) stays out of logs.
# 仅允许进日志的标量字段白名单；其余（含 base64 image / media url）一律不入日志。
_SAFE_LOG_KEYS: frozenset[str] = frozenset(
    {"model", "size", "resolution", "ratio", "duration", "n", "watermark", "prompt_extend", "seed"}
)


def safe_body_for_log(body: dict) -> dict:
    """Produce a safe log view: allowlisted scalars + truncated prompt + media/messages only counted.

    The body contains input.messages (image) or input.media (video) with embedded base64/URLs; these are never expanded,
    keeping sensitive data out of logs (aligned with the CodeQL clear-text-logging constraint).

    生成安全日志视图：白名单标量 + prompt 截断 + media/messages 仅计数。

    body 含 input.messages（图像）或 input.media（视频），内部嵌 base64/URL；
    一律不展开，避免敏感数据进日志（对齐 CodeQL clear-text-logging 约束）。
    """
    # _as_dict rather than `or {}`: with a truthy non-dict (e.g. parameters/input being list/str) `or {}` can't catch it,
    # and the following .get / in would raise AttributeError/TypeError, letting a fail-safe log helper mask the original exception.
    # _as_dict 而非 `or {}`：truthy 非 dict（如 parameters/input 为 list/str）下 `or {}` 兜不住，
    # 后续 .get / in 会抛 AttributeError/TypeError，反而让 fail-safe 日志辅助遮蔽原始异常。
    params = _as_dict(body.get("parameters"))
    view: dict = {"model": body.get("model")}
    for key in _SAFE_LOG_KEYS:
        if key in params:
            view[key] = params[key]

    inp = _as_dict(body.get("input"))
    prompt = inp.get("prompt")
    if isinstance(prompt, str):
        view["prompt"] = prompt[:120] + ("…" if len(prompt) > 120 else "")

    media = inp.get("media")
    if isinstance(media, list) and media:
        view["media"] = f"<{len(media)} item>"

    messages = inp.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        content = messages[0].get("content")
        if isinstance(content, list):
            images = sum(1 for c in content if isinstance(c, dict) and "image" in c)
            texts = sum(1 for c in content if isinstance(c, dict) and "text" in c)
            view["content"] = f"<{images} image, {texts} text>"
    return view
