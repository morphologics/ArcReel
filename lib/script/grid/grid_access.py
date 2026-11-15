"""Project-level admission check for grid write operations.

The four write entry points (regenerate / split / upload / version restore) share this single check: if any
entry point skips it, leftover grid records of a blocked project can be rewritten through that path. Loading
the project is left to each caller; this module only owns the policy "is this project allowed to rewrite grids
right now".

宫格写操作的项目级准入判定。

重生成 / 切分 / 上传 / 版本还原四条写入口共用这一份判定：任一入口漏掉它，
被封禁项目的残留 grid 记录就能从那条路被改写。项目的加载留给各调用方，
本模块只承担「这个项目此刻允不允许改写宫格」的策略。
"""

from __future__ import annotations

from typing import Any

from lib.infra.api_errors import BadRequestError
from lib.project.project_manager import grid_storyboard_enabled


def ensure_grid_writable(project: dict[str, Any]) -> None:
    """Raise BadRequestError when the project is not allowed to rewrite grids.

    - Ad/short projects do not offer multi-grid storyboards; existing grid records stay read-only;
    - Projects without grid enabled (including the reference_video generation mode) likewise keep existing grid
      records read-only; regenerate, split, upload and restore are not allowed.

    项目不允许改写宫格时抛 BadRequestError。

    - 广告/短片项目不开放宫格分镜，已有 grid 记录保持只读；
    - 未启用宫格的项目（含 reference_video 生成模式）同样只读已有 grid 记录，
      不允许重生成、切分、上传或还原。
    """
    if project.get("content_mode") == "ad":
        raise BadRequestError("ad_grid_not_supported")
    if not grid_storyboard_enabled(project):
        raise BadRequestError("grid_storyboard_not_enabled")
