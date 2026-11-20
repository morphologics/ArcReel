"""Runtime support for backends: submit and poll, retry decisions, requests with credential scoping, artifact download to
disk, provider response recording, and persisting provider task ids.

Video, image and audio backends and the custom provider backend share this module. The video contract lives in
:mod:`arcreel_market_core.video_backend_contract`; the image and audio contracts live in the ``base`` of their backend
packages. Image and audio backends don't load the video backend package through this module: video requests only appear
as type annotations, and video capability exceptions are only fetched inside video-specific functions.

调用通道的运行支持：提交与轮询、重试判定、带凭据作用域的请求、产物下载落盘、供应商响应留痕
与供应商任务 id 落库。

视频、图像、音频三类调用通道与自定义供应商的 backend 共用本模块。视频契约位于
:mod:`arcreel_market_core.video_backend_contract`，图像与音频契约位于各自通道包的 ``base``。图像与音频
通道经本模块不会加载视频通道包：视频请求只作类型标注出现，视频能力异常只在视频专属函数里取。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

import httpx
from sqlalchemy.exc import InterfaceError, OperationalError

from lib.backends.artifact_download_guard import (
    VIDEO_ARTIFACT_MAX_BYTES,
    artifact_http_client,
    buffered_error_response,
    stream_body_to_file,
)
from lib.backends.data_uri import file_to_data_uri
from lib.backends.http_status_errors import (
    AmbiguousSubmitError,
    ArtifactDownloadError,
    provider_rejected_error,
    redacted_status_error,
)
from lib.infra.async_thread import run_noninterruptible_async
from lib.infra.logging_utils import redact_diagnostic_text, sanitize_diagnostic_payload
from lib.infra.mp4_faststart import FASTSTART_SUFFIXES, ensure_faststart
from lib.infra.retry import (
    BASE_RETRYABLE_ERRORS,
    AsyncClock,
    NonRetryableError,
    resolve_clock,
    should_retry,
    with_retry_async,
)

if TYPE_CHECKING:
    from arcreel_market_core.video_backend_contract import ProviderResponseStage, VideoGenerationRequest

# `should_retry` by default does a string-pattern fallback ("timeout"/"503" etc.),
# but persist retries need strict "transient DB error" semantics -- business exceptions (e.g.
# `ValueError("Connection timed out: rate")`) must not be swallowed by a substring match.
# Explicitly pass `retry_if=lambda e: isinstance(e, _PERSIST_RETRYABLE_ERRORS)` to turn the fallback off.
# `should_retry` 默认会做字符串模式兜底（"timeout"/"503" 等），
# 而 persist 重试要严格"DB 瞬态错误"语义——业务异常（如
# `ValueError("Connection timed out: rate")`）不该被字符串子串吞掉。
# 显式传 `retry_if=lambda e: isinstance(e, _PERSIST_RETRYABLE_ERRORS)` 关掉兜底。

logger = logging.getLogger(__name__)

VIDEO_POLL_INTERVAL_SECONDS = 5.0
VIDEO_POLL_MAX_CONSECUTIVE_FAILURES = 10
VIDEO_POLL_MAX_BACKOFF_SECONDS = 60.0


# Set of transient DB errors: sqlite "database is locked", pg "could not connect" / connection closed.
# Deliberately excludes the DBAPIError parent -- it would catch non-transient errors like IntegrityError/DataError/ProgrammingError
# (SQL syntax / constraint violations), where retrying is pointless and delays fail-fast.
# DB 瞬态错误集合：sqlite "database is locked"、pg "could not connect" / 连接已关闭。
# 故意不收 DBAPIError 父类——会兜住 IntegrityError/DataError/ProgrammingError 等非瞬态
# 错误（SQL 语法 / 约束违反），重试无意义且拖延 fail-fast。
_PERSIST_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    OperationalError,
    InterfaceError,
    ConnectionError,
    TimeoutError,
)
_PERSIST_BACKOFF_SECONDS: tuple[int, ...] = (1, 2, 4)


class ProviderJobIdStore(Protocol):
    """Persistence entry for provider task ids: protocol identifier and actual request domain are written in the same write as job_id.

    供应商任务 id 的落库入口：与 job_id 同一次写入落下协议标识与实际请求域名。"""

    async def persist_provider_job_id(
        self, task_id: str, job_id: str, *, endpoint: str | None = None, base_url: str | None = None
    ) -> None: ...


_provider_job_id_store: Callable[[], ProviderJobIdStore] | None = None


def install_provider_job_id_store(get_store: Callable[[], ProviderJobIdStore]) -> None:
    """Register how the persistence entry is obtained; registered by the generation queue when its module loads.

    The persistence entry is injected from above rather than imported here: the generation queue reaches the declarative
    runtime via config resolution, so if runtime support imported it directly, every backend using runtime support
    (including the ComfyUI subpackage) would be dragged in by that chain. ``get_store`` is called on each persist, without
    caching the instance it returns.

    登记落库入口的取用方式；由生成队列在其模块加载时登记。

    落库入口由上层注入而非在此导入：生成队列经配置解析够得到声明式运行时，运行支持直接导入它，
    所有用到运行支持的调用通道（包括 ComfyUI 子包）都会被这条链拖过去。每次落库时调一次
    ``get_store``，不缓存它返回的实例。
    """
    global _provider_job_id_store
    _provider_job_id_store = get_store


@with_retry_async(
    max_attempts=3,
    backoff_seconds=_PERSIST_BACKOFF_SECONDS,
    retry_if=lambda e: isinstance(e, _PERSIST_RETRYABLE_ERRORS),
)
async def _persist_with_retry(task_id: str, job_id: str, endpoint: str | None, base_url: str | None) -> None:
    if _provider_job_id_store is None:
        raise RuntimeError("provider_job_id store not installed: lib.generation.generation_queue was never imported")
    await _provider_job_id_store().persist_provider_job_id(task_id, job_id, endpoint=endpoint, base_url=base_url)


async def persist_provider_job_id(
    task_id: str,
    job_id: str,
    *,
    provider: str,
    endpoint: str | None = None,
    base_url: str | None = None,
) -> None:
    """Call immediately after submit: persist job_id to the DB so a restart can resume.

    The caller passes task_id explicitly; ``endpoint`` is the protocol identifier (protocol dimension, only custom providers
    have it, recording which protocol this provider task was submitted with), ``base_url`` is the domain the request was
    actually sent to (connection dimension, common to both kinds of providers; resume uses it to replay polling against the
    original domain). Both land in the same write as job_id. Transient DB errors are retried up to 3 times; business
    exceptions raise immediately. When retries are exhausted it raises, and the worker's finally marks it failed (fail-fast).

    Submit 之后立即调：把 job_id 持久化到 DB 让重启可接续。

    Caller 显式传 task_id；``endpoint`` 是协议标识（协议维度，只有自定义供应商有，记录本笔供应商
    任务按哪套协议提交），``base_url`` 是请求实际发往的域名（连接维度，两类供应商通用，续跑据此
    回放原域名轮询）。两者与 job_id 同一次写入落地。DB 瞬态错误最多重试 3 次，业务异常立即抛。
    重试用尽抛异常，由 worker finally 兜底 mark_failed（fail-fast）。
    """
    try:
        await _persist_with_retry(task_id, job_id, endpoint, base_url)
        logger.info("provider_job_id 已持久化 task_id=%s provider=%s job_id=%s", task_id, provider, job_id)
    except Exception as exc:
        logger.error(
            "provider_job_id_persist_failed task_id=%s provider=%s job_id=%s error=%s",
            task_id,
            provider,
            job_id,
            exc,
        )
        raise


class ProviderJobIdPersistenceMixin:
    """The single point for persisting provider_job_id in submit-poll video backends.

    Each backend calls ``self._persist_provider_job_id`` after submit returns a job_id in ``generate()`` to write it back;
    persistence timing (after submit, before poll), skipping on None (non-worker path) and fail-fast semantics are
    concentrated at this single call point. Submit-poll backends get the capability by inheriting this mixin without
    handling persistence conditions themselves.

    ``provider`` is still passed explicitly by the backend as a PROVIDER_* constant rather than derived from ``self.name``:
    gemini's ``name`` is ``gemini-aistudio`` / ``gemini-vertex``, which differs from ``PROVIDER_GEMINI`` (``gemini``) used
    for billing/log attribution; taking the name automatically would rewrite the provider field of the persistence log.

    提交-轮询型 video backend 的 provider_job_id 持久化收口点。

    各 backend 在 ``generate()`` 内 submit 拿到 job_id 后调 ``self._persist_provider_job_id``
    统一写回；持久化时机（submit 后、poll 前）、None 跳过（非 worker 路径）、fail-fast 语义集中
    于此单一调用点。提交-轮询型 backend 继承本 mixin 即得能力，无需自己处理持久化条件。

    ``provider`` 仍由 backend 显式传 PROVIDER_* 常量而非从 ``self.name`` 推：gemini 的
    ``name`` 是 ``gemini-aistudio`` / ``gemini-vertex``，与计费/日志归因用的 ``PROVIDER_GEMINI``
    （``gemini``）不同，自动取 name 会改写持久化日志的 provider 字段。
    """

    async def _persist_provider_job_id(
        self,
        request: VideoGenerationRequest,
        job_id: str,
        *,
        provider: str,
        endpoint: str | None = None,
    ) -> None:
        """Call immediately after a successful submit: the worker path writes back job_id; the non-worker path (task_id=None) skips.

        Also writes back, per dimension, the endpoint info used by this submit: the protocol identifier comes from
        ``request.execution_endpoint`` (injected by the custom provider wrapper before forwarding; built-in providers lack this
        dimension and it is always None), the actual request domain from the ``endpoint`` parameter (passed by backends whose
        submit domain varies with user config; resume uses it to replay polling against the original domain). Both kinds of
        providers use the same writing method, and the domain always lands in ``submitted_base_url``. Persistence failures
        raise (transient DB errors were already retried 3 times inside ``persist_provider_job_id``) and the worker's finally
        marks it failed -- keeping the existing fail-fast semantics (ADR 0007).

        submit 成功后立即调：worker 路径写回 job_id，非 worker 路径（task_id=None）跳过。

        同时按维度分列写回该笔提交所用的端点信息：协议标识取 ``request.execution_endpoint``（由
        自定义供应商的包装层在转发前注入，内置供应商无此维度、恒 None），实际请求域名取参数
        ``endpoint``（由提交域名随用户配置变化的 backend 传入，续跑据此回放原域名轮询）。
        两类供应商共用同一套写法，域名一律落 ``submitted_base_url``。持久化失败抛出（DB 瞬态错误
        已在 ``persist_provider_job_id`` 内重试 3 次），由 worker finally 兜底 mark_failed ——
        保持现有 fail-fast 语义（ADR 0007）。
        """
        if request.task_id is not None:
            await persist_provider_job_id(
                request.task_id,
                job_id,
                provider=provider,
                endpoint=request.execution_endpoint,
                base_url=endpoint,
            )
        if request.on_provider_resubmit_unsafe is not None:
            request.on_provider_resubmit_unsafe()


def is_retryable_http_status(status_code: int, *, retry_not_found: bool = False) -> bool:
    """HTTP status code → whether retryable.

    Transient errors are always retried: 408 Request Timeout / 425 Too Early / 429 Too Many Requests / 5xx.
    404 fails fast by default (deterministic "does not exist", e.g. a misspelled endpoint); polling/download pass
    retry_not_found=True and retry it as "briefly not ready after task submit / resource not propagated". Other 4xx
    (400/401/403/422 etc.) are deterministic client errors and always fail fast -- retrying would only drag on to the
    max_wait timeout, wasting a worker slot.

    HTTP 状态码 → 是否可重试。

    瞬态错误恒重试：408 Request Timeout / 425 Too Early / 429 Too Many Requests / 5xx。
    404 默认快速失败（确定性"不存在"，如端点拼错）；轮询/下载场景传 retry_not_found=True，
    按"任务提交后短暂未就绪 / 资源未传播"重试。其余 4xx（400/401/403/422 等）确定性客户端
    错误一律快速失败——重试只会拖到 max_wait 超时，白占 worker 槽。
    """
    if status_code in (408, 425, 429):
        return True
    if 500 <= status_code <= 599:
        return True
    if status_code == 404:
        return retry_not_found
    return False


# The subset of httpx transport errors where "the request definitely wasn't delivered": failure during connection establishment / never got a connection / proxy handshake failure.
# Safe to retry -- the server can't have created a task / billed. Transport errors in the read/write phase and after (ReadTimeout,
# WriteError, connection dropped midway, RemoteProtocolError, etc.) may have reached the server; they're ambiguous and not in this list.
# httpx 传输错误中「请求确定未送达」的子集：连接建立阶段失败 / 从未取得连接 / 代理握手失败。
# 重试安全——服务端不可能已建任务 / 已计费。读/写阶段及之后的传输错误（ReadTimeout、
# WriteError、连接中途断开、RemoteProtocolError 等）请求可能已抵达服务端，归歧义态，不在此列。
_NOT_SENT_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    # proxy connect/handshake failure: the request never left the client→proxy leg and didn't reach the target provider
    httpx.ProxyError,  # 代理连接/握手失败：请求从未离开客户端→代理段，未抵达目标供应商
)

# Local/protocol errors that definitely fail before any request bytes are sent: unsupported URL scheme (UnsupportedProtocol)
# or local request construction violating the protocol (LocalProtocolError). Neither risks double billing, and retrying until max_wait
# won't help -- the poll path fails fast, the submit path raises as is (not ambiguous, no "request may have been delivered" wrapper).
# Note RemoteProtocolError is a sibling ProtocolError subclass, but it means "server disconnected midway" and is not in this list.
# 请求字节发出前就确定失败的本地/协议错误：URL scheme 不受支持（UnsupportedProtocol）
# 或本地请求构造违反协议（LocalProtocolError）。二者既无重复计费风险，重试到 max_wait
# 也不会变好——poll 路径快速失败、submit 路径原样抛出（非歧义态，不套「请求可能已送达」）。
# 注意 RemoteProtocolError 是其同级 ProtocolError 子类，但属「服务端中途断开」，不在此列。
_NON_RETRYABLE_LOCAL_ERRORS: tuple[type[Exception], ...] = (
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
)


def should_retry_submit(exc: Exception) -> bool:
    """Retry predicate for the create/submit phase (non-idempotent "create + bill" POST).

    Key difference from ``should_retry_poll``: transport errors are only retried for the "request definitely not delivered"
    subset (``_NOT_SENT_TRANSPORT_ERRORS``: connection establishment failure / never got a connection / proxy handshake
    failure), so retries never create duplicate tasks or double bill. Ambiguous states (ReadTimeout etc., "the request may
    have been processed by the server") are wrapped by ``submit_post`` into a terminal ``AmbiguousSubmitError`` failure,
    for which (and for all business exceptions) this predicate returns False. HTTPStatusError is gated explicitly by
    status_code: 5xx/408/425/429 are retried (the server explicitly says creation failed), 404 and deterministic 4xx fail fast.

    创建/提交阶段（非幂等「创建 + 计费」POST）重试谓词。

    与 ``should_retry_poll`` 的关键区别：传输错误只重试「请求确定未送达」的子集
    （``_NOT_SENT_TRANSPORT_ERRORS``：连接建立失败 / 从未取得连接 / 代理握手失败），重试不会重复建
    任务、不会重复计费。歧义态（ReadTimeout 等「请求可能已被服务端处理」）由
    ``submit_post`` 包成 ``AmbiguousSubmitError`` 终态失败，本谓词对其（及一切业务异常）
    返回 False。HTTPStatusError 按 status_code 显式闸门：5xx/408/425/429 重试（服务端
    明示创建失败），404 与确定性 4xx 快速失败。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return is_retryable_http_status(exc.response.status_code, retry_not_found=False)
    return isinstance(exc, _NOT_SENT_TRANSPORT_ERRORS)


def should_retry_poll(exc: Exception) -> bool:
    """Retry predicate for the poll/download phase (idempotent GET).

    Retrying idempotent queries has no side effects, so transport/network errors (RequestError) and base transient errors
    are always retried; only local/protocol errors (``_NON_RETRYABLE_LOCAL_ERRORS``: UnsupportedProtocol /
    LocalProtocolError) definitely fail before the request is sent, won't improve by retrying until max_wait, and fail fast.
    HTTPStatusError is gated by status_code, with 404 treated as "briefly not ready after task submit / resource not
    propagated" and retried. HTTPStatusError messages contain the URL/task_id, whose "500"/"503" substrings would be
    misjudged by the string fallback, so explicit status_code judgement is used to sidestep that; business exceptions such
    as ResumeExpiredError always fail fast.

    轮询/下载阶段（幂等 GET）重试谓词。

    幂等查询重试无副作用，故传输/网络错误（RequestError）与基础瞬态错误一律重试；唯本地/
    协议错误（``_NON_RETRYABLE_LOCAL_ERRORS``：UnsupportedProtocol / LocalProtocolError）在
    请求发出前就确定失败，重试到 max_wait 也不会变好，快速失败。HTTPStatusError 按
    status_code 闸门，404 视为"任务提交后短暂未就绪 / 资源未传播"重试。HTTPStatusError 消息
    含 URL/task_id，其中 "500"/"503" 子串会被字符串兜底误判，故走显式 status_code 判定绕开；
    ResumeExpiredError 等业务异常一律快速失败。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return is_retryable_http_status(exc.response.status_code, retry_not_found=True)
    if isinstance(exc, _NON_RETRYABLE_LOCAL_ERRORS):
        return False
    return isinstance(exc, (httpx.RequestError, *BASE_RETRYABLE_ERRORS))


def should_retry_signed_download(exc: Exception) -> bool:
    """Retry predicate for pre-signed URL downloads: all 4xx are deterministic failures.

    A signed URL is fully usable the moment it's issued, so 403/404 can only mean a bad signature or a missing object, and
    retrying until max won't help. Only 5xx/408/425/429 and transport/network errors are retried (retrying an idempotent
    GET has no side effects). HTTPStatusError is gated explicitly by status_code, sidestepping the string fallback's
    misjudgement of substrings like "503"/"timeout" in the result URL.

    预签发 URL 下载重试谓词：4xx 一律确定性失败。

    签名 URL 在签发那一刻即完整可用，403/404 只可能是签名错误或对象不存在，重试到 max
    也不会变好。只重试 5xx/408/425/429 与传输/网络错误（幂等 GET 重试无副作用）。
    HTTPStatusError 按 status_code 显式闸门，绕开字符串兜底对结果 URL 中 "503"/"timeout"
    等子串的误判。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return is_retryable_http_status(exc.response.status_code, retry_not_found=False)
    if isinstance(exc, _NON_RETRYABLE_LOCAL_ERRORS):
        return False
    return isinstance(exc, (httpx.RequestError, *BASE_RETRYABLE_ERRORS))


