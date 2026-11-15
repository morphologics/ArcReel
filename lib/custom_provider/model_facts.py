"""Effective model facts of a provider model: capability override > matching entry > runtime fallback (docs/adr/0104, 0105).

Model facts (text: image input, structured output, max output tokens; image: text-to-image, image-to-image; video: selectable
durations, resolutions and duration-linked constraints) are recorded on model entries. Provider models the user is
responsible for can replace any of these dimensions wholesale with a capability override, which has the highest priority;
dimensions not overridden take the matching entry's value. When no entry matches: selectable durations take the duration
preset inferred from the name (``[4, 8]`` when no family matches), other model facts have no constraint (``None``), and
out-of-range values are rejected upstream. Fallback results are computed only here and not written to any field.

Overrides of channel facts (video input mode, reference limits, prompt length limit, audio track form) are synthesized with
the call channel declaration by :mod:`lib.custom_provider.capabilities`, not in this module. Both kinds of dimensions share
the single sparse dict ``CustomProviderModel.capability_overrides``, with non-overlapping key names.

ComfyUI endpoint capabilities are derived only from node bindings (docs/adr/0082) and do not go through this module: callers
split by endpoint form. The only exception is the selectable durations of ComfyUI video model rows: a preference the user adds
or removes beyond the workflow's native duration, stored as a ``durations`` override and read via :func:`comfyui_duration_override`.

供应商模型的生效模型事实：能力覆盖 > 对应条目 > 运行时回落（docs/adr/0104、0105）。

模型事实（文本的图片输入、结构化输出、最大输出 token；图片的文生图、图生图；视频的可选时长、分辨率与
时长联动约束）记在模型条目上。用户负责的供应商模型可以用能力覆盖整维替换其中任一维度，覆盖优先级最高；
未覆盖的维度取对应条目的值。对应不上条目时：可选时长取按名字推断的时长预设（一个族都匹配不上时为
``[4, 8]``），其余模型事实不加约束（``None``），超出范围由上游拒绝。回落结果只在这里现算，不写入任何字段。

通道事实（视频的输入模式、参考上限、提示词长度上限、音轨形态）的覆盖由
:mod:`lib.custom_provider.capabilities` 与调用通道声明合成，不在本模块。两类维度共用
``CustomProviderModel.capability_overrides`` 这一个稀疏字典，键名互不重叠。

ComfyUI 端点的能力只从节点绑定推导（docs/adr/0082），不经过本模块：调用方按端点形态分流。唯一的例外是
ComfyUI 视频模型行的可选时长，它是用户在 workflow 原生时长之外增删的偏好，存为 ``durations`` 覆盖，经
:func:`comfyui_duration_override` 读取。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from arcreel_catalog_core import CatalogEntry, DurationConstraints, ImageFacts, TextFacts, VideoFacts
from lib.catalog import query as catalog
from lib.custom_provider.duration_presets import infer_supported_durations

logger = logging.getLogger(__name__)

#: Model-fact dimensions each media type can override. Audio has no model-fact dimensions. / 每种媒体类型可以覆盖的模型事实维度。音频没有模型事实维度。
MODEL_FACT_OVERRIDE_KEYS: Mapping[str, frozenset[str]] = {
    "text": frozenset({"image_input", "structured_output", "max_output_tokens"}),
    "image": frozenset({"text_to_image", "image_to_image", "sizes"}),
    "video": frozenset({"durations", "resolutions", "duration_constraints"}),
    "audio": frozenset(),
}

#: Keys of all model-fact dimensions, for merging into the write-side whitelist. / 全部模型事实维度的键，供写入侧白名单合并。
ALL_MODEL_FACT_OVERRIDE_KEYS: frozenset[str] = frozenset().union(*MODEL_FACT_OVERRIDE_KEYS.values())

_BOOL_KEYS = frozenset({"image_input", "structured_output", "text_to_image", "image_to_image"})
_CONSTRAINT_KEYS = frozenset({"by_resolution", "with_reference_images"})


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_duration_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_positive_int(item) for item in value)


def _is_constraints(value: object) -> bool:
    if not isinstance(value, dict) or not value.keys() <= _CONSTRAINT_KEYS:
        return False
    by_resolution = value.get("by_resolution", {})
    if not isinstance(by_resolution, dict):
        return False
    if any(
        not isinstance(res, str) or not res.strip() or not _is_duration_list(allowed)
        for res, allowed in by_resolution.items()
    ):
        return False
    with_reference = value.get("with_reference_images", [])
    return isinstance(with_reference, list) and all(_is_positive_int(item) for item in with_reference)


def model_fact_value_matches(key: str, value: object) -> bool:
    """Whether an override value can replace this model-fact dimension wholesale; write-side validation and read-side filtering share this check.

    Boolean dimensions accept only ``bool``; max output tokens is a positive integer; selectable durations and resolutions are
    non-empty lists; duration-linked constraints contain only the two keys ``by_resolution`` (resolution → non-empty duration list)
    and ``with_reference_images`` (duration list).

    覆盖值能否整维替换该模型事实维度；写入侧校验与读取侧过滤共用这一判定。

    布尔维度只认 ``bool``；最大输出 token 是正整数；可选时长、分辨率是非空列表；时长联动约束只含
    ``by_resolution``（分辨率 → 非空时长列表）与 ``with_reference_images``（时长列表）两个键。
    """
    if key in _BOOL_KEYS:
        return isinstance(value, bool)
    if key == "max_output_tokens":
        return _is_positive_int(value)
    if key == "durations":
        return _is_duration_list(value)
    if key == "resolutions":
        return isinstance(value, list) and bool(value) and all(isinstance(r, str) and r.strip() for r in value)
    if key == "duration_constraints":
        return _is_constraints(value)
    if key == "sizes":
        return isinstance(value, list) and bool(value) and all(_parse_size(item) is not None for item in value)
    return False


_SIZE_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$")


def _parse_size(value: object) -> tuple[int, int] | None:
    """``"1408x768"`` → ``(1408, 768)``; None when it isn't one."""
    match = _SIZE_RE.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    return (width, height) if width > 0 and height > 0 else None


