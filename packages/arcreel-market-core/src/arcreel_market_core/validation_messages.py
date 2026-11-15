"""Structured carrier for validation messages (locale-neutral ``key + params``).

Diagnostics produced by validators flow to several consumption boundaries: web requests render by ``Accept-Language``, Agent tools always render with the
app's default language, and the market source CLI carries no translation catalog. So a message cannot be fixed into a bare string of some language
at the point of production -- produce structure, render at the boundary. This package carries no translation catalog: rendering always takes a translator from the caller; boundaries
without a catalog use :func:`code_translator` to output the ``key`` and the parameters themselves.

Values in ``params`` are substituted directly via ``str.format`` by default; words that must follow the language are wrapped in ``MessageRef``,
and complete nested sentences use ``ValidationMessage``; at render time both are translated into the same language first and then substituted.

校验消息的结构化载体（locale-neutral 的 ``key + params``）。

校验器产出的诊断会流向多个消费边界：Web 请求按 ``Accept-Language`` 渲染，Agent 工具固定用
应用的默认语言渲染，市场源 CLI 不带任何翻译目录。消息因此不能在产出点就定死成某种语言的裸
字符串——产出结构、边界渲染。本包不携带翻译目录：渲染一律由调用方传入 translator；手里没有
翻译目录的边界用 :func:`code_translator` 输出 ``key`` 与参数本身。

``params`` 里的值默认按 ``str.format`` 直出；需要跟随语言变化的词用 ``MessageRef`` 包一层，
完整的嵌套句子用 ``ValidationMessage``，渲染时都先按同一语言翻译再代入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Pass-through key used by ``ValidationMessage.literal``: the message body is already finished text (Pydantic errors,
# third-party exception text) with no translatable structure, and is output as-is after substitution.
#: ``ValidationMessage.literal`` 用的透传 key：消息体本身已是成品文本（Pydantic 报错、
#: 第三方异常文案），没有可翻译的结构，占位后原样输出。
LITERAL_KEY = "val_literal"

#: Translator used to render messages: ``translate(key, **params) -> str``. / 渲染消息用的 translator：``translate(key, **params) -> str``。
type Translator = Callable[..., str]


@dataclass(frozen=True)
class MessageRef:
    """A nested translation key in a parameter slot: at render time it is first translated to the current language, then substituted as a parameter into the outer message.

    参数位上的嵌套翻译键：渲染时先把它翻成当前语言，再作为参数代入外层消息。
    """

    key: str


@dataclass(frozen=True)
class MessageJoin:
    """A sequence of fragments in a parameter slot: literal text mixed with ``MessageRef``, each resolved in turn and joined with the separator.

    For cases where one parameter slot must carry several sub-messages (e.g. compressing multiple Pydantic errors into a one-line summary).

    参数位上的片段序列：字面文本与 ``MessageRef`` 混排，逐段解析后按分隔符拼接。

    供一个参数位需要承载多条子消息的场景（如把多条 Pydantic 报错压成一行摘要）。
    """

    parts: tuple[MessagePart, ...]
    separator: str = "; "


# Elements allowed in a fragment sequence: literal text, a nested translation key, or a further nested fragment sequence.
#: 片段序列里允许出现的元素：字面文本、嵌套翻译键，或再嵌一层的片段序列。
type MessagePart = str | MessageRef | MessageJoin


@dataclass(frozen=True)
class ValidationMessage:
    """A locale-neutral validation message.

    一条 locale-neutral 的校验消息。
    """

    key: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def literal(cls, text: str) -> ValidationMessage:
        """Wrap an already-composed string into a message, so sources with no translatable structure (Pydantic errors, etc.) reuse the same channel.

        把已成文的字符串包成消息，供无可翻译结构的来源（Pydantic 报错等）复用同一通道。
        """
        return cls(LITERAL_KEY, {"text": text})

    def render(self, translate: Translator) -> str:
        """Render to text via ``translate``; messages and translation keys nested in parameters use the same translator.

        按 ``translate`` 渲染成文本；嵌套在参数里的消息与翻译键用同一个 translator。
        """
        resolved = {name: _resolve_param(value, translate) for name, value in self.params.items()}
        return translate(self.key, **resolved)


def code_translator(key: str, **params: Any) -> str:
    """A translator without a translation catalog: outputs the ``key`` and the parameters themselves, and literal messages as-is.

    Shaped like ``val_market_slug_invalid(slug=My Slug)``, with parameters sorted by name and values substituted as-is without quotes, so text rendered from nested
    messages is not escaped layer by layer when substituted into the outer one. For boundaries such as the CLI and exception text that have no language context and should not carry a translation
    catalog.

    不带翻译目录的 translator：输出 ``key`` 与参数本身，字面消息原样输出。

    形如 ``val_market_slug_invalid(slug=My Slug)``，参数按名字排序、值原样代入不加引号——嵌套消息
    渲染出的文本再次代入外层时不会层层转义。供 CLI 与异常文本这类没有语言上下文、也不该携带翻译
    目录的边界使用。
    """
    if key == LITERAL_KEY:
        return str(params.get("text", ""))
    if not params:
        return key
    rendered = ", ".join(f"{name}={value}" for name, value in sorted(params.items()))
    return f"{key}({rendered})"


def _resolve_param(value: Any, translate: Translator) -> Any:
    """Resolve nested translation markers in a parameter value into text in the current language; other values are returned as-is.

    把参数值里的嵌套翻译标记解析成当前语言的文本，其余值原样返回。
    """
    if isinstance(value, ValidationMessage):
        return value.render(translate)
    if isinstance(value, MessageRef):
        return translate(value.key)
    if isinstance(value, MessageJoin):
        return value.separator.join(str(_resolve_param(part, translate)) for part in value.parts)
    return value