def should_retry_download(exc: Exception) -> bool:
    """Retry predicate for video artifact downloads (idempotent GET of the result URL issued after the provider task succeeded).

    On top of :func:`should_retry_signed_download` it additionally retries 403/404: the artifact not yet ready after the
    terminal state or the CDN not yet synced is a real shape seen in sampling (ark ``video_not_ready``); a wrongly written
    URL itself is caught by the endpoint test before saving.

    视频产物下载重试谓词（幂等 GET 取 provider 任务成功后签发的结果 URL）。

    在 :func:`should_retry_signed_download` 之上额外重试 403/404：终态后产物尚未就绪、
    CDN 未同步是抽样中的真实形态（ark ``video_not_ready``），URL 本身写错由端点测试在保存
    前兜住。
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 404):
        return True
    return should_retry_signed_download(exc)


async def submit_post(
    post_fn: Callable[[], Awaitable[httpx.Response]],
    *,
    provider: str,
    request: VideoGenerationRequest | None = None,
    include_provider_reason: bool = True,
) -> httpx.Response:
    """Unified wrapper for the create/submit phase (non-idempotent POST): route failures by "was the request definitely delivered".

    - Connection/proxy establishment failure (ConnectError/ConnectTimeout/PoolTimeout/ProxyError): the request definitely
      wasn't delivered → raise as is, for ``should_retry_submit`` to retry.
    - Local/protocol errors (UnsupportedProtocol/LocalProtocolError): fail definitely before the request is sent, no billing
      risk → raise as is, ``should_retry_submit`` fails fast (not ambiguous, no "request may have been delivered" wrapper).
    - Other transport errors (ReadTimeout/WriteError/RemoteProtocolError, etc.): the request may have been processed by the
      server → raise ``AmbiguousSubmitError`` as a terminal failure, not retried, to avoid duplicate tasks + double billing.
    - A >=400 response: log the body first (to diagnose 413 etc.), then ``raise_for_status`` raises HTTPStatusError, for
      ``should_retry_submit`` to route by status_code.

    Used together with ``with_retry_async(retry_if=should_retry_submit)``: the decorator handles retries, this wrapper turns
    ambiguous states into non-retryable terminal exceptions before retrying.

    When ``request`` is passed, the received response body is recorded. Task creation failures happen before polling
    starts; if not recorded here they would never be recorded -- and that's exactly the kind of failure that most needs the
    provider's original text.

    create/提交阶段（非幂等 POST）统一包装：按「请求是否确定送达」给失败分流。

    - 连接/代理建立失败（ConnectError/ConnectTimeout/PoolTimeout/ProxyError）：请求确定未送达
      → 原样抛出，交 ``should_retry_submit`` 重试。
    - 本地/协议错误（UnsupportedProtocol/LocalProtocolError）：请求发出前就确定失败、无计费
      风险 → 原样抛出，由 ``should_retry_submit`` 快速失败（非歧义态，不套「请求可能已送达」）。
    - 其余传输错误（ReadTimeout/WriteError/RemoteProtocolError 等）：请求可能已被服务端
      处理 → 抛 ``AmbiguousSubmitError`` 终态失败，不重试，避免重复建任务 + 重复计费。
    - 收到 >=400 响应：先落 body 日志（诊断 413 等），再 ``raise_for_status`` 抛
      HTTPStatusError，交 ``should_retry_submit`` 按 status_code 分流。

    与 ``with_retry_async(retry_if=should_retry_submit)`` 配套使用：装饰器负责重试，
    本包装负责把歧义态在重试前转成不可重试的终态异常。

    传了 ``request`` 就把收到的响应体留痕。建任务失败发生在轮询开始之前，不在这里记就永远
    记不到——而那正是最需要供应商原文的一类失败。
    """
    try:
        resp = await post_fn()
    except httpx.RequestError as exc:
        # The request definitely wasn't delivered -- a connection establishment failure (transient, retryable) or a local/protocol error (deterministic, fail fast) --
        # neither risks duplicate tasks / double billing; raise as is for should_retry_submit to route; no ambiguous-state wrapper.
        # 请求确定未送达——连接建立失败（瞬态、可重试）或本地/协议错误（确定性、快速失败）——
        # 均无重复建任务 / 重复计费风险，原样抛出交 should_retry_submit 分流；不套歧义态。
        if isinstance(exc, (*_NOT_SENT_TRANSPORT_ERRORS, *_NON_RETRYABLE_LOCAL_ERRORS)):
            raise
        raise AmbiguousSubmitError(provider=provider) from exc
    if request is not None:
        await notify_provider_response(request, "submit", _response_body_or_text(resp))
    if resp.status_code >= 400:
        logger.warning("%s create 返回 %s: %s", provider, resp.status_code, redact_provider_text(resp.text)[:500])
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if include_provider_reason and is_provider_rejection(resp.status_code):
                raise provider_rejected_error(exc, provider_reason=provider_reason_summary(resp)) from None
            raise redacted_status_error(exc) from None
    return resp


def _dig(payload: object, path: tuple[str | int, ...]) -> object | None:
    """Walk dict keys / list indices along path (int segments are list indices); returns None if any level is missing.

    按 path 逐层走 dict key / list 下标（int 段表 list 下标），任一层缺失返回 None。"""
    cur: object = payload
    for seg in path:
        if isinstance(seg, int):
            if not isinstance(cur, list) or seg >= len(cur):
                return None
            cur = cur[seg]
        else:
            if not isinstance(cur, dict) or seg not in cur:
                return None
            cur = cur[seg]
    return cur


def first_str_by_paths(payload: object, paths: tuple[tuple[str | int, ...], ...]) -> str | None:
    """Try each path in priority order and take the first non-empty string value (ints are tolerated and stringified).

    When vendors' response structures differ, a priority-ordered path table is used to take values tolerantly, rather than
    writing a branch for each shape.

    按优先级逐个试取第一个非空字符串值（int 容忍并 str 化）。

    各家回包结构不一致时，用一张按优先级排序的路径表容错取值，而不是为每种形状写一条分支。
    """
    for path in paths:
        val = _dig(payload, path)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, int) and not isinstance(val, bool):
            return str(val)
    return None


def first_mapping_by_paths(payload: object, paths: tuple[tuple[str | int, ...], ...]) -> dict | None:
    """Try each path in priority order and take the first dict value; returns None if none found.

    Like ``first_str_by_paths``, for grouped substructures in the response (e.g. metadata) -- when the shape varies by
    deployment it goes through the same priority table as status and video URL, rather than a separate set of shape branches.

    按优先级逐个试取第一个 dict 值；取不到返回 None。

    同 ``first_str_by_paths``，用于回包里成组的子结构（如 metadata）——形状随部署变化时
    与状态、视频地址走同一张优先级表，不各写一套形状分支。
    """
    for path in paths:
        val = _dig(payload, path)
        if isinstance(val, dict):
            return val
    return None


# Common locations of error descriptions: a flat error and data.error inside a wrapper body. / 错误描述的常见落点：扁平 error 与包装体内的 data.error。
_ERROR_PATHS: tuple[tuple[str | int, ...], ...] = (("error",), ("data", "error"))


def extract_provider_error_message(state: object) -> str:
    """Best-effort extraction of the provider error description from the response (message/name from a dict, or a plain string); returns unknown if none.

    从回包里尽力取供应商错误描述（dict 取 message/name，或直接是字符串）；取不到返回 unknown。"""
    for path in _ERROR_PATHS:
        err = _dig(state, path)
        if isinstance(err, dict):
            # The two fields are judged separately: when message is blank or not a string, still fall through to name; don't skip the fallback too.
            # 两个字段各自判定：message 为空白或非字符串时仍要落到 name，别把回退一并跳过。
            for value in (err.get("message"), err.get("name")):
                if isinstance(value, str) and value.strip():
                    return value.strip()
        elif isinstance(err, str) and err.strip():
            return err.strip()
    return "unknown"


async def poll_with_retry[T](
    *,
    poll_fn: Callable[[], Awaitable[T]],
    is_done: Callable[[T], bool],
    is_failed: Callable[[T], str | None],
    max_wait: float,
    poll_interval: float = VIDEO_POLL_INTERVAL_SECONDS,
    retryable_errors: tuple[type[Exception], ...] = BASE_RETRYABLE_ERRORS,
    retry_if: Callable[[Exception], bool] | None = None,
    label: str = "",
    on_progress: Callable[[T, float], None] | None = None,
    clock: AsyncClock | None = None,
) -> T:
    """Generic async polling helper with transient error retry and timeout control.

    After `VIDEO_POLL_MAX_CONSECUTIVE_FAILURES` consecutive retryable errors (with no successful response in between) it
    raises RuntimeError as a terminal failure; any successful response resets the count. Retry waits back off as
    `poll_interval × 2^k`, capped at `VIDEO_POLL_MAX_BACKOFF_SECONDS`; when the response carries an integer-seconds
    `Retry-After` not exceeding the cap it takes precedence. The failure budget governs "provider unreachable", `max_wait`
    governs "provider reachable but slow". Every wait is cut at the `max_wait` deadline, so the last poll is always sent
    within budget.

    The failure budget applies to all consumers, video and image backends alike: on the image side
    `lib/backends/image_backends/vidu.py` and `lib/backends/kling_backend_base.py` likewise stop when consecutive
    failures reach the limit, without using up their own `max_wait` windows.

    Args:
        poll_fn: async function called on each poll, returns the latest status.
        is_done: decides whether the poll result means the task is done.
        is_failed: decides whether the poll result means the task failed, returning an error message or None.
        max_wait: max wait time (seconds); raises TimeoutError on timeout.
        poll_interval: poll interval after a successful response, also the base of failure backoff; video backends all use the default 5 seconds.
        retryable_errors: tuple of retryable exception types (effective when retry_if is not given).
        retry_if: custom retry predicate; when given it replaces the default `should_retry`, letting the caller control
            precisely which exceptions should be retried (e.g. distinguishing deterministic 4xx from transient 5xx by HTTP status_code).
        label: log prefix (e.g. "Ark", "Gemini").
        on_progress: optional progress callback, called after each non-terminal poll.
        clock: seam for monotonic timing and async waits; when not passed, the clock bound to the current context is taken via
            ``resolve_clock``, the production default being the system clock.

    通用异步轮询辅助函数，带瞬态错误重试和超时控制。

    连续可重试错误（其间无一次成功响应）满 `VIDEO_POLL_MAX_CONSECUTIVE_FAILURES` 次即抛
    RuntimeError 终态失败，任一成功响应清零。重试等待按 `poll_interval × 2^k` 退避、封顶
    `VIDEO_POLL_MAX_BACKOFF_SECONDS`；响应带整数秒且不超过该封顶的 `Retry-After` 时优先采用。
    失败预算管「供应商不可达」，`max_wait` 管「供应商可达但慢」。任何一次等待都截到 `max_wait`
    的截止时刻，故最后一次轮询发出时必定仍在预算内。

    失败预算对全部消费方生效，视频与图片两条通道同此一份：图片侧的 `lib/backends/image_backends/vidu.py`
    与 `lib/backends/kling_backend_base.py` 同样在连续失败满额时终止，不会用满各自的 `max_wait` 窗口。

    Args:
        poll_fn: 每次轮询调用的异步函数，返回最新状态。
        is_done: 判断轮询结果是否表示任务完成。
        is_failed: 判断轮询结果是否表示任务失败，返回错误信息或 None。
        max_wait: 最大等待时间（秒），超时抛出 TimeoutError。
        poll_interval: 成功响应后的轮询间隔，同时是失败退避的基数；视频调用通道统一用默认 5 秒。
        retryable_errors: 可重试的异常类型元组（未指定 retry_if 时生效）。
        retry_if: 自定义重试谓词，指定时替代默认的 `should_retry`，让调用方精确控制
            哪些异常应当重试（如按 HTTP status_code 区分确定性 4xx 与瞬态 5xx）。
        label: 日志前缀（如 "Ark"、"Gemini"）。
        on_progress: 可选的进度回调，每次非终态轮询后调用。
        clock: 单调计时与异步等待 seam；未传时按 ``resolve_clock`` 取当前上下文绑定的时钟，
            生产默认是系统时钟。
    """
    active_clock = resolve_clock(clock)
    start = active_clock.monotonic()
    prefix = f"{label} " if label else ""
    predicate = retry_if if retry_if is not None else (lambda e: should_retry(e, retryable_errors))
    consecutive_failures = 0

    # Query first, then wait: tasks already done / cache hits return immediately without wasting a poll_interval.
    # 先查询再等待：已完成/缓存命中的任务立刻返回，不被 poll_interval 白等一轮。
    while True:
        try:
            result = await poll_fn()
        except Exception as e:
            if not predicate(e):
                raise
            consecutive_failures += 1
            logger.warning("%s轮询异常（将重试）: %s - %s", prefix, type(e).__name__, str(e)[:200])
            if consecutive_failures >= VIDEO_POLL_MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(
                    f"{prefix}polling failed {VIDEO_POLL_MAX_CONSECUTIVE_FAILURES} times in a row, last error: {e}"
                ) from e
            retry_after = _retry_after_seconds(e)
            wait_time = (
                retry_after
                if retry_after is not None
                else min(
                    poll_interval * 2 ** (consecutive_failures - 1),
                    VIDEO_POLL_MAX_BACKOFF_SECONDS,
                )
            )
        else:
            consecutive_failures = 0
            error_msg = is_failed(result)
            if error_msg is not None:
                raise RuntimeError(error_msg)
            if is_done(result):
                return result
            if on_progress is not None:
                on_progress(result, active_clock.monotonic() - start)
            wait_time = poll_interval

        remaining = max_wait - (active_clock.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError(f"{prefix}task timed out ({max_wait:.0f}s)")
        # Waits don't exceed the remaining budget: the next poll is always sent at or before the max_wait deadline.
        # 等待不越过剩余预算：下一次轮询必定发在 max_wait 截止时刻或之前。
        await active_clock.sleep(min(wait_time, remaining))


def _retry_after_seconds(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    seconds = int(raw)
    return seconds if 0 <= seconds <= VIDEO_POLL_MAX_BACKOFF_SECONDS else None


def url_origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) triple; the port is filled with the scheme's default, for same-origin checks.

    (scheme, host, port) 三元组；端口按 scheme 补默认值，供同源判定使用。"""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None)
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


