"""Core interface definitions of the text generation service layer. / 文本生成服务层核心接口定义。"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue

from lib.infra.retry import NonRetryableError
from lib.infra.text_utils import strip_json_code_fences

_logger = logging.getLogger(__name__)

# Name of the Chat Completions output limit parameter: the official OpenAI endpoint has deprecated max_tokens
# (reasoning models reject it outright) in favor of max_completion_tokens; third-party compatible endpoints vary in
# support for the new parameter, so max_tokens must be kept conservatively. The caller chooses per endpoint.
# Chat Completions 输出上限参数名：官方 OpenAI 端点已弃用 max_tokens（推理模型
# 直接拒绝），改用 max_completion_tokens；第三方兼容端点对新参数支持情况不一，
# 须保守沿用 max_tokens。由调用方按端点选择。
TokenParam = Literal["max_tokens", "max_completion_tokens"]


def warn_if_truncated(
    finish_reason: str | None,
    *,
    provider: str,
    model: str,
    output_tokens: int | None = None,
    truncation_values: tuple[str, ...] = ("length", "MAX_TOKENS", "max_tokens"),
) -> bool:
    """Detect whether the model response was truncated by the token limit; if so, logger.warning.

    Returns True when truncated (for the caller's further handling). Truncation of free text (no response_schema) only
    warns without raising, and is carried to the caller via the result's ``truncated`` flag; truncation of structured
    output must go through :func:`check_truncation` to be escalated to a hard error.

    检测模型响应是否因 token 上限被截断，若是则 logger.warning。

    返回 True 表示被截断（供调用方用于进一步处理）。自由文本（无 response_schema）
    的截断只告警不抛错，由结果的 ``truncated`` 标记带给调用方；结构化输出的截断须走
    :func:`check_truncation` 升级为硬错误。
    """
    if finish_reason is None:
        return False
    if finish_reason in truncation_values:
        _logger.warning(
            "%s/%s 输出被截断（finish_reason=%s, output_tokens=%s）：已达模型输出上限。"
            "考虑切换到更大输出上限的模型，或减少请求规模。",
            provider,
            model,
            finish_reason,
            output_tokens,
        )
        return True
    return False


class TextOutputTruncatedError(NonRetryableError):
    """The text model's output was cut off by the output limit; the result is incomplete and not directly usable.

    Backends only raise it when the request carries response_schema (a structured output intent); free-text truncation
    only warns and marks ``truncated`` on the result (see docs/adr/0044), and when the caller requires complete output
    it is escalated to this exception by :meth:`lib.backends.text_generator.TextGenerator.generate`.

    Inherits NonRetryableError (rather than RuntimeError directly): the output_tokens embedded in the message is an
    arbitrary integer whose decimal text might happen to contain a substring of with_retry_async's transient error
    patterns (such as 429/500/502/503/504); without an explicit non-retryable mark it would be misjudged as transient,
    and each backend's @with_retry_async() wrapper would resend the same request, which would be truncated again.

    ``provider_id`` is the resolver-layer registry provider_id (``custom-<id>`` for custom providers), and
    ``custom_model`` marks that the model comes from a custom provider; both are filled in by
    :class:`lib.backends.text_generator.TextGenerator`, and the UI uses them to offer "register the max output length"
    or "switch to another text model". Both are unset when raised at the backend layer.

    文本模型输出被输出上限截断，结果不完整、不可直接使用。

    backend 只在请求带 response_schema（结构化输出诉求）时抛出；自由文本截断只告警并在结果上
    标记 ``truncated``（见 docs/adr/0044），调用方要求完整输出时由
    :meth:`lib.backends.text_generator.TextGenerator.generate` 升级为本异常。

    继承 NonRetryableError（而非直接 RuntimeError）：消息内嵌的 output_tokens 是任意
    整数，其十进制文本可能偶然包含 with_retry_async 瞬态错误模式的子串（如 429/500/
    502/503/504），若不显式标记为不可重试，会被误判为瞬态错误进而在各后端的
    @with_retry_async() 包裹下重发同一份必然再截断的请求。

    ``provider_id`` 是解析层的 registry provider_id（自定义供应商为 ``custom-<id>``），``custom_model``
    标明该模型由自定义供应商提供；二者由 :class:`lib.backends.text_generator.TextGenerator` 补齐，
    界面据此给出「去登记最大输出长度」或「换一个文本模型」的出路。backend 层抛出时二者缺省。
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        output_tokens: int | None = None,
        provider_id: str | None = None,
        custom_model: bool = False,
    ):
        self.provider = provider
        self.model = model
        self.output_tokens = output_tokens
        self.provider_id = provider_id
        self.custom_model = custom_model
        detail = f"在 output_tokens={output_tokens} 处" if output_tokens is not None else ""
        super().__init__(
            f"{provider}/{model} 的输出{detail}被模型输出上限截断，内容不完整。请改用输出能力更高的文本模型。"
        )


