"""v18->v19 migration: write each episode's largest used entry number, and move the basis of storyboard images and
grids to a basis that doesn't record asset names.

Numbering (``docs/adr/0090``: numbers are only issued, never reused): before the upgrade, new entries only avoided
numbers currently in the script, so after deleting the last entry a new one would take its number back. This step
computes each episode's largest used main sequence number from five sources (scripts, version history, the
artifact manifest, deletion records in the project trash, clip references in edit timelines) and writes it into
``item_id_high_water`` of the corresponding ledger entry in ``project.json``; from then on new entries always take
numbers after it. Existing numbers don't change and files aren't renamed.

Basis: the model inputs of storyboard images, grid composites and grid-split shots don't include asset names, and
from v19 their bases don't record asset names either (``project_basis_records_asset_names``), so renaming an asset
alone no longer makes them stale. This step plans the target state once under the v18 basis and once under v19, and
rewrites manifest entries that "were current before the rewrite and whose target registration changed" to the
rewritten registration: artifacts don't flip to stale because of this upgrade; entries already stale before the
rewrite are kept as-is, without faking currency. The bases frozen in version records store the full input and are
rewritten one by one with the same ``without_asset_names`` as the builder, so the player and version restore read
the same basis as the manifest.

Commit order is version records, manifest, ``project.json``; after a crash rerunning the whole step converges: the
version record rewrite is idempotent; the pre-rewrite plan is still computed from the untouched ``project.json``,
and already rewritten manifest entries no longer equal the pre-rewrite target and are skipped.

Manifest registrations in project trash deletion records hold only digests and aren't rewritten: storyboard images
or grids deleted before the upgrade and restored after it read as stale after restore if they referenced assets.

v18→v19 迁移：为每集写入条目编号用过的最大号，并把分镜图与宫格的依据平移到不记资产名的口径。

编号（``docs/adr/0090``：编号只分配、不复用）：升级前新增条目只避开剧本里现存的编号，删掉末尾条目后
再新增会取回它的编号。本步按五个来源（剧本、版本历史、产物清单、项目回收站里的删除记录、剪辑时间线的
片段引用）算出每集用过的最大主序号，写进 ``project.json`` 对应账本条目的 ``item_id_high_water``；此后的
新增一律从它往后取号。存量编号不变，文件不改名。

依据：分镜图、宫格联合图与切格分镜的模型输入不含资产名，v19 起它们的依据也不记资产名
（``project_basis_records_asset_names``），资产只改名不再让它们判过期。本步按 v18 与 v19 口径各规划一次
目标态，把「改写前正是 current、且目标登记变了」的清单条目改写为改写后的登记：产物不因这次升级翻
过期，改写前就已过期的条目原样保留，不伪造时新性。版本记录冻结的依据保存着完整输入，按与构造器同一个
``without_asset_names`` 逐条改写，播放器与版本恢复读到的口径与清单一致。

提交顺序是版本记录、清单、``project.json``，崩溃后整步重跑可收敛：版本记录的改写幂等；改写前规划仍从
未动过的 ``project.json`` 算出，已改写的清单条目不再等于改写前目标而被跳过。

项目回收站删除记录里的清单登记只有摘要，不在改写之列：升级前删除、升级后恢复的分镜图或宫格若引用了
资产，恢复后读作过期。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from lib.artifacts.artifact_activation import assert_artifact_target_state_plan_unchanged
from lib.artifacts.artifact_manifest import (
    MANIFEST_FILENAME,
    ArtifactBasis,
    ArtifactKey,
    ArtifactKind,
    ArtifactManifestEntry,
    ProjectArtifactManifestAdapter,
)
from lib.artifacts.artifact_planner import ArtifactTargetStatePlan, TargetStatePlanner
from lib.artifacts.artifact_version_provenance import IMAGE_ARTIFACT_BASIS_FIELD
from lib.artifacts.formal_write import project_metadata_lock
from lib.artifacts.visual_artifact_provenance import ASSET_NAME_FREE_IMAGE_BASIS_KINDS, without_asset_names
from lib.infra.json_io import atomic_write_bytes, atomic_write_json
from lib.project.item_id_history import max_used_item_numbers
from lib.project.project_migration_report import ArtifactBackfillOutcome
from lib.project.project_migrations.backups import ensure_versioned_backup
from lib.project.project_schema import parse_project_schema_version
from lib.script.item_ids import raise_item_id_high_water

TARGET_SCHEMA_VERSION = 19

#: Artifact kinds whose basis changes to not recording asset names (grid-split shots and single storyboard images
#: share the storyboard image key).
#: 依据改为不记资产名的产物种类（切格分镜与单张分镜图共用分镜图键）。
_NAME_FREE_KINDS = frozenset({ArtifactKind.EPISODE_STORYBOARD, ArtifactKind.EPISODE_GRID})
#: Version record buckets whose frozen basis may record asset names. / 冻结依据可能记着资产名的版本记录桶。
_NAME_FREE_VERSION_BUCKETS = ("storyboards", "grids")


def migrate_project_dict(project: Mapping[str, Any], used: Mapping[int, int]) -> dict[str, Any]:
    """Pure function: raise each episode's ledger entry maximum per ``used`` (``{episode ID: largest used main sequence
    number}``). Idempotent, only goes up.

    纯函数：按 ``used``（``{集 ID: 用过的最大主序号}``）抬高各集账本条目的最大号。幂等，只升不降。"""

    migrated = dict(project)
    episodes = migrated.get("episodes")
    if not isinstance(episodes, list):
        return migrated
    entries: list[Any] = []
    for entry in episodes:
        if isinstance(entry, dict) and isinstance(episode := entry.get("episode"), int) and episode in used:
            entry = dict(entry)
            raise_item_id_high_water(entry, used[episode])
        entries.append(entry)
    migrated["episodes"] = entries
    return migrated


def without_asset_names_in_versions(versions: dict[str, Any]) -> bool:
    """Rewrite the bases frozen in storyboard image and grid version records in place into the form without asset names;
    return whether anything changed.

    The rewrite is another notation for the same inputs and doesn't judge whether it is current for the present
    project: a version already stale is still judged by its frozen inputs after the rewrite. Frozen bases that are
    unreadable, whose digests don't match, or whose kind isn't among those rewritten are kept as-is.

    就地把分镜图与宫格版本记录冻结的依据改写为不记资产名的形态，返回是否有改动。

    改写的是同一组输入的另一种记法，不判断它对当前项目是否时新：本就过期的版本改写后仍按它
    冻结的输入判时效。读不出、摘要对不上或种类不在改写之列的冻结依据原样保留。
    """

    changed = False
    for bucket_name in _NAME_FREE_VERSION_BUCKETS:
        bucket = versions.get(bucket_name)
        if not isinstance(bucket, Mapping):
            continue
        for resource in bucket.values():
            records = resource.get("versions") if isinstance(resource, Mapping) else None
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                try:
                    frozen = ArtifactBasis.from_evidence_dict(record.get(IMAGE_ARTIFACT_BASIS_FIELD))
                except (TypeError, ValueError):
                    continue
                if frozen.kind not in ASSET_NAME_FREE_IMAGE_BASIS_KINDS:
                    continue
                inputs = frozen.to_evidence_dict()["inputs"]
                if not isinstance(inputs, Mapping):
                    continue
                try:
                    rewritten = ArtifactBasis.build(
                        frozen.kind,
                        kind_version=frozen.kind_version,
                        inputs=without_asset_names(frozen.kind, inputs),
                    )
                except (TypeError, ValueError):
                    continue
                if rewritten.digest != frozen.digest:
                    record[IMAGE_ARTIFACT_BASIS_FIELD] = rewritten.to_evidence_dict()
                    changed = True
    return changed


def _plan(project_dir: Path, project: Mapping[str, Any]) -> ArtifactTargetStatePlan:
    project_bytes = json.dumps(project, ensure_ascii=False).encode("utf-8")
    return TargetStatePlanner(project_dir, project_bytes=project_bytes).plan()


def _rebase_entries(
    stored: Mapping[ArtifactKey, ArtifactManifestEntry],
    before: Mapping[ArtifactKey, ArtifactManifestEntry],
    after: Mapping[ArtifactKey, ArtifactManifestEntry],
) -> dict[ArtifactKey, ArtifactManifestEntry]:
    """Pick storyboard image and grid entries that were current before the rewrite and whose target registration changed
    after it; a different path means it's not the same artifact.

    挑出改写前 current、改写后目标登记变了的分镜图与宫格条目；路径不同就不是同一件产物。"""

    rebased: dict[ArtifactKey, ArtifactManifestEntry] = {}
    for key, current in stored.items():
        if key.kind not in _NAME_FREE_KINDS:
            continue
        target_after = after.get(key)
        if target_after is None or current == target_after or target_after.artifact_path != current.artifact_path:
            continue
        if current == before.get(key):
            rebased[key] = target_after
    return rebased


def _with_versions_bytes(plan: ArtifactTargetStatePlan, versions_path: Path, raw: bytes) -> ArtifactTargetStatePlan:
    """Version records are a planning dependency: after this step rewrites them, the stability gate must go by the
    rewritten bytes.

    版本记录是规划依赖：本步改写它之后，稳定性闸门要以改写后的字节为准。"""

    resolved = versions_path.resolve()
    return replace(
        plan,
        dependency_bytes={
            path: (raw if path.resolve() == resolved else content) for path, content in plan.dependency_bytes.items()
        },
    )


def _rebase_name_free_bases(
    project_dir: Path, project: Mapping[str, Any], migrated: Mapping[str, Any], project_bytes: bytes
) -> ArtifactBackfillOutcome | None:
    """Rewrite the bases frozen in version records and the manifest registrations that were current before the rewrite.
    The caller holds the project metadata lock.

    改写版本记录冻结的依据与改写前时新的清单登记。调用方持有项目元数据锁。"""

    versions_path = project_dir / "versions" / "versions.json"
    try:
        versions_bytes: bytes | None = versions_path.read_bytes()
    except FileNotFoundError:
        versions_bytes = None
    versions = json.loads(versions_bytes) if versions_bytes is not None else None
    versions_changed = isinstance(versions, dict) and without_asset_names_in_versions(versions)

    adapter = ProjectArtifactManifestAdapter(project_dir)
    stored = adapter.snapshot_entries()
    rebased: dict[ArtifactKey, ArtifactManifestEntry] = {}
    plans: list[ArtifactTargetStatePlan] = []
    if any(key.kind in _NAME_FREE_KINDS for key in stored):
        plans = [_plan(project_dir, project), _plan(project_dir, migrated)]
        for plan in plans:
            assert_artifact_target_state_plan_unchanged(project_dir, plan, expected_project_bytes=project_bytes)
        rebased = _rebase_entries(stored, plans[0].entries, plans[1].entries)

    if versions_changed:
        ensure_versioned_backup(versions_path, TARGET_SCHEMA_VERSION - 1)
        atomic_write_json(versions_path, versions)
        rewritten_bytes = versions_path.read_bytes()
        plans = [_with_versions_bytes(plan, versions_path, rewritten_bytes) for plan in plans]
    if not rebased:
        return None
    expected = {key: stored[key] for key in rebased}

    def restore_versions() -> None:
        if versions_changed and versions_bytes is not None:
            atomic_write_bytes(versions_path, versions_bytes)

    ensure_versioned_backup(project_dir / MANIFEST_FILENAME, TARGET_SCHEMA_VERSION - 1)
    try:
        if not adapter.replace_entries_if_matches_atomically(expected=expected, replacements=rebased):
            raise RuntimeError("artifact manifest changed while rebasing storyboard and grid bases")
    except BaseException:
        restore_versions()
        raise
    try:
        for plan in plans:
            assert_artifact_target_state_plan_unchanged(project_dir, plan, expected_project_bytes=project_bytes)
    except BaseException as original_error:
        restored = adapter.replace_entries_if_matches_atomically(expected=rebased, replacements=expected)
        if not restored and any(adapter.get_entry(key) != entry for key, entry in expected.items()):
            # When the manifest rollback is incomplete, version records stay rewritten: both stop at the new basis together, so a
            # rerun won't split them.
            # 清单回滚不完整时版本记录保持已改写：两者一起停在新口径，重跑时不会分裂。
            raise RuntimeError("artifact manifest dependency drifted and rollback was incomplete") from original_error
        restore_versions()
        raise
    return ArtifactBackfillOutcome.from_entries(adapter.snapshot_entries(), preserve_previous_skips=True)


def migrate_v18_to_v19(project_dir: Path) -> ArtifactBackfillOutcome | None:
    """v18->v19 file-level migration. Crash-retryable. / v18→v19 文件级迁移。崩溃可重试。"""

    project_dir = Path(project_dir)
    project_file = project_dir / "project.json"
    if not project_file.is_file():
        return None
    with project_metadata_lock(project_dir):
        project_bytes = project_file.read_bytes()
        project = json.loads(project_bytes)
        if not isinstance(project, dict):
            raise ValueError("project.json 必须是对象")
        if parse_project_schema_version(project) >= TARGET_SCHEMA_VERSION:
            return None
        used = max_used_item_numbers(project_dir)
        migrated = {**migrate_project_dict(project, used), "schema_version": TARGET_SCHEMA_VERSION}
        outcome = _rebase_name_free_bases(project_dir, project, migrated, project_bytes)
        atomic_write_json(project_file, migrated)
    return outcome


__all__ = ["TARGET_SCHEMA_VERSION", "migrate_project_dict", "migrate_v18_to_v19", "without_asset_names_in_versions"]
