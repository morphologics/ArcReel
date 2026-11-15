"""Binding check for ``@[registered name]`` in shot scene descriptions.

A shot entry's reference images are decided by its reference fields (``characters_in_*`` / ``scenes`` / ``props`` /
``products_in_shot``); ``@[name]`` in the body ``image_prompt.scene`` only points and doesn't derive reference images: for a
mention to be replaced by "图N", it must both be registered as an asset and be listed in the entry's reference fields. If
either is missing, the render layer sends the bare name, and this module collects such mentions into locale-neutral
``{"key", "params"}`` warnings for script-writing tools to return with their receipt.

分镜画面描述里 ``@[登记名]`` 的绑定检查。

分镜条目的参考图由引用字段（``characters_in_*`` / ``scenes`` / ``props`` / ``products_in_shot``）
决定，正文 ``image_prompt.scene`` 里的 ``@[名称]`` 只指认、不派生参考图：一个 mention 要换成
「图N」，它既须登记为资产，又须写进该条目的引用字段。两者缺一时，渲染层按裸名发送，本模块
把这类 mention 收成 locale-neutral 的 ``{"key", "params"}`` 警告，供写剧本的工具随回执返回。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from lib.project.asset_types import ASSET_SPECS, asset_name_comparison_key
from lib.references.reference_catalog import build_reference_catalog
from lib.script.reference_video.text_parser import extract_mentions
from lib.script.storyboard_sequence import get_storyboard_items

#: ``@[name]`` in a scene description is not registered as an asset or not in the shot's reference fields; it will be sent as a bare name.
#: 画面描述里的 ``@[名称]`` 未登记为资产或不在该分镜引用字段中，将按裸名发送。
WARN_STORYBOARD_MENTION_UNBOUND = "storyboard_warn_mention_unbound"

_REFERENCE_LIST_FIELDS: tuple[str, ...] = tuple(
    field for spec in ASSET_SPECS.values() for field in spec.reference_list_fields
)


def _scene_text(item: dict[str, Any]) -> str | None:
    image_prompt = item.get("image_prompt")
    if isinstance(image_prompt, dict):
        image_prompt = image_prompt.get("scene")
    return image_prompt if isinstance(image_prompt, str) else None


def _declared_names(item: dict[str, Any]) -> set[str]:
    declared: set[str] = set()
    for field in _REFERENCE_LIST_FIELDS:
        names = item.get(field)
        if not isinstance(names, list):
            continue
        declared.update(asset_name_comparison_key(name) for name in names if isinstance(name, str))
    return declared


def storyboard_mention_warnings(
    project: object,
    script: dict[str, Any],
    *,
    unit_ids: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Return warnings for mentions in shot scene descriptions not bound to a reference image, in entry order, and within an entry in order of first appearance.

    When ``unit_ids`` is given, only those entries are checked (write tools are only responsible for entries changed this time).
    Skeletons without shot entries (reference-to-video) return an empty list.

    返回分镜条目画面描述里没有绑定到参考图的 mention 警告，按条目顺序、条目内按首次出现顺序。

    ``unit_ids`` 给出时只检查这些条目（写工具只对本次改动的条目负责）。没有分镜条目的骨架
    （参考生视频）返回空列表。
    """
    items, id_field, _char_field, _scene_field, _prop_field = get_storyboard_items(script)
    catalog = build_reference_catalog(project)
    warnings: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        unit_id = str(item.get(id_field) or "")
        if unit_ids is not None and unit_id not in unit_ids:
            continue
        text = _scene_text(item)
        if text is None:
            continue
        declared = _declared_names(item)
        for name in extract_mentions(text):
            if name in declared and catalog.resolve(name) is not None:
                continue
            warnings.append({"key": WARN_STORYBOARD_MENTION_UNBOUND, "params": {"unit_id": unit_id, "name": name}})
    return warnings


def render_storyboard_mention_warnings(
    warnings: Sequence[Mapping[str, Any]], translate: Callable[..., str]
) -> list[str]:
    """Render the entries of :func:`storyboard_mention_warnings` one by one into receipt text lines via ``translate``.

    The two script-writing tool boundaries (``generate_episode_script`` / ``patch_episode_script``) share this rendering; the copy
    is registered only in the i18n key table.

    把 :func:`storyboard_mention_warnings` 的条目按 ``translate`` 逐条渲染成回执文本行。

    写剧本的两个工具边界（``generate_episode_script`` / ``patch_episode_script``）共用这一份
    渲染，文案只登记在 i18n key 表里。
    """
    return [translate(str(warning["key"]), **warning["params"]) for warning in warnings]


__all__ = [
    "WARN_STORYBOARD_MENTION_UNBOUND",
    "render_storyboard_mention_warnings",
    "storyboard_mention_warnings",
]
