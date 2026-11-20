"""Instructor fallback support: the structured output fallback chain when the native json_schema channel is unavailable.

The chain is TOOLS → MD_JSON: the former passes the schema at the wire level via function calling, the latter
injects the schema into the prompt. Only wire-level failures continue to the next tier; validation failures are
judged terminal (see :func:`_classify_mode_failure`). The OpenAI-compatible and Ark backends share this module, so
changes to the fallback chain take effect here in one place.

Instructor 降级支持 — 原生 json_schema 通道不可用时的结构化输出降级链。

链路为 TOOLS → MD_JSON：前者用 function calling 在 wire 层传 schema，后者把 schema 注入
prompt。只有 wire 层失败才继续降档，校验类失败即判终局（见 :func:`_classify_mode_failure`）。
OpenAI 兼容与 Ark 两个 backend 共用本模块，故降级链的调整在此一处生效。
"""

from __future__ import annotations

import logging
from enum import Enum
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any

import instructor
from instructor import Mode
from instructor.core import (
    AsyncValidationError,
    IncompleteOutputException,
    InstructorRetryException,
    ResponseParsingError,
)
from pydantic import BaseModel, ValidationError

from lib.backends.text_backends.base import (
    StructuredOutputExhaustedError,
    TextGenerationResult,
    TextOutputTruncatedError,
    TokenParam,
    chat_completion_result,
    check_truncation,
    merge_billed_usage,
    model_facing_response_model,
    strip_leading_think_block,
    truncate_for_log,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Structured output fallback chain: TOOLS passes the schema via function calling, the most constrained and most widely
# compatible tier among OpenAI-compatible endpoints; MD_JSON relies purely on prompt injection of the schema as the
# last resort. The native json_schema tier lives in each backend's generate(); this module only handles the two tiers
# after native (see docs/adr/0014).
# 结构化输出降级链：TOOLS 用 function calling 传 schema，是 OpenAI 兼容端点里约束最强、
# 兼容面最广的一档；MD_JSON 纯靠 prompt 注入 schema，是最后兜底。native json_schema 档在
# 各 backend 的 generate() 里，本模块只负责 native 之后的两档（见 docs/adr/0014）。
_STRUCTURED_MODE_CHAIN: tuple[Mode, ...] = (Mode.TOOLS, Mode.MD_JSON)

# Error keywords when the upstream does not accept the tools parameter; the error text is lowercased before matching
# (proxies are inconsistent about case). Same shape as the openai backend's _SCHEMA_ERROR_KEYWORDS: proxy gateways
# wrap upstream parameter incompatibility in non-400 status codes, so recognizing only BadRequestError would miss them.
# 上游不接受 tools 参数时的错误关键字，匹配前把错误文本转小写（各家代理的大小写不统一）。
# 与 openai backend 的 _SCHEMA_ERROR_KEYWORDS 同构：代理网关会把上游的参数不兼容包装成非 400
# 状态码，只认 BadRequestError 会漏判。
_TOOLS_UNSUPPORTED_KEYWORDS = (
    "tools",
    "tool_calls",
    "tool_choice",
    "function_call",
    "functions",
)

# Parse / validation exceptions that terminate a tier; the same set as _RETRYABLE_PARSE_ERRORS in Instructor v2's
# retry loop. The criterion "is the termination cause of this kind" distinguishes model output problems from API call
# problems, and drift in the set would break that distinction, so TestInstructorExceptionShape pins it against the real Instructor.
# 终止一档的解析 / 校验类异常，与 Instructor v2 重试循环的 _RETRYABLE_PARSE_ERRORS 同集合。
# 判据靠「终止原因是否属这一类」区分模型输出问题与 API 调用问题，集合漂移会让区分失准，
# 故由 TestInstructorExceptionShape 对真实 Instructor 钉住。
_PARSE_FAILURE_TYPES: tuple[type[BaseException], ...] = (
    ValidationError,
    JSONDecodeError,
    AsyncValidationError,
    ResponseParsingError,
)


def _mode_chain_steps() -> list[tuple[Mode, Mode | None]]:
    """Sequence of (current tier, next tier) of the fallback chain; next tier None means it is already the last tier with nowhere to fall back to.

    降级链的 (当前档, 下一档) 序列；下一档为 None 表示已是末档、无处可退。"""
    return [
        (mode, _STRUCTURED_MODE_CHAIN[index + 1] if index + 1 < len(_STRUCTURED_MODE_CHAIN) else None)
        for index, mode in enumerate(_STRUCTURED_MODE_CHAIN)
    ]


def _output_tokens_from_incomplete(exc: IncompleteOutputException) -> int | None:
    """Best-effort retrieval of output_tokens from the partial response carried by the truncation exception; None when unavailable (does not block exception conversion).

    尽力从截断异常携带的部分响应里取 output_tokens，取不到则 None（不阻断异常转换）。"""
    usage = getattr(getattr(exc, "last_completion", None), "usage", None)
    return getattr(usage, "completion_tokens", None) if usage else None


def _raw_output_from_exception(exc: BaseException) -> str:
    """Best-effort retrieval of the model's raw output text for this round from an Instructor exception; returns a placeholder string when unavailable.

    Instructor attaches the last (failed) completion to the exception; response shapes differ by tier (MD_JSON in
    message.content, TOOLS in the tool_calls arguments), so both are tried, letting the diagnostic log at the fallback
    point state "what the model actually output".

    尽力从 Instructor 异常里取模型这一轮的原始输出文本，取不到返回占位串。

    Instructor 把最后一次（失败的）completion 挂在异常上；不同档位的响应形态不同
    （MD_JSON 在 message.content、TOOLS 在 tool_calls 的 arguments），两处都取，
    供降级点的诊断日志说清「模型到底输出了什么」。
    """
    completion = getattr(exc, "last_completion", None)
    if completion is None:
        for attempt in getattr(exc, "failed_attempts", None) or []:
            completion = getattr(attempt, "completion", None)
            if completion is not None:
                break
    if completion is None:
        return "<no response>"
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return "<no response>"
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if content:
        return truncate_for_log(str(content))
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        return truncate_for_log(str(getattr(getattr(tool_calls[0], "function", None), "arguments", None)))
    return "<no response>"


def _api_call_failure(exc: InstructorRetryException) -> BaseException | None:
    """Get the original exception that terminated this tier; None if it terminated on parsing / validating model output.

    Instructor attaches the termination cause on ``__cause__`` (``raise ... from last_exception``); the check must look
    through this wrapper to recognize "upstream rejected the tools parameter".

    ``failed_attempts`` being empty cannot be used as a proxy: parse / validation failures accumulate into
    ``failed_attempts`` one by one without being cleared, so on the path "one parse failure, then a proxy 503",
    ``failed_attempts`` is non-empty while the termination cause is the 503. Judging by the former would treat a
    transient error as non-conforming model output and swallow the caller's retry. Whether the termination cause lies
    in the model output is decided by :func:`_model_output_failure` from the type of the cause.

    取终止这一档的原始异常；若终止在模型输出的解析 / 校验上则返回 None。

    Instructor 把终止原因挂在 ``__cause__`` 上（``raise ... from last_exception``），判据必须
    穿过这层包装才认得出「上游拒收 tools 参数」。

    不能拿 ``failed_attempts`` 是否为空当代理：解析 / 校验类失败会逐次累积进 ``failed_attempts``
    且不清空，因此「先解析失败一次、再撞上代理 503」这条路径下 ``failed_attempts`` 非空而终止
    原因是 503。按前者判会把瞬态错误当成模型输出不合规，吞掉调用方的重试。终止原因是否落在
    模型输出上由 :func:`_model_output_failure` 按终止原因的类型判定。
    """
    if exc.__cause__ is None or _model_output_failure(exc) is not None:
        return None
    return exc.__cause__


def _model_output_failure(exc: InstructorRetryException) -> BaseException | None:
    """If this tier broke on parsing / validating model output, return that parse / validation exception; if it broke on the API call, return None.

    Instructor attaches the termination cause on ``__cause__``; for parse / validation failures it is the parse exception
    of the last attempt. A TOOLS-tier response without a tool call also belongs here: Instructor treats it as a
    retryable parse failure, resends with a correction prompt on reask, and terminates with ``ResponseParsingError``
    only once in-tier retries are exhausted.

    This only answers "broke on model output", not "worth switching tier": whether to switch tier is decided by
    :func:`_tool_call_absent` from the response structure.

    这一档若折在模型输出的解析 / 校验上，返回那条解析 / 校验异常；折在 API 调用上返回 None。

    Instructor 把终止原因挂在 ``__cause__`` 上；解析 / 校验类失败时它就是最后一次尝试的解析
    异常。TOOLS 档响应里没有 tool call 也属此类：Instructor 把它当作可重试的解析失败，reask 时
    附上纠正提示重发，档内重试耗尽才以 ``ResponseParsingError`` 终止。

    这里只回答「折在模型输出上」，不回答「值得换档」：换不换档由 :func:`_tool_call_absent`
    按响应结构判定。
    """
    cause = exc.__cause__
    return cause if isinstance(cause, _PARSE_FAILURE_TYPES) else None


def _first_choice_message(exc: BaseException | None) -> Any | None:
    """The message of the first choice in the raw response carried by the parse exception; None when it is not a parse exception, has no response or no choice.

    取解析异常携带的原始响应里首个 choice 的 message；不是解析异常、无响应或无 choice 时为 None。"""
    if not isinstance(exc, ResponseParsingError):
        return None
    choices = getattr(getattr(exc, "raw_response", None), "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "message", None)


def _tool_call_absent(exc: BaseException | None) -> bool:
    """The response has no tool call at all (wire-level incompatibility), as opposed to a tool call whose arguments are unusable.

    In the TOOLS tier ``ResponseParsingError`` has three causes: no tool call, missing tool call arguments, arguments not
    JSON-serializable. Only the first shows that the upstream channel does not produce tool calls and is worth switching
    tier; in the latter two the upstream did return a tool call but its content does not conform, a validation failure,
    and a weaker tier would only do worse. Judged by response structure rather than error text, so it does not break when
    Instructor changes its wording.

    响应里根本没有 tool call（wire 层不兼容），而非给了 tool call 但参数不可用。

    TOOLS 档下 ``ResponseParsingError`` 有三种成因：没有 tool call、tool call 的 arguments
    缺失、arguments 不可 JSON 序列化。只有第一种说明上游这条通道不产 tool call、值得换档；
    后两种上游确实回了 tool call，只是内容不合规，属校验类失败——换约束更弱的档只会更差。
    按响应结构判而非按错误文案判，免得 Instructor 改文案就失效。
    """
    if not isinstance(exc, ResponseParsingError):
        return False
    message = _first_choice_message(exc)
    if getattr(message, "tool_calls", None):
        return False
    return getattr(message, "function_call", None) is None


def _failure_reason(exc: BaseException) -> str:
    """Compress one tier's failure into a readable reason sentence, carried by StructuredOutputExhaustedError.

    把某一档的失败压成一句可读原因，供 StructuredOutputExhaustedError 携带。"""
    if not isinstance(exc, InstructorRetryException):
        return f"fallback chain failed ({type(exc).__name__}: {exc})"
    api_failure = _api_call_failure(exc)
    if api_failure is not None:
        return f"the upstream rejected every fallback tier's way of passing the schema ({api_failure})"
    last = exc.failed_attempts[-1].exception if exc.failed_attempts else None
    detail = type(last).__name__ if last is not None else "unknown reason"
    return f"model output still invalid after all fallback tiers exhausted their retries ({exc.n_attempts} attempts, last: {detail})"


def _billed_usage(exc: BaseException, *, provider: str, model: str) -> TextGenerationResult:
    """Get the tokens already billed for this tier, to merge into the final result when falling back.

    Every attempt within in-tier retries that got an HTTP 200 has been billed, even if the response ultimately failed
    parsing / validation; Instructor sums them in ``total_usage``. When the upstream rejects the parameter outright the
    sum is zero, which is then treated as "no counts", so None does not collapse to a literal 0 tokens.

    取这一档已经计费的 token，供降档时并入最终结果。

    档内重试里每次拿到 HTTP 200 的尝试都已被计费，即便响应最终没通过解析 / 校验；Instructor
    把它们累加在 ``total_usage`` 上。上游直接拒收参数时该累加值为零，此时按「无计量」处理，
    以免把 None 塌成字面 0 token。
    """
    usage = getattr(exc, "total_usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    return TextGenerationResult(
        text="",
        provider=provider,
        model=model,
        input_tokens=getattr(usage, "prompt_tokens", None) or None,
        output_tokens=getattr(usage, "completion_tokens", None) or None,
        cache_read_tokens=getattr(details, "cached_tokens", None) or None,
    )


def _propagated_cause(exc: Exception) -> Exception:
    """The exception to raise when bubbling up: for API call failures the original exception itself, otherwise as is.

    冒泡时该抛的异常：API 调用失败抛原异常本身，其余抛原样。"""
    if isinstance(exc, InstructorRetryException):
        api_failure = _api_call_failure(exc)
        if isinstance(api_failure, Exception):
            return api_failure
    return exc


class _ModeFailure(Enum):
    """某一档失败后的处置。"""

    DOWNGRADE = "downgrade"
    """wire 层不兼容，换下一档还有机会。"""

    TERMINAL = "terminal"
    """模型输出反复不合规，换约束更弱的档只会更差。"""

    PROPAGATE = "propagate"
    """与结构化输出能力无关，交调用方判定。"""


def _classify_mode_failure(exc: BaseException) -> _ModeFailure:
    """Decide whether a tier's failure should fall back, be judged terminal, or bubble up unchanged.

    Only wire-level incompatibility falls back, in two forms: the upstream rejects the tools parameter (an API call
    exception, which only counts if the error text names tools / functions), or accepts it but returns no tool call (see
    :func:`_tool_call_absent`). The latter only falls back after Instructor's in-tier reask retries are exhausted: the
    correction prompt may let a model that occasionally skips the tool succeed within this more constrained tier, at the
    cost of extra in-tier retry requests when the upstream really produces no tool calls.

    API call exceptions always go through the keyword check, 400 included: Ark models without the ``STRUCTURED_OUTPUT``
    capability bit enter this chain directly without the native tier, and a 400 here may just as well be an invalid model
    name, context overflow or policy refusal. Treating all of those as tools incompatibility would waste an MD_JSON call
    and replace the real cause with a non-retryable terminal exception, giving the caller the wrong handling guidance.

    When the upstream did return tool calls but the argument structure repeatedly does not conform, it is a validation
    failure and judged terminal.

    Everything else bubbles up: transient 5xx and connection errors are left to the caller's ``@with_retry_async`` and are
    not swallowed by the fallback chain into a non-retryable terminal exception; ``TextOutputTruncatedError`` is an
    actionable hard error, and resending the same request that would be truncated again is pointless (see
    docs/adr/0044); it is already a NonRetryableError itself, so it is passed to the caller unchanged.

    判定某一档的失败该降档、判终局，还是原样冒泡。

    只有 wire 层不兼容才降档，两种形态：上游拒收 tools 参数（API 调用异常，须由错误文本指名
    tools / functions 才算数），或收下了却不回 tool call（见 :func:`_tool_call_absent`）。后者
    要等 Instructor 在本档内 reask 重试耗尽才降档：纠正提示可能让偶尔漏调工具的模型在约束更强的
    本档内成功，代价是上游确实不产 tool call 时多花档内重试的请求。

    API 调用异常一律走关键字判据，400 也不例外：无 ``STRUCTURED_OUTPUT`` 能力位的 Ark 模型
    不经原生档直接进本链，此处的 400 同样可能是模型名无效、上下文超限或策略拒绝。把这些无差别
    当成 tools 不兼容，既白花一次 MD_JSON 调用，又会把真实原因替换成不可重试的终局异常、
    使调用方拿到错误的处置指引。

    上游确实回了 tool call、只是参数结构反复不合规，属校验类失败，判终局。

    其余一律冒泡：瞬态 5xx 与连接错误交调用方的 ``@with_retry_async`` 判定，不被降级链吞成
    不可重试的终局异常；``TextOutputTruncatedError`` 是可操作硬错误，重发同一份必然再截断的
    请求没有意义（见 docs/adr/0044），它自身已是 NonRetryableError，原样传给调用方即可。
    """
    if not isinstance(exc, InstructorRetryException):
        return _ModeFailure.PROPAGATE
    api_failure = _api_call_failure(exc)
    if api_failure is None:
        if _tool_call_absent(_model_output_failure(exc)):
            return _ModeFailure.DOWNGRADE
        return _ModeFailure.TERMINAL
    if any(kw in str(api_failure).lower() for kw in _TOOLS_UNSUPPORTED_KEYWORDS):
        return _ModeFailure.DOWNGRADE
    return _ModeFailure.PROPAGATE


def _strip_think_block_in_response(response: Any) -> None:
    """Instructor ``completion:response`` hook: strips the thinking block at the start of the response body in place before parsing.

    Instructor's MD_JSON tier takes the first parseable JSON in the body; draft JSON inside a thinking block would be
    adopted as the result. The hook rewrites ``message.content`` before parsing, so parsing, reask and
    ``failed_attempts`` all see the body, same as the native path.

    Instructor ``completion:response`` 钩子：解析前就地剥掉响应正文开头的思考块。

    Instructor 的 MD_JSON 档取正文里第一段能解析的 JSON；思考块里若出现草稿 JSON，会被当成
    结果采用。钩子在解析前改写 ``message.content``，解析、reask 与 ``failed_attempts`` 看到的
    都是正文，与原生路径同口径。
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if message is None or not isinstance(content, str):
        return
    stripped = strip_leading_think_block(content)
    if stripped != content:
        message.content = stripped


def generate_structured_via_instructor(
    client,
    model: str,
    messages: list[dict],
    response_model: type[BaseModel],
    mode: Mode = Mode.MD_JSON,
    max_retries: int = 2,
    max_tokens: int | None = None,
    token_param: TokenParam = "max_tokens",
    provider: str = "",
) -> TextGenerationResult:
    """Generate structured output via Instructor (sync version, for sync SDKs such as Ark).

    token_param decides the parameter name of the max_tokens value on the wire, chosen by the caller per endpoint.
    The returned result has the JSON text as ``text`` and carries this call's counts. Instructor's
    ``IncompleteOutputException`` (output truncated by max_tokens) is normalized to :class:`TextOutputTruncatedError`,
    same as the truncation behavior of the native structured channel (see docs/adr/0044).

    通过 Instructor 生成结构化输出（同步版，供 Ark 等同步 SDK 使用）。

    token_param 决定 max_tokens 值在导线上的参数名，由调用方按端点选择。
    返回的结果以 JSON 文本为 ``text``，带这次调用的计量。Instructor 的
    ``IncompleteOutputException``（输出被 max_tokens 截断）归一为 :class:`TextOutputTruncatedError`，
    与原生结构化通道的截断行为同口径（见 docs/adr/0044）。
    """
    patched = instructor.from_openai(client, mode=mode)
    patched.on("completion:response", _strip_think_block_in_response)
    extra: dict = {token_param: max_tokens} if max_tokens is not None else {}
    try:
        result, completion = patched.chat.completions.create_with_completion(
            model=model,
            # messages are plain dicts shared by the OpenAI and Ark channels; instructor only declares openai's message TypedDict
            messages=messages,  # type: ignore[arg-type]  # messages 是 OpenAI 与 Ark 通道共用的普通 dict，instructor 只声明 openai 的消息 TypedDict
            response_model=model_facing_response_model(response_model),
            max_retries=max_retries,
            **extra,
        )
    except IncompleteOutputException as exc:
        raise TextOutputTruncatedError(
            provider=provider, model=model, output_tokens=_output_tokens_from_incomplete(exc)
        ) from exc
    return chat_completion_result(text=result.model_dump_json(), provider=provider, model=model, usage=completion.usage)


async def generate_structured_via_instructor_async(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    response_model: type[BaseModel],
    mode: Mode = Mode.MD_JSON,
    max_retries: int = 2,
    max_tokens: int | None = None,
    token_param: TokenParam = "max_tokens",
    provider: str = "",
) -> TextGenerationResult:
    """Generate structured output via Instructor (async version, for OpenAI AsyncOpenAI).

    token_param decides the parameter name of the max_tokens value on the wire, chosen by the caller per endpoint.
    The returned result has the JSON text as ``text`` and carries this call's counts. Instructor's
    ``IncompleteOutputException`` (output truncated by max_tokens) is normalized to :class:`TextOutputTruncatedError`,
    same as the truncation behavior of the native structured channel (see docs/adr/0044).

    通过 Instructor 生成结构化输出（异步版，供 OpenAI AsyncOpenAI 使用）。

    token_param 决定 max_tokens 值在导线上的参数名，由调用方按端点选择。
    返回的结果以 JSON 文本为 ``text``，带这次调用的计量。Instructor 的
    ``IncompleteOutputException``（输出被 max_tokens 截断）归一为 :class:`TextOutputTruncatedError`，
    与原生结构化通道的截断行为同口径（见 docs/adr/0044）。
    """
    patched = instructor.from_openai(client, mode=mode)
    patched.on("completion:response", _strip_think_block_in_response)
    extra: dict = {token_param: max_tokens} if max_tokens is not None else {}
    try:
        result, completion = await patched.chat.completions.create_with_completion(
            model=model,
            # messages are plain dicts shared by the OpenAI and Ark channels; instructor only declares openai's message TypedDict
            messages=messages,  # type: ignore[arg-type]  # messages 是 OpenAI 与 Ark 通道共用的普通 dict，instructor 只声明 openai 的消息 TypedDict
            response_model=model_facing_response_model(response_model),
            max_retries=max_retries,
            **extra,
        )
    except IncompleteOutputException as exc:
        raise TextOutputTruncatedError(
            provider=provider, model=model, output_tokens=_output_tokens_from_incomplete(exc)
        ) from exc
    return chat_completion_result(text=result.model_dump_json(), provider=provider, model=model, usage=completion.usage)


def inject_json_instruction(messages: list[dict]) -> list[dict]:
    """Inject a JSON format instruction into messages so json_object mode can be used.

    The OpenAI API requires the prompt to contain the keyword "JSON" to enable json_object mode.
    If messages already contain "JSON", a copy is returned unchanged.

    向 messages 注入 JSON 格式指令，确保 json_object 模式可用。

    OpenAI API 要求 prompt 中包含 "JSON" 关键字才能启用 json_object 模式。
    若 messages 中已包含 "JSON"，则原样返回副本。
    """
    fb_messages = list(messages)
    if any("JSON" in (m.get("content") or "") for m in fb_messages):
        return fb_messages
    sys_idx = next((i for i, m in enumerate(fb_messages) if m.get("role") == "system"), None)
    if sys_idx is not None:
        orig = fb_messages[sys_idx]
        fb_messages[sys_idx] = {**orig, "content": (orig.get("content") or "") + "\nRespond in JSON format."}
    else:
        fb_messages.insert(0, {"role": "system", "content": "Respond in JSON format."})
    return fb_messages


def _handle_mode_failure(
    exc: Exception,
    *,
    mode: Mode,
    next_mode: Mode | None,
    provider: str,
    model: str,
) -> TextGenerationResult:
    """Handle the failure of one tier of the fallback chain: if it can fall back, log and return normally; otherwise raise a terminal exception or bubble up unchanged.

    A normal return means "the caller should continue with the next tier"; the return value is the tokens already
    billed for this tier that must be merged into the final result.

    处理降级链某一档的失败：可降档则记日志后正常返回，否则抛终局异常或原样冒泡。

    正常返回即「调用方应继续下一档」，返回值是这一档已计费、需并入最终结果的 token。
    """
    failure = _classify_mode_failure(exc)
    if failure is _ModeFailure.PROPAGATE:
        # Strip Instructor's wrapper when bubbling up: the caller's @with_retry_async first checks transience by exception type
        # (ConnectionError / TimeoutError); with a wrapper only message text matching is left, missing connection errors.
        # 冒泡时剥掉 Instructor 的包装：调用方的 @with_retry_async 先按异常类型判瞬态
        # （ConnectionError / TimeoutError），包着一层就只剩消息文本匹配，漏判连接类错误。
        raise _propagated_cause(exc)
    if failure is _ModeFailure.DOWNGRADE and next_mode is not None:
        logger.warning(
            "Instructor %s 档 wire 层不兼容（%s），降档到 %s 档；模型原始输出：%s",
            mode.value,
            exc,
            next_mode.value,
            _raw_output_from_exception(exc),
        )
        return _billed_usage(exc, provider=provider, model=model)
    # Validation exhausted, or the last tier is still wire-level incompatible: the fallback chain has no weaker tier left.
    # 校验类耗尽，或末档仍是 wire 层不兼容——降级链已无更弱的档可退。
    logger.warning(
        "Instructor %s 档失败且降级链已走完，判定为结构化输出能力不足；模型原始输出：%s",
        mode.value,
        _raw_output_from_exception(exc),
    )
    raise StructuredOutputExhaustedError(provider=provider, model=model, reason=_failure_reason(exc)) from exc


def instructor_fallback_sync(
    client,
    model: str,
    messages: list[dict],
    response_schema: dict | type[BaseModel] | None,
    provider: str,
    max_tokens: int | None = None,
    token_param: TokenParam = "max_tokens",
):
    """Synchronous Instructor fallback path.

    - response_schema is a Pydantic class → TOOLS → MD_JSON fallback chain
    - response_schema is a dict → inject JSON instruction + json_object mode

    For sync SDK backends such as Ark (the caller wraps it with asyncio.to_thread).
    No transient retries; transient errors are handled by the caller's retry loop; in-tier structured validation retries
    are built into Instructor.

    同步 Instructor 降级路径。

    - response_schema 为 Pydantic 类 → TOOLS → MD_JSON 降级链
    - response_schema 为 dict → inject JSON instruction + json_object 模式

    供 Ark 等同步 SDK 后端使用（调用方用 asyncio.to_thread 包装）。
    不做瞬态重试，瞬态错误由调用方的重试循环统一处理；档内的结构化校验重试由 Instructor 自带。
    """
    if isinstance(response_schema, type):
        result: TextGenerationResult | None = None
        billed = TextGenerationResult(text="", provider=provider, model=model)
        for mode, next_mode in _mode_chain_steps():
            try:
                result = generate_structured_via_instructor(
                    client=client,
                    model=model,
                    messages=messages,
                    response_model=response_schema,
                    mode=mode,
                    max_tokens=max_tokens,
                    token_param=token_param,
                    provider=provider,
                )
                break
            except Exception as exc:
                discarded = _handle_mode_failure(exc, mode=mode, next_mode=next_mode, provider=provider, model=model)
                merge_billed_usage(billed, discarded)
        # every failure path in the chain raises, so reaching here means there is a result
        assert result is not None  # 链内每条失败路径都抛异常，走到这里必有结果
        merge_billed_usage(billed, result)
        billed.text = result.text
        return billed

    logger.info("response_schema 为 dict，无法使用 Instructor，回退到 json_object 模式")
    fb_messages = inject_json_instruction(messages)
    create_kwargs: dict = {
        "model": model,
        "messages": fb_messages,
        "response_format": {"type": "json_object"},
    }
    if max_tokens is not None:
        create_kwargs[token_param] = max_tokens
    response = client.chat.completions.create(**create_kwargs)
    usage = getattr(response, "usage", None)
    choice = response.choices[0]
    content = choice.message.content or ""
    # Same as the native path: the thinking block that thinking models embed at the start of content is not included in the result.
    # 与原生路径同口径：思考模型内嵌在 content 开头的思考块不进结果。
    text = strip_leading_think_block(content) if isinstance(content, str) else str(content)
    output_tokens = getattr(usage, "completion_tokens", None) if usage else None
    # A dict schema is still a structured output intent (response_schema is non-empty, there is just no Pydantic model for
    # the native Instructor channel), so truncation is likewise escalated to a hard error.
    # dict schema 仍是结构化输出诉求（response_schema 非空，只是无 Pydantic 模型可走原生
    # Instructor 通道），截断同样升级为硬错误。
    check_truncation(
        getattr(choice, "finish_reason", None),
        provider=provider,
        model=model,
        output_tokens=output_tokens,
        structured=True,
    )
    return chat_completion_result(text=text.strip(), provider=provider, model=model, usage=usage)


async def instructor_fallback_async(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    response_schema: dict | type[BaseModel] | None,
    provider: str,
    max_tokens: int | None = None,
    token_param: TokenParam = "max_tokens",
):
    """Asynchronous Instructor fallback path.

    - response_schema is a Pydantic class → TOOLS → MD_JSON fallback chain (async)
    - response_schema is a dict → inject JSON instruction + json_object mode (async)

    For natively async SDK backends such as OpenAI.
    No transient retries; transient errors are handled by the caller's retry loop; in-tier structured validation retries
    are built into Instructor.

    异步 Instructor 降级路径。

    - response_schema 为 Pydantic 类 → TOOLS → MD_JSON 降级链 (async)
    - response_schema 为 dict → inject JSON instruction + json_object 模式 (async)

    供 OpenAI 等原生异步 SDK 后端使用。
    不做瞬态重试，瞬态错误由调用方的重试循环统一处理；档内的结构化校验重试由 Instructor 自带。
    """
    if isinstance(response_schema, type):
        result: TextGenerationResult | None = None
        billed = TextGenerationResult(text="", provider=provider, model=model)
        for mode, next_mode in _mode_chain_steps():
            try:
                result = await generate_structured_via_instructor_async(
                    client=client,
                    model=model,
                    messages=messages,
                    response_model=response_schema,
                    mode=mode,
                    max_tokens=max_tokens,
                    token_param=token_param,
                    provider=provider,
                )
                break
            except Exception as exc:
                discarded = _handle_mode_failure(exc, mode=mode, next_mode=next_mode, provider=provider, model=model)
                merge_billed_usage(billed, discarded)
        # every failure path in the chain raises, so reaching here means there is a result
        assert result is not None  # 链内每条失败路径都抛异常，走到这里必有结果
        merge_billed_usage(billed, result)
        billed.text = result.text
        return billed

    logger.info("response_schema 为 dict，无法使用 Instructor，回退到 json_object 模式")
    fb_messages = inject_json_instruction(messages)
    create_kwargs: dict = {
        "model": model,
        "messages": fb_messages,
        "response_format": {"type": "json_object"},
    }
    if max_tokens is not None:
        create_kwargs[token_param] = max_tokens
    response = await client.chat.completions.create(**create_kwargs)
    usage = getattr(response, "usage", None)
    choice = response.choices[0]
    content = choice.message.content or ""
    # Same as the native path: the thinking block that thinking models embed at the start of content is not included in the result.
    # 与原生路径同口径：思考模型内嵌在 content 开头的思考块不进结果。
    text = strip_leading_think_block(content) if isinstance(content, str) else str(content)
    output_tokens = getattr(usage, "completion_tokens", None) if usage else None
    # A dict schema is still a structured output intent (response_schema is non-empty, there is just no Pydantic model for
    # the native Instructor channel), so truncation is likewise escalated to a hard error.
    # dict schema 仍是结构化输出诉求（response_schema 非空，只是无 Pydantic 模型可走原生
    # Instructor 通道），截断同样升级为硬错误。
    check_truncation(
        getattr(choice, "finish_reason", None),
        provider=provider,
        model=model,
        output_tokens=output_tokens,
        structured=True,
    )
    return chat_completion_result(text=text.strip(), provider=provider, model=model, usage=usage)
