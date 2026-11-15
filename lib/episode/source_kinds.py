"""Source kind: whether a source in drama is a novel or a screenplay, recorded with the source file (ADR 0036).

- Each item of the whole-source manifest records one (``whole_source_files[].source_kind``); episodes with their own
  source record it on the ledger entry (``episodes[].source_kind``). A cut episode takes the kind of the file where
  its source range starts, not stored separately; episodes without source have no kind.
- Only drama projects have source kinds; other content modes always read as having no kind, and none is written.
- In drama, a missing field or invalid value reads as novel.
- Rule for the project overview: when all sources have the same kind, take that kind; when mixed or there is no
  source, novel.

This module only reads and writes the in-memory form of ``project.json`` and takes no lock; callers use it under the
project lock.

源文件类型：剧情演绎中一份原文是小说还是剧本，随源文件记录（ADR 0036）。

- 整本源文清单的每一项各记一份（``whole_source_files[].source_kind``），自带原文的集记在账本条目上
  （``episodes[].source_kind``）。切出集取原文范围起点所在文件的类型，不另存；无原文的集没有类型。
- 只有剧情演绎项目有源文件类型；其他创作类型一律读作没有类型，也不写入。
- 剧情演绎里字段缺失或取值非法时按小说读。
- 项目概览的口径：全部源文同一类型时取该类型，混合或没有源文时按小说。

本模块只读写 ``project.json`` 的内存形态，不取锁；调用方在项目锁内使用。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypeGuard

from lib.episode.episode_ledger import parse_positive_episode_num, parse_source_range
from lib.episode.episode_sources import (
    WHOLE_SOURCE_FILES_KEY,
    SourceOrigin,
    episode_entry,
    episode_source_origin,
    whole_source_files,
)

SourceKind = Literal["novel", "screenplay"]

#: All values of source kind. / 源文件类型的全部取值。
SOURCE_KINDS: tuple[SourceKind, ...] = ("novel", "screenplay")

#: Default kind. / 缺省类型。
DEFAULT_SOURCE_KIND: SourceKind = "novel"

#: Field name on whole-source manifest items and ledger entries. / 整本源文清单项与账本条目上的字段名。
SOURCE_KIND_FIELD = "source_kind"


def is_source_kind(value: object) -> TypeGuard[SourceKind]:
    return isinstance(value, str) and value in SOURCE_KINDS


def source_kind_applies(project: Mapping[str, Any]) -> bool:
    """Whether the project has source kinds: only drama does. / 项目是否有源文件类型：只有剧情演绎有。"""
    return project.get("content_mode") == "drama"


def _recorded(raw: Mapping[str, Any] | None) -> SourceKind:
    value = raw.get(SOURCE_KIND_FIELD) if raw is not None else None
    return value if is_source_kind(value) else DEFAULT_SOURCE_KIND


def _whole_source_item(project: Mapping[str, Any], rel: str) -> Mapping[str, Any] | None:
    raw = project.get(WHOLE_SOURCE_FILES_KEY)
    return next(
        (
            item
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, Mapping) and item.get("source_file") == rel
        ),
        None,
    )


def whole_source_file_kind(project: Mapping[str, Any], rel: str) -> SourceKind | None:
    """Kind of the whole-source file ``rel``; None for non-drama projects or when the file is not in the manifest.

    整本源文文件 ``rel`` 的类型；非剧情演绎项目或文件不在清单里时为 None。"""
    if not source_kind_applies(project) or rel not in whole_source_files(project):
        return None
    return _recorded(_whole_source_item(project, rel))


def entry_source_kind(project: Mapping[str, Any], entry: Mapping[str, Any]) -> SourceKind | None:
    """The kind of this ledger entry's episode: taken from the entry record for an episode with its own source, from the file where the range starts for a cut episode; episodes without source have no kind.

    When a cut episode's range does not fall into the manifest files (no range, file not in the manifest), it reads as
    novel. Always None for non-drama projects.

    账本条目这一集的类型：自带原文的集取条目记录，切出集取范围起点所在文件，无原文的集没有类型。

    切出集的范围落不到清单文件里（没有范围、文件不在清单里）时按小说读。非剧情演绎项目一律为 None。
    """
    if not source_kind_applies(project):
        return None
    origin = episode_source_origin(entry)
    if origin is SourceOrigin.NONE:
        return None
    if origin is SourceOrigin.OWN:
        return _recorded(entry)
    span = parse_source_range(entry)
    if span is None or span.source_file not in whole_source_files(project):
        return DEFAULT_SOURCE_KIND
    return _recorded(_whole_source_item(project, span.source_file))


def episode_source_kind(project: Mapping[str, Any], episode: int) -> SourceKind | None:
    """Kind of the episode whose episode ID is ``episode``, by the same rules as :func:`entry_source_kind`; None when the episode is not in the ledger.

    集 ID 为 ``episode`` 的集的类型，规则同 :func:`entry_source_kind`；集不在账本里时为 None。"""
    entry = episode_entry(project, episode)
    return None if entry is None else entry_source_kind(project, entry)


def _all_source_kinds(project: Mapping[str, Any]) -> Iterable[SourceKind]:
    for rel in whole_source_files(project):
        yield _recorded(_whole_source_item(project, rel))
    raw = project.get("episodes")
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, Mapping) and episode_source_origin(entry) is SourceOrigin.OWN:
            yield _recorded(entry)


def project_overview_source_kind(project: Mapping[str, Any]) -> SourceKind:
    """Which kind the project overview is generated for: when all sources (whole-source files and episodes with their own source) have the same kind, that kind; otherwise novel.

    项目概览按哪种类型生成：全部源文（整本源文的文件与自带原文的集）同一类型时取该类型，否则按小说。"""
    if not source_kind_applies(project):
        return DEFAULT_SOURCE_KIND
    kinds = set(_all_source_kinds(project))
    return kinds.pop() if len(kinds) == 1 else DEFAULT_SOURCE_KIND


def record_whole_source_file_kind(project: dict[str, Any], rel: str, kind: SourceKind | None) -> None:
    """Record the kind of a whole-source file in the manifest; when ``kind`` is None, record the default novel. Not recorded for non-drama projects.

    给清单里的整本源文文件记类型；``kind`` 为 None 时记缺省的小说。非剧情演绎项目不记。"""
    if not source_kind_applies(project):
        return
    raw = project.get(WHOLE_SOURCE_FILES_KEY)
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get("source_file") == rel:
            item[SOURCE_KIND_FIELD] = kind or DEFAULT_SOURCE_KIND


def record_episode_kind(project: Mapping[str, Any], entry: dict[str, Any], kind: SourceKind | None) -> None:
    """Record the kind on the ledger entry of an episode with its own source; when ``kind`` is None keep the existing record, recording novel when there is none. Not recorded for non-drama projects.

    给自带原文的集的账本条目记类型；``kind`` 为 None 时保留已有记录，没有记录时记小说。非剧情演绎项目不记。"""
    if not source_kind_applies(project):
        return
    if kind is not None:
        entry[SOURCE_KIND_FIELD] = kind
    elif not is_source_kind(entry.get(SOURCE_KIND_FIELD)):
        entry[SOURCE_KIND_FIELD] = DEFAULT_SOURCE_KIND


def episodes_from_whole_source_file(project: Mapping[str, Any], rel: str) -> list[int]:
    """Cut episodes whose source range starts in the whole-source file ``rel``, in episode order. / 原文范围起点落在整本源文文件 ``rel`` 的切出集，按播出顺序。"""
    raw = project.get("episodes")
    ids: list[int] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping) or episode_source_origin(entry) is not SourceOrigin.WHOLE_SOURCE:
            continue
        span = parse_source_range(entry)
        episode = parse_positive_episode_num(entry.get("episode"))
        if span is not None and span.source_file == rel and episode is not None:
            ids.append(episode)
    return ids


__all__ = [
    "DEFAULT_SOURCE_KIND",
    "SOURCE_KINDS",
    "SOURCE_KIND_FIELD",
    "SourceKind",
    "entry_source_kind",
    "episode_source_kind",
    "episodes_from_whole_source_file",
    "is_source_kind",
    "project_overview_source_kind",
    "record_episode_kind",
    "record_whole_source_file_kind",
    "source_kind_applies",
    "whole_source_file_kind",
]
