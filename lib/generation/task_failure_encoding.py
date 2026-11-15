"""Encode task execution exceptions into the persisted ``error_message``.

The two task execution paths, regular execution (``GenerationWorker._process_task``) and resume
(``VideoResumeRunner.run``), catch the same set of exceptions and share this encoding, so the same handling doesn't
drift into two copies. Encoding primitives and read-side rendering: see ``lib.generation.task_failure``.

把任务执行异常编码为落库的 ``error_message``。

常规执行（``GenerationWorker._process_task``）与续跑（``VideoResumeRunner.run``）两条任务执行路径
捕获同一批异常，共用这份编码，避免同一处理漂移成两份。编码原语与读侧渲染见
``lib.generation.task_failure``。
"""

from __future__ import annotations

import logging
from typing import Any

from arcreel_market_core.video_backend_contract import VideoCapabilityError
from lib.backends.grok_shared import grpc_rejection
from lib.backends.http_status_errors import ArtifactDownloadError, ProviderRejectedError
from lib.backends.image_backends.base import ImageCapabilityError, ImageOutputFormatError, ImageOutputMissingError
from lib.billing.call_failure import CallErrorCode, classify_call_failure
from lib.catalog.errors import ModelNotInCatalogError
from lib.config.resolver import ImageBucketCapabilityError, VideoBucketCapabilityError
from lib.custom_provider.comfyui.failures import ComfyuiError
from lib.custom_provider.declarative_backend import DeclarativeRuntimeError
from lib.generation.task_failure import encode_failure
from lib.generation.video_request_facts import VideoRequestFactsError
from lib.infra.api_errors import ApiError
from lib.references.reference_compression import ReferencePayloadFloorError
from lib.script.reference_video.execution_checkpoint import ReferenceExecutionIdentityError
from lib.script.reference_video.request_projection import ReferenceProjectionBlockedError
from lib.script.script_editor import ScriptEditError

logger = logging.getLogger(__name__)


def encode_task_failure_message(exc: Exception) -> str:
    """ScriptEditError, deterministic upstream 4xx rejections and structured execution rejections are structured as
    code/params; other exceptions keep str(exc).

    Only machine codes are persisted, and localised text is rendered by the read side per ``Accept-Language`` -- the worker
    background has no request context, and rendering here would lock the task's failure reason into a single language.

    ScriptEditError、上游确定性 4xx 拒绝与结构化执行拒绝走 code/params 结构化，其余异常沿用 str(exc)。

    落库只存机器码，本地化文案由读侧按 ``Accept-Language`` 渲染——worker 后台无 request
    上下文，在这里渲染会把任务的失败原因锁死成单一语言。
    """
    if isinstance(exc, ScriptEditError):
        # If it cannot be encoded fall back to the generic script_edit_error, keeping the "it was a script edit failure" level of information.
        # 编不出来时退到通用 script_edit_error，保住"是剧本编辑失败"这一层信息。
        return _try_encode_failure(exc.key, exc.params) or encode_failure("script_edit_error")
    if isinstance(exc, ApiError):
        return _try_encode_failure(exc.key, exc.params) or str(exc)
    if isinstance(exc, ProviderRejectedError):
        # Deterministic upstream 4xx: status code and redacted summary each take one parameter. The summary is upstream's
        # original text and does not go into the translation template -- the read side shows it as is from a separate field in
        # error_params, and only the outer wording changes with Accept-Language. When the summary is absent only the status code
        # is stored: the rejection itself must still be structured, so the read side has localised text and FIX_INPUT.
        # 上游确定性 4xx：状态码与脱敏摘要各占一个参数。摘要是上游原文，不进译文模板——
        # 读侧按 error_params 里的独立字段原样展示，只有外层措辞随 Accept-Language 变。
        # 摘要缺席时只落状态码：拒绝本身仍要结构化，读侧才有本地化文案与 FIX_INPUT。
        params: dict[str, Any] = {"status": exc.response.status_code}
        if exc.provider_reason:
            params["provider_reason"] = exc.provider_reason
        return _try_encode_failure("provider_rejected", params) or str(exc)
    if (rejection := grpc_rejection(exc)) is not None:
        # Deterministic rejections on the gRPC channel (xAI) are encoded the same as HTTP 4xx: the debug representation of AioRpcError is not stored.
        # gRPC 通道（xAI）的确定性拒绝与 HTTP 4xx 同一编码：不落 AioRpcError 的调试表示。
        status, reason = rejection
        grpc_params: dict[str, Any] = {"status": status}
        if reason:
            grpc_params["provider_reason"] = reason
        return _try_encode_failure("provider_rejected", grpc_params) or str(exc)
    if isinstance(
        exc,
        ArtifactDownloadError
        | ImageCapabilityError
        | ImageOutputMissingError
        | ImageOutputFormatError
        | VideoCapabilityError
        | ReferencePayloadFloorError
        | ImageBucketCapabilityError
        | ModelNotInCatalogError
        | VideoBucketCapabilityError
        | ReferenceProjectionBlockedError
        | VideoRequestFactsError
        | ReferenceExecutionIdentityError
        | DeclarativeRuntimeError
        | ComfyuiError,
    ):
        # Structured execution rejections have no generic fallback code to fall back to, so fall back to str(exc) (i.e. the code
        # itself) -- unstructured text is passed through as is on the read side, so the reason is not lost.
        # 结构化执行拒绝没有通用兜底 code 可退，退回 str(exc)（即 code 本身）——
        # 非结构化文本在读侧原样透传，不会丢失原因。
        return _try_encode_failure(exc.code, exc.params) or str(exc)
    return str(exc)