_TYPE_NAMES: Mapping[str, str] = {
    "max_output_tokens": "int > 0",
    "durations": "list[int > 0]（非空）",
    "resolutions": "list[str]（非空）",
    "duration_constraints": "{by_resolution: {str: list[int > 0]}, with_reference_images: list[int > 0]}",
    "sizes": "list[\"<width>x<height>\"]（非空）",
}


def model_fact_type_name(key: str) -> str:
    """Expected value shape of a model-fact dimension, for 422 messages. / 模型事实维度期望的值形状，供 422 文案使用。"""
    return "bool" if key in _BOOL_KEYS else _TYPE_NAMES.get(key, "unknown")


def model_fact_overrides(overrides: object | None, media_type: str, *, model_id: str = "") -> dict[str, object]:
    """Take the model-fact dimensions of this media type from the override dict, dropping invalid values (logging a warning, so one dirty config doesn't break execution).

    取覆盖字典里该媒体类型的模型事实维度，丢弃不合法的值（落 warning，不让一条脏配置拖垮执行）。"""
    if not isinstance(overrides, Mapping):
        return {}
    keys = MODEL_FACT_OVERRIDE_KEYS.get(media_type, frozenset())
    applied: dict[str, object] = {}
    for key, value in overrides.items():
        if key not in keys:
            continue
        if not model_fact_value_matches(key, value):
            logger.warning("忽略 %s 的能力覆盖 %s=%r：值不合法，该维度回到条目或回落值", model_id, key, value)
            continue
        applied[key] = value
    return applied


@dataclass(frozen=True)
class TextModelFacts:
    """``None`` means no constraint on this dimension (no matching entry and no override). / ``None`` 表示这一维不加约束（对应不上条目且没有覆盖）。"""

    image_input: bool | None = None
    structured_output: bool | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ImageModelFacts:
    """``None`` means no constraint on this dimension (no matching entry and no override). / ``None`` 表示这一维不加约束（对应不上条目且没有覆盖）。"""

    text_to_image: bool | None = None
    image_to_image: bool | None = None
    #: The only sizes the model accepts (a fixed-size model), from the row's ``sizes`` override; ``None`` = any
    #: size. Requests take the allowed size closest to the one derived from aspect ratio and resolution.
    sizes: tuple[tuple[int, int], ...] | None = None