class StructuredOutputExhaustedError(NonRetryableError):
    """The structured output fallback chain is exhausted and the model still did not produce content satisfying the schema.

    All backends funnel the terminal failure of "insufficient structured output capability" into this exception: when
    Instructor's in-tier validation retries are exhausted or Gemini's prompt injection fallback is exhausted, they raise
    this instead of their own internal exceptions (``InstructorRetryException``, bare ``ValueError``), so the caller can
    give the user an actionable explanation instead of leaking the SDK's internal exception text.

    It inherits NonRetryableError for the same reason as :class:`TextOutputTruncatedError`: the failure reason embedded
    in the message contains model-side dynamic text that might happen to match with_retry_async's transient error
    string patterns. Semantically it also should not be retried: the fallback chain has already tried every tier and
    the in-tier retries.

    结构化输出降级链已走完，模型仍未产出满足 schema 的内容。

    各后端把「结构化输出能力不足」的终局失败统一收敛到本异常：Instructor 档内校验重试
    耗尽、Gemini prompt 注入降级耗尽，都抛它而非各自的内部异常（``InstructorRetryException``、
    裸 ``ValueError``），调用方据此给用户一句可行动的解释，而不是把 SDK 内部异常原文透出去。

    继承 NonRetryableError 的理由同 :class:`TextOutputTruncatedError`：消息里嵌的失败原因
    含模型侧动态文本，可能偶然命中 with_retry_async 的瞬态错误字符串模式。语义上也确实
    不该重试——降级链本身已经把该试的档位与档内重试都试过了。
    """

    def __init__(self, *, provider: str, model: str, reason: str):
        self.provider = provider
        self.model = model
        self.reason = reason
        super().__init__(f"{provider}/{model} 的结构化输出能力不足：{reason}。请更换文本模型或供应商后重试。")


# Truncation length of raw model output in diagnostic logs: enough to see structural problems (prose or JSON, object or
# array at top level, which field is missing) without writing the whole generated content into the log.
# 诊断日志里模型原始输出的截断长度：够看清结构性问题（是散文还是 JSON、顶层是对象还是数组、
# 哪个字段缺了），又不至于把整份生成内容写进日志。
RAW_OUTPUT_LOG_LIMIT = 500


def truncate_for_log(text: str | None, limit: int = RAW_OUTPUT_LOG_LIMIT) -> str:
    """Shrink raw model output to a loggable length.

    Structured output fallback and parse rejections are determined by "what the model actually output"; recording only a
    summary (field path, error type) cannot be replayed afterwards, so a fragment of the original text is deliberately
    kept here. A different trade-off from :func:`summarize_validation_error`: that one is a high-frequency path taken on
    every validation failure, while this function is only used in warning-level fallback / rejection branches.

    把模型原始输出压到可入日志的长度。

    结构化输出降级与解析拒绝由「模型实际输出了什么」决定，只记摘要（字段路径、错误类型）
    事后无法复盘，故这里刻意保留原文片段。与 :func:`summarize_validation_error` 的取舍不同：
    后者是每次校验失败都会走的高频路径，本函数只用在 warning 级的降级 / 拒绝分支。
    """
    if not text:
        return "<空>"
    if len(text) > limit:
        return f"{text[:limit]}…（截断，共 {len(text)} 字符）"
    return text


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def strip_leading_think_block(text: str) -> str:
    """Strip the leading ``<think>…</think>`` thinking block from the output and return the body after it.

    Thinking models served via OpenAI-compatible APIs (such as MiniMax M2 / M3) write their thinking process directly
    into ``content`` by default, wrapped in ``<think>`` tags, with the actual answer after ``</think>``; JSON for
    structured output also follows it. Only one block at the very start is recognized: when the block is not at the
    start or the closing tag is missing (usually truncated output), the text is returned unchanged, no guessing.

    剥掉输出开头的 ``<think>…</think>`` 思考块，返回其后的正文。

    走 OpenAI 兼容接口的思考模型（如 MiniMax M2 / M3）默认把思考过程直接写进 ``content``，
    用 ``<think>`` 标签包住，正式回答跟在 ``</think>`` 之后；结构化输出的 JSON 也在其后。
    只认开头的一个块：块不在开头，或闭合标签缺失（多为输出被截断）时原样返回，不做猜测。
    """
    body = text.lstrip()
    if not body.startswith(_THINK_OPEN):
        return text
    end = body.find(_THINK_CLOSE, len(_THINK_OPEN))
    if end < 0:
        return text
    return body[end + len(_THINK_CLOSE) :].lstrip()


