"""Registry consistency: the resource registry is derived from a single source of truth.

`lib.project.resource_paths` is the single source of truth for "resource type -> path/extension";
VersionManager's `RESOURCE_TYPES` / `EXTENSIONS` are both derived from it. This test asserts the
derivation has not come apart: if someone changes VersionManager back to a hand-written copy that drifts
from the source of truth, it goes red immediately.

Registry consistency — 资源注册表派生自单一真相源。

`lib.project.resource_paths` 是「资源类型 → 路径/扩展名」的唯一真相源；VersionManager 的
`RESOURCE_TYPES` / `EXTENSIONS` 均从它派生。本测试断言派生未脱钩——若有人把
VersionManager 改回手写副本、与真相源漂移，立刻红。
"""

from __future__ import annotations

from lib.artifacts.version_manager import VersionManager
from lib.project.resource_paths import RESOURCE_TYPES, resource_extension


def test_version_manager_derives_from_resource_paths() -> None:
    assert set(VersionManager.RESOURCE_TYPES) == set(RESOURCE_TYPES)
    assert {rt: resource_extension(rt) for rt in RESOURCE_TYPES} == VersionManager.EXTENSIONS