@dataclass(frozen=True)
class VideoModelFacts:
    """Selectable durations always have a value (the preset inferred from the name when no entry matches); resolutions ``None`` means no constraint.

    可选时长恒有值（对应不上条目时取按名字推断的预设）；分辨率为 ``None`` 表示不加约束。"""

    durations: tuple[int, ...] = ()
    resolutions: tuple[str, ...] | None = None
    duration_constraints: DurationConstraints = field(default_factory=DurationConstraints)


def model_entry(model_id: str, media_type: str, entry_binding: str | None = None) -> CatalogEntry | None:
    """The entry a provider model matches: entry binding first, then live lookup by name (docs/adr/0105). The media type comes from the call endpoint.

    供应商模型对应的条目：先看条目绑定，再按名字现查（docs/adr/0105）。媒体类型取调用端点。"""
    return catalog.find_entry(model_id, media_type=media_type, binding=catalog.binding_key(entry_binding, media_type))


def text_model_facts(model_id: str, overrides: object | None, *, entry_binding: str | None = None) -> TextModelFacts:
    """Effective model facts of a text provider model: capability override > matching entry; no constraint when neither.

    文本供应商模型的生效模型事实：能力覆盖 > 对应条目；都没有时不加约束。"""
    applied = model_fact_overrides(overrides, "text", model_id=model_id)
    entry = model_entry(model_id, "text", entry_binding)
    facts = entry.facts if entry is not None and isinstance(entry.facts, TextFacts) else None
    max_output_tokens = applied.get("max_output_tokens")
    if max_output_tokens is None and facts is not None:
        max_output_tokens = facts.max_output_tokens
    return TextModelFacts(
        image_input=_bool_fact(applied, "image_input", None if facts is None else facts.image_input),
        structured_output=_bool_fact(applied, "structured_output", None if facts is None else facts.structured_output),
        max_output_tokens=max_output_tokens if isinstance(max_output_tokens, int) else None,
    )


def image_model_facts(model_id: str, overrides: object | None, *, entry_binding: str | None = None) -> ImageModelFacts:
    """Effective model facts of an image provider model (not intersected with the call channel; for the intersection see :mod:`lib.backends.image_capabilities`).

    图片供应商模型的生效模型事实（未与调用通道取交集，交集见 :mod:`lib.backends.image_capabilities`）。"""
    applied = model_fact_overrides(overrides, "image", model_id=model_id)
    entry = model_entry(model_id, "image", entry_binding)
    facts = entry.facts if entry is not None and isinstance(entry.facts, ImageFacts) else None
    return ImageModelFacts(
        text_to_image=_bool_fact(applied, "text_to_image", None if facts is None else facts.text_to_image),
        image_to_image=_bool_fact(applied, "image_to_image", None if facts is None else facts.image_to_image),
        sizes=_sizes_fact(applied.get("sizes")),
    )


def _sizes_fact(value: object) -> tuple[tuple[int, int], ...] | None:
    if not isinstance(value, list):
        return None
    sizes = tuple(size for size in (_parse_size(item) for item in value) if size is not None)
    return sizes or None


def video_model_facts(model_id: str, overrides: object | None, *, entry_binding: str | None = None) -> VideoModelFacts:
    """Effective model facts of a video provider model.

    Selectable durations: capability override > matching entry > duration preset inferred from the name (when no entry matches).

    视频供应商模型的生效模型事实。

    可选时长：能力覆盖 > 对应条目 > 按名字推断的时长预设（对应不上条目时）。
    """
    applied = model_fact_overrides(overrides, "video", model_id=model_id)
    entry = model_entry(model_id, "video", entry_binding)
    facts = entry.facts if entry is not None and isinstance(entry.facts, VideoFacts) else None

    overridden = applied.get("durations")
    if isinstance(overridden, list):
        durations = tuple(sorted({int(d) for d in overridden}))
    elif facts is not None:
        durations = tuple(facts.durations)
    else:
        durations = tuple(infer_supported_durations(model_id))

    raw_resolutions = applied.get("resolutions")
    if isinstance(raw_resolutions, list):
        resolutions: tuple[str, ...] | None = tuple(str(r).strip() for r in raw_resolutions)
    else:
        resolutions = tuple(facts.resolutions) if facts is not None else None

    raw_constraints = applied.get("duration_constraints")
    if isinstance(raw_constraints, dict):
        constraints = _constraints_from_override(raw_constraints)
    else:
        constraints = facts.duration_constraints if facts is not None else DurationConstraints()
    return VideoModelFacts(durations=durations, resolutions=resolutions, duration_constraints=constraints)


