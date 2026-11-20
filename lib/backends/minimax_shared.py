"""MiniMax (Hailuo) shared utility module.

Reused by text_backends / image_backends / video_backends / config / connection tests. MiniMax itself is OpenAI
compatible with a single `/v1` base (no text/native dual base derivation like DashScope); text and native video
endpoints share the same base:
- MINIMAX_BASE_URL — default base for the China site (including `/v1`)
- MINIMAX_INTL_BASE_URL — international site base, as a reference for config overrides
- resolve_minimax_api_key — Bearer API Key resolution (raises if missing, no env fallback)
- minimax_text_base_url / minimax_video_base_url — normalized to {host}/v1, tolerating the user entering a host or one with a `/v1` suffix
- minimax_video_v2_base_url — multimodal video endpoint base {host}/v2 (H3)
- minimax_headers — Bearer auth headers
- extract_image_url / extract_image_base64 — take the first image from a single-step image_generation response
- minimax_failure_reason — error description when the image response's base_resp.status_code is non-zero
- safe_body_for_log — allowlisted log view (strips base64/URL/prompt)
- two-step video URL state machine: submit → poll query until status=Success to get file_id → files/retrieve to get download_url
- v2 single-step video URL state machine: submit → poll query until task.status=succeeded, then take task.content.url directly

MiniMax（海螺）共享工具模块。

供 text_backends / image_backends / video_backends / config / 连接测试复用。MiniMax 本身 OpenAI
兼容，单 `/v1` base（无 DashScope 那种文本/原生双 base 派生），文本与原生视频端点共用同一 base：
- MINIMAX_BASE_URL — 国内站默认 base（含 `/v1`）
- MINIMAX_INTL_BASE_URL — 国际站 base，供配置覆盖参考
- resolve_minimax_api_key — Bearer API Key 解析（缺失即 raise，不走 env fallback）
- minimax_text_base_url / minimax_video_base_url — 归一化为 {host}/v1，容忍用户填 host 或带 `/v1` 后缀
- minimax_video_v2_base_url — 多模态视频端点 base {host}/v2（H3）
- minimax_headers — Bearer 鉴权头
- extract_image_url / extract_image_base64 — 单步 image_generation 响应取首图
- minimax_failure_reason — 图像响应 base_resp.status_code 非零时的错误描述
- safe_body_for_log — 日志白名单视图（剥 base64/URL/prompt）
- 视频两步取 URL 状态机：submit→轮询 query 至 status=Success 取 file_id→files/retrieve 取 download_url
- v2 视频单步取 URL 状态机：submit→轮询 query 至 task.status=succeeded 直接取 task.content.url
"""

from __future__ import annotations

from pathlib import Path

from lib.backends.data_uri import image_to_data_uri as _image_to_data_uri

# Default base for the China site (including /v1); the international site overrides base_url in config to point to MINIMAX_INTL_BASE_URL.
# 国内站默认 base（含 /v1）；国际站经配置覆盖 base_url 指向 MINIMAX_INTL_BASE_URL。
MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
MINIMAX_INTL_BASE_URL = "https://api.minimax.io/v1"

# The single known path suffix, stripped when normalizing the host to tolerate users entering a full base.
# 单一已知路径后缀，归一化 host 时剥除以容忍用户填入完整 base。
_V1_SUFFIX = "/v1"
# The v2 video endpoint (H3) has the same host as v1 but a different prefix, see minimax_video_v2_base_url.
# v2 视频端点（H3）与 v1 同 host 不同前缀，见 minimax_video_v2_base_url。
_V2_SUFFIX = "/v2"

# Terminal states of async video tasks: Success/Fail; intermediate states Preparing/Queueing/Processing keep polling.
# 视频异步任务终态：Success/Fail；中间态 Preparing/Queueing/Processing 继续轮询。
MINIMAX_STATUS_SUCCESS = "Success"
MINIMAX_STATUS_FAIL = "Fail"
_TERMINAL_VIDEO_STATES = frozenset({MINIMAX_STATUS_SUCCESS, MINIMAX_STATUS_FAIL})

# v2 task status values are completely different from v1 (lowercase, plus a cancelled state), and a success response gives the download URL directly
# without a file_id retrieval step, so the two state machines are independent and v1's checks don't get an extra branch.
# v2 任务状态取值与 v1 完全不同（小写、多出取消态），且成功响应直接给下载 URL、无 file_id
# 取回步骤，故两套状态机各自独立，不在 v1 判定里加分支。
MINIMAX_V2_STATUS_SUCCEEDED = "succeeded"
MINIMAX_V2_STATUS_FAILED = "failed"
MINIMAX_V2_STATUS_CANCELLED = "cancelled"
_TERMINAL_V2_VIDEO_STATES = frozenset(
    {MINIMAX_V2_STATUS_SUCCEEDED, MINIMAX_V2_STATUS_FAILED, MINIMAX_V2_STATUS_CANCELLED}
)

