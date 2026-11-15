"""Single source of truth for speech rate estimation.

Funnels "how many seconds it takes to read a piece of spoken text" into one place, shared by duration suggestions,
episode-volume conversion and the upper-bound check of speech volume against scene duration, so that the call sites
don't each keep their own speech rate constants and drift. Subtitle timing does not read the speech rate.

Speech rate is expressed as "reading units / second"; the per-language counting of reading units reuses
``lib.infra.text_metrics`` (zh counts Han characters / CJK punctuation, en / vi count words) -- so the unit
conversion naturally follows the language and there is no need for separate character rules for Chinese and
English. Rates are tunable, overridden per ``source_language``, and fall back to the default; a new language only
needs registering in ``SPEECH_RATE_UPS_BY_LANGUAGE``, call sites hard-code no numbers.

Precedence: project-level override (top-level ``speech_rate_units_per_second`` in ``project.json``, parsed via
``project_speech_rate_override``) > language default > global default. The override is applied only here;
consumers still get values only through this module's two functions and don't read the project.json field themselves.

语速估算单一真相源。

把「一段口播文本朗读需多少秒」收敛到一处，供时长建议、分集体量折算与说话量对场景
时长的上界校验共用，避免各处各自维护一套语速常量而漂移。字幕定时不读语速。

语速以「阅读单位 / 秒」表示，阅读单位的语言裁剪口径复用 ``lib.infra.text_metrics``
（zh 计汉字 / CJK 标点，en / vi 计词）——因此单位换算天然随语言切换，不必为
中英文各写一套字符规则。语速值可调、按 ``source_language`` 覆盖、缺省回退默认；
新增语言只在 ``SPEECH_RATE_UPS_BY_LANGUAGE`` 登记，调用点不写死任何数值。

取值优先级：项目级覆盖（``project.json`` 顶层 ``speech_rate_units_per_second``，经
``project_speech_rate_override`` 解析）> 语言默认 > 全局默认。覆盖只在这里叠加，
消费方仍只经本模块两个函数取值，不各自读 project.json 字段。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.infra.text_metrics import count_reading_units

#: Default speech rate (reading units / second). Intelligible Chinese voiceover is commonly about 4–6 characters / second; 5 is the median.
#: Unregistered / missing languages fall back to this value (aligned with ``count_reading_units`` counting unknown languages as zh characters).
#: 默认语速（阅读单位 / 秒）。中文可懂配音常见约 4–6 字 / 秒，取 5 为中位。
#: 未登记 / 缺失语言回退此值（与 ``count_reading_units`` 未知语言按 zh 计字的口径对齐）。
DEFAULT_SPEECH_RATE_UPS: float = 5.0

#: Per-language speech rate override (reading units / second), keyed by the project's ``source_language`` (zh / en / vi / ru).
#: For en / vi / ru the reading unit is the "word"; normal speech is about 2–3 words / second, 2.5 is used; this is not directly
#: comparable with zh's "characters / second", so it must be registered per language rather than as one global number. Values
#: are tunable estimates, fine-tune them against actual voiceover pacing.
#: 按语言代码覆盖语速（阅读单位 / 秒），键用项目 ``source_language``（zh / en / vi / ru）。
#: en / vi / ru 的阅读单位是「词」，正常口语约 2–3 词 / 秒，取 2.5；与 zh 的「字 / 秒」不可
#: 直接通约，故必须分语言登记而非全局一个数值。值为可调估算，按实际配音节奏微调。
SPEECH_RATE_UPS_BY_LANGUAGE: dict[str, float] = {
    "zh": 5.0,
    "en": 2.5,
    "vi": 2.5,
    "ru": 2.5,
}


#: Top-level field name in ``project.json`` for the project-level speech rate override (reading units / second).
#: Different from the TTS provider's voiceover multiplier ``narration_speed``: that is a multiplier, this is an absolute rate.
#: 项目级语速覆盖在 ``project.json`` 的顶层字段名（阅读单位 / 秒）。
#: 与 TTS 供应商配音倍率 ``narration_speed`` 是两码事：后者是倍率、前者是绝对速度。
SPEECH_RATE_FIELD: str = "speech_rate_units_per_second"

#: Hard range (inclusive, reading units / second) of the project-level speech rate override. The loose range only blocks obvious
#: mis-entries (such as a TTS multiplier or a millisecond value); within the range no preference hints are given.
#:
#: The lower bound does more than block ≤0; it also leaves headroom for downstream time representations: a hypothetical single
#: text of 1e6 reading units (far beyond any real spoken text) at 0.001 reading units / second estimates to 1e9 seconds, which is
#: 1e15 in JianYing draft microseconds, still three orders of magnitude below the int64 limit. If the lower bound only required
#: "the estimate is finite", acceptable rates could go down to around 1e-302, at which point any downstream multiplier (microsecond
#: conversion is one) could push a finite value back to inf or overflow the integer type -- what must be guarded is headroom, not
#: finiteness. 0.001 means 1000 seconds per reading unit; real speech is far above that.
#: 项目级语速覆盖的硬区间（闭区间，阅读单位 / 秒）。宽松区间只拦明显误输入（如把 TTS
#: 倍率或毫秒数填进来），区间内不做任何倾向性提示。
#:
#: 下界不止于拦掉 ≤0，它同时给下游时间表示留出余量：取 1e6 阅读单位（远超任何真实口播
#: 文本）的假想单段文本，按 0.001 阅读单位 / 秒估算得 1e9 秒，换算成剪映草稿的微秒是 1e15，
#: 离 int64 上限仍有三个数量级。若下界改为只要求「估算结果是有限数」，可接受语速会低到
#: 1e-302 量级，此时任何下游乘数（微秒换算即是其一）都能把有限值重新推成 inf 或撑破整数
#: 类型——要守的是余量而非有限性。0.001 意味着每个阅读单位读满 1000 秒，真实口播远在其上。
MIN_SPEECH_RATE_UPS: float = 0.001
MAX_SPEECH_RATE_UPS: float = 20.0


def is_valid_speech_rate(value: float) -> bool:
    """Whether the value lies within the hard range of the project-level speech rate override (``0.001 <= value <= 20``).

    Frontend input validation, request model validation and the post-persistence read guard share this one yardstick,
    so the three places don't each write their own bounds.
    The input may be a value parsed directly from project.json, so two input pathologies are handled here and callers
    don't need to: JSON integer literals have no width limit and ``float()`` raises ``OverflowError`` for integers
    beyond double range; a JSON boolean parses to ``bool``, a subclass of ``int``, and ``float(True)`` gives ``1.0``,
    which would fall in the valid range. Both are judged "unusable". The range itself excludes ``inf`` and ``nan``
    (comparisons with the bounds are false for both), and values within the range guarantee that downstream duration
    conversion does not overflow; see ``MIN_SPEECH_RATE_UPS`` for the rationale.

    该数值是否落在项目级语速覆盖的硬区间内（``0.001 <= value <= 20``）。

    前端输入校验、请求模型校验与持久化后的读时守卫共用这一把尺，避免三处各写一套边界。
    入参允许是 project.json 直接解析出的值，故两类输入病理在这里收掉，调用方不必各自处理：
    JSON 整数字面量没有位宽上限，``float()`` 对超出双精度表示范围的整数抛 ``OverflowError``；
    JSON 布尔解析出的 ``bool`` 是 ``int`` 子类，``float(True)`` 得 ``1.0`` 会落进合法区间。
    两者一律判为「不可用」。区间本身同时排除 ``inf`` 与 ``nan``（两者与边界的比较均为假），
    区间内的值则保证下游时长换算不溢出，依据见 ``MIN_SPEECH_RATE_UPS``。
    """
    if isinstance(value, bool):
        return False
    try:
        rate = float(value)
    except OverflowError:
        return False
    return MIN_SPEECH_RATE_UPS <= rate <= MAX_SPEECH_RATE_UPS


def project_speech_rate_override(project: Mapping[str, Any] | None) -> float | None:
    """Parse the project-level speech rate override from project.json; unset / dirty / out-of-range values all return ``None``.

    Returning ``None`` means "no override"; callers pass it unchanged to the two functions below, which fall back to the
    language default -- projects without the field are always estimated with the language default. The write side
    (create / PATCH request models) already rejects out-of-range values with the same yardstick; the guard here is the
    read-time fallback for hand-edited project.json and legacy dirty data: an estimated speech rate is not worth crashing
    an already-paid generation on a dirty field.

    从 project.json 解析项目级语速覆盖，未填 / 脏值 / 越界一律返回 ``None``。

    返回 ``None`` 即「无覆盖」，调用方把它原样交给下面两个函数即回退语言默认——未填该
    字段的项目一律按语言默认估算。写入侧（创建 / PATCH 请求模型）已
    按同一把尺拒绝越界值，这里的守卫是对手改 project.json 与历史脏数据的读时兜底：估算
    语速不值得让一次已付费的生成崩在脏字段上。
    """
    if not isinstance(project, Mapping):
        return None
    raw = project.get(SPEECH_RATE_FIELD)
    # bool is a subclass of int and True would be taken as 1.0; exclude it explicitly so dirty data doesn't push the rate to 1 unit / second.
    # bool 是 int 的子类，True 会被当成 1.0；显式排除，避免脏数据把语速压到 1 单位 / 秒。
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    # Check the range before converting to float: values in range always convert safely; huge integers are rejected as out of range inside is_valid_speech_rate.
    # 先过区间再转 float：区间内的值必然可安全转换，超大整数在 is_valid_speech_rate 内按越界收掉。
    return float(raw) if is_valid_speech_rate(raw) else None


def speech_rate_units_per_second(language: str | None = None, override: float | None = None) -> float:
    """Return the effective speech rate (reading units / second): ``override`` first, otherwise the language default.

    ``override`` is the project-level override (parsed by ``project_speech_rate_override``); out-of-range / non-finite
    values are treated as no override. Language codes are case-insensitive; ``None`` / empty / unregistered languages
    fall back to ``DEFAULT_SPEECH_RATE_UPS``.

    返回生效语速（阅读单位 / 秒）：``override`` 优先，否则按语言取默认。

    ``override`` 是项目级覆盖（由 ``project_speech_rate_override`` 解析），越界 / 非有限数
    按无覆盖处理。语言代码大小写不敏感；``None`` / 空 / 未登记语言回退 ``DEFAULT_SPEECH_RATE_UPS``。
    """
    if override is not None and is_valid_speech_rate(override):
        return float(override)
    if not language:
        return DEFAULT_SPEECH_RATE_UPS
    return SPEECH_RATE_UPS_BY_LANGUAGE.get(language.strip().lower(), DEFAULT_SPEECH_RATE_UPS)


def estimate_spoken_seconds(text: str | None, language: str | None = None, override: float | None = None) -> float:
    """Estimate the seconds needed to read ``text`` aloud in ``language``.

    Rule: number of reading units ÷ speech rate (see ``lib.infra.text_metrics.count_reading_units`` for counting reading
    units; see ``speech_rate_units_per_second`` for rate precedence). None / empty string / whitespace-only /
    punctuation-only (no reading units) all count as 0 seconds -- it is a single term of the speech-volume sum, and
    call sites share the same conversion rather than each repeating it.

    估算 ``text`` 以 ``language`` 朗读所需秒数。

    口径：阅读单位数 ÷ 语速（阅读单位计法见 ``lib.infra.text_metrics.count_reading_units``；
    语速取值优先级见 ``speech_rate_units_per_second``）。None / 空串 / 纯空白 / 纯标点
    （无阅读单位）一律计 0 秒——它是说话量求和的单项，调用点共用同一换算、不各自重复。
    """
    if not text:
        return 0.0
    units = count_reading_units(text, language)
    if units <= 0:
        return 0.0
    return units / speech_rate_units_per_second(language, override)
