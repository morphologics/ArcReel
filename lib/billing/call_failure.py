"""Machine-stable codes for provider call failures (ledger layer).

Besides the original text (``error_message``), a failed ``api_calls`` row also stores a machine code and a set of
parameters: the read side uses them to render the failure phrase in the current language, so historical failure
reasons get translated after switching language. Unrecognized exceptions keep only the original text, with
``error_code`` and ``error_params`` left empty -- the read side shows it as is.

**Does not share an enum** with the task failure codes of :mod:`lib.generation.task_failure`: each code there needs a
server-side translation in the ``lib/i18n`` errors table (registration enforced by ``FAILURE_CODE_KEYS``), and rendering
happens in the task read endpoint; the four codes here classify provider call failures and are rendered in the frontend.
Mixing them into one enum would make each side require the other to register its codes.

The serialized form follows that side's ``[code] {json}`` -- a machine code plus a JSON parameter dict whose values must
be native JSON values. But ``api_calls`` already has two columns ``error_code`` / ``error_params``, so the two halves are
stored separately instead of joined into one string that the read side would have to parse again.

Classification is based on the exception type and its structured attributes (HTTP status, the ``code`` of the upstream
error body), without guessing from message text.

供应商调用失败的机器稳定编码（记账层）。

失败的 ``api_calls`` 行除了原文（``error_message``）还落一个机器码与一组参数：读侧据此按当前
语言渲染失败短语，切换语言后历史失败原因跟着翻译。认不出的异常只留原文，``error_code`` 与
``error_params`` 留空——读侧原样展示。

与 :mod:`lib.generation.task_failure` 的任务失败码**不共用枚举**：那一套的码要在 ``lib/i18n`` 的 errors
表里各有一条服务端译文（``FAILURE_CODE_KEYS`` 强制登记），渲染发生在任务读接口内；这里的四个码
是供应商调用的失败分类，渲染在前端。混进同一枚举会让两侧互相要求对方登记自己的码。

序列化形式沿用那边的 ``[code] {json}``——机器码加一个 JSON 参数字典，参数值须是 JSON 原生值。
只是 ``api_calls`` 已有 ``error_code`` / ``error_params`` 两列，两半分列存放，不再拼成一个字符串
让读侧再解析一次。

分类依据取异常类型与其结构化属性（HTTP 状态、上游错误体的 ``code``），不做消息文本猜测。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
from openai import APITimeoutError

from lib.backends.http_status_errors import ArtifactDownloadError


class CallErrorCode(StrEnum):
    """失败调用的机器码取值集合（读侧按码渲染短语）。"""

    RATE_LIMITED = "rate_limited"
    CONTENT_POLICY = "content_policy"
    TIMEOUT = "timeout"
    DOWNLOAD_FAILED = "download_failed"
    # A process restart cut this call off in pending: there is no exception object to classify; the startup cleanup writes it as "no task identity".
    # 进程重启把这次调用截断在 pending：没有异常对象可分类，由启动收口按「没有任务身份」写入。
    INTERRUPTED = "interrupted"


# The ``code`` in an upstream error body meaning "content blocked by policy". OpenAI-compatible protocols (official, Azure,
# various relays) put it in the error body's code field and the SDK attaches it to the exception; it's a machine field, not message text.
# 上游错误体里表示「内容被策略拦下」的 ``code``。OpenAI 兼容协议（官方、Azure、各中转）把它
# 放在错误体的 code 字段并由 SDK 挂到异常上，是机器字段而非消息文本。
CONTENT_POLICY_PROVIDER_CODES: frozenset[str] = frozenset(
    {
        "content_filter",
        "content_policy_violation",
        "moderation_blocked",
    }
)

# The status code itself says "waited too long": 408 request timeout, 504 gateway timeout. / 状态码本身就在说「等太久了」：408 请求超时、504 网关超时。
_TIMEOUT_STATUS_CODES: frozenset[int] = frozenset({408, 504})

_RATE_LIMIT_STATUS_CODE = 429

# Stable code for "artifact download exhausted". Built-in backends raise ``ArtifactDownloadError``, custom declarative backends
# raise ``DeclarativeRuntimeError`` with the same ``code`` -- recognized by code rather than type, so both paths land in the same category.
# 「产物下载耗尽」的稳定码。内置 backend 抛 ``ArtifactDownloadError``，自定义声明式 backend 抛
# 带同一 ``code`` 的 ``DeclarativeRuntimeError``——按码认而不按类型认，两条链路落同一个分类。
_ARTIFACT_DOWNLOAD_FAILED_CODE = ArtifactDownloadError.code


@dataclass(frozen=True, slots=True)
class CallFailure:
    """The triple stored for a failed call: original text + machine code + parameters. Unrecognized exceptions only have the original text.

    失败调用落库的三元组：原文 + 机器码 + 参数。认不出的异常只有原文。"""

    error_message: str
    error_code: str | None = None
    error_params: dict[str, Any] | None = None


def classify_call_failure(exc: BaseException | str) -> CallFailure:
    """Encode an exception caught by the ledger bracket into the stored triple.

    Order is priority: artifact download failure is checked first -- the root cause of exhausted downloads is often a
    timeout or an HTTP status, but for the user it is a "download failure" (retry fetching), not a timed-out generation.
    The other three categories don't overlap and are checked layer by layer along the ``__cause__`` chain: outer
    exceptions such as the ambiguous-state wrapper of the submit phase carry no classifiable info themselves; the root
    cause is in the layer they wrap. ``error_message`` always takes the outermost original text.

    Also accepts a failure text: some exits of resume back-accounting only have a piece of text, no exception object.
    Classification relies entirely on the exception's type and attributes, text has no usable signal, so only the original
    text is kept -- the same result as an unrecognized exception.

    把记账括号捕获的异常编码为落库三元组。

    顺序即优先级：产物下载失败先判——下载耗尽的根因常是一次超时或一个 HTTP 状态，但对用户而言
    它是「下载失败」（可重试取件），不是一次超时的生成。其余三类互不重叠，沿 ``__cause__`` 链
    逐层判：提交阶段的歧义态包装之类的外层异常自身不带可分类的信息，根因在它包住的那一层。
    ``error_message`` 始终取最外层的原文。

    也收失败原文：resume 补账的部分出口只拿得到一段文本而没有异常对象。分类依据全在异常的
    类型与属性上，文本没有可判的信号，因而只留原文——与认不出的异常同一结果。
    """
    if isinstance(exc, str):
        return CallFailure(exc)
    message = str(exc)
    if _provider_error_code(exc) == _ARTIFACT_DOWNLOAD_FAILED_CODE:
        return CallFailure(message, CallErrorCode.DOWNLOAD_FAILED, _download_params(exc))
    for candidate in _cause_chain(exc):
        classified = _classify_single(candidate)
        if classified is not None:
            code, params = classified
            return CallFailure(message, code, params)
    return CallFailure(message)


def _classify_single(exc: BaseException) -> tuple[CallErrorCode, dict[str, Any]] | None:
    status = _http_status(exc)
    if status == _RATE_LIMIT_STATUS_CODE:
        # 429 covers openai.RateLimitError (response attached to the exception) and google's ResourceExhausted
        # (status code on the exception's own code), without enumerating each SDK's types.
        # 429 覆盖 openai.RateLimitError（响应挂在异常上）与 google 的 ResourceExhausted
        # （状态码挂在异常自身的 code 上），无需按各家 SDK 的类型逐一枚举。
        return CallErrorCode.RATE_LIMITED, _retry_after_params(exc)
    if _provider_error_code(exc) in CONTENT_POLICY_PROVIDER_CODES:
        return CallErrorCode.CONTENT_POLICY, {}
    if isinstance(exc, TimeoutError | httpx.TimeoutException | APITimeoutError) or status in _TIMEOUT_STATUS_CODES:
        # TimeoutError covers the builtin and the asyncio alias (that's what video polling timeouts raise); httpx and openai
        # timeout exceptions each have their own base class, not inheriting TimeoutError, so they must be listed explicitly.
        # TimeoutError 覆盖内置与 asyncio 别名（视频轮询超时抛的就是它）；httpx 与 openai 的
        # 超时异常各有自己的基类，不继承 TimeoutError，须显式列出。
        return CallErrorCode.TIMEOUT, {}
    return None


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """Explicit causal chain from outside in (``raise ... from``); the implicit ``__context__`` doesn't count.

    从外到内的显式因果链（``raise ... from``）；隐式的 ``__context__`` 不算。"""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__
    return chain


def _download_params(exc: BaseException) -> dict[str, Any]:
    """Parameters for an artifact download failure: include the HTTP status if it can be found.

    The download failure exception itself only has detail text; the status code is on the download exception it wraps
    (``raise ... from exc``), taken along the ``__cause__`` chain.

    产物下载失败的参数：能问出 HTTP 状态就带上。

    下载失败异常自身只有 detail 文本，状态码在它包裹的下载异常上（``raise ... from exc``），
    沿 ``__cause__`` 链取。
    """
    status = _http_status_in_chain(exc)
    return {} if status is None else {"status": status}


def _retry_after_params(exc: BaseException) -> dict[str, Any]:
    """Parameters for rate limiting: include the retry wait seconds when the response carries an integer-seconds ``Retry-After``.

    限流的参数：响应带整数秒 ``Retry-After`` 时带上重试等待秒数。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return {}
    raw = headers.get("Retry-After")
    # Retry-After in HTTP-date form is not parsed: the read side wants "how many seconds to wait", and turning an absolute time into
    # seconds requires knowing the response time, which can't be reliably obtained here.
    # HTTP-date 形式的 Retry-After 不解析：读侧要的是「等多少秒」，把一个绝对时刻算成秒数需要
    # 知道响应时刻，那不是这里能可靠拿到的。
    if not isinstance(raw, str) or not raw.strip().isdigit():
        return {}
    return {"retry_after_seconds": int(raw.strip())}


def _http_status(exc: BaseException) -> int | None:
    """HTTP status code carried by the exception: on ``response`` for the httpx family, on the exception itself for the google family.

    异常携带的 HTTP 状态码：httpx 系挂在 ``response`` 上，google 系挂在异常自身。"""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        # bool is a subclass of int, exclude it; ``code`` on this repo's capability exceptions is a string, which isinstance also blocks.
        # bool 是 int 子类，排掉；``code`` 在本仓库的能力异常上是字符串，isinstance 一并挡住。
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _http_status_in_chain(exc: BaseException) -> int | None:
    """Find the first exception with an HTTP status along the ``__cause__`` chain. / 沿 ``__cause__`` 链找出第一个带 HTTP 状态的异常。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        status = _http_status(current)
        if status is not None:
            return status
        seen.add(id(current))
        current = current.__cause__
    return None


def _provider_error_code(exc: BaseException) -> str | None:
    """The upstream error body's ``code`` (string form); returns ``None`` if absent or not a string.

    上游错误体的 ``code``（字符串形态）；没有或不是字符串返回 ``None``。"""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None
