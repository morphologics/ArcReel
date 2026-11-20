"""English display names for catalog entries whose official display name is Chinese.

The catalog data (factory snapshot and official sync) comes from the upstream catalog repository and is replaced by
each official sync, and the snapshot is pinned by the manifest hash, so the names are not edited in the data. Instead
the composed catalog passes official data through :func:`with_english_display_names`: Chinese product terms in a
display name are replaced with the providers' English product names. Names without Chinese characters, and
user-edited entry copies, are left as they are.

中文显示名条目的英文显示名。

目录数据（出厂快照与官方同步）来自上游目录仓库，每次官方同步整份替换，快照又由清单哈希锁定，所以不在数据里改名；
目录合成时把官方数据过一遍 :func:`with_english_display_names`：显示名里的中文产品用语换成供应商的英文产品名。
不含中文的名称与用户修改过的条目副本保持原样。
"""

from __future__ import annotations

import re
from dataclasses import replace

from arcreel_catalog_core import CatalogData, CatalogEntry

#: Ordered term replacements: longer terms first so that a compound term wins over its parts.
#: 有序的用语替换：长词在前，复合词优先于其组成部分。
_TERMS: tuple[tuple[str, str], ...] = (
    ("参考生视频", "Reference-to-Video"),
    ("图生视频", "Image-to-Video"),
    ("文生视频", "Text-to-Video"),
    ("可灵图像", "Kling Image"),
    ("（图像）", " (Image)"),
    ("可灵", "Kling"),
    ("万相", "Wan"),
    ("豆包", "Doubao"),
    ("图像", "Image"),
    ("视频", "Video"),
)

_CJK = re.compile(r"[　-〿一-鿿＀-￯]")


def english_display_name(name: str) -> str:
    """The English form of a display name; names without Chinese characters are returned unchanged.

    显示名的英文写法；不含中文的名称原样返回。"""
    if not _CJK.search(name):
        return name
    for zh, en in _TERMS:
        name = name.replace(zh, en)
    return re.sub(r"\s{2,}", " ", name).strip()


def _english_entry(entry: CatalogEntry) -> CatalogEntry:
    english = english_display_name(entry.display_name)
    return entry if english == entry.display_name else replace(entry, display_name=english)


def with_english_display_names(data: CatalogData) -> CatalogData:
    """A copy of the catalog data with Chinese display names in English; the same object when nothing changes.

    把中文显示名换成英文后的目录数据副本；无需改动时返回原对象。"""
    entries = tuple(_english_entry(entry) for entry in data.entries)
    if all(new is old for new, old in zip(entries, data.entries, strict=True)):
        return data
    return replace(data, entries=entries)
