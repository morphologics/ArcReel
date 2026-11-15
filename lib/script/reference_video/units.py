"""Reference-to-video unit lookup and pure bucket mapping.

The actual bucket of an executable request is decided by ``ReferenceUnitRequestProjector``, which first derives
reference images from the text and then hydrates project files; this module only keeps the pure mapping from given
boolean facts and unit lookup.

参考生视频 unit 的查找与定桶纯映射。

可执行请求的实际定桶由 ``ReferenceUnitRequestProjector`` 先从正文派生参考图、再水合项目
文件完成；本模块只保留已给定布尔事实的纯映射与 unit 查找。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only import: lib.project.project_manager loads this module indirectly via the lib.script.reference_video
    # package init, and lib.config.resolver in turn imports lib.project.project_manager, so a runtime import would cycle.
    # 仅类型导入：lib.project.project_manager 经 lib.script.reference_video 包初始化间接加载本模块，而
    # lib.config.resolver 又反向 import lib.project.project_manager，运行时导入会成环。
    from lib.config.resolver import VideoGenerationType


def reference_video_bucket(*, with_references: bool) -> VideoGenerationType:
    """Task type bucket of a video unit: with reference images → r2v; without → i2v.

    This function only maps given boolean facts. Preflight, quoting, Agent, queue and execution layers must first have
    ``ReferenceUnitRequestProjector`` derive references from the text and hydrate the actually usable assets, and must
    not guess on their own.

    视频单元的任务类型桶：有参考图 → r2v；无参考图 → i2v。

    本函数只对已给定的布尔事实做纯映射。预检、报价、Agent、队列与执行层须先由
    ``ReferenceUnitRequestProjector`` 从正文派生引用并水合实际可用资产，不能自行猜测。
    """
    return "r2v" if with_references else "i2v"


def find_reference_unit(script: dict, unit_id: str) -> dict | None:
    """Locate a unit among the script's self-contained ``video_units``. / 在剧本的自包含 ``video_units`` 中定位单元。"""
    units = script.get("video_units") or []
    return next((u for u in units if isinstance(u, dict) and u.get("unit_id") == unit_id), None)