def strip_json_code_fence(text: str) -> str:
    """Strip a Markdown JSON code fence wrapping the whole output and return the JSON text inside the fence.

    Only handles a matching pair of ``json`` / language-less fences at start and end; surrounding whitespace and the
    form without a trailing newline are normalized. Non-fenced content is returned unchanged, to avoid altering plain
    text or truncated JSON.

    剥掉包裹整个输出的 Markdown JSON 代码围栏，返回围栏内的 JSON 文本。

    只处理首尾成对的 ``json`` / 无语言围栏；正文前后空白与无尾换行的形态都归一化。
    非围栏内容原样返回，避免误改普通文本或截断的 JSON。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text

    lines = stripped.splitlines()
    if len(lines) < 2 or lines[-1].strip() != "```":
        return text

    opening = lines[0].strip().lower()
    if opening not in {"```", "```json"}:
        return text

    return "\n".join(lines[1:-1]).strip()


# Text output limit: a non-binding safety valve only against degenerate model runaway, not a functional budget. The
# normal output size of episode planning, script generation and drama script_plan normalization is naturally bounded
# by their schemas/content and never reaches this high value; it is only hit by pathologically large batches or when
# the user configured a model with low output capacity. All three share this constant; adjust only here (see
# docs/adr/0044).
# 文本输出上限：非约束安全阀，仅防模型退化性 runaway，不是功能预算——分集规划、剧本生成、
# drama script_plan 规范化三处的正常输出体量由各自 schema/内容天然约束，永远不会触碰这个高位值；
# 只有病态超大批量，或用户配置了输出能力偏低的模型时才会命中。三处共用同一常量，调整只改
# 这一处（见 docs/adr/0044）。
DEFAULT_MAX_OUTPUT_TOKENS = 64000


def check_truncation(
    finish_reason: str | None,
    *,
    provider: str,
    model: str,
    structured: bool,
    output_tokens: int | None = None,
    truncation_values: tuple[str, ...] = ("length", "MAX_TOKENS", "max_tokens"),
) -> bool:
    """Detect output truncation: truncation of structured output (``structured=True``) is a hard error; free text warns and returns True.

    ``structured`` is passed by the caller according to whether the original request of this ``generate()`` carries
    response_schema: the criterion is "this generation's intent", not "whether this specific wire call really carried
    the schema parameter", so even when an internal fallback path temporarily clears response_schema for its fallback
    strategy (such as writing the schema into the prompt), ``structured=True`` should still be passed down explicitly.

    Truncation raises :class:`TextOutputTruncatedError`, naturally short-circuiting the caller's validation retry loop:
    resending the same request that would be truncated again is pointless (see docs/adr/0044).

    检测输出截断：结构化输出（``structured=True``）截断是硬错误，自由文本告警并返回 True。

    ``structured`` 由调用方按本次 ``generate()`` 的原始请求是否带 response_schema 传入——
    判断口径是"这次生成诉求"而非"这次具体 wire 调用是否真的带了 schema 参数"，故内部降级
    路径即使为兜底策略（如把 schema 写进 prompt）临时清空了 response_schema，仍应把
    ``structured=True`` 显式传下来。

    截断即抛 :class:`TextOutputTruncatedError`，天然短路调用方的校验重试循环——重发同一份
    必然再截断的请求没有意义（见 docs/adr/0044）。
    """
    truncated = warn_if_truncated(
        finish_reason,
        provider=provider,
        model=model,
        output_tokens=output_tokens,
        truncation_values=truncation_values,
    )
    if truncated and structured:
        raise TextOutputTruncatedError(provider=provider, model=model, output_tokens=output_tokens)
    return truncated


class TextCapability(StrEnum):
    """文本后端支持的能力枚举。"""

    TEXT_GENERATION = "text_generation"
    STRUCTURED_OUTPUT = "structured_output"
    VISION = "vision"


class TextTaskType(StrEnum):
    """文本生成任务类型。"""

    SCRIPT = "script"
    OVERVIEW = "overview"
    STYLE_ANALYSIS = "style"


class TextTaskTier(StrEnum):
    """文本任务档位。

    调用点在代码里固定归档（见 TEXT_TASK_TIERS），用户只配置「每档用哪个文本 backend」，
    不配置映射本身（docs/adr/0051）。
    """

    SIMPLE = "simple"
    COMPLEX = "complex"


# The single mapping TextTaskType → tier. New task types must be explicitly assigned here;
# a missing tier fails at module import (with an exhaustive test as backup).
# TextTaskType → 档位的唯一映射。新增任务类型必须在此显式归档，
# 缺档在模块导入时即失败（另有穷举测试兜底）。
TEXT_TASK_TIERS: dict[TextTaskType, TextTaskTier] = {
    TextTaskType.SCRIPT: TextTaskTier.COMPLEX,
    TextTaskType.OVERVIEW: TextTaskTier.SIMPLE,
    TextTaskType.STYLE_ANALYSIS: TextTaskTier.SIMPLE,
}

if _missing := set(TextTaskType) - set(TEXT_TASK_TIERS):
    raise RuntimeError(f"TextTaskType 成员未归档到 TEXT_TASK_TIERS: {sorted(m.value for m in _missing)}")

# Tasks that need image input (vision) capability. If the resolved model does not support vision, raise directly instead of silently switching models.
# 需要图像输入（vision）能力的任务。解析出的模型不支持 vision 时直接报错，不静默换模型。
VISION_REQUIRED_TASKS: frozenset[TextTaskType] = frozenset({TextTaskType.STYLE_ANALYSIS})


@dataclass
class ImageInput:
    """Image input (for vision). / 图片输入（用于 vision）。"""

    path: Path | None = None
    url: str | None = None


@dataclass
class TextGenerationRequest:
    """Generic text generation request. Each backend ignores fields it does not support. / 通用文本生成请求。各 Backend 忽略不支持的字段。"""

    prompt: str
    response_schema: dict | type | None = None
    images: list[ImageInput] | None = None
    system_prompt: str | None = None
    max_output_tokens: int | None = None


@dataclass
class TextGenerationResult:
    """Generic text generation result.

    ``truncated`` marks that free-text output was cut off by the model's output limit (truncation of structured output
    raises :class:`TextOutputTruncatedError` directly and is never returned with this flag).

    Usage is reported per the usage contract (docs/adr/0100): ``input_tokens`` is the total input, including cache reads
    and cache writes; ``cache_read_tokens``, ``cache_write_5m_tokens`` and ``cache_write_1h_tokens`` are the cache read and
    two cache write tiers within it, and uncached input is total input minus these three. Differences in vendor usage
    semantics (whether cache counts as input) are handled by each backend before filling these fields.

    When the result includes several requests along the fallback chain, the counts above are sums, and
    ``billed_requests`` lists per-request counts so billing can assign each one to its context tier; with only one
    request it is empty; use :meth:`request_tokens` to read uniformly.

    通用文本生成结果。

    ``truncated`` 标明自由文本输出被模型输出上限截断（结构化输出截断直接抛
    :class:`TextOutputTruncatedError`，不会带着这个标记返回）。

    用量按用量契约上报（docs/adr/0100）：``input_tokens`` 是总输入，含缓存读与缓存写；``cache_read_tokens``、
    ``cache_write_5m_tokens``、``cache_write_1h_tokens`` 是其中缓存读与两档缓存写的部分，未缓存输入由总输入减去
    这三项得出。各家 usage 的语义差异（缓存是否计入输入）由各 backend 在填这些字段前处理。

    结果含降级链上的多次请求时，上面各计量是各次之和，``billed_requests`` 按请求列出各次的计量，供计费逐次落
    上下文档；只含一次请求时为空，用 :meth:`request_tokens` 统一读取。
    """

    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    truncated: bool = False
    cache_read_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    billed_requests: list[RequestTokens] = field(default_factory=list)

    def request_tokens(self) -> list[RequestTokens]:
        """Counts of each billed request; empty when there are no counts at all (not tracked). / 各次已计费请求的计量；没有任何计量（未追踪）时为空。"""
        if self.billed_requests:
            return list(self.billed_requests)
        own = RequestTokens(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens,
        )
        return [] if own == RequestTokens() else [own]


@dataclass(frozen=True)
class RequestTokens:
    """Token counts of one provider request; fields have the same semantics as the same-named fields of :class:`TextGenerationResult`.

    一次供应商请求的 token 计量，字段语义同 :class:`TextGenerationResult` 的同名字段。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None


