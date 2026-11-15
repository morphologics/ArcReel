"""Consistency between an entry and its definition (rules 4 and 5): the definition passes the shared validator, and the index entry equals the projection of the definition's `meta`.

条目与其定义的一致性（规则 ④⑤）：定义过共享校验器，索引条目等于定义 ``meta`` 的投影。
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from arcreel_market_core.endpoint_definition import definition_media_type, validate_definition

from .index import ENDPOINT_ENTRY_TYPE, MarketIndexEntry
from .issues import INDEX_FILENAME, ROOT_PATH, MarketIssue, MarketIssueCode, join_path

# Fields projected from the definition `meta` into the index entry; the order is the order the generator writes.
#: 从定义 ``meta`` 投影进索引条目的字段，顺序即生成器写出的顺序。
PROJECTED_META_FIELDS = ("name", "author", "version", "description", "homepage", "min_app_version")


def project_meta(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The projection of a definition into an index entry: the fields that appear in `meta`, plus the media type the definition declares.

        The media type is not a fixed value: the definition itself declares whether it produces images or video, and a declarative definition that does not write it is video. The reading rule is shared with the endpoint projection and
        the mirror column through the single `definition_media_type` implementation. The `media_type` on the market index is the value the mirror column will record once this definition is loaded into the library;
        if the two disagree, an image endpoint would show up as video in the market.

        When `kind` is missing or not recognized by this version, the projection has no `media_type`: the definition itself is invalid, and
        :func:`validate_definition` has already said so in the same check, so guessing a value here would only mask it.

    一份定义在索引条目里的投影：``meta`` 里出现的那些字段，加上定义声明的媒体类型。

    媒体类型不是固定值：定义自己声明产图还是产视频，声明式定义不写时为视频。读法与端点投影、
    镜像列共用 ``definition_media_type`` 一份实现——市场索引上的 ``media_type`` 就是这份定义装进库
    以后镜像列会写下的那个值，两处对不上会让一个图像端点在市场里显示成视频。

    ``kind`` 缺失或本版本不认得时投影里没有 ``media_type``：那份定义本身不合法，
    :func:`validate_definition` 已在同一次检查里说了这件事，这里再猜一个值只会掩盖它。
    """
    meta: Mapping[str, Any] = definition.get("meta") or {}
    projection = {field: meta[field] for field in PROJECTED_META_FIELDS if field in meta}
    with suppress(KeyError, ValueError):
        projection["media_type"] = definition_media_type(definition)
    return projection


def check_entry_definition(
    entry: MarketIndexEntry,
    definition: object,
    *,
    definition_file: str,
    entry_path: str = ROOT_PATH,
) -> list[MarketIssue]:
    """Rules 4 and 5. `definition_file` and `entry_path` are used only for locating diagnostics.

    规则 ④⑤。``definition_file`` 与 ``entry_path`` 只用于诊断定位。
    """
    issues = [
        MarketIssue(
            definition_file,
            issue.path,
            MarketIssueCode.DEFINITION_INVALID,
            {"code": issue.code.value, "detail": issue.message},
        )
        for issue in validate_definition(definition).errors
    ]
    meta = definition.get("meta") if isinstance(definition, Mapping) else None
    if isinstance(definition, Mapping) and isinstance(meta, Mapping):
        issues.extend(_projection_issues(entry, definition, entry_path))
    return issues


def _projection_issues(entry: MarketIndexEntry, definition: Mapping[str, Any], entry_path: str) -> list[MarketIssue]:
    expected = project_meta(definition)
    actual: dict[str, Any] = {
        "name": entry.name,
        "author": entry.author,
        "version": entry.version,
        "description": entry.description,
        "homepage": entry.homepage,
        "min_app_version": entry.min_app_version,
        "media_type": entry.media_type,
    }
    issues: list[MarketIssue] = []
    if entry.type != ENDPOINT_ENTRY_TYPE:
        issues.append(_mismatch(entry_path, "type", entry.type, ENDPOINT_ENTRY_TYPE))
    for field, index_value in actual.items():
        definition_value = expected.get(field)
        if index_value != definition_value:
            issues.append(_mismatch(entry_path, field, index_value, definition_value))
    return issues


def _mismatch(entry_path: str, field: str, index_value: object, definition_value: object) -> MarketIssue:
    return MarketIssue(
        INDEX_FILENAME,
        join_path(entry_path, field),
        MarketIssueCode.PROJECTION_MISMATCH,
        {"field": field, "index_value": _display(index_value), "definition_value": _display(definition_value)},
    )


def _display(value: object) -> str:
    return "—" if value is None else str(value)