# Poll interval (seconds): Hailuo video generation usually takes tens of seconds to several minutes; 10s balances latency and request volume.
# 轮询间隔（秒）：海螺视频生成通常数十秒至数分钟，10s 一次平衡时延与请求量。
MINIMAX_VIDEO_POLL_INTERVAL_SECONDS = 10.0

# Image suffix → MIME (first_frame_image accepts a data URI). This table is shared by the MiniMax video series, taking the union of the formats
# accepted by each model; heic/heif are only accepted by H3 (source:
# https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create.md).
# 图片后缀 → MIME（first_frame_image 接受 data URI）。本表为 MiniMax 视频系列共用，取各型号
# 受理格式的并集，heic/heif 仅 H3 受理（出处：
# https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create.md）。
_IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def resolve_minimax_api_key(api_key: str | None = None) -> str:
    if api_key is None or not api_key.strip():
        raise ValueError("Enter the MiniMax API Key on the settings page")
    return api_key.strip()


def _minimax_host(configured: str | None) -> str:
    """Extract the host part from the configured base_url (stripping the `/v1` suffix), falling back to the China site host.

    从配置的 base_url 提取 host 段（剥除 `/v1` 后缀），缺省回落国内站 host。"""
    # Strip before checking for empty: a whitespace-only string ("   ") is truthy and bypasses `or`, so the fallback must come after strip,
    # otherwise base becomes an empty string and derives invalid relative URLs like "/v1".
    # 先 strip 再判空：纯空白串（"   "）是真值会绕过 or，回落必须在 strip 之后，
    # 否则 base 变空串、派生出 "/v1" 这类非法相对 URL。
    base = ((configured or "").strip() or MINIMAX_BASE_URL).rstrip("/")
    if base.endswith(_V1_SUFFIX):
        return base[: -len(_V1_SUFFIX)]
    return base


def minimax_text_base_url(configured: str | None = None) -> str:
    """Text (OpenAI-compatible) base: {host}/v1. / 文本（OpenAI 兼容）base：{host}/v1。"""
    return f"{_minimax_host(configured)}{_V1_SUFFIX}"


def minimax_video_base_url(configured: str | None = None) -> str:
    """Native video endpoint base: {host}/v1. Shares the single /v1 base with text; the name only distinguishes the purpose.

    原生视频端点 base：{host}/v1。与文本共用单一 /v1 base，仅命名区分用途。"""
    return f"{_minimax_host(configured)}{_V1_SUFFIX}"


def minimax_video_v2_base_url(configured: str | None = None) -> str:
    """Multimodal video endpoint base: {host}/v2. Same host as /v1, used by H3's content[] endpoint.

    多模态视频端点 base：{host}/v2。与 /v1 同 host，供 H3 的 content[] 端点使用。"""
    return f"{_minimax_host(configured)}{_V2_SUFFIX}"


def minimax_headers(api_key: str) -> dict[str, str]:
    """Bearer auth headers. / Bearer 鉴权头。"""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _as_dict(value: object) -> dict:
    """Normalize any value to a dict: non-dicts (None / list / str and other abnormal upstream structures) all return an empty dict.

    Avoids AttributeError from calling .get on truthy non-dicts returned by relay proxies / error responses.

    把任意值归一化为 dict：非 dict（None / list / str 等异常上游结构）一律回空 dict。

    避免对中转代理 / 错误响应给出的非 dict 真值调用 .get 抛 AttributeError。
    """
    return value if isinstance(value, dict) else {}


def image_to_data_uri(image_path: Path) -> str:
    """Local image → base64 data URI (first_frame_image accepts a URL or data URI).

    本地图片 → base64 data URI（first_frame_image 接受 URL 或 data URI）。"""
    return _image_to_data_uri(image_path, _IMAGE_MIME_TYPES)


# ── single-step image_generation response tools ────────────────────────────────────────────
# ── 单步 image_generation 响应工具 ────────────────────────────────────────────


def extract_image_url(payload: object) -> str | None:
    """Take the first URL from the image_generation response's data.image_urls (response_format=url, valid 24h).

    Returns None when no URL is usable (top level / data not a dict, field missing, not a list, all empty); the caller falls back to base64 or errors.

    从 image_generation 响应 data.image_urls 取首个 URL（response_format=url，24h 有效）。

    无可用 URL（顶层 / data 非 dict、字段缺失、非 list、全为空）返回 None，由 caller 回落 base64 或报错。
    """
    urls = _as_dict(_as_dict(payload).get("data")).get("image_urls")
    if isinstance(urls, list):
        for url in urls:
            if isinstance(url, str) and url:
                return url
    return None


