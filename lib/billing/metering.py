"""Price table calculator and usage contract (docs/adr/0100).

Call channels report :class:`Usage` per the usage contract: non-overlapping meter usage and selection dimension values;
uncached input, cache read and the two cache write tiers sum to the total input; each vendor's usage semantic
differences are handled by the call channel before reporting. Character count is the exception: the call channel only
reports the raw text, and the calculator counts characters per the "Han character counts as 2" attribute on the entry's
price table.

:func:`price_usage` sums over the price table's meters: cost = multiplier × Σ max(usage − free quota, 0) × unit
price(selection dimensions), without dispatching on pricing shape, media type or provider. When no unit price is found
it returns :class:`Unpriced` with a reason, without falling back to another model or tier.

When usage is reported but the price table has no such meter, it is handled by meter kind
(``arcreel_catalog_core.MeterKind``): token kinds are part of one token total, so a missing unit price counts as unit
price not found, not as 0; count kinds (seconds, images, call counts, etc.) are each an independent billing axis, and
their absence from the price table means the vendor doesn't charge for it. Call channels can therefore truthfully report
all count-kind usage that actually occurred without knowing which item the entry is priced by.

One call may contain several provider requests (requests discarded in the fallback chain were billed too):
:func:`price_requests` places each request in its context tier by its own total input, prices it separately, then sums the costs.

价目表计算器与用量契约（docs/adr/0100）。

调用通道按用量契约上报 :class:`Usage`：互不重叠的计量项用量与选择维度取值，未缓存输入、缓存读与两档缓存写之和
等于总输入；各家 usage 的语义差异由调用通道在上报前处理。字符数是例外：调用通道只上报原始文本，字符数由计算器
按条目价目表上的「汉字计 2」属性统计。

:func:`price_usage` 按价目表的计量项求和：费用 = 倍率 × Σ max(用量 − 免费额度, 0) × 单价(选择维度)，不按定价
形状、媒体类型或供应商分派。查不到单价时返回 :class:`Unpriced` 并带原因，不回落到别的模型或别的档。

上报了用量、价目表却没有这个计量项时，按计量项的类别处理（``arcreel_catalog_core.MeterKind``）：token 类是同一份
token 总量拆出的一部分，缺单价就算查不到单价，不按 0 计；计数类（秒数、张数、调用次数等）各是一条独立的计费轴，
价目表没有它表示厂商不按它收费。调用通道因此可以如实上报实际发生的全部计数类用量，不必知道条目按哪一项计价。

一次调用可能含多次供应商请求（降级链上被丢弃的请求也已计费）：:func:`price_requests` 让每次请求按自己的输入
总量落上下文档、单独计价，再把费用相加。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from arcreel_catalog_core import CJK_DOUBLE_METER, METERS, Meter, PriceTable

#: Input-kind meters counted in the total used for context tier placement: a single request's total input tokens including cache reads and writes.
#: 计入上下文档落档总量的输入类计量项：单次请求的输入 token 总量含缓存读写。
_INPUT_TOKEN_METERS = frozenset(
    {
        "uncached_input_tokens",
        "cache_read_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "audio_input_tokens",
        "uncached_image_input_tokens",
        "image_cache_read_tokens",
    }
)

_CONTEXT_TIER = "context_tier"

# Han characters for "Han character counts as 2": CJK unified ideographs (including extensions) and compatibility ideographs.
# 「汉字计 2」中的汉字：CJK 统一表意文字（含扩展区）与兼容表意文字。
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]")


class UnpricedReason(StrEnum):
    """未定价的原因，原样写入 ``api_calls.unpriced_reason``。"""

    #: The provider model doesn't map to a model entry. / 供应商模型对应不上模型条目。
    NO_ENTRY = "no_entry"
    #: The entry has no price table. / 条目没有价目表。
    NO_PRICE_TABLE = "no_price_table"
    #: Unit price not found: the rate table lacks the requested dimension combination and has no default; a token-kind meter with reported
    #: usage is not in the price table; or at settlement none of the price table's meters got any usage.
    #: 查不到单价：单价表缺少请求的维度组合且没有默认值；上报了用量的 token 类计量项不在价目表里；或结算时
    #: 价目表的计量项一项用量都没有拿到。
    MISSING_RATE = "missing_rate"
    #: The price table depends on a currency, meter or selection dimension the current code doesn't recognize. / 价目表依赖当前代码不认识的币种、计量项或选择维度。
    UNRECOGNIZED_TERMS = "unrecognized_terms"
    #: Only for pre-submit estimates: a meter in the price table has usage that can neither be derived from the request facts nor has an estimation function.
    #: 只用于提交前预估：价目表里有计量项的用量既不能从请求事实得出，也没有估算函数。
    NO_USAGE_ESTIMATE = "no_usage_estimate"


@dataclass(frozen=True)
class Usage:
    """Usage reported by one provider call.

    ``quantities`` gives usage per meter (in the meter's counting unit, e.g. tokens, images, seconds), non-overlapping;
    ``dimensions`` are the selection dimension values, taken from the request fields actually sent to the provider, with
    unsent dimensions omitted; ``text`` is the raw text sent when priced by character. The context tier is not reported by
    the call channel; the calculator places it by total input tokens.

    一次供应商调用上报的用量。

    ``quantities`` 按计量项给出用量（按计量项的计数单位，如 token 数、张数、秒数），各项互不重叠；
    ``dimensions`` 是选择维度的取值，取自真正发给供应商的请求字段，没发送的维度不写；``text`` 是按字符计价时
    发送的原始文本。上下文档不由调用通道上报，由计算器按输入 token 总量落档。
    """

    quantities: Mapping[str, float] = field(default_factory=dict)
    dimensions: Mapping[str, object] = field(default_factory=dict)
    text: str | None = None


def token_usage(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None = None,
    cache_write_5m_tokens: int | None = None,
    cache_write_1h_tokens: int | None = None,
    output_meter: str = "output_tokens",
) -> dict[str, float]:
    """Split non-overlapping token meters from the total input and the three cache figures.

    ``input_tokens`` is the total input (including cache reads and writes); uncached input = total input − cache read −
    the two cache write tiers; unreported items are not written.

    按总输入与三项缓存拆出互不重叠的 token 计量项。

    ``input_tokens`` 是总输入（含缓存读写），未缓存输入 = 总输入 − 缓存读 − 两档缓存写；没有上报的项不写。
    """
    caches = {
        "cache_read_tokens": cache_read_tokens or 0,
        "cache_write_5m_tokens": cache_write_5m_tokens or 0,
        "cache_write_1h_tokens": cache_write_1h_tokens or 0,
    }
    quantities: dict[str, float] = {meter: value for meter, value in caches.items() if value}
    if input_tokens is not None:
        quantities["uncached_input_tokens"] = max(input_tokens - sum(caches.values()), 0)
    if output_tokens is not None:
        quantities[output_meter] = output_tokens
    return quantities


def count_characters(text: str, *, cjk_double: bool) -> int:
    """Usage of the character-count meter: with ``cjk_double`` each Han character counts as 2 characters, other characters as 1.

    字符数计量项的用量：``cjk_double`` 时每个汉字计 2 个字符，其余字符计 1 个。"""
    if not cjk_double:
        return len(text)
    return len(text) + len(_HAN.findall(text))


@dataclass(frozen=True)
class Priced:
    amount: float
    currency: str


@dataclass(frozen=True)
class Unpriced:
    reason: UnpricedReason


Cost = Priced | Unpriced


def _quantity(meter: Meter, usage: Usage) -> float | None:
    """The meter's usage; ``None`` when usage was not reported. / 计量项的用量；用量没有上报时为 ``None``。"""
    if meter.meter == CJK_DOUBLE_METER and usage.text is not None:
        return count_characters(usage.text, cjk_double=meter.cjk_double)
    return usage.quantities.get(meter.meter)


def _context_tier(table: PriceTable, usage: Usage) -> int | None:
    """The context tier the whole request falls into: price table values are the tiers' lower bounds (exclusive); take the highest tier whose bound the total exceeds; ``None`` when untiered.

    整个请求落入的上下文档：价目表各取值为档的下界（不含），取总量大于它的最大一档；不分档时为 ``None``。"""
    bounds = sorted(
        {bound for meter in table.meters for row in meter.rows if isinstance(bound := row.when.get(_CONTEXT_TIER), int)}
    )
    if not bounds:
        return None
    total = sum(quantity for meter, quantity in usage.quantities.items() if meter in _INPUT_TOKEN_METERS)
    reached = [bound for bound in bounds if total > bound]
    return reached[-1] if reached else bounds[0]


def _same(expected: object, actual: object) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.casefold() == actual.casefold()
    return type(expected) is type(actual) and expected == actual


def _rate(meter: Meter, dimensions: Mapping[str, object]) -> float | None:
    """The meter's unit price under the request dimensions; dimensions missing from the request take the meter's default; returns ``None`` if still no row matches.

    计量项在请求维度下的单价；请求缺少的维度取计量项的默认值，仍对不上任何一行时返回 ``None``。"""
    values = {dim: dimensions.get(dim, meter.defaults.get(dim)) for dim in meter.dims}
    for row in meter.rows:
        if all(values.get(dim) is not None and _same(expected, values[dim]) for dim, expected in row.when.items()):
            return row.price
    return None


def _unpriced_meter(table: PriceTable, usage: Usage) -> bool:
    """Whether a token-kind meter not in the price table was reported: those tokens have no unit price and can't be counted as 0.

    A count-kind meter absent from the price table means the vendor doesn't charge for it, which is not "unit price not
    found". Unknown meters are treated as token kind.

    是否上报了价目表没有的 token 类计量项：这部分 token 查不到单价，不能按 0 计。

    计数类计量项不在价目表里表示厂商不按它收费，不算查不到单价。不认识的计量项按 token 类处理。
    """
    priced = {meter.meter for meter in table.meters}
    return any(
        quantity > 0 and meter not in priced and _is_token_meter(meter) for meter, quantity in usage.quantities.items()
    )


def _is_token_meter(meter: str) -> bool:
    spec = METERS.get(meter)
    return spec is None or spec.kind == "token"


def price_usage(
    table: PriceTable | None,
    usage: Usage,
    *,
    multiplier: float,
    entry_found: bool = True,
    estimate: bool = False,
) -> Cost:
    """Compute the reference cost of one provider request from the price table; shared by settlement and pre-submit estimates.

    ``multiplier`` is the product of the provider multiplier and the provider model multiplier; when 0 it records 0
    directly without lookup; ``entry_found`` False means the provider model doesn't map to a model entry. Meters in the
    price table with usage 0 or not reported don't take part in lookup: at settlement, a provider not reporting means no
    such usage, but when none of the price table's meters got any usage the call's cost can't be known, so it is recorded
    as unpriced rather than silently 0; when ``estimate`` is true usage comes from request facts and estimation functions,
    and if one is missing it can't be estimated, recorded as unpriced. A reported token-kind meter with usage above 0 that
    the price table lacks is recorded as unpriced (:attr:`UnpricedReason.MISSING_RATE`); count kinds absent from the price
    table are not billed.

    按价目表计算一次供应商请求的参考费用；结算与提交前预估共用。

    ``multiplier`` 是供应商倍率与供应商模型倍率之积，为 0 时直接记 0、不查价；``entry_found`` 为 False 表示
    供应商模型对应不上模型条目。价目表里用量为 0 或没有上报的计量项不参与查价：结算时供应商没回报就是没有这项
    用量，但价目表的计量项一项用量都没有拿到时，这次调用的费用无从得知，记为未定价，不静默记 0；``estimate``
    为真时用量来自请求事实与估算函数，缺一项就无从估算，记为未定价。上报的 token 类计量项用量大于 0、价目表
    却没有这个计量项时记为未定价（:attr:`UnpricedReason.MISSING_RATE`），计数类不在价目表里的不计费。
    """
    if multiplier == 0:
        return Priced(0.0, table.currency if table is not None and table.is_priceable else "USD")
    if not entry_found:
        return Unpriced(UnpricedReason.NO_ENTRY)
    if table is None:
        return Unpriced(UnpricedReason.NO_PRICE_TABLE)
    if not table.is_priceable:
        return Unpriced(UnpricedReason.UNRECOGNIZED_TERMS)
    if _unpriced_meter(table, usage):
        return Unpriced(UnpricedReason.MISSING_RATE)
    if not estimate and table.meters and all(_quantity(meter, usage) is None for meter in table.meters):
        return Unpriced(UnpricedReason.MISSING_RATE)

    dimensions = dict(usage.dimensions)
    tier = _context_tier(table, usage)
    if tier is not None:
        dimensions[_CONTEXT_TIER] = tier

    total = 0.0
    for meter in table.meters:
        quantity = _quantity(meter, usage)
        if quantity is None and estimate:
            return Unpriced(UnpricedReason.NO_USAGE_ESTIMATE)
        billable = (quantity or 0) - (meter.free_allowance or 0)
        if billable <= 0:
            continue
        rate = _rate(meter, dimensions)
        if rate is None:
            return Unpriced(UnpricedReason.MISSING_RATE)
        total += billable / METERS[meter.meter].quantity_per_price * rate
    return Priced(total * multiplier, table.currency)


def price_requests(
    table: PriceTable | None,
    requests: Sequence[Usage],
    *,
    multiplier: float,
    entry_found: bool = True,
    estimate: bool = False,
) -> Cost:
    """Sum of costs of all provider requests in one call: each request placed in its own context tier and priced separately; if any one is unpriced the whole call is unpriced.

    Parameters as in :func:`price_usage`.

    一次调用所含各次供应商请求的费用之和：每次请求单独落上下文档、单独计价，任一次未定价则整笔未定价。

    参数含义同 :func:`price_usage`。
    """
    costs = [
        price_usage(table, usage, multiplier=multiplier, entry_found=entry_found, estimate=estimate)
        for usage in requests
    ] or [price_usage(table, Usage(), multiplier=multiplier, entry_found=entry_found, estimate=estimate)]
    for cost in costs:
        if isinstance(cost, Unpriced):
            return cost
    priced = [cost for cost in costs if isinstance(cost, Priced)]
    return Priced(sum(cost.amount for cost in priced), priced[0].currency)