#: Max hops when following redirects manually, same as httpx's default. / 手动跟随重定向时的跳数上限，与 httpx 的缺省一致。
_MAX_REDIRECTS = 20


def _redirect_location(response: httpx.Response) -> str | None:
    return response.headers.get("location") if response.is_redirect else None


def _rewrites_to_get(status_code: int, method: str) -> bool:
    """Whether following a redirect should rewrite the method to GET and drop the request body (same rules as httpx / RFC 9110).

    303 rewrites every method except HEAD; 301 / 302 only rewrite POST -- methods like PUT keep their method and body on
    these two, and rewriting them would make the endpoint receive a request with completely different semantics. 307 / 308
    always resend as is.

    Only decides method rewriting; the request body for cross-origin hops is dropped separately by :func:`request_with_scoped_credentials`.

    跟随重定向时该不该把方法改写成 GET 并丢掉请求体（与 httpx / RFC 9110 同规则）。

    303 对除 HEAD 外的一切方法改写；301 / 302 只改写 POST——PUT 等方法在这两档上保留方法
    与请求体，改写它们会让端点收到一个语义完全不同的请求。307 / 308 一律原样重发。

    只判方法改写；跨源续跳的请求体由 :func:`request_with_scoped_credentials` 另行卸掉。
    """
    if status_code == 303:
        return method != "HEAD"
    if status_code in (301, 302):
        return method == "POST"
    return False