def extract_image_base64(payload: object) -> str | None:
    """Take the first base64 from the image_generation response's data.image_base64 (response_format=base64).

    Returns None when no base64 is usable (top level / data not a dict, field missing, etc.); the caller errors.

    从 image_generation 响应 data.image_base64 取首个 base64（response_format=base64）。

    无可用 base64（顶层 / data 非 dict、字段缺失等）返回 None，由 caller 报错。
    """
    items = _as_dict(_as_dict(payload).get("data")).get("image_base64")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str) and item:
                return item
    return None


def minimax_failure_reason(payload: object) -> str | None:
    """Returns an error description when base_resp.status_code is non-zero; None on success (0) or missing base_resp.

    MiniMax business errors are carried as HTTP 200 + non-zero base_resp.status_code (auth failures etc. may also come as
    4xx, caught by submit_post/raise_for_status), so synchronous image responses must check base_resp before taking the image.

    base_resp.status_code 非零时返回错误描述；成功（0）或缺失 base_resp 返回 None。

    MiniMax 业务错误以 HTTP 200 + base_resp.status_code 非零承载（鉴权失败等可能另走 4xx，
    由 submit_post/raise_for_status 兜住），故同步图像响应须先查 base_resp 再取图。
    """
    base = _as_dict(_as_dict(payload).get("base_resp"))
    status = base.get("status_code")
    if status is not None and status != 0:
        msg = base.get("status_msg") or ""
        return f"MiniMax image generation failed status_code={status}: {msg}".strip()
    return None


# ── log redaction ──────────────────────────────────────────────────────────────────
# ── 日志脱敏 ──────────────────────────────────────────────────────────────────

# Allowlist of scalar fields allowed into logs; prompt only logs its length, subject_reference is only counted,
# base64/URLs never go into logs (aligned with the CodeQL clear-text-logging constraint).
# 仅允许进日志的标量字段白名单；prompt 仅记长度、subject_reference 仅计数，
# base64/URL 一律不入日志（对齐 CodeQL clear-text-logging 约束）。
_SAFE_LOG_KEYS: frozenset[str] = frozenset(
    {"model", "aspect_ratio", "width", "height", "response_format", "n", "prompt_optimizer", "seed"}
)


def safe_body_for_log(body: dict) -> dict:
    """Produce a safe log view: allowlisted scalars + prompt length only + subject_reference count only.

    subject_reference embeds reference image base64/URLs and prompt is long text; neither is expanded.

    生成安全日志视图：白名单标量 + prompt 仅长度 + subject_reference 仅计数。

    subject_reference 内嵌参考图 base64/URL，prompt 为长文本，一律不展开。
    """
    view: dict = {key: body[key] for key in _SAFE_LOG_KEYS if key in body}
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        view["prompt_len"] = len(prompt)
    refs = body.get("subject_reference")
    if isinstance(refs, list) and refs:
        view["subject_reference"] = f"<{len(refs)} ref>"
    return view


# ── two-step video URL state machine tools ──────────────────────────────────────────────────
# ── 视频两步取 URL 状态机工具 ──────────────────────────────────────────────────


def _base_resp_error(payload: dict) -> str | None:
    """``base_resp.status_code != 0`` → error description; 0 or missing → None.

    All MiniMax responses carry ``base_resp`` (``{status_code, status_msg}``), 0 meaning success.
    Failures of the submit/query/retrieve endpoints themselves (auth, invalid task_id, etc.) surface here.

    ``base_resp.status_code != 0`` → 错误描述；0 或缺失 → None。

    MiniMax 所有响应带 ``base_resp``（``{status_code, status_msg}``），0 表成功。
    提交/查询/取回接口本身失败（鉴权、task_id 非法等）即在此暴露。
    """
    base = _as_dict(payload.get("base_resp"))
    code = base.get("status_code")
    if code is not None and code != 0:
        return f"MiniMax base_resp status_code={code}: {base.get('status_msg', '')}".strip()
    return None


def extract_minimax_video_task_id(submit_payload: dict) -> str:
    """Extract the top-level task_id from the submit response; if missing, raise per the base_resp error or as is.

    从提交响应提取顶层 task_id；缺失则按 base_resp 错误或原样抛出。"""
    task_id = submit_payload.get("task_id")
    if not task_id:
        reason = _base_resp_error(submit_payload)
        raise RuntimeError(reason or f"MiniMax video submit response is missing task_id: {submit_payload}")
    return str(task_id)


