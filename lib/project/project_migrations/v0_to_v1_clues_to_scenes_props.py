"""v0->v1 migration: split clues -> scenes + props; delete importance; cascade to script JSON.

v0→v1 迁移：拆分 clues → scenes + props；删除 importance；级联剧本 JSON。"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any

from lib.infra.json_io import atomic_write_json, load_json


def _split_clues(clues: dict[str, dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    scenes: dict[str, dict] = {}
    props: dict[str, dict] = {}
    for name, data in clues.items():
        clue_type = (data.get("type") or "prop").lower()
        new_item: dict[str, Any] = {
            "description": data.get("description", ""),
        }
        # Keep the generated image path but rename the field / 保留生成图路径但换字段名
        sheet = data.get("clue_sheet")
        if clue_type == "location":
            if sheet:
                new_item["scene_sheet"] = sheet.replace("clues/", "scenes/", 1)
            scenes[name] = new_item
        else:
            if sheet:
                new_item["prop_sheet"] = sheet.replace("clues/", "props/", 1)
            props[name] = new_item
    return scenes, props


def _relocate_clue_files(project_dir: Path, old_clues: dict[str, dict]) -> None:
    clues_dir = project_dir / "clues"
    if not clues_dir.exists():
        return
    scenes_dir = project_dir / "scenes"
    props_dir = project_dir / "props"
    scenes_dir.mkdir(exist_ok=True)
    props_dir.mkdir(exist_ok=True)

    for name, data in old_clues.items():
        clue_type = (data.get("type") or "prop").lower()
        target = scenes_dir if clue_type == "location" else props_dir
        for ext in ("png", "jpg", "jpeg", "webp"):
            src = clues_dir / f"{name}.{ext}"
            if src.exists():
                shutil.move(str(src), str(target / f"{name}.{ext}"))

    # Clean up the empty clues directory (keep it if unknown leftover files remain, to avoid wrong deletion); note we must
    # not return on failure -- the versions/clues migration below is unrelated to whether this directory can be deleted
    # and must continue unconditionally, otherwise version files would be lost forever after schema_version goes to 1.
    # If the directory isn't empty (unknown leftover files), keep it; not a migration failure
    # 清理空 clues 目录（即使有残余未知文件也保留，避免误删）；
    # 注意不能在失败时 return—— 下方 versions/clues 迁移与此目录的可删性无关，
    # 必须无条件继续执行，否则 schema_version 升 1 后永久遗失版本文件。
    # 目录非空（有残余未知文件）则保留，不视为迁移失败
    with contextlib.suppress(OSError):
        clues_dir.rmdir()  # hard-delete: infra -- empty directory after migration moved files away / 迁移搬走后的空目录

    # versions/clues are likewise split by the original clue type / versions/clues 同样按原 clue type 分流
    versions_clues = project_dir / "versions" / "clues"
    if versions_clues.exists():
        for name, data in old_clues.items():
            clue_type = (data.get("type") or "prop").lower()
            target_versions = project_dir / "versions" / ("scenes" if clue_type == "location" else "props")
            target_versions.mkdir(parents=True, exist_ok=True)
            for file in versions_clues.glob(f"{name}*"):
                shutil.move(str(file), str(target_versions / file.name))
        # If versions/clues isn't empty (unclassified files), keep it; not a migration failure / versions/clues 非空（有未归类文件）则保留，不视为迁移失败
        with contextlib.suppress(OSError):
            versions_clues.rmdir()  # hard-delete: infra -- empty directory after migration moved files away / 迁移搬走后的空目录


def _migrate_scripts(project_dir: Path, old_clues: dict[str, dict]) -> None:
    """Split each scene/segment's clues[] in the script into scenes[] + props[] / 把剧本里每条 scene/segment 的 clues[] 拆为 scenes[] + props[]"""
    scripts_dir = project_dir / "scripts"
    if not scripts_dir.exists():
        return

    def kind(clue_name: str) -> str:
        data = old_clues.get(clue_name, {})
        return "scene" if (data.get("type") or "prop").lower() == "location" else "prop"

    for sp in scripts_dir.glob("*.json"):
        try:
            data = load_json(sp)
        except Exception:
            continue
        if (data.get("schema_version") or 0) >= 1:
            continue

        # Historical clue reference fields in v0 scripts: clues / clues_in_segment / clues_in_scene. All three may coexist and
        # need to be merged before splitting into scenes/props.
        # v0 剧本中线索引用历史字段：clues / clues_in_segment / clues_in_scene。
        # 三者都可能并存，需要合并后再拆为 scenes/props。
        legacy_fields = ("clues", "clues_in_segment", "clues_in_scene")
        for bucket_key in ("scenes", "segments"):
            items = data.get(bucket_key) or []
            for item in items:
                merged: list[str] = []
                seen: set[str] = set()
                found_any = False
                for lf in legacy_fields:
                    if lf in item:
                        found_any = True
                        for nm in item.pop(lf) or []:
                            if nm not in seen:
                                seen.add(nm)
                                merged.append(nm)
                if not found_any:
                    continue
                scenes_list: list[str] = []
                props_list: list[str] = []
                for nm in merged:
                    (scenes_list if kind(nm) == "scene" else props_list).append(nm)
                item["scenes"] = scenes_list
                item["props"] = props_list

        data["schema_version"] = 1
        atomic_write_json(sp, data)