#: The only override dimension accepted on ComfyUI video endpoints: selectable durations. ComfyUI endpoint capabilities are derived
#: only from node bindings (docs/adr/0082), but duration tiers are a preference the user adds or removes beyond the workflow's
#: native duration, not a capability declaration (docs/adr/0104).
#: ComfyUI 视频端点上唯一接受的覆盖维度：可选时长。ComfyUI 端点的能力只从节点绑定推导（docs/adr/0082），
#: 但时长档位是用户在 workflow 原生时长之外自己增删的偏好，不是能力声明（docs/adr/0104）。
COMFYUI_VIDEO_OVERRIDE_KEYS: frozenset[str] = frozenset({"durations"})


def comfyui_duration_override(overrides: object | None, *, model_id: str = "") -> list[int] | None:
    """Selectable durations set by the user on a ComfyUI video model row; ``None`` when unset or invalid (take the tiers the endpoint derives from node bindings).

    ComfyUI 视频模型行上用户设置的可选时长；没有设置或值不合法时为 ``None``（取端点从节点绑定推导的档位）。"""
    if not isinstance(overrides, Mapping):
        return None
    value = overrides.get("durations")
    if value is None:
        return None
    if not isinstance(value, list) or not model_fact_value_matches("durations", value):
        logger.warning("忽略 %s 的可选时长覆盖 %r：值不合法，回到端点推导的档位", model_id, value)
        return None
    return sorted({int(d) for d in value})


def _bool_fact(applied: Mapping[str, object], key: str, entry_value: bool | None) -> bool | None:
    value = applied.get(key)
    return value if isinstance(value, bool) else entry_value


def _constraints_from_override(raw: Mapping[str, object]) -> DurationConstraints:
    """Convert the duration-linked constraints in an override to the entry's shape; resolution keys are compared lowercased, as in entries.

    覆盖里的时长联动约束转成条目同一形状；分辨率键与条目一样按小写比较。"""
    by_resolution_raw = raw.get("by_resolution")
    by_resolution: dict[str, tuple[int, ...]] = {}
    if isinstance(by_resolution_raw, Mapping):
        for res, allowed in by_resolution_raw.items():
            if isinstance(allowed, list):
                by_resolution[str(res).strip().lower()] = tuple(int(d) for d in allowed)
    with_reference_raw = raw.get("with_reference_images")
    with_reference = tuple(int(d) for d in with_reference_raw) if isinstance(with_reference_raw, list) else ()
    return DurationConstraints(by_resolution=by_resolution, with_reference_images=with_reference)


def matched_entry_summary(model_id: str, media_type: str, entry_binding: str | None = None) -> dict[str, object] | None:
    """Summary of the entry a provider model matches, for the settings page to show "matched model" or "unpriced"; ``None`` when no entry matches.

    ``priced`` means the entry has a price list the current code understands: when false, calls of this model are also recorded as unpriced.

    供应商模型对应到的条目摘要，供设置页显示「对应到的模型」或「未定价」；对应不上条目时为 ``None``。

    ``priced`` 表示条目有当前代码认得的价目表：为假时这个模型的调用同样记为未定价。
    """
    entry = model_entry(model_id, media_type, entry_binding)
    return entry_summary(entry) if entry is not None else None


def entry_summary(entry: CatalogEntry) -> dict[str, object]:
    """Entry summary: vendor, official model name, media type, display name and whether it has a price (see :func:`matched_entry_summary`).

    条目摘要：厂商、官方模型名、媒体类型、显示名与是否有价格（见 :func:`matched_entry_summary`）。"""
    return {
        "maker": entry.maker,
        "name": entry.name,
        "media_type": entry.media_type,
        "display_name": entry.display_name,
        "priced": entry.price is not None and entry.price.is_priceable,
    }