def minimax_video_status(payload: dict) -> str | None:
    """The status field of the query response (Preparing/Queueing/Processing/Success/Fail).

    查询响应的 status 字段（Preparing/Queueing/Processing/Success/Fail）。"""
    return payload.get("status")


def is_minimax_video_terminal(payload: dict) -> bool:
    """Success / Fail are terminal and stop polling; anything else (including unknown/empty) keeps polling.

    Success / Fail 为终态，停止轮询；其余（含未知/空）继续轮询。"""
    return minimax_video_status(payload) in _TERMINAL_VIDEO_STATES


def minimax_video_failure_reason(payload: dict) -> str | None:
    """status=Fail or a top-level base_resp hard error of the query endpoint → error description; otherwise None.

    status=Success / intermediate states return None (intermediate states are judged not done by is_terminal and keep polling).

    status=Fail 或查询接口顶层 base_resp 硬错误 → 错误描述；否则 None。

    status=Success / 中间态返回 None（中间态由 is_terminal 判定为未完成继续轮询）。
    """
    if minimax_video_status(payload) == MINIMAX_STATUS_FAIL:
        base = _as_dict(payload.get("base_resp"))
        return (
            f"MiniMax video task failed task_id={payload.get('task_id')} "
            f"status_code={base.get('status_code')}: {base.get('status_msg', '')}"
        ).strip()
    # Top-level base_resp hard error (e.g. task_id doesn't exist / auth failure): non-zero means failure.
    # 顶层 base_resp 硬错误（如 task_id 不存在 / 鉴权失败），非 0 即失败。
    return _base_resp_error(payload)


def extract_minimax_file_id(query_payload: dict) -> str:
    """Extract file_id from a status=Success query response. / 从 status=Success 的查询响应提取 file_id。"""
    file_id = query_payload.get("file_id")
    if not file_id:
        raise RuntimeError(f"MiniMax video task completed but file_id is missing: {query_payload}")
    return str(file_id)


def extract_minimax_download_url(retrieve_payload: dict) -> str:
    """Extract file.download_url from the files/retrieve response. / 从 files/retrieve 响应提取 file.download_url。"""
    url = _as_dict(retrieve_payload.get("file")).get("download_url")
    if not url:
        reason = _base_resp_error(retrieve_payload)
        raise RuntimeError(reason or f"MiniMax files/retrieve response is missing download_url: {retrieve_payload}")
    return url


# ── v2 video task state machine tools (single-step URL) ───────────────────────────────────────
# ── v2 视频任务状态机工具（单步取 URL） ───────────────────────────────────────


def minimax_v2_video_status(payload: dict) -> str | None:
    """task.status of the query response (queued/running/succeeded/failed/cancelled).

    查询响应 task.status（queued/running/succeeded/failed/cancelled）。"""
    return _as_dict(payload.get("task")).get("status")


def is_minimax_v2_video_terminal(payload: dict) -> bool:
    """succeeded / failed / cancelled are terminal; queued / running (including unknown) keep polling.

    succeeded / failed / cancelled 为终态；queued / running（含未知）继续轮询。"""
    return minimax_v2_video_status(payload) in _TERMINAL_V2_VIDEO_STATES


def minimax_v2_video_failure_reason(payload: dict) -> str | None:
    """status=failed/cancelled or a top-level base_resp hard error → error description; otherwise None.

    The cancelled state is also treated as failure: the task will never produce a video, and polling further would only wait until timeout.

    status=failed/cancelled 或顶层 base_resp 硬错误 → 错误描述；否则 None。

    取消态一并按失败处理：任务不会再产出视频，继续轮询只会等到超时。
    """
    status = minimax_v2_video_status(payload)
    if status in (MINIMAX_V2_STATUS_FAILED, MINIMAX_V2_STATUS_CANCELLED):
        task = _as_dict(payload.get("task"))
        detail = task.get("error") or task.get("status_msg") or ""
        return f"MiniMax video task {status} task_id={task.get('id')}: {detail}".strip()
    return _base_resp_error(payload)


def extract_minimax_v2_download_url(query_payload: dict) -> str:
    """Extract task.content.url (time-limited download address) from a status=succeeded query response.

    从 status=succeeded 的查询响应提取 task.content.url（限时下载地址）。"""
    url = _as_dict(_as_dict(query_payload.get("task")).get("content")).get("url")
    if not url:
        raise RuntimeError(f"MiniMax video task completed but content.url is missing: {query_payload}")
    return str(url)
