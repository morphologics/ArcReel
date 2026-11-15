"""Comparison of ``schema_version`` and tier determination, plus the threshold check of ``meta.min_app_version``.

A tier is only a hint signal for the user and **not a gate**: a definition of any tier must pass the current schema to be saved, and failing is an
ordinary validation error, unrelated to version. The determination serves two places: confirmation before import (file version vs current version) and the
newer/older relation of duplicate lineages (the existing definition's ``meta.version`` vs the file's).

Version convention: adding optional fields / relaxing constraints is a minor; removing fields / changing semantics is a major.

``schema_version`` 的比对与档位判定，以及 ``meta.min_app_version`` 的门槛判定。

档位只是给用户的提示信号，**不是闸门**：任何档位的定义都须过当前 schema 才能保存，过不了即
普通校验错误，与版本无关。判定同时服务两处——导入前确认（文件版本 vs 当前版本）与重复血统的
新旧关系（既有定义的 ``meta.version`` vs 文件的）。

版本约定：加可选字段 / 放宽约束走 minor，删字段 / 改语义走 major。
"""

from __future__ import annotations

import re
from enum import StrEnum

from packaging.version import InvalidVersion, Version

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class SchemaVersionLevel(StrEnum):
    """Tier of the file's ``schema_version`` relative to the current version.

    文件 ``schema_version`` 相对当前版本的档位。
    """

    #: Same version: import directly. / 同版：直接导入。
    DIRECT = "direct"
    #: Same major but older: warn and allow (the current schema can only be wider than it). / 同主版本但更旧：警告放行（当前 schema 只会比它更宽）。
    WARNING = "warning"
    # File is newer, or has a lower major: needs user confirmation. The former may contain fields this version does not know, and the latter has changed semantics.
    # Also in this tier when the version is missing or not semver -- if newer/older cannot be determined, it should not be waved through silently.
    #: 文件更新，或主版本更低：需用户确认。前者可能含本版不认识的字段，后者语义已变。
    #: 版本缺失或不是 semver 时也归此档——判不出新旧就不该悄悄放过去。
    CONFIRM = "confirm"


class VersionRelation(StrEnum):
    """Newer/older relation of the existing definition's ``meta.version`` relative to the imported file.

    既有定义的 ``meta.version`` 相对导入文件的新旧关系。
    """

    NEWER = "newer"
    SAME = "same"
    OLDER = "older"


def parse_semver(value: object) -> tuple[int, int, int] | None:
    """Parse ``"1.2.3"`` into a comparable triple; returns None if it is not a semver string or a component has more digits than the integer conversion limit.

    把 ``"1.2.3"`` 解析成可比较的三元组；不是 semver 串、或分量位数超出整数转换上限时返回 None。
    """
    if not isinstance(value, str):
        return None
    match = _SEMVER.fullmatch(value)
    if match is None:
        return None
    major, minor, patch = match.groups()
    try:
        return int(major), int(minor), int(patch)
    except ValueError:
        return None


def schema_version_level(file_version: object, current_version: str) -> SchemaVersionLevel:
    """Determine the tier of the file version relative to the current version.

    判定文件版本相对当前版本的档位。
    """
    file_parsed = parse_semver(file_version)
    current_parsed = parse_semver(current_version)
    if file_parsed is None or current_parsed is None:
        return SchemaVersionLevel.CONFIRM
    if file_parsed == current_parsed:
        return SchemaVersionLevel.DIRECT
    if file_parsed > current_parsed or file_parsed[0] < current_parsed[0]:
        return SchemaVersionLevel.CONFIRM
    return SchemaVersionLevel.WARNING


def version_relation(existing_version: object, file_version: object) -> VersionRelation:
    """Newer/older of the existing definition relative to the imported file. If either side cannot be parsed it is treated as ``same``, and only a duplicate is reported without saying which is newer.

    既有定义相对导入文件的新旧。任一侧不可解析时按 ``same`` 处理，只提示重复不谈新旧。
    """
    existing_parsed = parse_semver(existing_version)
    file_parsed = parse_semver(file_version)
    if existing_parsed is None or file_parsed is None or existing_parsed == file_parsed:
        return VersionRelation.SAME
    return VersionRelation.NEWER if existing_parsed > file_parsed else VersionRelation.OLDER


def meets_min_app_version(required: object, app_version: str) -> bool:
    """Whether the app version reaches the definition's declared ``meta.min_app_version``.

    The threshold accepts strict semver only; the app version is parsed as PEP 440, where a prerelease is lower than the release of the same number. If either side cannot be
    ordered, it counts as not satisfied.

    应用版本是否达到定义声明的 ``meta.min_app_version``。

    门槛只认严格 semver；应用版本按 PEP 440 解析，预发布版低于同号正式版。任一侧判不出高低即
    视为不满足。
    """
    if parse_semver(required) is None:
        return False
    try:
        return Version(app_version) >= Version(str(required))
    except InvalidVersion:
        return False
