"""Vidu (Shengshu Technology) shared utility module.

Reused by image_backends / video_backends / providers router. Contains:
- VIDU_BASE_URL — Vidu open platform API base URL
- resolve_vidu_api_key — API Key resolution (raises if missing, no env fallback)
- create_vidu_client — httpx.AsyncClient factory with outbound destination validation
- image_to_data_uri — local image → base64 data URI (API limits body ≤20MB)
- fetch_vidu_task / is_vidu_done / vidu_failure_reason / extract_vidu_url — task polling tools
- VIDU_RETRYABLE_ERRORS — set of Vidu HTTPX transient errors (only NetworkError / TimeoutException)
- VIDU_MAX_BODY_BYTES / assert_vidu_body_size — 20MB request body limit check
- safe_body_for_log — allowlisted log view (avoids leaking base64 images or unexpected fields)
- test_vidu_connection — connection test (fork-only, extracted from the providers router)

Vidu (生数科技) 共享工具模块。

供 image_backends / video_backends / providers router 复用。包含：
- VIDU_BASE_URL — Vidu 开放平台 API 基础 URL
- resolve_vidu_api_key — API Key 解析（缺失即 raise，不走 env fallback）
- create_vidu_client — 经出站目的地校验的 httpx.AsyncClient 工厂
- image_to_data_uri — 本地图片 → base64 data URI（API 限制 body ≤20MB）
- fetch_vidu_task / is_vidu_done / vidu_failure_reason / extract_vidu_url — 任务轮询工具
- VIDU_RETRYABLE_ERRORS — Vidu HTTPX 瞬态错误集合（仅 NetworkError / TimeoutException）
- VIDU_MAX_BODY_BYTES / assert_vidu_body_size — 20MB 请求体上限校验
- safe_body_for_log — 日志输出白名单视图（避免泄漏 base64 图片或意外字段）
- test_vidu_connection — 连接测试（fork-only，从 providers router 抽出）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from lib.backends.artifact_download_guard import artifact_http_client
from lib.backends.data_uri import image_to_data_uri as _image_to_data_uri
from lib.backends.http_status_errors import raise_for_status_redacted
from lib.backends.transport_layer import TransportLayer
from lib.infra.retry import BASE_RETRYABLE_ERRORS

logger = logging.getLogger(__name__)

VIDU_BASE_URL = "https://api.vidu.cn/ent/v2"

# Only retry transient network errors: HTTPError includes HTTPStatusError, which would keep retrying business errors like 401/404
# until timeout and waste API quota; only NetworkError + TimeoutException are taken here.
# 仅重试瞬态网络错误：HTTPError 涵盖 HTTPStatusError，会让 401/404 等业务错误持续
# 重试到超时浪费 API 配额；这里只取 NetworkError + TimeoutException。
VIDU_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    *BASE_RETRYABLE_ERRORS,
    httpx.NetworkError,
    httpx.TimeoutException,
)

# Vidu docs limit a single request body to ≤ 20MB; base64 inflates by ~33%, leaving 2MB headroom for non-images fields.
# Vidu 文档限制单次请求体 ≤ 20MB；base64 自带 ~33% 膨胀，留 2MB 余量给非 images 字段。
VIDU_MAX_BODY_BYTES = 18 * 1024 * 1024

_IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def resolve_vidu_api_key(api_key: str | None = None) -> str:
    if api_key is None or not api_key.strip():
        raise ValueError("请到系统配置页填写 Vidu API Key")
    return api_key.strip()


def create_vidu_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
    transport_layer: TransportLayer | None = None,
) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` with the Authorization header and outbound destination validation.

    创建带 Authorization 头、经出站目的地校验的 ``httpx.AsyncClient``。"""
    token = resolve_vidu_api_key(api_key)
    return artifact_http_client(
        base_url=base_url or VIDU_BASE_URL,
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
        transport_layer=transport_layer,
    )


def image_to_data_uri(image_path: Path) -> str:
    """Local image → base64 data URI (Vidu accepts a URL or data URI; using a data URI avoids depending on a file service).

    本地图片 → base64 data URI（Vidu 接受 URL 或 data URI；走 data URI 免依赖文件服务）。"""
    return _image_to_data_uri(image_path, _IMAGE_MIME_TYPES)


# Allowlist of fields allowed in log output (so CodeQL doesn't worry about other body fields containing sensitive data).
# 日志输出仅允许的字段白名单（避免 CodeQL 担心 body 里其他字段含敏感数据）。
_SAFE_LOG_KEYS: frozenset[str] = frozenset(
    {"model", "duration", "resolution", "aspect_ratio", "seed", "audio", "off_peak"}
)