def _without_query(url: str) -> str:
    """URL with the query string and userinfo removed: credentials passed in the query, signatures and Basic-auth passwords are there,
    and they shouldn't go into error messages and logs.

    去掉查询串与 userinfo 的 URL：按 query 传的凭证、签名与 Basic 认证的口令都在那里，
    不该进错误消息与日志。"""
    return str(httpx.URL(url).copy_with(query=None, userinfo=b""))


#: Character limit of the rejection reason summary. Upstream error bodies can be thousands of characters long, while the summary goes into the task
#: failure envelope and is returned to the user with the task list, so it must be bounded; the excess is truncated and marked with :data:`_PROVIDER_REASON_ELLIPSIS`.
#: 拒因摘要的字符上限。上游错误体可以长达数千字符，而摘要要落进任务失败信封、随任务列表
#: 回传给用户，必须有界；超限部分以 :data:`_PROVIDER_REASON_ELLIPSIS` 收尾标记截断。
PROVIDER_REASON_MAX_CHARS = 300

_PROVIDER_REASON_ELLIPSIS = "…"

#: Field names in the error body carrying "why it was rejected", taking the first non-empty string in order.
#: 错误体里承载「为什么被拒」的字段名，按顺序取第一个非空字符串。
_REASON_MESSAGE_KEYS = ("message", "msg", "error_msg", "detail", "description", "reason")