def merge_billed_tokens(kept: int | None, discarded: int | None) -> int | None:
    """Merge the tokens of the call discarded on the fallback path into the counts of the kept result.

    A call before fallback is billed as soon as it got an HTTP 200; not merging would systematically under-record usage
    and cost. Add only when at least one side has counts; when both are None (not tracked) keep None rather than
    collapsing to a literal 0 tokens: "not tracked" and "zero consumption" are not the same thing for cost accounting.

    把降级路径上被丢弃的那次调用的 token 并入保留结果的计量。

    降级前的调用只要拿到过 HTTP 200 就已被计费，不并账会系统性漏记用量与成本。
    仅在至少一侧有计量时相加；两侧皆 None（未追踪）保持 None，不塌成字面 0 token——
    「未追踪」与「零消耗」在成本口径上不是一回事。
    """
    if kept is None and discarded is None:
        return None
    return (kept or 0) + (discarded or 0)


def chat_completion_result(
    *, text: str, provider: str, model: str, usage: object | None, truncated: bool = False
) -> TextGenerationResult:
    """Fill the result's counts from Chat Completions-shaped usage.

    ``prompt_tokens`` is the total input, with the cache-hit part in ``prompt_tokens_details.cached_tokens``;
    ``completion_tokens`` includes reasoning tokens. When usage is missing, all counts are ``None``.

    按 Chat Completions 的 usage 形态填结果的计量。

    ``prompt_tokens`` 是总输入，其中缓存命中的部分在 ``prompt_tokens_details.cached_tokens``；``completion_tokens``
    含推理 token。usage 缺失时各计量为 ``None``。
    """
    details = getattr(usage, "prompt_tokens_details", None)
    return TextGenerationResult(
        text=text,
        provider=provider,
        model=model,
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        truncated=truncated,
        cache_read_tokens=getattr(details, "cached_tokens", None),
    )