def _reconstruct_old_clues_from_v1(data: dict) -> dict[str, dict]:
    """Reconstruct old_clues from the v1 schema (for self-healing reruns of half-migrated projects).

    scene -> type=location, prop -> type=prop; the clue_sheet field can't be recovered (already deleted), but
    _relocate_clue_files only depends on name + type to route files, not on clue_sheet.

    从 v1 schema 反推 old_clues（用于半迁移项目的自愈补跑）。

    scene → type=location，prop → type=prop；clue_sheet 字段无法恢复（已删除），但
    _relocate_clue_files 仅依赖 name + type 分流文件，不依赖 clue_sheet。
    """
    old: dict[str, dict] = {}
    for name in data.get("scenes") or {}:
        old[name] = {"type": "location"}
    for name in data.get("props") or {}:
        old[name] = {"type": "prop"}
    return old


def migrate_v0_to_v1(project_dir: Path) -> None:
    """v0->v1 migration. Idempotent + self-healing for half migrations.

    Order: move files first -> then change scripts -> finally bump schema_version. That way if any step crashes,
    schema_version is still 0 and the next startup retries (not skipped as "already bumped", losing images).

    v0→v1 迁移。幂等 + 半迁移自愈。

    顺序：先搬文件 → 再改剧本 → 最后升 schema_version。这样任一步崩溃时
    schema_version 仍是 0，下次启动会重新尝试（不会因"已升版本"而跳过丢图）。
    """
    pj = project_dir / "project.json"
    if not pj.exists():
        return
    data = load_json(pj)
    # or 0: explicit null means the same as a missing field (v0); comparing None >= 1 directly would raise TypeError
    # or 0：显式 null 与字段缺失同义（v0），直接比较 None >= 1 会 TypeError
    current_version = data.get("schema_version") or 0

    # Self-heal: schema_version>=1 but clues/ still exists -> rerun the file/script migration
    # 自愈：schema_version>=1 但 clues/ 仍存在 → 补跑文件/剧本迁移
    if current_version >= 1:
        clues_dir = project_dir / "clues"
        if clues_dir.is_dir() and any(clues_dir.iterdir()):
            old_clues = _reconstruct_old_clues_from_v1(data)
            _relocate_clue_files(project_dir, old_clues)
            _migrate_scripts(project_dir, old_clues)
        return

    old_clues: dict[str, dict] = data.get("clues") or {}

    # 1. Move files first (if any step fails, schema_version is still 0 and a restart retries) / 1. 先搬文件（任一步失败时 schema_version 仍为 0，重启会重试）
    _relocate_clue_files(project_dir, old_clues)

    # 2. Cascade to scripts / 2. 级联剧本
    _migrate_scripts(project_dir, old_clues)

    # 3. Finally update project.json (atomic write; bumping schema_version serves as the "commit" marker). Only rewrite
    # scenes/props when clues actually has data; otherwise keep fields a new project already has, so "missing
    # schema_version + no clues + existing scenes/props" isn't wrongly cleared.
    # 3. 最后更新 project.json（原子写，schema_version 升级作为"提交"标志）
    # 仅在 clues 实际有数据时改写 scenes/props；否则保留新项目已有的字段，
    # 避免"schema_version 缺失 + 无 clues + 已有 scenes/props"被误清空。
    if old_clues:
        scenes, props = _split_clues(old_clues)
        data["scenes"] = scenes
        data["props"] = props
    else:
        data.setdefault("scenes", {})
        data.setdefault("props", {})
    data.pop("clues", None)
    data["schema_version"] = 1
    atomic_write_json(pj, data)