def safe_body_for_log(body: dict) -> dict:
    """Produce a safe log view: only allowlisted fields + truncated prompt + images only counted.

    生成安全的日志视图：仅保留白名单字段 + prompt 截断 + images 仅计数。"""
    view: dict = {k: body[k] for k in _SAFE_LOG_KEYS if k in body}
    if "prompt" in body:
        prompt = body["prompt"] or ""
        view["prompt"] = prompt[:120] + ("…" if len(prompt) > 120 else "")
    imgs = body.get("images") or []
    if imgs:
        view["images"] = f"<{len(imgs)} data uri>"
    return view


def assert_vidu_body_size(body: dict) -> None:
    """Vidu's hard limit on a single request body is 20MB; estimate after serialization here and reject early (with 2MB headroom).

    Vidu 单次请求体硬上限 20MB；这里在序列化后估算并提前拒绝（留 2MB 余量）。"""
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(encoded) > VIDU_MAX_BODY_BYTES:
        raise ValueError(
            f"Vidu 请求体大小 {len(encoded) / 1024 / 1024:.1f}MB 超过上限 "
            f"{VIDU_MAX_BODY_BYTES / 1024 / 1024:.0f}MB（base64 编码自带 ~33% 膨胀，"
            f"请减少参考图数量或压缩图片后重试）。"
        )


# Terminal status enum / 终态状态枚举
_DONE_STATE = "success"
_FAILED_STATE = "failed"


async def fetch_vidu_task(client: httpx.AsyncClient, task_id: str) -> dict:
    """Query Vidu task status (GET /tasks/{id}/creations). / 查询 Vidu 任务状态（GET /tasks/{id}/creations）。"""
    resp = await client.get(f"/tasks/{task_id}/creations")
    raise_for_status_redacted(resp)
    return resp.json()


def is_vidu_done(payload: dict) -> bool:
    return payload.get("state") == _DONE_STATE


def vidu_failure_reason(payload: dict) -> str | None:
    if payload.get("state") == _FAILED_STATE:
        err = payload.get("err_code") or "unknown"
        return f"Vidu 任务失败: err_code={err}"
    return None


def extract_vidu_url(payload: dict) -> str:
    """Extract the generated artifact URL from a success response. / 从 success 响应中提取生成物 URL。"""
    creations = payload.get("creations") or []
    if not creations:
        raise RuntimeError(f"Vidu 任务无 creations: task_id={payload.get('id')}")
    url = creations[0].get("url")
    if not url:
        raise RuntimeError(f"Vidu creations[0].url 为空: task_id={payload.get('id')}")
    return url


# ---------------------------------------------------------------------------
# Connection test (fork-only, originally in server/routers/providers.py, extracted to reduce router intrusion)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 连接测试（fork-only，原住在 server/routers/providers.py，抽出以降低 router 侵入）
# ---------------------------------------------------------------------------
def test_vidu_connection(config: dict[str, str]) -> None:
    """Validate the Vidu API Key by querying a nonexistent task id.

    Vidu has no list-models endpoint, so ``GET /tasks/<bogus>/creations`` is used to check auth.
    The Vidu server parses the task id as an integer, so a numeric string must be passed; a non-numeric one returns a 400
    CODEC parse error before the auth check, making the connection test always fail.

    Allowlist approach: with a valid credential it returns 404 (task doesn't exist); 401/403 mean an invalid credential;
    any other status (including 5xx) is treated as undecidable and raises, to avoid a false success.

    通过查询不存在的 task id 验证 Vidu API Key。

    Vidu 无 list-models 端点，借助 ``GET /tasks/<bogus>/creations`` 校验认证。
    Vidu 服务端把 task id 当作整数解析，必须传数字字符串；非数字会在 auth 检查
    之前返回 400 CODEC parse error，让连接测试始终失败。

    采用白名单：凭证有效时返回 404（task 不存在）；401/403 视为凭证无效；
    其他状态码（含 5xx）一律视为不可判定，抛错避免误判成功。
    """
    api_key = resolve_vidu_api_key(config.get("api_key"))
    base_url = (config.get("base_url") or VIDU_BASE_URL).rstrip("/")
    headers = {"Authorization": f"Token {api_key}"}
    # Use a syntactically valid but extremely unlikely numeric id (avoiding the 400 CODEC integer parse failure)
    # 用一个语法上合法但极不可能存在的数字 id（避开整数解析失败的 400 CODEC）
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(f"{base_url}/tasks/0/creations", headers=headers)
    if resp.status_code == 404:
        return
    if resp.status_code in (401, 403):
        raise RuntimeError(f"HTTP {resp.status_code}: 凭证无效")
    raise RuntimeError(f"HTTP {resp.status_code}: 连接测试无法判定（{resp.text[:200]}）")