def merge_billed_usage(kept: TextGenerationResult, discarded: TextGenerationResult) -> None:
    """Merge all token counts of the call discarded on the fallback path into the kept result (modifies ``kept`` in place).

    Counts are added, and each request's counts are also recorded in ``billed_requests``: context tiers are assigned by
    the input total of a single request, not by the sum.

    把降级路径上被丢弃的那次调用的全部 token 计量并入保留结果（原地修改 ``kept``）。

    各计量相加，各次请求的计量另记进 ``billed_requests``：上下文档按单次请求的输入总量落档，不能按合计落档。
    """
    kept.billed_requests = [*kept.request_tokens(), *discarded.request_tokens()]
    kept.input_tokens = merge_billed_tokens(kept.input_tokens, discarded.input_tokens)
    kept.output_tokens = merge_billed_tokens(kept.output_tokens, discarded.output_tokens)
    kept.cache_read_tokens = merge_billed_tokens(kept.cache_read_tokens, discarded.cache_read_tokens)
    kept.cache_write_5m_tokens = merge_billed_tokens(kept.cache_write_5m_tokens, discarded.cache_write_5m_tokens)
    kept.cache_write_1h_tokens = merge_billed_tokens(kept.cache_write_1h_tokens, discarded.cache_write_1h_tokens)


