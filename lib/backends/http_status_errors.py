"""Shared exceptions for provider HTTP failures: redacted status errors, artifact fetch failures and submit ambiguous state.

Independent of the backend layer: shared modules below the backends such as ``lib.backends.vidu_shared`` also raise
redacted status errors, and placing them in some backend package would make those modules downstream of the backend
layer; likewise ``lib.billing.call_failure`` (the ledger layer's failure classification) -- it recognizes "download
failure" by exception type, and the ledger layer is below the backend layer.

供应商 HTTP 失败的共享异常：脱敏后的状态错误、产物取件失败与提交歧义态。

独立于 backend 层：``lib.backends.vidu_shared`` 等 backend 之下的共享模块也要抛脱敏后的状态错误，
放在某个通道包里会把它们拖成 backend 层的下游；``lib.billing.call_failure``
（记账层的失败分类）同理——它按异常类型认出「下载失败」，而记账层在 backend 层之下。
"""

from __future__ import annotations

import httpx


class ProviderRejectedError(httpx.HTTPStatusError):
    """Upstream deterministic 4xx rejection, optionally carrying a redacted, truncated rejection reason summary.

    Still an ``httpx.HTTPStatusError``: retry predicates and existing ``except httpx.HTTPStatusError`` branches judge by
    status_code as usual; it only adds a ``provider_reason`` for the persisting side to pick up separately -- the summary
    is upstream original text, stored separately from localized copy, so the read side can show it verbatim rather than
    stuffing it into a translation template.

    The summary may be absent (empty response body, auth status codes not passed through, no fields recognized); then
    ``provider_reason`` is ``None``: the rejection itself must still be persisted in structured form, otherwise the read
    side doesn't get the status code and the follow-up action "fix the input".

    上游确定性 4xx 拒绝，可另带脱敏截断后的拒因摘要。

    仍是 ``httpx.HTTPStatusError``：重试谓词与既有 ``except httpx.HTTPStatusError`` 分支
    照常按 status_code 判定，只是多出一个 ``provider_reason`` 供落库侧单独取用——摘要是
    上游原文，与本地化文案分开存放，读侧才能把它按原文展示而不塞进译文模板。

    摘要可以缺席（响应体为空、认证类状态码不透传、字段全不认得），此时 ``provider_reason``
    为 ``None``：拒绝这件事本身仍要结构化落库，否则读侧拿不到状态码与「修改输入」的后续动作。
    """

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        provider_reason: str | None = None,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.provider_reason = provider_reason


class ArtifactDownloadError(RuntimeError):
    """The provider task succeeded and only the artifact download was exhausted; fetching can be retried by continuing the original task.

    供应商任务已成功、仅产物下载耗尽，可接续原任务重试取件。"""

    code = "artifact_download_failed"

    def __init__(self, *, detail: str) -> None:
        self.params = {"detail": detail}
        super().__init__(detail)


class AmbiguousSubmitError(RuntimeError):
    """Ambiguous-state failure during create/submit (non-idempotent "create + bill").

    The request may already have reached the server and been persisted + billed, but the response was lost in transit
    (ReadTimeout, write timeout, connection dropped midway, RemoteProtocolError, etc.). Retrying automatically would create
    duplicate tasks + duplicate billing, so it is not retried and fails terminally; error_message carries a
    ``[create_ambiguous]`` prefix prompting ops to confirm the task status on the provider side before retrying manually
    (consistent with the ``[restart_lost]`` / ``[resume_unsupported]`` precedent of "rather retry manually than bill twice";
    agent-facing, exempt from i18n).

    create/submit（非幂等的「创建 + 计费」）阶段的歧义态失败。

    请求可能已抵达服务端并已落库 + 已计费，但响应在途丢失（ReadTimeout、写超时、
    连接中途断开、RemoteProtocolError 等）。此时自动重试会重复建任务 + 重复计费，故
    不重试、直接终态失败；error_message 带 ``[create_ambiguous]`` 前缀提示运维到供应商侧
    确认任务状态后再手动重试（与 ``[restart_lost]`` / ``[resume_unsupported]``
    「宁可手动重试、不可重复计费」先例一致，agent-facing 豁免 i18n）。
    """

    def __init__(self, *, provider: str, message: str = "") -> None:
        self.provider = provider
        super().__init__(
            message
            or f"[create_ambiguous] {provider} 创建请求可能已送达服务端但响应在途丢失"
            "（读超时/连接中途断开），为避免重复建任务与重复计费不自动重试；"
            "请到供应商侧确认任务状态后再手动重试"
        )


