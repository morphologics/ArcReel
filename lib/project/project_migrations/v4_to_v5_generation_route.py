"""v4->v5 migration: generation mode narrows to a binary, grid drops to the grid_storyboard switch.

Four responsibilities:
- re-encode ``generation_mode == "grid"`` as ``storyboard + grid_storyboard=true`` (grid isn't a generation mode,
  only a way of producing storyboard images);
- back-fill an explicit ``storyboard`` when ``generation_mode`` is missing or a non-binary dirty value (consistent
  with the pre-migration read side falling back to storyboard for unknown values; behavior unchanged);
- remove all episode-level ``episodes[].generation_mode`` override fields (generation mode is always fixed per
  project);
- don't touch script files.

v4→v5 迁移：生成模式收缩为二值，宫格降为 grid_storyboard 开关。

四项职责：
- ``generation_mode == "grid"`` 重编码为 ``storyboard + grid_storyboard=true``（宫格不是生成模式，
  只是分镜图的生产方式）；
- ``generation_mode`` 缺失或非二值脏值补写显式 ``storyboard``（与迁移前读侧对未知值回退
  storyboard 的口径一致，行为不变）；
- 剔除全部集级 ``episodes[].generation_mode`` 覆盖字段（生成模式一律按项目定轴）；
- 不触碰剧本文件。
"""

from __future__ import annotations

from pathlib import Path

from lib.infra.json_io import atomic_write_json, load_json

_ROUTE_MODES = {"storyboard", "reference_video"}


def migrate_project_dict(project: dict) -> dict:
    """Pure function: convert a v4-shaped project dict into the v5 shape. Idempotent.

    Doesn't change schema_version (written when the file-level migrate commits).

    纯函数：把 v4 形态的 project dict 转为 v5 形态。幂等。

    不改 schema_version（由文件级 migrate 提交时写入）。
    """
    data = dict(project)

    mode = data.get("generation_mode")
    if mode == "grid":
        data["generation_mode"] = "storyboard"
        data["grid_storyboard"] = True
    # Check the type before set membership: project.json is a plain-text file and generation_mode may have been written as
    # an unhashable list / dict; a direct `in` would raise TypeError and abort the startup migration
    # 先判类型再做集合成员检查：project.json 是明文文件，generation_mode 可能被写成
    # list / dict 等不可哈希值，直接 `in` 会抛 TypeError 令启动期迁移中止
    elif not isinstance(mode, str) or mode not in _ROUTE_MODES:
        data["generation_mode"] = "storyboard"
    data.setdefault("grid_storyboard", False)

    episodes = data.get("episodes")
    if isinstance(episodes, list):
        data["episodes"] = [
            {k: v for k, v in ep.items() if k != "generation_mode"} if isinstance(ep, dict) else ep for ep in episodes
        ]

    return data


def migrate_v4_to_v5(project_dir: Path) -> None:
    """v4->v5 file-level migration. A single atomic write, crash-retryable (either old value or new value, no half state).

    v4→v5 文件级迁移。单次原子写，崩溃可重试（要么旧值要么新值，无半态）。"""
    pj = project_dir / "project.json"
    if not pj.exists():
        return
    data = load_json(pj)
    # int-normalize the same way as the runner's version read: historical project.json may store a string version number
    # 与 runner 的版本读取同口径做 int 归一化：历史 project.json 可能存字符串版本号
    if int(data.get("schema_version") or 0) >= 5:
        return
    migrated = migrate_project_dict(data)
    migrated["schema_version"] = 5
    atomic_write_json(pj, migrated)
