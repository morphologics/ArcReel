"""v3->v4 migration: old task-level text backend keys -> task tier keys (docs/adr/0051).

Mapping: text_backend_script -> text_backend_complex; text_backend_overview / text_backend_style ->
text_backend_simple; when both have values, take style's (style tasks need vision; the reverse would move style
analysis to a model that may not support image input). Old keys are deleted after migration.

v3→v4 迁移：旧任务级文本 backend 键 → 任务档位键（docs/adr/0051）。

映射：text_backend_script → text_backend_complex；text_backend_overview / text_backend_style
→ text_backend_simple，两者都有值时取 style 的值（style 任务需要 vision，反向会让风格分析
换到可能不支持图像输入的模型）。迁移后删除旧键。
"""

from __future__ import annotations

from pathlib import Path

from lib.infra.json_io import atomic_write_json, load_json

_LEGACY_TEXT_TASK_KEYS = ("text_backend_script", "text_backend_overview", "text_backend_style")


def _clean_str(value: object) -> str | None:
    """Non-empty strings are returned as-is; anything else (missing / null / empty string / non-string dirty values) counts
    as unset.

    非空字符串原样返回，其余（缺失 / null / 空串 / 非字符串脏值）视为未设置。"""
    if isinstance(value, str) and value.strip():
        return value
    return None


def migrate_project_dict(project: dict) -> dict:
    """Pure function: convert a v3-shaped project dict into the v4 shape. Idempotent.

    Tier keys that already have values aren't overwritten (so a retry doesn't revert newer values the user set later).
    Doesn't change schema_version (written when the file-level migrate commits).

    纯函数：把 v3 形态的 project dict 转为 v4 形态。幂等。

    档位键已有值时不覆盖（避免重试时回退用户后配的新值）。
    不改 schema_version（由文件级 migrate 提交时写入）。
    """
    data = dict(project)

    script = _clean_str(data.get("text_backend_script"))
    if script and not _clean_str(data.get("text_backend_complex")):
        data["text_backend_complex"] = script

    simple = _clean_str(data.get("text_backend_style")) or _clean_str(data.get("text_backend_overview"))
    if simple and not _clean_str(data.get("text_backend_simple")):
        data["text_backend_simple"] = simple

    for key in _LEGACY_TEXT_TASK_KEYS:
        data.pop(key, None)

    return data


def migrate_v3_to_v4(project_dir: Path) -> None:
    """v3->v4 file-level migration. A single atomic write, crash-retryable (either old value or new value, no half state).

    v3→v4 文件级迁移。单次原子写，崩溃可重试（要么旧值要么新值，无半态）。"""
    pj = project_dir / "project.json"
    if not pj.exists():
        return
    data = load_json(pj)
    # int-normalize the same way as the runner's version read: historical project.json may store a string version number
    # 与 runner 的版本读取同口径做 int 归一化：历史 project.json 可能存字符串版本号
    if int(data.get("schema_version") or 0) >= 4:
        return
    migrated = migrate_project_dict(data)
    migrated["schema_version"] = 4
    atomic_write_json(pj, migrated)