#: Common outer containers of error objects: the rejection reason is often wrapped under these keys. / 错误对象常见的外层容器：拒因往往裹在这些键下面。
_REASON_NESTED_KEYS = ("error", "output", "data", "detail", "result")

#: The machine code at the same level as the rejection reason. It points to the triggered rejection rule better than natural language, so when found it's prepended to the summary.
#: 与拒因同层的机器码。它比自然语言更能指向被触发的那条拒绝规则，取到就前置进摘要。
_REASON_CODE_KEYS = ("code", "error_code", "type")

#: Authentication status codes: 401 unauthenticated, 407 proxy unauthenticated. These response bodies produce no rejection reason summary.
#: 认证类状态码：401 未认证、407 代理未认证。这类响应体不产出拒因摘要。
_CREDENTIAL_STATUS_CODES = frozenset({401, 407})

#: Max container drill-down depth, guarding against deep recursion on malformed error bodies. / 容器下钻深度上限，防畸形错误体上的深递归。
_REASON_MAX_DEPTH = 3

#: URLs or any URL-like token with a query string. Providers occasionally omit the scheme; the query may still be a credential or signature parameter.
#: URL 或任何带查询串的 URL-like token。供应商偶尔省略 scheme；query 仍可能是凭证或签名参数。
_REASON_URL_RE = re.compile(r"https?://[^\s\"'<>]+|[^\s\"'<>?]+\?[^\s\"'<>]+", re.IGNORECASE)


def _reason_code(body: Mapping[str, object]) -> str | None:
    """Take the machine code at the same level as the rejection reason; ``bool`` doesn't count as a code (in JSON ``True`` is a flag, not an error number).

    取与拒因同层的机器码；``bool`` 不算码（JSON 里 ``True`` 是标志位而非错误号）。"""
    for key in _REASON_CODE_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _reason_text(payload: object, depth: int = 0) -> str | None:
    """Take the "why it was rejected" sentence from the parsed error body; returns ``None`` if not found.

    Only recognizes message fields and a few outer containers without serializing the whole thing: error bodies often echo
    the request back verbatim (including the full prompt), and serializing it all would carry that into the user-visible summary.

    从解析后的错误体里取「为什么被拒」那句话，取不到返回 ``None``。

    只认消息字段与少数外层容器，不整体序列化：错误体常把请求原样回显回来（含 prompt 全文），
    整体序列化会把它一并带进用户可见的摘要。
    """
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict) or depth >= _REASON_MAX_DEPTH:
        return None
    body = cast(dict[str, object], payload)
    text: str | None = None
    for key in _REASON_MESSAGE_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    if text is None:
        for key in _REASON_NESTED_KEYS:
            if key in body:
                text = _reason_text(body[key], depth + 1)
                if text:
                    break
    if not text:
        return None
    code = _reason_code(body)
    return f"{code}: {text}" if code and code not in text else text


def _strip_url_queries(text: str) -> str:
    """Remove the query string, fragment and userinfo of every absolute URL in the text: pre-signed parameters and credentials passed in the
    query are in the query string, and the ``https://TOKEN@host`` style of stuffing a token into the authority can only be stopped by clearing
    the whole part -- the generic redaction that follows only recognizes the ``user:password@`` form and can't mask a bare token without a colon.

    去掉文本里每个绝对 URL 的查询串、片段与 userinfo：预签名参数与按 query 传的凭证都在
    查询串里，而 ``https://TOKEN@host`` 这种把令牌塞进权限段的写法只有整段清掉才拦得住——
    随后的通用脱敏只认得出 ``user:password@`` 形态，遮不住没有冒号的裸令牌。"""

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0).rstrip(".,;:!?)]}\"'")
        trailing = match.group(0)[len(raw) :]
        try:
            stripped = str(httpx.URL(raw).copy_with(query=None, fragment=None, userinfo=b""))
        except (httpx.InvalidURL, ValueError, UnicodeError):
            stripped = "<url>"
        return stripped + trailing

    return _REASON_URL_RE.sub(_replace, text)


def redact_provider_text(value: object) -> str:
    """Redacted form of provider original text: first strip the query string of each URL, then mask credentials.

    Both layers are needed: ``redact_diagnostic_text`` only recognizes common credential parameter names, so vendor-private
    parameters on pre-signed URLs must be caught by stripping the whole query string; while credential echoes outside query
    strings are only recognized by the former.

    供应商原文的脱敏形态：先剥掉每个 URL 的查询串，再遮蔽凭证。

    两层缺一不可：``redact_diagnostic_text`` 只认得出常见的凭证参数名，预签名 URL 上
    的厂商私有参数要靠查询串整体剥离兜住；而查询串之外的凭证回显只有前者认得出。
    """
    if not isinstance(value, str):
        # Non-strings only appear in the recording fallback branch: there's no URL text to strip, so let the generic masker stringify it
        # 非字符串只出现在留痕的兜底分支：没有 URL 文本可剥，交通用遮蔽器统一 str 化
        return redact_diagnostic_text(value)
    return redact_diagnostic_text(_strip_url_queries(value))


def is_provider_rejection(status_code: int) -> bool:
    """Whether this status code counts as "submit rejected upstream": deterministic 4xx, excluding the few that are transient and retryable.

    Independent of whether there's a summary -- the rejection itself decides the read side's copy and follow-up action (fix the input rather than retry).

    该状态码是否算「提交被上游拒绝」：确定性 4xx，不含瞬态可重试的那几个。

    与摘要有无无关——拒绝这件事本身决定读侧的文案与后续动作（修改输入而非重试）。
    """
    return 400 <= status_code < 500 and not is_retryable_http_status(status_code, retry_not_found=False)


def provider_reason_summary(response: httpx.Response) -> str | None:
    """Distill an upstream deterministic 4xx response body into a redacted, truncated rejection reason summary; returns ``None`` if nothing usable.

    Only produced for 4xx: 5xx and transport errors say "upstream is unavailable right now", their bodies have no actionable
    info for the user, and merging them into the error message would only make a transient fault look like a definite rejection.

    Redaction has two layers: first remove each URL's query string (pre-signed parameters and query-passed credentials are
    there, same rule as ``_without_query``), then ``redact_diagnostic_text`` masks Authorization / Bearer / quoted secret
    values etc., sharing one set of rules with the log side.

    把上游确定性 4xx 的响应体提炼成脱敏、截断后的拒因摘要；无可用内容返回 ``None``。

    只对 4xx 产出：5xx 与传输错误说的是「上游此刻不可用」，其响应体对用户没有可行动的信息，
    并入错误信息只会把一次瞬态故障写成看似确定的拒绝。

    脱敏分两层：先去掉每个 URL 的查询串（预签名参数与按 query 传的凭证都在那里，与
    ``_without_query`` 同口径），再交 ``redact_diagnostic_text`` 遮蔽 Authorization / Bearer /
    引号包裹的 secret 等值，与日志侧共用一套规则。
    """
    if not is_provider_rejection(response.status_code):
        return None
    if response.status_code in _CREDENTIAL_STATUS_CODES:
        # The response body of an auth failure is about the credential itself, and upstream often echoes the whole key in the sentence; such rejection reasons
        # carry no actionable info beyond "the credential wasn't accepted", and that is already conveyed by the status code in the localized copy.
        # 认证失败的响应体讲的就是凭证本身，上游常把整把密钥回显在句子里；这类拒因除了
        # 「凭证没被接受」没有别的可行动信息，而那一层已由本地化文案的状态码给出。
        return None
    try:
        body_text = response.text
    except httpx.ResponseNotRead:
        # Streaming response not yet aread: on the error path there's no readable body, and we don't send another request for it.
        # 流式响应尚未 aread：错误路径上没有可读的体，不为此再发一次请求。
        return None
    try:
        payload: object = response.json()
    except ValueError:
        reason = body_text
    else:
        reason = _reason_text(payload)
    return summarize_provider_reason(reason)


