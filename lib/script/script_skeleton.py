"""Single source of truth for script skeleton knowledge (zero-dependency leaf module).

Gathers the structural fact "how a script is organized by skeleton kind" in one place: a narrow table keyed by skeleton kind,
plus two resolvers that close off the interaction of the two axes content_mode / generation_mode.

- Narrow table ``SKELETONS``: the key is the entry array key in the script (``segments`` / ``scenes`` / ``shots`` /
  ``video_units``), the row ``Skeleton(id_field, chars_field)``. ``chars_field`` may be ``None``
  -- ``video_units`` has no per-entry character list (characters exist as ``@[name]`` mentions in the body ``text``,
  derived on read), and the table honestly declares the gap; consumers receiving ``None`` must decide explicitly (derive it
  themselves or declare it not applicable); no fake field name is provided that would make ``get()`` return empty.
- Canonical resolution ``resolve_declared_kind(content_mode, generation_mode)``: serves consumers that only have the project
  config at hand; inputs are the validated project-level content_mode and the project's declared generation_mode. **fail-loud** -- unknown/missing content_mode raises ``ValueError``, no silent fallback.
- Evidence-based resolution ``resolve_script_kind(script)``: serves consumers holding script data (edit / view / export),
  data shape first -- it answers "what does this script look like now", independent of the project declaration.
- Generation mode gate ``ensure_route_skeleton(script, content_mode, generation_mode)``: used by generation entry points;
  rejects when the script's actual skeleton and the skeleton required by the project's generation mode are not in the same family, with re-split guidance.

Behaviour is not in the table: validate hooks, Pydantic model mapping and edit whitelists are not in the registry; they stay local to each consumer.

Design rationale (including the explicitly rejected composite key / wide table / silent fallback) is in ``docs/adr/0045``.

剧本骨架知识的单一真相源（零依赖叶子模块）。

把「剧本按骨架种类如何组织」这一结构事实收归一处：一张以骨架种类（skeleton kind）为键
的窄表，加上两个把 content_mode / generation_mode 两轴交互收口的解析器。

- 窄表 ``SKELETONS``：键即剧本里的条目数组键（``segments`` / ``scenes`` / ``shots`` /
  ``video_units``），行 ``Skeleton(id_field, chars_field)``。``chars_field`` 可为 ``None``
  ——``video_units`` 无逐条角色名单（角色以正文 ``text`` 里的 ``@[名称]`` 提及形态存在，
  读时派生），表如实声明缺位；消费方拿到 ``None`` 必须显式决策（自行派生或声明不适用），
  不提供假字段名使 ``get()`` 返回空值。
- 规范解析 ``resolve_declared_kind(content_mode, generation_mode)``：服务只有项目配置在手
  的消费方，输入为项目级已过校验的 content_mode 与项目声明的 generation_mode。**fail-loud**——未知/缺失 content_mode 抛 ``ValueError``，不静默兜底。
- 取证解析 ``resolve_script_kind(script)``：服务手持剧本数据的消费方（编辑 / 查看 / 导出），
  数据形状优先——回答的是「这份剧本现在长什么样」，与项目声明无关。
- 生成模式闸门 ``ensure_route_skeleton(script, content_mode, generation_mode)``：生成入口用，
  剧本实际骨架与项目生成模式要求的骨架不属同一族时拒绝，并给出重拆指引。

行为不进表：validate 钩子、Pydantic 模型映射、编辑白名单不入注册表，留各消费方本地。

设计依据（含明确不采用的复合键 / 宽表 / 静默兜底）见 ``docs/adr/0045``。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from arcreel_market_core.validation_messages import MessageRef, ValidationMessage
from lib.infra.validation_messages import default_translate

logger = logging.getLogger(__name__)

STORYBOARD_ITEM_ID_PATTERN = re.compile(r"^E\d+S\d+(?:_\d+)?$")

# Episode number prefix: only matches `E{digits}` immediately followed by S/U (S for segment/scene, U for video_unit),
# keeping the suffix (e.g. `E1S03_2` -> `E2S03_2`). The design contract is in lib/script/script_models.py.
# 集号前缀：仅匹配 `E{数字}` + 紧随 S/U（segment/scene 用 S，video_unit 用 U），
# 保留后缀（如 `E1S03_2` → `E2S03_2`）。设计契约见 lib/script/script_models.py。
_EPISODE_PREFIX_RE = re.compile(r"^E\d+(?=[SU])")


def rewrite_episode_prefix(item_id: object, episode: int) -> object:
    """Force the ``E{digits}`` prefix in an entry id to ``E{episode}``; non-strings or ids without the prefix are returned as is.

    A fallback for the LLM writing the wrong prefix even though the prompt injected the episode number. Rewriting before writing to
    disk and "which id the script plan entry fingerprint is filed under" are the same thing, so it lives with the skeleton's id
    knowledge and is shared by generation and staleness checks.

    把条目 id 中的 ``E{数字}`` 前缀强制改写为 ``E{episode}``；非字符串或无该前缀的原样返回。

    兜底 LLM 在 prompt 已注入集号的情况下仍写错前缀的场景。落盘前的改写与「脚本规划条目
    指纹按哪个 id 归档」是同一件事，故与骨架的 id 知识同处一室，由生成与时效判定共用。
    """
    if not isinstance(item_id, str):
        return item_id
    rewritten, count = _EPISODE_PREFIX_RE.subn(f"E{episode}", item_id)
    if count and rewritten != item_id:
        logger.warning("episode prefix rewritten: %s → %s", item_id, rewritten)
    return rewritten


@dataclass(frozen=True)
class Skeleton:
    """Structural facts of a skeleton kind: per-item id field name / per-item character list field name (may be absent).

    The key (skeleton kind) is itself the entry array key in the script, so there is no separate items_key. ``chars_field`` of
    ``None`` means the skeleton has no per-entry character list field -- consumers receiving ``None`` must decide explicitly and must
    not ``get()`` it as an empty field name.

    某个骨架种类的结构事实：每项 id 字段名 / 每项角色名单字段名（可缺位）。

    键（骨架种类）本身即剧本里的条目数组键，故不再单列 items_key。``chars_field`` 为
    ``None`` 表示该骨架无逐条角色名单字段——消费方拿到 ``None`` 必须显式决策，不得当空字段名
    去 ``get()``。
    """

    id_field: str
    chars_field: str | None


SKELETONS: dict[str, Skeleton] = {
    "segments": Skeleton("segment_id", "characters_in_segment"),
    "scenes": Skeleton("scene_id", "characters_in_scene"),
    "shots": Skeleton("shot_id", "characters_in_shot"),
    "video_units": Skeleton("unit_id", None),
}

# The i18n key of an entry label is derived from the skeleton kind -- driving the notification copy shared by shot-level events and
# task completion events (e.g. "分镜「E1S01」"). The copy itself is stored per language in lib/i18n as ``event_label_*``; only
# stable identifiers are registered here. Both event paths must read the same table instead of each maintaining one.
# 条目标签的 i18n key 按骨架种类派生——驱动分镜级事件与任务完成事件共用的通知文案
# （如「分镜「E1S01」」）。文案本身按语言存放在 lib/i18n 的 ``event_label_*``，此处只登记
# 稳定标识。两套事件路径必须读同一张表，不各自维护一份。
SKELETON_ITEM_LABEL_KEYS: dict[str, str] = {
    "segments": "skeleton_segments",
    "scenes": "skeleton_scenes",
    "shots": "skeleton_shots",
    "video_units": "skeleton_video_units",
}

# An event's entity type is derived from the skeleton kind, from the same source as ``SKELETON_ITEM_LABEL_KEYS``. Drives the frontend
# grouping label mapping (``ENTITY_LABELS``): the three shot skeletons all show "shot", the reference-to-video skeleton shows "video
# unit". Values don't conflict with the existing ``entity_type`` enum (drama uses ``drama_scene`` to avoid grouping with the named entity ``scene``).
# 事件的实体类型按骨架种类推导，与 ``SKELETON_ITEM_LABEL_KEYS`` 同源。驱动前端分组标签映射
# （``ENTITY_LABELS``）：三种分镜骨架统一显示「分镜」，参考生视频骨架显示「视频单元」。取值与既有
# ``entity_type`` 枚举不冲突（drama 用 ``drama_scene`` 避免与命名实体 ``scene`` 撞组）。
SKELETON_ENTITY_TYPES: dict[str, str] = {
    "segments": "segment",
    "scenes": "drama_scene",
    "shots": "shot",
    "video_units": "reference_unit",
}

# An event's anchor type is derived from the skeleton kind, with values aligned to the scroll target type guards of each frontend
# canvas: video_units map to ``reference_unit`` (the reference-to-video canvas selects and highlights the video unit by it), the rest
# to ``segment`` (narration/drama/ad share the timeline shot view, selecting entries by id).
# 事件的锚点类型按骨架种类推导，取值与前端各画布的滚动目标类型守卫对齐：video_units 归
# ``reference_unit``（参考生视频画布按此选中并高亮对应视频单元），其余归 ``segment``
# （narration/drama/ad 共用时间线分镜视图，按 id 选中条目）。
SKELETON_ANCHOR_TYPES: dict[str, str] = {
    "segments": "segment",
    "scenes": "segment",
    "shots": "segment",
    "video_units": "reference_unit",
}


def _validate_registry() -> None:
    """Import-time table self-consistency check: all four skeletons present, fields valid. Fails at startup on error (same technique as ``docs/adr/0039``).

    import 期表自洽校验：四骨架齐全、字段合法。失败即启动炸（同 ``docs/adr/0039`` 手法）。"""
    expected = {"segments", "scenes", "shots", "video_units"}
    if set(SKELETONS) != expected:
        raise RuntimeError(f"SKELETONS kinds incomplete: expected {expected}, got {set(SKELETONS)}")
    for kind, skeleton in SKELETONS.items():
        if not skeleton.id_field:
            raise RuntimeError(f"SKELETONS[{kind!r}].id_field is invalid: {skeleton.id_field!r}")
        # chars_field of None is valid (video_units explicitly absent); an empty string is invalid.
        # chars_field 为 None 合法（video_units 显式缺位）；空串非法。
        if skeleton.chars_field is not None and not skeleton.chars_field:
            raise RuntimeError(f"SKELETONS[{kind!r}].chars_field is invalid: {skeleton.chars_field!r}")
    for name, table in (
        ("SKELETON_ITEM_LABEL_KEYS", SKELETON_ITEM_LABEL_KEYS),
        ("SKELETON_ENTITY_TYPES", SKELETON_ENTITY_TYPES),
        ("SKELETON_ANCHOR_TYPES", SKELETON_ANCHOR_TYPES),
    ):
        if set(table) != expected:
            raise RuntimeError(f"{name} skeleton kinds incomplete: expected {expected}, got {set(table)}")


_validate_registry()


def resolve_declared_kind(content_mode: str | None, generation_mode: str | None) -> str:
    """Canonical resolution: determine the skeleton kind from the project's declared ``(content_mode, generation_mode)``.

    Inputs are the validated project-level content_mode and the project's declared generation_mode (``project.json`` field).

    - any content mode + ``generation_mode == "reference_video"`` -> ``video_units``
    - ``ad`` + ``storyboard`` -> ``shots``
    - ``narration`` -> ``segments``, ``drama`` -> ``scenes``
    - unknown/missing content_mode -> raises ``ValueError`` (fail-loud, no silent default to drama/narration)

    Serves consumers that only have the project config at hand; consumers holding a script dict that may lack fields use ``resolve_script_kind``.

    规范解析：由项目声明的 ``(content_mode, generation_mode)`` 定骨架种类。

    输入为项目级已过校验的 content_mode 与项目声明的 generation_mode（``project.json`` 字段）。

    - 任意创作类型 + ``generation_mode == "reference_video"`` → ``video_units``
    - ``ad`` + ``storyboard`` → ``shots``
    - ``narration`` → ``segments``，``drama`` → ``scenes``
    - 未知/缺失 content_mode → 抛 ``ValueError``（fail-loud，不静默默认到 drama/narration）

    服务只有项目配置在手的消费方；手持可能缺字段的剧本 dict 的消费方走 ``resolve_script_kind``。
    """
    if content_mode == "ad":
        return "video_units" if generation_mode == "reference_video" else "shots"
    if content_mode == "narration":
        return "video_units" if generation_mode == "reference_video" else "segments"
    if content_mode == "drama":
        return "video_units" if generation_mode == "reference_video" else "scenes"
    raise ValueError(f"Unknown or missing content_mode: {content_mode!r}")


def resolve_script_kind(script: dict[str, Any]) -> str:
    """Evidence-based resolution: determine the current shot array kind from a script dict.

    Returns ``"video_units"`` / ``"scenes"`` / ``"segments"`` / ``"shots"``.

    **Data shape only**: this resolution answers the evidence question "what does this script look like now", serving edit / view /
    export -- these capabilities must work for any script on disk, including mismatched scripts whose skeleton doesn't fit the project
    generation mode. If the project generation mode decided one-way instead, mismatched episodes would be completely unreachable via
    all MCP editing tools (``resolve_items`` returns an empty list, id-based edits all report "not found"), and the Agent seeing the
    error couldn't locate the cause either. Generation paths don't use this resolution: generation entry points dispatch by project
    generation mode, and mismatches are explicitly rejected by ``ensure_route_skeleton``.

    Decision order:
    1. ``video_units`` present and none of ``segments`` / ``scenes`` / ``shots`` present -> reference (prevents a stray
       ``video_units`` mistakenly stuffed into a storyboard script from hijacking the decision)
    2. ``content_mode`` is authoritative (``drama`` -> scenes, ``narration`` -> segments, ``ad`` -> shots)
    3. legacy compatibility for ``content_mode=narration`` with data under the ``scenes`` key (no ``segments``)
    4. when ``content_mode`` is missing, infer from which top-level keys exist

    ``script_structure_validator._select_model`` (structure validation) / ``script_editor.resolve_items``
    (editing core) / the unified write entry's metadata recomputation share this decision: one truth in one place, no drift.

    取证解析：由剧本 dict 判别当前的分镜数组种类。

    返回 ``"video_units"`` / ``"scenes"`` / ``"segments"`` / ``"shots"``。

    **只看数据形状**：本解析回答的是取证提问「这份剧本现在长什么样」，服务于编辑 / 查看 /
    导出——这些能力对任何一份磁盘上的剧本都必须成立，包括骨架与项目生成模式不符的失配剧本。
    若改由项目生成模式单向定夺，失配集通过所有 MCP 编辑工具
    完全不可触达（``resolve_items`` 返回空列表、按 id 编辑都报"未找到"），Agent 看到错误也
    无法定位成因。生成路径不走本解析：由生成入口按项目生成模式分派，失配由 ``ensure_route_skeleton``
    显式拒绝。

    判别顺序：
    1. ``video_units`` 在场且 ``segments`` / ``scenes`` / ``shots`` 都不在 → reference（避免
       storyboard 脚本被误塞的游离 ``video_units`` 抢走判别）
    2. ``content_mode`` 为权威（``drama`` → scenes，``narration`` → segments，``ad`` → shots）
    3. ``content_mode=narration`` 但数据落 ``scenes`` 键（无 ``segments``）的历史遗留兼容
    4. ``content_mode`` 缺失时按顶层键存在性推断

    ``script_structure_validator._select_model``（结构校验）/ ``script_editor.resolve_items``
    （编辑核心）/ 写盘统一入口的 metadata 重算共用本判别，多处只此一处真相、不漂移。
    """
    if "video_units" in script and not any(k in script for k in ("segments", "scenes", "shots")):
        return "video_units"
    content_mode = script.get("content_mode")
    if content_mode == "ad":
        return "shots"
    if content_mode == "drama":
        return "scenes"
    if content_mode == "narration":
        # Malformed script compatibility: the legacy state where content_mode=narration but the data actually lives under the scenes key (no segments key)
        # -- fall back to reading scenes, rather than literally mapping content_mode to the non-existent segments.
        # 畸形脚本兼容：content_mode=narration 但数据实际落在 scenes 键下（无 segments 键）的
        # 历史遗留状态——回退去读 scenes，而非按 content_mode 字面映射到不存在的 segments。
        if "segments" not in script and "scenes" in script:
            return "scenes"
        return "segments"
    if "scenes" in script and "segments" not in script:
        return "scenes"
    if "shots" in script and "segments" not in script:
        return "shots"
    return "segments"


def resolve_kind_items(script: dict[str, Any], *, kind: str | None = None) -> tuple[Any, str, str]:
    """The single entry point for getting the entry array and its id field by skeleton kind: returns ``(items, id_field, kind)``.

    When ``kind`` is omitted it is decided from the script's data shape via ``resolve_script_kind`` (evidence-based resolution); a
    caller already holding a kind computed from the project declaration or the generation mode gate (return value of
    ``resolve_declared_kind`` / ``ensure_route_skeleton``) can pass it explicitly to skip repeated resolution.

    The returned entry value is ``script.get(kind)`` as is -- **no type check, no fallback of non-lists to an empty array**:
    ``None`` when the key is missing, and the illegal value as is when the key exists but isn't a list (including ``null``). This
    function only unifies the structural fact "which key, which id field"; whether a dirty value should fail loud or degrade, and
    whether an empty array is valid, is each consumer path's own policy, belonging to the caller, not closed off here. The character
    reference field inside entries (``chars_field``) is not entry access and is still looked up directly in ``SKELETONS``.

    按骨架种类取条目数组与其 id 字段的唯一入口：返回 ``(items, id_field, kind)``。

    ``kind`` 缺省时经 ``resolve_script_kind``（取证解析）由剧本数据形状判别；调用方已持有
    项目声明或生成模式闸门算出的种类（``resolve_declared_kind`` / ``ensure_route_skeleton`` 的
    返回值）可显式传入，跳过重复判别。

    返回的条目值是 ``script.get(kind)`` 原样——**不做类型校验、不把非 list 兜底为空数组**：
    键缺失时为 ``None``，键存在但非 list（含 ``null``）时原样返回该非法值。本函数只统一
    「查哪个键、哪个 id 字段」这一份结构事实；脏值该 fail-loud 还是降级、空数组算不算合法，
    是各消费路径自己的策略，归调用方，不在此收口。条目内的角色引用字段（``chars_field``）
    不属于条目访问，仍直查 ``SKELETONS``。
    """
    resolved_kind = kind if kind is not None else resolve_script_kind(script)
    return script.get(resolved_kind), SKELETONS[resolved_kind].id_field, resolved_kind


# Skeleton family required by the generation mode: reference-to-video needs ``video_units``, other modes need a shot-family skeleton
# (``segments`` / ``scenes`` / ``shots``). Differences within a family (e.g. the historical form of narration data under the ``scenes``
# key) are not a mismatch; only crossing families is -- crossing families means the array the generation side needs to read isn't in the script at all.
# 生成模式要求的骨架族：参考生视频要 ``video_units``，其余模式要分镜族骨架
# （``segments`` / ``scenes`` / ``shots``）。族内差异（如 narration 数据落 ``scenes`` 键的历史
# 形态）不构成失配，只有跨族才是——跨族意味着生成侧要读的数组根本不在剧本里。
_REFERENCE_ROUTE_SKELETON = "video_units"
_STORYBOARD_ROUTE_SKELETONS = ("segments", "scenes", "shots")


class SkeletonRouteMismatchError(ValueError):
    """The script skeleton and the project generation mode are not in the same family -- generation refused.

    The only way out for a mismatched script is re-split and regenerate: the generation mode is locked at creation, and a script cannot
    switch family in place. View / edit / export don't pass through this gate, so mismatched scripts remain readable, editable and exportable.

    ``actual`` of ``None`` means the script has no skeleton array at all (malformed or half-finished script), worded separately from
    "has an array but of another family" -- the latter needs a re-split to switch family, the former needs content split out first.

    剧本骨架与项目生成模式不属同一族——生成被拒。

    失配剧本的唯一出路是重拆重生成：生成模式创建时锁定，剧本不可就地换族。查看 / 编辑 /
    导出不经本闸门，失配剧本仍可读可改可导出。

    ``actual`` 为 ``None`` 表示剧本一个骨架数组都没有（畸形或半成品剧本），与「有数组但
    属另一族」分开措辞——后者要重拆换族，前者要先拆出内容。
    """

    def __init__(self, *, expected: str, actual: str | None, generation_mode: str | None) -> None:
        self.expected = expected
        self.actual = actual
        guidance = "reference" if expected == _REFERENCE_ROUTE_SKELETON else "storyboard"
        params: dict[str, Any] = {
            "route": MessageRef(
                "val_route_reference_video" if generation_mode == "reference_video" else "val_route_storyboard"
            ),
            "expected": expected,
            "expected_noun": MessageRef(f"val_skeleton_noun_{expected}"),
        }
        if actual is not None:
            params["actual"] = actual
            params["actual_noun"] = MessageRef(f"val_skeleton_noun_{actual}")
        self._message = ValidationMessage(
            f"val_skeleton_mismatch_{guidance}_{'known' if actual is not None else 'none'}",
            params,
        )
        super().__init__()

    def __str__(self) -> str:
        return self._message.render(default_translate)

    def to_validation_message(self) -> ValidationMessage:
        """Structured form, for the validator to report the mismatch to the user in the request language. / 结构化形态，供校验器把失配事实按请求语言报告给用户。"""
        return self._message


def ensure_route_skeleton(script: dict[str, Any], content_mode: str | None, generation_mode: str | None) -> str:
    """Generation mode gate at generation entry points: confirm the script skeleton belongs to the family required by the project generation mode, and return the script's actual skeleton kind.

    Generation dispatch always follows the project generation mode (``resolve_declared_kind``); the script itself carries no generation
    mode information. For a script whose skeleton mismatches the generation mode, the array that generation needs doesn't exist, and
    neither silently downgrading nor quietly switching paths is acceptable -- it is explicitly rejected here with re-split guidance.

    The criterion is "is the array the generation mode needs to read present", not "does the evidence-based answer equal the declared
    value"; both directions are judged symmetrically by key presence:

    - Reference-to-video only asks whether the ``video_units`` key is present. When a script also has leftover shot-family arrays,
      evidence-based resolution answers ``segments`` by shape first, but the reference-to-video generation side reads exactly
      ``video_units``, and leftover arrays get no vote (cost estimation uses the same rule: ``is_reference_video_project`` decides
      the path, shape doesn't vote).
    - Storyboard mode only asks whether one of ``segments`` / ``scenes`` / ``shots`` is present. Historical form differences within
      the family (narration data under the ``scenes`` key) are let through as is and the evidence-based answer is returned unchanged;
      the gate only handles cross-family cases. But when all three keys are missing it cannot pass -- ``resolve_script_kind`` would
      synthesize an in-family answer from ``content_mode``, and following it, storyboard enqueueing would land in the fake success
      "✨ 所有分镜的分镜图都已生成".

    Both branches only ask whether the key is present, not the value's type: dirty data like ``"video_units": {}`` is a type error,
    not a generation mode mismatch; reporting it belongs to the downstream "must be an array" validation, and the gate doesn't overstep
    (otherwise it would report self-contradictory copy like "requires video_units, currently video_units").

    Raises:
        SkeletonRouteMismatchError: the script skeleton isn't in the family required by the generation mode, or the script has no skeleton array.
        ValueError: content_mode unknown or missing (fail-loud from ``resolve_declared_kind``).

    生成入口的生成模式闸门：确认剧本骨架属于项目生成模式要求的族，返回剧本实际骨架种类。

    生成分派一律按项目生成模式（``resolve_declared_kind``），剧本自身不携带生成模式信息。骨架与生成模式
    失配的剧本，按生成模式生成时要读的数组不存在，静默降档与悄悄换路径都不可接受——此处显式拒绝
    并给出重拆指引。

    判据是「生成模式要读的那个数组在不在」，不是「取证解析的答案等不等于声明值」，两个方向对称按
    键在场性判定：

    - 参考生视频只问 ``video_units`` 键在不在。剧本同时残留分镜族数组时取证解析会按形状优先答
      ``segments``，但参考生视频的生成侧读的就是 ``video_units``，残留数组不参与投票（费用估算
      同此口径：``is_reference_video_project`` 定路径，形状不投票）。
    - 分镜图生视频只问 ``segments`` / ``scenes`` / ``shots`` 有没有一个在场。族内的历史形态差异
      （narration 数据落 ``scenes`` 键）照实放行并原样返回取证解析的答案，闸门只管跨族；而
      三个键全缺时不能放行——``resolve_script_kind`` 会按 ``content_mode`` 合成一个族内答案，
      顺着走下去分镜图入队会落进"✨ 所有分镜的分镜图都已生成"的假成功。

    两个分支都只问键在不在、不问值的类型：``"video_units": {}`` 这类脏数据是类型错误、不是
    生成模式失配，报错权归下游的「必须是数组」校验，闸门不越俎代庖（否则会报出「要求 video_units、
    当前 video_units」的自相矛盾文案）。

    Raises:
        SkeletonRouteMismatchError: 剧本骨架与生成模式要求的骨架不属同一族，或剧本没有任何骨架数组。
        ValueError: content_mode 未知或缺失（由 ``resolve_declared_kind`` fail-loud）。
    """
    expected = resolve_declared_kind(content_mode, generation_mode)
    has_reference = _REFERENCE_ROUTE_SKELETON in script
    has_storyboard = any(key in script for key in _STORYBOARD_ROUTE_SKELETONS)
    if expected == _REFERENCE_ROUTE_SKELETON:
        if has_reference:
            return _REFERENCE_ROUTE_SKELETON
        actual = resolve_script_kind(script) if has_storyboard else None
    else:
        if has_storyboard:
            return resolve_script_kind(script)
        actual = _REFERENCE_ROUTE_SKELETON if has_reference else None
    raise SkeletonRouteMismatchError(expected=expected, actual=actual, generation_mode=generation_mode)