class _ModelFacingJsonSchema(GenerateJsonSchema):
    """Generates the schema shown to the model only: class docstrings do not go into ``description``.

    Class docstrings are implementation notes for maintainers (ADR numbers, migration rules etc.); by default Pydantic
    renders them as ``description`` on model, enum and other definitions and sends them with structured output requests;
    only ``description`` written explicitly on fields is meant for the model and is kept as is.

    Pydantic writes the docstring descriptions of enums and RootModel only after each type method returns, so they are
    not intercepted in ``model_schema`` and similar methods; instead the class docstrings encountered are recorded
    during generation, and after generation descriptions with the same text are stripped from the top level and each
    ``$defs`` definition; different descriptions given explicitly via ``json_schema_extra`` etc. are kept.

    只生成给模型看的 schema：类 docstring 不进 ``description``。

    类 docstring 是写给维护者的实现说明（ADR 编号、迁移口径等），Pydantic 默认把它渲染成模型、枚举等定义上的
    ``description`` 随结构化输出请求发出；字段上显式写的 ``description`` 才是给模型的说明，原样保留。

    枚举与 RootModel 的 docstring 说明由 Pydantic 在各类型方法返回之后才写入，故不在 ``model_schema`` 等方法里拦，
    而是生成途中记下遇到的类 docstring，生成完成后从顶层与各 ``$defs`` 定义上剥除同文的说明；经
    ``json_schema_extra`` 等显式给出的不同说明保留。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._class_docstrings: set[str] = set()

    def generate_inner(self, schema: Any) -> JsonSchemaValue:
        cls = schema.get("cls")
        if isinstance(cls, type) and cls.__doc__:
            self._class_docstrings.add(inspect.cleandoc(cls.__doc__))
        return super().generate_inner(schema)

    def handle_ref_overrides(self, json_schema: JsonSchemaValue) -> JsonSchemaValue:
        # Pydantic drops field descriptions that match the text of the referenced definition; the docstring description on the definition is stripped afterwards, so the field description must stay.
        # Pydantic 会去掉与被引用定义同文的字段 description；定义上的 docstring 说明随后被剥除，字段说明须留下。
        reduced = super().handle_ref_overrides(json_schema)
        if "description" in json_schema and "description" not in reduced:
            reduced = {**reduced, "description": json_schema["description"]}
        return reduced

    def generate(self, schema: Any, mode: JsonSchemaMode = "validation") -> JsonSchemaValue:
        json_schema = super().generate(schema, mode)
        for node in (json_schema, *json_schema.get("$defs", {}).values()):
            if node.get("description") in self._class_docstrings:
                del node["description"]
        return json_schema


def model_facing_response_model[M: BaseModel](model: type[M]) -> type[M]:
    """A ``model`` subclass for libraries that generate their own schema without :func:`resolve_schema` (Instructor, xai_sdk).

    The subclass has no docstring and ``model_json_schema`` uses :class:`_ModelFacingJsonSchema` by default, so the schema
    and tool descriptions the library generates from it contain no class docstring; fields, validation and
    serialization are those of ``model``.

    供自行生成 schema、不经 :func:`resolve_schema` 的库（Instructor、xai_sdk）使用的 ``model`` 子类。

    子类不带 docstring，``model_json_schema`` 默认改用 :class:`_ModelFacingJsonSchema`，库据此生成的 schema
    与工具说明都不含类 docstring；字段、校验与序列化沿用 ``model``。
    """

    def model_json_schema(cls: type[M], *args: Any, **kwargs: Any) -> dict[str, Any]:
        # The subclass adds or changes no fields, so its schema is the model's schema; class docstrings are stripped by the generator
        # 子类不增改字段，schema 即 model 的 schema；类 docstring 由生成器剥除
        kwargs.setdefault("schema_generator", _ModelFacingJsonSchema)
        return model.model_json_schema(*args, **kwargs)

    namespace = {
        "__doc__": None,
        "__module__": model.__module__,
        "__qualname__": model.__qualname__,
        "model_json_schema": classmethod(model_json_schema),
    }
    return type(model)(model.__name__, (model,), namespace)


def resolve_schema(schema: dict | type[BaseModel]) -> dict:
    """Convert response_schema into a plain JSON Schema dict without $ref to send to the model.

    - BaseModel subclass: generate the schema without class docstring descriptions (see :class:`_ModelFacingJsonSchema`), then inline $ref
    - dict: inline $ref directly (if any)

    将 response_schema 转为发给模型的、无 $ref 的纯 JSON Schema dict。

    - BaseModel 子类: 生成不含类 docstring 说明的 schema（见 :class:`_ModelFacingJsonSchema`）后内联 $ref
    - dict: 直接内联 $ref（如果有）
    """
    if isinstance(schema, type):
        schema_dict: dict = schema.model_json_schema(schema_generator=_ModelFacingJsonSchema)
    else:
        schema_dict = schema

    defs = schema_dict.get("$defs", {})
    if not defs:
        return schema_dict

    def _inline(obj: Any, visited_refs: frozenset[str] = frozenset()) -> Any:
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_name = obj["$ref"].split("/")[-1]
                if ref_name in visited_refs:
                    raise ValueError(f"检测到 schema 中的循环引用: {ref_name}")
                resolved = _inline(defs[ref_name], visited_refs | {ref_name})
                extra = {k: v for k, v in obj.items() if k != "$ref"}
                return {**resolved, **extra} if extra else resolved
            return {k: _inline(v, visited_refs) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_inline(item, visited_refs) for item in obj]
        return obj

    result: dict = _inline(schema_dict)
    result.pop("$defs", None)
    return result


def is_valid_json(text: str) -> bool:
    """Determine whether the string is valid JSON.

    Some native structured channels (common with OpenAI-compatible proxies and custom providers) silently ignore the
    structured output parameter and return plain text/markdown, which must trigger the fallback.

    判断字符串是否为合法 JSON。

    一些原生结构化通道（OpenAI 兼容代理、自定义供应商常见情况）会静默忽略结构化输出
    参数并返回纯文本/markdown，需要据此触发降级。
    """
    if not text or not text.strip():
        return False
    try:
        json.loads(text)
        return True
    except (ValueError, TypeError):
        return False


def summarize_validation_error(exc: ValidationError) -> str:
    """Compress a ValidationError into a short field-location summary.

    Only takes the field paths (loc) and error count, **without the model's raw input values**: those can be large and
    would write model-generated content into logs (leaking via diagnostic log bundles), and it also keeps a single log
    line from bloating to several KB. include_input=False removes input at the source so raw values never enter the
    error dict (defense in depth).

    把 ValidationError 压成简短的字段定位摘要。

    只取字段路径（loc）与错误数，**不含模型原始输入值**——后者可能很大且会把
    模型生成内容写进日志（经诊断日志打包外泄），也避免单条日志膨胀到数 KB。
    用 include_input=False 在源头剔除 input，不让原始值进入 error dict（防御纵深）。
    """
    locs = [
        ".".join(str(part) for part in err.get("loc", ())) or "<root>" for err in exc.errors(include_input=False)[:5]
    ]
    suffix = "…" if exc.error_count() > 5 else ""
    return f"{exc.error_count()} 处字段不符（{', '.join(locs)}{suffix}）"


def structured_fallback_reason(text: str, response_schema: dict | type | None, *, strict: bool = True) -> str | None:
    """Determine whether the HTTP 200 response of a native structured call needs to fall back to the validated path.

    A pure function independent of provider / client type / native API shape, shared by all text backends. Returns the
    fallback reason (for logging); None means the native output can be used directly. Two trigger scenarios:

    1. Non-JSON response: the provider silently ignored the structured output parameter and emitted plain text/markdown.
    2. Valid JSON that violates the schema: the provider accepts the structured output parameter but does not really
       enforce the schema (common with proxy gateways / non-first-party models), with invalid enum values or missing
       required fields. If such JSON were let through it would only raise a bare ValidationError at downstream
       Pydantic validation or rendering.

    ``strict`` is passed by the caller to match whether each backend's native request declares strict: backends that
    declare strict use strict=True (coercible values that do not match the type strictly, such as "30" for an int
    field, also count as schema not enforced); backends that do not declare strict use strict=False (tolerating
    coercible values, falling back only for output that truly cannot satisfy the schema), to avoid unnecessary billed
    fallback calls for valid responses the provider already accepted.

    Only Pydantic models can be schema-validated; dict schemas have no model, so the existing "only check for valid
    JSON" behavior is kept without tightening. response_schema None (no structured output intent) is treated as no
    fallback needed.

    判断原生结构化调用 HTTP 200 的返回是否需要降级到带校验的路径。

    与 provider / client 类型 / 原生 API 形态无关的纯函数，全文本后端共享。返回降级原因
    （用于日志）；None 表示原生输出可直接采用。两类触发场景：

    1. 返回非 JSON：供应商静默忽略结构化输出参数，吐出纯文本/markdown。
    2. 返回违反 schema 的合法 JSON：供应商接受结构化输出参数却不真正强制 schema（代理网关 /
       非原厂模型常见），枚举值非法或缺必填字段。此类违例 JSON 若直接放行，会一路漏到下游
       Pydantic 校验或渲染处才抛裸 ValidationError。

    ``strict`` 由调用方按各后端原生请求是否声明 strict 传入对齐：声明了 strict 的后端用
    strict=True（可强转但类型不严格匹配的值，如 int 字段给 "30"，也判为未强制 schema）；
    未声明 strict 的后端用 strict=False（容忍可强转值，只对真正无法满足 schema 的输出降级），
    避免对供应商已接受的合法响应触发多余的计费降级调用。

    仅 Pydantic 模型可做 schema 校验；dict schema 无对应模型，沿用「仅校验是否合法 JSON」的
    既有行为，不额外收紧。response_schema 为 None（无结构化输出诉求）直接视为无需降级。
    """
    if response_schema is None:
        # Plain text generation has no structured intent; the native output is used directly and must not be misjudged as "non-JSON" to trigger fallback.
        # 纯文本生成无结构化诉求，原生输出直接采用，不能按「非 JSON」误判触发降级。
        return None
    if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
        # A single model_validate_json parse covers both "non-JSON" and "violates the schema".
        # model_validate_json 单次解析即同时覆盖「非 JSON」与「违反 schema」两种情况。
        try:
            response_schema.model_validate_json(text, strict=strict)
        except ValidationError as exc:
            return f"返回内容不满足 response_schema（供应商可能未强制 schema）：{summarize_validation_error(exc)}"
        return None
    if not is_valid_json(text):
        return "返回非 JSON 内容（供应商可能未支持结构化输出）"
    return None


# Max calls of the prompt injection fallback: the first schema injection + one retry with validation error feedback.
# Each is a billed call; for models/proxy gateways that still fail twice in a row after schema injection, more retries gain next to nothing.
# prompt 注入降级的最大调用次数：首次注入 schema + 一次带校验错误反馈的重试。
# 每次都是计费调用；schema 注入后仍两连败的模型/代理网关，再多重试收益趋零。
PROMPT_JSON_FALLBACK_MAX_ATTEMPTS = 2


async def prompt_json_fallback(
    request: TextGenerationRequest,
    generate_once: Callable[[TextGenerationRequest], Awaitable[TextGenerationResult]],
    *,
    provider: str,
    model: str,
) -> TextGenerationResult:
    """Prompt injection fallback: write the schema into the prompt and resend as a plain text call, validate after stripping fences, retry with feedback on failure.

    The trigger is a provider rejecting or silently ignoring wire-level structured parameters; then writing the schema
    constraints into the prompt is the only means not relying on wire parameters. ``generate_once`` is the backend's
    single native call (with logging, truncation handling and transient error retries); the request it receives has
    ``response_schema`` cleared; it is still a structured output attempt, so truncation must be handled by the
    structured rule. After validation passes, normalized JSON (model_dump_json) is returned, so downstream parsing always succeeds.

    Prompt 注入降级：schema 写进 prompt 重发纯文本调用，剥栅栏后校验，失败带反馈重试。

    触发场景是供应商拒收或静默忽略 wire 级结构化参数，此时把 schema 约束写进 prompt 是唯一
    不依赖 wire 参数的手段。``generate_once`` 是后端的单次原生调用（含日志、截断处理与瞬态
    错误重试），收到的请求 ``response_schema`` 已置空；它仍是一次结构化输出尝试，截断须按
    结构化口径处理。校验通过后返回规范化 JSON（model_dump_json），下游解析必定成功。
    """
    assert request.response_schema is not None  # guaranteed by the caller (generate's re-validation branch) / 调用方（generate 复验分支）保证
    schema_dict = resolve_schema(request.response_schema)
    schema_model = request.response_schema if isinstance(request.response_schema, type) else None

    base_prompt = (
        f"{request.prompt}\n\n"
        "仅输出一个符合以下 JSON Schema 的 JSON 对象，"
        "不要输出任何解释、前后缀或 markdown 代码栅栏：\n"
        f"{json.dumps(schema_dict, ensure_ascii=False)}"
    )
    feedback = ""
    billed = TextGenerationResult(text="", provider=provider, model=model)
    last_reason = ""
    for _attempt in range(PROMPT_JSON_FALLBACK_MAX_ATTEMPTS):
        fb_request = replace(request, prompt=base_prompt + feedback, response_schema=None)
        fb_result = await generate_once(fb_request)
        merge_billed_usage(billed, fb_result)

        text = strip_json_code_fences(fb_result.text)
        normalized, last_reason = _validate_fallback_text(text, schema_model)
        if normalized is not None:
            billed.text = normalized
            return billed
        feedback = f"\n\n上次输出不符合要求（{last_reason}），请严格按上述 JSON Schema 重新输出纯 JSON。"
        _logger.warning(
            "prompt 注入降级输出校验未通过（%s），带反馈重试；模型原始输出：%s",
            last_reason,
            truncate_for_log(text),
        )

    raise StructuredOutputExhaustedError(
        provider=provider,
        model=model,
        reason=f"prompt 注入降级 {PROMPT_JSON_FALLBACK_MAX_ATTEMPTS} 次尝试后输出仍不合规（{last_reason}）",
    )


def _validate_fallback_text(text: str, schema_model: type | None) -> tuple[str | None, str]:
    """Validate and normalize fallback output. Returns (normalized JSON text, "") or (None, failure reason).

    Pydantic models go through full schema validation and are normalized with model_dump_json; dict schemas have no
    model and keep the "valid JSON passes" rule (aligned with structured_fallback_reason's behavior for dict schemas).

    校验降级输出并规范化。返回 (规范化 JSON 文本, "") 或 (None, 失败原因)。

    Pydantic 模型走完整 schema 校验并以 model_dump_json 规范化；dict schema 无对应模型，
    沿用「合法 JSON 即放行」口径（与 structured_fallback_reason 对 dict schema 的行为对齐）。
    """
    if schema_model is not None and issubclass(schema_model, BaseModel):
        try:
            return schema_model.model_validate_json(text).model_dump_json(), ""
        except ValidationError as exc:
            return None, summarize_validation_error(exc)
    if is_valid_json(text):
        return text, ""
    return None, "输出不是合法 JSON"


class TextBackend(Protocol):
    """Text generation backend protocol. / 文本生成后端协议。"""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> set[TextCapability]: ...

    async def generate(self, request: TextGenerationRequest) -> TextGenerationResult: ...