def summarize_provider_reason(reason: object) -> str | None:
    """Redact the upstream rejection reason text, collapse whitespace and truncate; returns ``None`` if nothing usable.

    HTTP response bodies (:func:`provider_reason_summary`) and gRPC status details share the same rules.

    把上游拒因原文脱敏、压平空白并截断；无可用内容返回 ``None``。

    HTTP 响应体（:func:`provider_reason_summary`）与 gRPC 状态详情共用同一套口径。
    """
    if not reason:
        return None
    summary = " ".join(redact_provider_text(reason).split())
    if not summary:
        return None
    if len(summary) <= PROVIDER_REASON_MAX_CHARS:
        return summary
    return summary[: PROVIDER_REASON_MAX_CHARS - len(_PROVIDER_REASON_ELLIPSIS)] + _PROVIDER_REASON_ELLIPSIS


@dataclass(frozen=True)
class _Hop:
    """The target of one request and its accompanying credentials. On cross-origin hops the credentials are dropped entirely.

    一次请求的目标与随行凭证。跨源跳转时把凭证整个卸掉。"""

    url: str
    headers: Mapping[str, str] | None
    params: Mapping[str, str] | None = None
    #: Credentials passed in the query. ``Location`` replaces the query string wholesale, so they must be re-attached on same-origin hops.
    #: 按 query 传的凭证。``Location`` 会整体替换查询串，同源续跳时要重新贴回去。
    auth_query: Mapping[str, str] | None = None

    def redirected_to(self, location: str, credential_origin: tuple[str, str, int | None]) -> _Hop:
        target = httpx.URL(self.url).join(location)
        if url_origin(str(target)) != credential_origin:
            # Cross-origin: drop the headers, query credentials and the original request's params together. / 跨源：请求头、查询凭证与原请求的 params 一并卸掉。
            return _Hop(str(target), None, None, None)
        # Same origin: credentials are still in scope. The original request's params are not replayed (the redirect target carries its own query string), but
        # query-passed credentials must be re-added -- otherwise a `/jobs` → `/jobs/` normalizing redirect would lose the api_key.
        # Merge them into the target URL's query string rather than via params: the latter replaces the whole string, wiping the parameters Location brought.
        # 同源：凭证仍在作用域内。原请求的 params 不重放（重定向目标自带查询串），但按 query
        # 传的凭证必须补回——否则一次 `/jobs` → `/jobs/` 的规范化跳转就会丢掉 api_key。
        # 合并进目标 URL 的查询串而不是走 params：后者会整串替换，把 Location 自带的参数冲掉。
        if self.auth_query:
            target = target.copy_merge_params(dict(self.auth_query))
        return _Hop(str(target), self.headers, None, self.auth_query)


@dataclass(frozen=True)
class _Body:
    """The body of one request. When a redirect rewrites to GET or hops cross-origin it's dropped entirely; the three fields are never half-dropped.

    一次请求的请求体。跟随重定向改写成 GET 或跨源续跳时整个丢掉，三个字段不会各丢一半。"""

    json: object | None = None
    files: Mapping[str, Any] | None = None
    data: Mapping[str, Any] | None = None