def is_provider_refusal(exc: BaseException) -> bool:
    """Whether the provider truly rejected the content or inputs of this request: resending as is would only be rejected the same way; the input should change.

    Same criterion as :func:`encode_task_failure_message` storing ``provider_rejected`` (deterministic 4xx on the httpx
    channel, deterministic rejections on gRPC), plus content policy codes in the upstream error body (same criterion as the
    accounting layer's ``content_policy``). Judged layer by layer along the ``__cause__`` chain: callers often wrap the SDK
    exception before raising. All other exceptions don't count as rejection.

    供应商是否真正拒绝了这次请求的内容或输入：原样重发只会被同样拒绝，该改输入。

    与 :func:`encode_task_failure_message` 落 ``provider_rejected`` 的判据同口径（httpx 通道的确定性 4xx、
    gRPC 的确定性拒绝），另认上游错误体里的内容策略码（与记账层 ``content_policy`` 同一判据）。
    沿 ``__cause__`` 链逐层判：调用方常把 SDK 异常包一层再抛。其余异常都不算拒绝。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ProviderRejectedError) or grpc_rejection(current) is not None:
            return True
        seen.add(id(current))
        current = current.__cause__
    return classify_call_failure(exc).error_code == CallErrorCode.CONTENT_POLICY


def _try_encode_failure(code: str, params: dict[str, Any]) -> str | None:
    """Structurally encode the failure reason; return None if it cannot be encoded, for the caller to degrade.

    An encoding exception must never interrupt mark_task_failed, otherwise the task would be stuck in running.

    结构化编码失败原因，编不出来返回 None 交调用方降级。

    编码异常绝不能打断 mark_task_failed，否则任务会卡死在 running。
    """
    try:
        return encode_failure(code, **params)
    except KeyError:
        # code not registered in FAILURE_CODE_KEYS -- the two lists are kept in sync by convention, not runtime validation.
        # code 未登记进 FAILURE_CODE_KEYS——两份清单靠约定同步而非运行时校验。
        logger.warning("失败 code 未登记进 FAILURE_CODE_KEYS，降级为通用失败原因: code=%s", code)
    except (TypeError, ValueError, RecursionError):
        # params cannot be JSON-serialised (TypeError: key types even default=str cannot convert;
        # ValueError: json.dumps's default check_circular detected a circular reference;
        # RecursionError: containers nested too deep, and default=str doesn't handle the container itself).
        # params 无法 JSON 序列化（TypeError：default=str 也转不了的键类型；
        # ValueError：json.dumps 默认 check_circular 检出循环引用；
        # RecursionError：嵌套过深的容器，default=str 不接管容器本身）。
        logger.warning("失败 params 无法序列化，降级为通用失败原因: code=%s", code)
    return None
