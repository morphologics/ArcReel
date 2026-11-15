"""Closed sets of catalog data: media types, currencies, meters, and selection dimensions.

These sets are defined in code and catalog data cannot invent its own. When the instance side meets a meter,
selection dimension, or currency outside the sets, the price list containing it is treated as unpriced; the build
side rejects it outright.

目录数据的封闭集合：媒体类型、币种、计量项与选择维度。

这些集合由代码定义，目录数据不能自创。实例侧遇到集合之外的计量项、选择维度或币种时，
所在价目表按未定价处理；构建侧直接拒绝。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

MediaType = Literal["text", "image", "video", "audio"]
MEDIA_TYPES: Final[tuple[MediaType, ...]] = ("text", "image", "video", "audio")

Currency = Literal["USD", "CNY"]
CURRENCIES: Final[tuple[Currency, ...]] = ("USD", "CNY")

_PER_MILLION: Final = 1_000_000
_PER_TEN_THOUSAND: Final = 10_000

# Category of a meter; it decides what happens when usage is reported but the price list has no such meter:
#
# - ``token``: one token total is reported split into non-overlapping meters (such as uncached input and cache reads).
#   If the unit price of any one is missing, the cost of those tokens cannot be known and the whole charge is recorded as unpriced.
# - ``count``: measured by countable quantities such as calls, duration, image count, and character count, each an independent billing axis
#   (such as a video's seconds and its reference image count). If the price list lacks one, the vendor does not charge for it, and it is not billed.
#: 计量项的类别，决定上报了用量、价目表却没有这个计量项时怎样处理：
#:
#: - ``token``：一份 token 总量按互不重叠的计量项拆开上报（如未缓存输入与缓存读），缺了哪一项的单价，
#:   这部分 token 的费用就无从得知，整笔记为未定价。
#: - ``count``：按次数、时长、张数、字符数这类可数的量计，各是一条独立的计费轴（如视频的秒数与参考图
#:   张数）。价目表没有这一项，表示厂商不按它收费，这一项不计费。
MeterKind = Literal["token", "count"]


@dataclass(frozen=True)
class MeterSpec:
    """Definition of one meter.

    ``quantity_per_price`` is the count that a unit price applies to: token meters are priced per million tokens,
    character counts per ten thousand characters, and the rest per image, per second, or per call. The free quota is
    counted in the counting unit itself and is unrelated to this conversion. ``kind`` is the meter's category, see
    :data:`MeterKind`.

    一个计量项的定义。

    ``quantity_per_price`` 是单价对应的计数量：token 类按每百万 token 标价，字符数按每万字标价，
    其余按每张、每秒、每次标价。免费额度按计数单位本身计，与这个换算无关。``kind`` 是计量项的类别，见
    :data:`MeterKind`。
    """

    media_types: frozenset[MediaType]
    quantity_per_price: int
    kind: MeterKind


def _token(media_types: tuple[MediaType, ...]) -> MeterSpec:
    return MeterSpec(frozenset(media_types), _PER_MILLION, "token")


def _count(media_types: tuple[MediaType, ...], quantity_per_price: int = 1) -> MeterSpec:
    return MeterSpec(frozenset(media_types), quantity_per_price, "count")


# Closed set of meters. Usage of the meters does not overlap: uncached input, cache reads, and the two cache-write tiers sum to total input.
#: 计量项的封闭集合。各计量项的用量互不重叠：未缓存输入、缓存读与两档缓存写之和等于总输入。
METERS: Final[Mapping[str, MeterSpec]] = MappingProxyType(
    {
        # token: the main meter for text models; text input to image and audio models is also counted by it.
        # token：文本模型的主计量；图片与音频模型的文字输入也按它计。
        "uncached_input_tokens": _token(("text", "image", "audio")),
        "cache_read_tokens": _token(("text", "image")),
        "cache_write_5m_tokens": _token(("text",)),
        "cache_write_1h_tokens": _token(("text",)),
        "output_tokens": _token(("text", "image")),
        "audio_input_tokens": _token(("text",)),
        # Image. / 图片
        "output_images": _count(("image",)),
        "input_images": _count(("image", "video")),
        "uncached_image_input_tokens": _token(("image",)),
        "image_cache_read_tokens": _token(("image",)),
        "image_output_tokens": _token(("image",)),
        # Video. Video tokens are converted from output seconds and frame size, and are two ways of measuring the same billing axis as seconds, so they are a count meter:
        # an entry priced per second does not take it, and an entry priced per token does not take seconds.
        # 视频。视频 token 由输出秒数与画面尺寸换算而来，与秒数是同一条计费轴的两种量法，归计数类：
        # 按秒计价的条目不收它，按 token 计价的条目不收秒数。
        "output_seconds": _count(("video",)),
        "video_tokens": _count(("video",), _PER_MILLION),
        "input_video_seconds": _count(("video",)),
        # Audio. / 音频
        "characters": _count(("audio",), _PER_TEN_THOUSAND),
        "audio_output_tokens": _token(("audio",)),
        # General. / 通用
        "requests": _count(MEDIA_TYPES),
    }
)

#: The only meter that can carry "each Chinese character counts as 2". / 唯一可以带「汉字计 2」的计量项。
CJK_DOUBLE_METER: Final = "characters"

ServiceTier = Literal["standard", "flex", "fast"]
SERVICE_TIERS: Final[tuple[ServiceTier, ...]] = ("standard", "flex", "fast")

DimensionKind = Literal["tier", "label", "flag", "seconds", "service_tier"]

# Closed set of selection dimensions and their value shapes. Values come from the request fields actually sent to the provider.
#
# - ``context_tier``: the value is the (exclusive) lower bound of the tier, a non-negative integer, with 0 for the first tier. A request falls into a tier when its
#   total input tokens (including cache reads and writes) exceed that value and do not exceed the next larger value in the price list,
#   and every meter of the whole request takes its unit price from that tier.
# - ``resolution``, ``quality``: non-empty strings.
# - ``audio`` (whether there is sound), ``input_video`` (input shape: whether it includes an input video), ``reference_to_video`` (input shape:
#   whether it is reference-to-video, i.e. reference images serve as the subject reference rather than first/last frames or plain text): booleans.
# - ``duration``: a positive integer number of seconds.
# - ``service_tier``: ``standard`` / ``flex`` / ``fast``.
#: 选择维度的封闭集合与取值形态。取值来自真正发给供应商的请求字段。
#:
#: - ``context_tier``（上下文档）：取值为该档的下界（不含），非负整数，首档写 0。单次请求的
#:   输入 token 总量（含缓存读写）大于某个取值、且不大于价目表中下一个更大的取值时落入该档，
#:   整个请求的各计量项都按这一档取单价。
#: - ``resolution``（分辨率）、``quality``（质量档）：非空字符串。
#: - ``audio``（是否有声）、``input_video``（输入形态：是否含输入视频）、``reference_to_video``（输入形态：
#:   是否是参考生视频，即以参考图作主体参考，而不是首尾帧或纯文本）：布尔值。
#: - ``duration``（时长档）：正整数秒。
#: - ``service_tier``（服务档）：``standard`` / ``flex`` / ``fast``。
DIMENSIONS: Final[Mapping[str, DimensionKind]] = MappingProxyType(
    {
        "context_tier": "tier",
        "resolution": "label",
        "quality": "label",
        "audio": "flag",
        "service_tier": "service_tier",
        "duration": "seconds",
        "input_video": "flag",
        "reference_to_video": "flag",
    }
)

# Media types a selection dimension applies to: when editing a price list, "price by condition" only lists dimensions usable for the entry's media type. A dimension's values come from the request fields actually sent to
# the provider, so the dimension applies to whichever media types have that field. This is guidance for editing, not a format constraint: the build side
# and the instance side do not reject on it when reading.
#: 选择维度适用的媒体类型：编辑价目表时「按条件分价」只列出条目媒体类型可用的维度。维度的取值来自真正发给
#: 供应商的请求字段，所以哪种媒体类型有这个字段，就适用于哪种媒体类型。这是编辑时的引导，不是格式约束：构建侧
#: 与实例侧读入不按它拒收。
DIMENSION_MEDIA_TYPES: Final[Mapping[str, frozenset[MediaType]]] = MappingProxyType(
    {
        "context_tier": frozenset({"text", "image", "audio"}),
        "resolution": frozenset({"image", "video"}),
        "quality": frozenset({"image"}),
        "audio": frozenset({"video"}),
        "service_tier": frozenset({"text", "image", "video"}),
        "duration": frozenset({"video"}),
        "input_video": frozenset({"video"}),
        "reference_to_video": frozenset({"video"}),
    }
)


def is_valid_dimension_value(dimension: str, value: object) -> bool:
    """Whether ``value`` is a valid value of the known selection dimension ``dimension``.

    ``value`` 是否是已知选择维度 ``dimension`` 的合法取值。
    """
    match DIMENSIONS.get(dimension):
        case "tier":
            return type(value) is int and value >= 0
        case "seconds":
            return type(value) is int and value > 0
        case "label":
            return isinstance(value, str) and value != ""
        case "flag":
            return type(value) is bool
        case "service_tier":
            return value in SERVICE_TIERS
        case None:
            return False