async def request_with_scoped_credentials(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None,
    json: object | None,
    auth_query: Mapping[str, str] | None = None,
    files: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """Send a request and follow redirects hop by hop ourselves, dropping the credentials in ``headers`` when leaving this request's origin.

    httpx's ``follow_redirects`` only strips ``Authorization`` cross-origin; custom header names (``X-API-Key`` and the
    like) would be sent as is to the redirect target. Every request carrying a rendered auth section must go through here
    rather than relying on the client's automatic following.

    The credential scope is ``url``'s own origin, not some external base: the address the caller specified is where the
    credential goes, and what must be prevented is the server using ``Location`` to lead it elsewhere.

    Besides dropping credentials, cross-origin hops don't forward the request body either: 307 / 308 keep the method, but
    the body likewise stays within the credential scope. Same-origin hops are handled per :func:`_rewrites_to_get`, with
    307 / 308 resent as is.

    ``files`` / ``data`` are multipart bodies, mutually exclusive with ``json``; they're resent as is on same-origin hops, so
    ``files`` content must be bytes rather than file handles -- after a handle is read to the end, the second hop would send an empty body.

    发一次请求并自行逐跳跟随重定向，跳出本请求的源时卸掉 ``headers`` 里的凭证。

    httpx 的 ``follow_redirects`` 跨源只摘 ``Authorization``，自定义头名（``X-API-Key`` 之类）
    会原样送到重定向目标。凡是携带渲染出的 auth 节的请求都要走这里，而不是交给客户端自动跟随。

    凭证的作用域取 ``url`` 自己的源，而不是某个外部基准：调用方指定的地址就是凭证的去处，
    需要防的是服务端用 ``Location`` 把它引到别处。

    跨源续跳除卸掉凭证外也不转发请求体：307 / 308 保留方法，但请求体同样只在凭证作用域内。
    同源续跳按 :func:`_rewrites_to_get` 的规则处置，307 / 308 原样重发。

    ``files`` / ``data`` 走 multipart 的请求体，与 ``json`` 三选一；同源续跳时原样重发，故
    ``files`` 的内容要是字节而不是文件句柄——句柄读到结尾后第二跳会发出一个空体。
    """
    credential_origin = url_origin(url)
    # The first hop's auth.query is already on the url, so params aren't added again; they're only re-added on same-origin hops.
    # 首跳的 auth.query 已经拼在 url 上，params 不重复带；只在同源续跳时补回。
    hop = _Hop(url, headers, None, auth_query)
    current_method = method.upper()
    body = _Body(json, files, data)
    for _ in range(_MAX_REDIRECTS + 1):
        response = await client.request(
            current_method,
            hop.url,
            headers=hop.headers,
            params=hop.params,
            json=body.json,
            files=body.files,
            data=body.data,
            follow_redirects=False,
        )
        location = _redirect_location(response)
        if location is None:
            return response
        if _rewrites_to_get(response.status_code, current_method):
            current_method = "GET"
            body = _Body()
        hop = hop.redirected_to(location, credential_origin)
        if url_origin(hop.url) != credential_origin:
            body = _Body()
    raise RuntimeError(f"request exceeded {_MAX_REDIRECTS} redirects: {_without_query(url)}")


async def stream_to_file(
    client: httpx.AsyncClient,
    url: str,
    output_path: Path,
    *,
    max_bytes: int,
    # passed to httpx as the I/O timeout config, not an async cancellation deadline
    timeout: int = 120,  # noqa: ASYNC109 -- 转交 httpx 的 I/O 超时配置，非 async 取消 deadline
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
    credential_origin: tuple[str, str, int | None] | None = None,
    auth_query: Mapping[str, str] | None = None,
) -> None:
    """Stream the URL content into a local file, without retries -- retries are handled uniformly by :func:`with_artifact_retry`.

    When the body exceeds ``max_bytes`` it aborts and raises :class:`ArtifactTooLargeError`, leaving no fragment at the
    artifact path; for error responses only the first ``ERROR_BODY_MAX_BYTES`` bytes of the body are read. The outbound
    destination is constrained by the ``client`` passed in by the caller (see
    :func:`lib.backends.artifact_download_guard.artifact_http_client`).

    ``credential_origin`` gives the only origin the credentials in ``headers`` may be sent to. When given, redirects are
    followed hop by hop here, and ``headers`` are dropped entirely when hopping to another origin: httpx only strips
    ``Authorization`` cross-origin, while an endpoint definition's auth section can use any header name like ``X-API-Key``,
    and leaving it to ``follow_redirects`` would send those credentials as is to the redirect target (object storage / CDN).

    Only raw bytes are written, no faststart: this function runs within :func:`with_artifact_retry`'s wall-clock budget; the
    caller remuxes the video via :func:`faststart_video_artifact` after the retry returns.

    把 URL 内容流式写入本地文件，不含重试——重试由 :func:`with_artifact_retry` 统一承担。

    响应体超过 ``max_bytes`` 即中止并抛 :class:`ArtifactTooLargeError`，产物路径上不留残片；
    错误响应的响应体只读前 ``ERROR_BODY_MAX_BYTES`` 字节。出站目的地由调用方传入的 ``client``
    约束（见 :func:`lib.backends.artifact_download_guard.artifact_http_client`）。

    ``credential_origin`` 给出 ``headers`` 里的凭证只许发往哪个源。给了它就自行逐跳跟随
    重定向，跳到别的源时把 ``headers`` 整个丢掉：httpx 跨源只摘 ``Authorization``，而端点
    定义的 auth 节可以用 ``X-API-Key`` 之类的任意头名，交给 ``follow_redirects`` 会把这些
    凭证原样送到重定向目标（对象存储 / CDN）去。

    只落原样字节，不做 faststart：本函数在 :func:`with_artifact_retry` 的墙钟预算之内运行，
    视频的重封装由调用方在重试返回之后经 :func:`faststart_video_artifact` 进行。
    """

    async def _write(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            # Swap in a copy with the (truncated) body already read, so HTTPStatusError.response.text is usable
            # 换成读完（且截断）响应体的副本，HTTPStatusError.response.text 才可用
            resp = await buffered_error_response(resp)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The artifact URL's query string may be a signature or a query-passed credential -- neither should go into logs or task records.
            # 产物地址的查询串可能是签名，也可能是按 query 传的凭证——都不该进日志与任务记录。
            raise redacted_status_error(exc) from None
        await stream_body_to_file(resp, output_path, max_bytes=max_bytes)

    if credential_origin is None:
        async with client.stream("GET", url, timeout=timeout, headers=headers, params=params) as resp:
            await _write(resp)
        return

    hop = _Hop(url, headers, params, auth_query)
    for _ in range(_MAX_REDIRECTS + 1):
        async with client.stream(
            "GET",
            hop.url,
            timeout=timeout,
            headers=hop.headers,
            params=hop.params,
            follow_redirects=False,
        ) as resp:
            location = _redirect_location(resp)
            if location is None:
                # A 3xx without Location has nowhere to go: raise_for_status lets 3xx through, and writing directly would store the redirect
                # response body as the artifact. Raise and leave it to the retry budget.
                # 3xx 没给 Location 就无处可跳：raise_for_status 放行 3xx，直接落盘会把跳转
                # 响应体当成产物存下来。抛错交给重试预算。
                if resp.is_redirect:
                    raise RuntimeError(f"redirect without a Location header: {_without_query(hop.url)}")
                await _write(resp)
                return
        hop = hop.redirected_to(location, credential_origin)
    raise RuntimeError(f"artifact download exceeded {_MAX_REDIRECTS} redirects: {_without_query(url)}")


#: Wall-clock cap for artifact downloads. Termination is mainly the failure budget's job (10 consecutive + backoff capped at 60s, about 435s total);
#: this value only covers the case "connects every time, just slow". Video backends override it with `poll_timeout_seconds`;
#: image / audio backends lack that dimension and use this default.
#: 产物下载的墙钟上限。终止主要由失败预算负责（连续 10 次 + 退避封顶 60s，累计约 435s），
#: 本值只兜住「每次都连得上、只是慢」的情形。视频通道传 `poll_timeout_seconds` 覆盖它，
#: 图片 / 音频通道没有该维度，用本缺省。
ARTIFACT_DOWNLOAD_MAX_WAIT_SECONDS = 1800


async def with_artifact_retry[T](
    attempt: Callable[[], Awaitable[T]],
    *,
    label: str,
    retry_if: Callable[[Exception], bool] | None = should_retry_download,
    retryable_errors: tuple[type[Exception], ...] = BASE_RETRYABLE_ERRORS,
    max_wait: float = ARTIFACT_DOWNLOAD_MAX_WAIT_SECONDS,
) -> T:
    """Retry one artifact fetch with the budget shared with polling.

    Artifact download and polling are both "idempotent fetches after the provider task has been created", so they use the
    same termination conditions: terminal failure after ``VIDEO_POLL_MAX_CONSECUTIVE_FAILURES`` consecutive failures,
    exponential backoff capped at ``VIDEO_POLL_MAX_BACKOFF_SECONDS``, and ``Retry-After`` taking precedence when present.
    All backends (built-in video / image / audio and the declarative runtime) share this entry and the same download retry constants.

    HTTP-style backends use the default ``should_retry_download`` gated by status_code; SDK-style backends' download
    exceptions are not ``HTTPStatusError``, so they pass ``retry_if=None`` to fall back to ``retryable_errors``.

    按与轮询共用的预算重试一次产物取件。

    产物下载与轮询同属「供应商任务已建成后的幂等取件」，故用同一套终止条件：连续失败满
    ``VIDEO_POLL_MAX_CONSECUTIVE_FAILURES`` 次即终态失败、指数退避封顶
    ``VIDEO_POLL_MAX_BACKOFF_SECONDS``、响应带 ``Retry-After`` 时优先采用。全部调用通道
    （内置视频 / 图片 / 音频与声明式运行时）共用本入口及同一份下载重试常量。

    HTTP 式通道用缺省的 ``should_retry_download`` 按 status_code 闸门；SDK 式通道的下载
    异常不是 ``HTTPStatusError``，传 ``retry_if=None`` 退到按 ``retryable_errors`` 判定。
    """

    # Wrap the fetch result in a one-element tuple: `poll_with_retry` decides terminal state via is_done, while a fetch is a success as long as it
    # didn't raise; the result itself (None, empty bytes, etc.) takes no part in the decision.
    # 单元素元组包住取件结果：`poll_with_retry` 按 is_done 判终态，而取件只要没抛异常就是
    # 成功，结果本身（None、空 bytes 等）不参与判定。
    async def once() -> tuple[T]:
        return (await attempt(),)

    # The wall-clock cap wraps the whole retry: `poll_with_retry` only compares against max_wait after each poll_fn returns,
    # while a single fetch can itself take a long time to return (connects, just slow) -- exactly the case this cap covers.
    # 墙钟上限套在整个重试之外：`poll_with_retry` 只在每次 poll_fn 返回后才比对 max_wait，
    # 而一次取件本身就可能长时间不返回（连得上、只是慢），那正是本上限要兜的情形。
    async with asyncio.timeout(max_wait):
        fetched = await poll_with_retry(
            poll_fn=once,
            is_done=lambda _fetched: True,
            is_failed=lambda _fetched: None,
            max_wait=max_wait,
            retryable_errors=retryable_errors,
            retry_if=retry_if,
            label=label,
        )
    return fetched[0]


async def faststart_video_artifact(output_path: Path) -> None:
    """When the artifact on disk is mp4 / mov / m4v, best-effort faststart (move moov before mdat) so the browser can start playing
    without downloading to the end of the file; if the bundled ffmpeg is unavailable or remuxing fails or times out, the original bytes are kept.

    Called after :func:`with_artifact_retry` returns, outside the download's wall-clock budget: the video backend budget is
    ``poll_timeout_seconds``, as low as 60 seconds, and if remuxing were cancelled at budget expiry, an artifact already
    downloaded successfully would be voided along with it and generation would fail. Remuxing has its own deadline and
    atomically replaces the artifact via a temp file; on failure the original file is unchanged.

    已落盘的产物是 mp4 / mov / m4v 时尽力做 faststart（moov 挪到 mdat 之前），浏览器不必
    下载到文件尾即可开播；随包 ffmpeg 不可用、重封装失败或超时都保留原样字节。

    在 :func:`with_artifact_retry` 返回之后调用，不放进下载的墙钟预算：视频通道的预算取
    ``poll_timeout_seconds``，最低只有 60 秒，重封装若被预算到期取消，已经下载成功的产物会
    随之作废、生成失败。重封装有自己的期限，经临时文件原子替换产物，失败时原文件不变。
    """
    if output_path.suffix.lower() in FASTSTART_SUFFIXES:
        # A downloaded video must continue to be committed; remuxing is still bounded by its internal subprocess deadline.
        # 已下载的成片必须继续提交；重封装仍受其内部子进程期限约束。
        await run_noninterruptible_async(ensure_faststart(output_path))


async def download_video(
    url: str,
    output_path: Path,
    *,
    label: str = "",
    # passed to httpx as the I/O timeout config, not an async cancellation deadline
    timeout: int = 120,  # noqa: ASYNC109 -- 转交 httpx 的 I/O 超时配置，非 async 取消 deadline
    retry_if: Callable[[Exception], bool] | None = should_retry_download,
    retryable_errors: tuple[type[Exception], ...] = BASE_RETRYABLE_ERRORS,
    max_wait: float = ARTIFACT_DOWNLOAD_MAX_WAIT_SECONDS,
) -> None:
    """Stream a video from a URL into a local file, with retries via the shared artifact download budget; best-effort faststart after writing.

    从 URL 流式下载视频到本地文件，重试走共用的产物下载预算；落盘后尽力做 faststart。"""

    async def attempt() -> None:
        async with artifact_http_client(follow_redirects=True) as http_client:
            await stream_to_file(http_client, url, output_path, max_bytes=VIDEO_ARTIFACT_MAX_BYTES, timeout=timeout)

    await with_artifact_retry(
        attempt, label=label, retry_if=retry_if, retryable_errors=retryable_errors, max_wait=max_wait
    )
    await faststart_video_artifact(output_path)


async def download_resumable_video(url: str, output_path: Path, *, label: str) -> None:
    """Download the video of a resumable task; when the budget is exhausted, convert to a stable failure that allows retrying the download.

    ``NonRetryableError`` (destination not allowed, over size cap) is raised as is: re-fetching wouldn't change the result.

    下载可续跑任务的成片；预算耗尽转成可重试下载的稳定失败。

    ``NonRetryableError``（目的地不合规、超出体积上限）原样抛出：重新取件结果不会变。
    """
    try:
        await download_video(url, output_path, label=label)
    except NonRetryableError:
        raise
    except Exception as exc:
        raise ArtifactDownloadError(detail=str(exc)) from exc


def reference_audio_to_data_uri(path: Path, *, model: str, mime_types: Mapping[str, str]) -> str:
    """Reference audio → base64 data URI; unsupported format or unreadable file always raises.

    Audio can't be "skipped if missing" like reference images: "audio N" in the prompt is numbered by the order of audio
    items in the content array, and skipping one would shift all later numbers, putting one character's voice on another
    character -- silently wrong, and billed as usual.

    ``mime_types`` is passed by each backend: vendors accept different MIME spellings for the same extension (mp3 has both
    ``audio/mp3`` and ``audio/mpeg``), and merging the tables would send one of them an unverified MIME.

    参考音频 → base64 data URI；格式不受支持或文件不可读一律抛错。

    音频不能像参考图那样「缺失即跳过」：prompt 里的「音频N」按 content 数组中音频条目的
    出现顺序编号，跳过一段会让其后所有编号整体前移，把某个角色的音色安到另一个角色头上
    ——错得无声无息，且照常扣费。

    ``mime_types`` 由各 backend 传入：同一个扩展名各家接受的 MIME 写法不一致（mp3 有
    ``audio/mp3`` 与 ``audio/mpeg`` 两种口径），合表会让其中一家收到没验证过的 MIME。
    """
    # Only video backends reach here; the deferred import lets image and audio backends use runtime support without loading the video contract.
    # 只有视频调用通道会调到这里；延迟导入让图像、音频通道使用运行支持时无需加载视频契约。
    from arcreel_market_core.video_backend_contract import VideoCapabilityError

    mime = mime_types.get(path.suffix.lower())
    if mime is None:
        raise VideoCapabilityError(
            "video_reference_audio_format_unsupported",
            model=model,
            name=path.name,
            supported=", ".join(sorted(mime_types)),
        )
    try:
        return file_to_data_uri(path, mime)
    except OSError as exc:
        raise VideoCapabilityError("video_reference_audio_unreadable", model=model, names=path.name) from exc


async def notify_provider_response(request: VideoGenerationRequest, stage: ProviderResponseStage, body: object) -> None:
    """Send the provider response of an HTTP-style backend and its phase to the optional diagnostic callback.

    Recording is diagnostic data and takes no part in business parsing: write failures are only logged, so a generation
    already accepted by the provider (most likely already billed) doesn't fail because the diagnostic column can't be written.

    The response text is redacted once here: recordings are persisted and read back via the API, and upstream bodies may
    carry signed URLs or echo credentials in fields. Doing it in this function rather than at each call site means all
    recording points share the same redaction. The per-string rule is ``redact_provider_text``, the same as the rejection
    reason summary and failure logs: the generic masker only recognizes common credential parameter names, and
    vendor-private query parameters must be caught by stripping the whole query string.

    把 HTTP 式调用通道的供应商响应及其阶段送到可选诊断回调。

    留痕是诊断数据，不参与业务解析：写入失败只记日志，不让一笔已被供应商受理（多半已计费）
    的生成因为诊断列写不进去而失败。

    响应原文在这里脱敏一次：留痕会落库并经 API 回读，而上游响应体可能带回签名 URL 或把
    凭证回显在字段里。收口在本函数而非各调用点，所有留痕点都共享同一套脱敏。逐字符串
    的口径与拒因摘要、失败日志同为 ``redact_provider_text``：通用遮蔽器只认得出常见的凭证
    参数名，厂商私有的查询参数要靠查询串整体剥离兜住。
    """
    await notify_provider_response_to(request.on_provider_response, stage, body, label=request.task_id)


async def notify_provider_response_to(
    sink: Callable[[ProviderResponseStage, object], Awaitable[None]] | None,
    stage: ProviderResponseStage,
    body: object,
    *,
    label: str | None = None,
) -> None:
    """Same as above, but only takes the callback itself: image requests have no ``task_id``, so the identifier has to be supplied by the caller.

    同上，但只收回调本身：图像请求没有 ``task_id``，只能把标识交给调用方给。"""
    if sink is None:
        return
    try:
        await sink(stage, sanitize_diagnostic_payload(body, redact_text=redact_provider_text))
    except Exception:
        logger.warning("供应商响应留痕写入失败 task_id=%s", label, exc_info=True)


def recording_poll[T](
    poll_fn: Callable[[], Awaitable[T]],
    request: VideoGenerationRequest,
    *,
    stage: ProviderResponseStage = "poll",
) -> Callable[[], Awaitable[T]]:
    """Wrap a poll or secondary fetch: record each provider response received, before status and error interpretation.

    Terminal failures are raised by the ``is_failed`` predicate inside :func:`poll_with_retry`, and HTTP error responses are
    raised by ``poll_fn`` itself; both happen before ``poll_with_retry`` returns. If recording were placed after polling,
    it would end up only on successful calls, leaving the failed calls that most need diagnosis empty.

    包一层轮询或二次取件：每收到一次供应商响应就留痕，早于状态与错误解读。

    终态失败由 ``is_failed`` 谓词在 :func:`poll_with_retry` 内部抛出、HTTP 错误响应则从
    ``poll_fn`` 自己抛出，两者都发生在 ``poll_with_retry`` 返回之前。留痕若放在轮询之后，
    恰好只在成功调用上留下，最需要诊断的失败调用反而为空。
    """

    async def once() -> T:
        try:
            body = await poll_fn()
        except httpx.HTTPStatusError as exc:
            await notify_provider_response(request, stage, _response_body_or_text(exc.response))
            raise
        await notify_provider_response(request, stage, body)
        return body

    return once


def resume_expiry_gate[T](
    poll_fn: Callable[[], Awaitable[T]],
    *,
    resume_job_id: str | None,
    provider: str,
) -> Callable[[], Awaitable[T]]:
    """Expiry gate for resume polling: an HTTP 404 when querying by job id turns into ``ResumeExpiredError``.

    :func:`should_retry_poll` retries a polling 404 as "just submitted, query side not ready"; a resumed task was submitted
    long ago, so 404 means it no longer exists on the provider side, and retrying would only exhaust the failure budget and
    end as an ordinary failure. When ``resume_job_id`` is None (new submit path) ``poll_fn`` is returned as is, and 404 is
    still handled by the retry predicate.

    Recording must be wrapped inside the gate, i.e. pass the product of :func:`recording_poll` as ``poll_fn``: the gate
    turns 404 into ResumeExpiredError, and wrapped outside it wouldn't see that response.

    续跑轮询的过期闸门：按 job id 查询得到 HTTP 404 即转 ``ResumeExpiredError``。

    :func:`should_retry_poll` 把轮询 404 当作「刚提交、查询端未就绪」重试；续跑的任务早已提交，
    404 说明它在供应商侧已不存在，重试只会耗尽失败预算、以普通失败收场。``resume_job_id`` 为
    None（新提交路径）时原样返回 ``poll_fn``，404 仍交重试谓词处理。

    留痕要包在闸门里侧，即把 :func:`recording_poll` 的产物作为 ``poll_fn`` 传入：闸门把 404
    换成 ResumeExpiredError，包在外侧就看不到那个响应。
    """
    if resume_job_id is None:
        return poll_fn
    from arcreel_market_core.video_backend_contract import ResumeExpiredError

    async def gated() -> T:
        try:
            return await poll_fn()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ResumeExpiredError(job_id=resume_job_id, provider=provider) from exc
            raise

    return gated


def _response_body_or_text(response: httpx.Response) -> object:
    """Response body: store the structure if it parses as JSON, otherwise the original text (truncation is handled uniformly at the recording boundary).

    响应体：能解析成 JSON 就存结构，否则存原文（截断由留痕边界统一负责）。"""
    try:
        return response.json()
    except ValueError:
        return response.text