def redacted_status_error(exc: httpx.HTTPStatusError, *, provider_reason: str | None = None) -> httpx.HTTPStatusError:
    """The same response with a message that contains no query string or userinfo.

    ``raise_for_status`` writes the whole request URL into the exception message, and backends that pass credentials in the
    query would render the api_key in that URL; a Basic-auth-style base_url also leaves the password in the userinfo part;
    that message ends up via ``str(exc)`` in ``task.error_message``, logs and API responses.
    Type and ``response`` are kept as is, so retry predicates judge by status_code as usual.

    A non-empty ``provider_reason`` turns it into :class:`ProviderRejectedError`; when the summary is absent but it really is
    a rejected submit, construct that subclass directly with :func:`provider_rejected_error`.

    同一个响应，换一条不含查询串与 userinfo 的消息。

    ``raise_for_status`` 把整条请求 URL 写进异常消息，而按 query 传凭证的通道会把 api_key
    渲染在那条 URL 里，Basic 认证式的 base_url 还会把口令留在 userinfo 段；该消息经
    ``str(exc)`` 落进 ``task.error_message``、日志与 API 响应。
    类型与 ``response`` 原样保留，重试谓词照常按 status_code 判定。

    给了非空 ``provider_reason`` 就换成 :class:`ProviderRejectedError`；摘要缺席但确实是
    一次提交被拒时，用 :func:`provider_rejected_error` 直接构造该子类。
    """
    if provider_reason:
        return provider_rejected_error(exc, provider_reason=provider_reason)
    return httpx.HTTPStatusError(_redacted_message(exc), request=exc.request, response=exc.response)


def provider_rejected_error(exc: httpx.HTTPStatusError, *, provider_reason: str | None) -> ProviderRejectedError:
    """Deterministic 4xx submit rejection: the same response with a message containing no query string or userinfo; the summary may be absent.

    Used whether or not there is a summary: the read side recognizes "rejected" by exception type and renders localized
    copy plus FIX_INPUT accordingly; routing only by whether a summary exists would make empty response bodies and
    non-passed-through auth failures fall back to a bare status line.

    确定性 4xx 的提交被拒：同一响应，换一条不含查询串与 userinfo 的消息，摘要可缺席。

    摘要有无都走这条：读侧靠异常类型认出「被拒」，据此渲染本地化文案并给出 FIX_INPUT；
    只按摘要有无分流会让空响应体与不透传的认证失败退回裸状态行。
    """
    return ProviderRejectedError(
        _redacted_message(exc), request=exc.request, response=exc.response, provider_reason=provider_reason
    )


def _redacted_message(exc: httpx.HTTPStatusError) -> str:
    # Strip userinfo along with the query string: base_url can be entered by the user, and when written as https://user:pw@host the password is
    # in the authority part; httpx keeps it as is, and this message would end up in task.error_message and be returned with the task list.
    # userinfo 与查询串一并剥掉：base_url 允许用户自填，写成 https://user:pw@host 时口令就在
    # 权限段里，httpx 原样保留它，这条消息又会落进 task.error_message 并随任务列表回传。
    return f"{exc.response.status_code} response for {exc.request.url.copy_with(query=None, userinfo=b'')}"


def raise_for_status_redacted(response: httpx.Response) -> None:
    """Validate the response status; on failure raise an ``HTTPStatusError`` without query string or userinfo.

    校验响应状态；失败时抛出不含查询串与 userinfo 的 ``HTTPStatusError``。"""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise redacted_status_error(exc) from None
