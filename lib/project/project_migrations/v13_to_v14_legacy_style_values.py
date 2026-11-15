"""v13->v14 migration: normalize existing projects' legacy style values once, and rewrite the artifact bases that gain
the style description to the new basis.

Two legacy style values: the "画风：" (art style:) prefix at the start of the style value (how old style templates
wrote it; together with the English ``Style:`` label it would render as a mixed "Style: 画风：..."), and the three
short labels ``Photographic`` / ``Anime`` / ``3D Animation`` (an earlier style enum, previously resolved into a
template id and expanded into a snapshot every time ``ProjectManager`` read a project). After normalization is on
disk, the runtime only reads normalized values; prefix stripping and short-label resolution no longer exist.

Style description: custom-style projects (with an uploaded style reference image) have an empty ``style``, and
``style_description`` is the only style signal. The prompts of grid composites, grid-split shots and reference
videos all consume it; from v14 these three bases record the description when it's non-empty; projects with lower
schema keep the basis without the description per ``project_basis_style_description``, so activation before this
step won't register them under the new basis or discard them as stale.

Both change the target digests of artifact bases. This step plans the target state once as a v13 project and once
as a v14 project, and rewrites manifest entries that "were current before the rewrite and whose target registration
changed" to the rewritten registration: artifacts don't flip to stale because of this upgrade, while entries already
stale before the rewrite are kept as-is, without faking currency. The player and version restore read the bases
frozen in version records, so for the three artifact kinds with rewritten registrations, their selected version
records also get the description added.

Commit order is version records, manifest, ``project.json``: if it crashes after the manifest rewrite is on disk but
before the schema is bumped, the whole step reruns; the pre-rewrite plan is still computed from the untouched
``project.json``, and rewritten entries no longer match the "pre-rewrite digest" and are skipped; if the process is
killed after the version records are rewritten but before the manifest, the rerun recognizes the records are already
on the new basis and doesn't rewrite them, and the manifest is rewritten as usual -- neither rewritten twice nor
losing the fix. The reverse order would make the rerun see fixed entries as unfamiliar digests, leaving a stale
mark forever.

v13→v14 迁移：把存量项目的遗留风格值一次性归一，并把补记风格描述的产物依据改写到新口径。

两种遗留风格值：风格值开头的「画风：」前缀（旧版风格模版的写法，叠加英文 ``Style:`` 标签会
渲染成「Style: 画风：…」的中英混叠），以及 ``Photographic`` / ``Anime`` / ``3D Animation``
三个短标签（更早的风格枚举，此前在 ``ProjectManager`` 每次读项目时解析成模版 id 并展开快照）。
归一落盘后运行时只读已归一的值，前缀剥离与短标签解析都不再存在。

风格描述：自定义风格项目（上传过风格参考图）的 ``style`` 为空，``style_description`` 是唯一的
风格信号。宫格联合图、切格分镜与参考视频的提示词都消费它，v14 起这三类依据在描述非空时记下描述；
schema 更低的项目按 ``project_basis_style_description`` 沿用不记描述的口径，本步之前的激活因此
不会把它们登记到新口径或当作过期丢掉。

两件事都会改变产物依据的目标摘要。本步按 v13 项目与 v14 项目各规划一次目标态，把「改写前正是
current、且目标登记变了」的清单条目改写为改写后的登记：产物不因这次升级翻过期，而改写前就已过期
的条目原样保留，不伪造时新性。播放器与版本恢复读的是版本记录冻结的依据，被改写登记的三类产物，
其选中版本记录一并补记描述。

提交顺序是版本记录、清单、``project.json``：清单改写落盘而 schema 尚未提升时崩溃，本步会整步
重跑，届时改写前规划仍从未动过的 ``project.json`` 算出，已改写的条目不再匹配「改写前摘要」而被
跳过；版本记录已改写而清单未改写时进程被杀，重跑认出记录已是新口径而不再改写它，清单照常改写，
既不重复改写也不丢失修复结果。反序则会在重跑时把已修复的条目认成陌生摘要，永久留下过期标记。
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
    compose_video_artifact_basis,
)
from lib.artifacts.artifact_planner import ArtifactTargetStatePlan, TargetStatePlanner
from lib.artifacts.artifact_version_provenance import IMAGE_ARTIFACT_BASIS_FIELD, parse_image_version_basis
from lib.artifacts.formal_write import project_metadata_lock
from lib.artifacts.video_artifact_facts import VideoArtifactCurrencyFacts
from lib.infra.json_io import atomic_write_bytes, atomic_write_json
from lib.project.project_migration_report import ArtifactBackfillOutcome, MigrationSkippedArtifact
from lib.project.project_migrations.backups import ensure_versioned_backup
from lib.project.project_schema import parse_project_schema_version
from lib.prompts.prompt_style import normalize_style_value
from lib.prompts.style_templates import resolve_template_prompt

TARGET_SCHEMA_VERSION = 14

_GRID_MEMBER_KIND = "artifact-visual/grid-member"
_REFERENCE_VIDEO_VISUAL_KIND = "artifact-visual/video-reference"

#: Earlier style enum values -> current style template id. Only this migration reads it: after resolving once and
#: expanding the snapshot, the value no longer appears.
#: 更早的风格枚举值 → 当前风格模版 id。只有本迁移读它：解析一次、展开快照后该值不再出现。
LEGACY_STYLE_MAP: dict[str, str] = {
    "Photographic": "live_premium_drama",
    "Anime": "anim_kyoto",
    "3D Animation": "anim_3d_cg",
}

#: The "画风：" (art style:) prefix at the start of a style value (full-width/half-width colon). ``anim_arcane``'s
#: "油画三渲二画风：" doesn't start with "画风", so it isn't matched -- its "画风" is part of a compound word, not a
#: removable prefix.
#: 风格值开头的「画风：」前缀（全角/半角冒号）。``anim_arcane`` 的「油画三渲二画风：」不以
#: 「画风」起头，因而不在此匹配之列——它的「画风」是复合词的一部分，不是可删前缀。
_STYLE_PREFIX_RE = re.compile(r"^画风[：:]\s*")


def migrate_project_dict(project: dict[str, Any]) -> dict[str, Any]:
    """Pure function: convert a v13-shaped project dict into the v14 shape. Idempotent.

    When ``style`` isn't a string it's kept as-is, with its structural error reported separately by DataValidator.
    Short-label resolution only happens when the project doesn't yet have ``style_template_id``: having that field means
    the user chose a style afterwards, and that choice is accurate. The reference-image-first basis follows the existing
    behavior at resolution time -- with ``style_image``, clear the style value and set the template id to null. Doesn't
    change ``schema_version`` (written when the file-level migrate commits).

    纯函数：把 v13 形态的 project dict 转为 v14 形态。幂等。

    ``style`` 不是字符串时原样保留，其结构错误由 DataValidator 另行报告。短标签解析只在项目
    还没有 ``style_template_id`` 时发生：已有该字段说明用户此后选过风格，那时的选择是准的。
    参考图优先的口径沿用解析期的既有行为——有 ``style_image`` 时清空风格值、模版 id 置 null。
    不改 ``schema_version``（由文件级 migrate 提交时写入）。
    """
    data = copy.deepcopy(project)
    raw = data.get("style")
    if not isinstance(raw, str):
        return data
    if "style_template_id" not in data and raw in LEGACY_STYLE_MAP:
        if data.get("style_image"):
            data["style_template_id"] = None
            data["style"] = ""
        else:
            template_id = LEGACY_STYLE_MAP[raw]
            data["style_template_id"] = template_id
            data["style"] = resolve_template_prompt(template_id)
        return data
    stripped = raw.strip()
    if _STYLE_PREFIX_RE.match(stripped):
        data["style"] = _STYLE_PREFIX_RE.sub("", stripped)
    return data


@dataclass(frozen=True, slots=True)
class _Planned:
    plan: ArtifactTargetStatePlan
    bases: Mapping[ArtifactKey, ArtifactBasis]


def _plan(project_dir: Path, project: Mapping[str, Any], *, allow_stale: bool) -> _Planned:
    """Plan one complete target state from the given project content, without writing it first.

    ``allow_stale`` lets stale formal targets through when the description is non-empty: the bases frozen in reference
    video version records don't include the style description, and a v14-basis plan would skip them as "content changed
    after generation", leaving nothing to rewrite. Currency is decided by whether the registration equals the
    pre-rewrite target, so letting stale targets through won't mix in entries that were already stale.

    按给定项目内容规划一次完整目标态，不必先落盘。

    ``allow_stale`` 在描述非空时放行过期的正式目标：参考视频版本记录冻结的依据不含风格描述，v14
    口径的规划会把它们当作「生成后内容已变」跳过，改写便无从谈起。时新与否由登记是否等于改写前
    目标来裁决，放行过期目标不会让本就过期的条目混进来。
    """
    project_bytes = json.dumps(project, ensure_ascii=False).encode("utf-8")
    planner = TargetStatePlanner(project_dir, project_bytes=project_bytes, allow_stale_formal_targets=allow_stale)
    plan = planner.plan()
    return _Planned(plan=plan, bases=dict(planner.bases))


def _description_bound(key: ArtifactKey, bases: Mapping[ArtifactKey, ArtifactBasis]) -> bool:
    """The three bases that gain the style description: grid composites, grid-split shots and videos (shot video bases
    don't include style and stay unchanged).

    Grid-split shots and single storyboard images share the ``episode_storyboard`` key; only the basis kind tells them
    apart.

    补记风格描述的三类依据：宫格联合图、切格分镜与视频（分镜视频的依据不含风格，前后不变）。

    切格分镜与单张分镜图共用 ``episode_storyboard`` 键，只有依据种类能区分二者。
    """
    if key.kind in {ArtifactKind.EPISODE_GRID, ArtifactKind.EPISODE_VIDEO}:
        return True
    basis = bases.get(key)
    return key.kind is ArtifactKind.EPISODE_STORYBOARD and basis is not None and basis.kind == _GRID_MEMBER_KIND


def _rebase_entries(
    stored: Mapping[ArtifactKey, ArtifactManifestEntry],
    before: Mapping[ArtifactKey, ArtifactManifestEntry],
    after: Mapping[ArtifactKey, ArtifactManifestEntry],
) -> dict[ArtifactKey, ArtifactManifestEntry]:
    """Pick entries current before the rewrite whose target registration changed after it, and give their rewritten
    registrations.

    The artifact path must match too: a different path means it's not the same artifact.

    挑出改写前 current、改写后目标登记变了的条目，给出它们改写后的登记。

    产物路径也必须一致：路径不同就不是同一件产物。
    """
    rebased: dict[ArtifactKey, ArtifactManifestEntry] = {}
    for key, current in stored.items():
        target_after = after.get(key)
        if target_after is None or current == target_after or target_after.artifact_path != current.artifact_path:
            continue
        if current == before.get(key):
            rebased[key] = target_after
    return rebased


def _selected_version_record(
    versions: Mapping[str, Any], resource_type: str, resource_id: str
) -> dict[str, Any] | None:
    bucket = versions.get(resource_type)
    resource = bucket.get(resource_id) if isinstance(bucket, Mapping) else None
    if not isinstance(resource, Mapping):
        return None
    records = resource.get("versions")
    if not isinstance(records, list):
        return None
    selected = [
        record
        for record in records
        if isinstance(record, dict) and record.get("version") == resource.get("current_version")
    ]
    return selected[0] if len(selected) == 1 else None


def _migrated_video_facts(
    facts: VideoArtifactCurrencyFacts, migrated: Mapping[str, Any], description: str
) -> VideoArtifactCurrencyFacts | None:
    """Replace the style inputs of frozen reference video facts with the migrated style value and description, and rebuild
    the video basis. Returns None for non-reference videos.

    把冻结的参考视频事实的风格入参换成迁移后的风格值与描述，重组视频依据。不是参考视频的返回 None。"""
    inputs = facts.visual_basis.to_evidence_dict()["inputs"]
    if facts.visual_basis.kind != _REFERENCE_VIDEO_VISUAL_KIND or not isinstance(inputs, Mapping):
        return None
    style = migrated.get("style")
    visual = ArtifactBasis.build(
        facts.visual_basis.kind,
        kind_version=facts.visual_basis.kind_version,
        inputs={**inputs, "style": style if isinstance(style, str) else "", "style_description": description},
    )
    video = compose_video_artifact_basis(visual=visual, speech=facts.speech_basis, duration=facts.duration_basis)
    return replace(facts, visual_basis=visual, video_basis=video)


@dataclass(frozen=True, slots=True)
class _VersionRewrite:
    changed: bool
    #: Entries whose selected version record doesn't match the manifest registration and can't be rewritten together: the
    #: registration isn't rewritten and is left to be judged stale at read time.
    #: 选中版本记录与清单登记对不上、无法一并改写的条目：不改写登记，留给读时判过期。
    withdrawn: tuple[ArtifactKey, ...]


def _rewrite_selected_versions(
    versions: dict[str, Any],
    stored: Mapping[ArtifactKey, ArtifactManifestEntry],
    rebased: Mapping[ArtifactKey, ArtifactManifestEntry],
    after: _Planned,
    migrated: Mapping[str, Any],
    description: str,
) -> _VersionRewrite:
    """Rewrite the selected version records of the three artifact kinds with registrations to rewrite to the new basis, in
    place.

    The player and version restore judge currency by the bases frozen in version records: changing only the manifest
    would leave a current video marked "older than the current content" in the player, and restoring the current version
    would register back to the old basis. Records whose frozen basis equals the pre-rewrite registration are rewritten;
    records already on the new basis (process killed after the rewrite was on disk, rerun) are kept as-is; if neither,
    or still inconsistent with the rewritten target after substituting the migrated style inputs, that registration's
    rewrite is withdrawn, so the manifest doesn't judge current while the player judges stale. Artifacts without a
    selected version record only have the manifest changed.

    把待改写登记的三类产物的选中版本记录就地改到新口径。

    播放器与版本恢复按版本记录冻结的依据判断时新：只改清单，时新的视频在播放器上仍标「比当前内容
    旧」，恢复当前版本也会登记回旧口径。冻结依据等于改写前登记的记录改写过去；已是新口径的记录
    （改写落盘后进程被杀、重跑）原样保留；两者都不是，或换上迁移后的风格入参仍与改写后目标不一致，这条登记撤回
    改写，免得清单判时新而播放器判过期。没有选中版本记录的产物只改清单。
    """
    changed = False
    withdrawn: list[ArtifactKey] = []
    for key, target in rebased.items():
        if not _description_bound(key, after.bases):
            continue
        old_digest = stored[key].basis_digest
        resource_id = str(key.components[-1])
        if key.kind is ArtifactKind.EPISODE_VIDEO:
            record = _selected_version_record(versions, "reference_videos", resource_id)
            if record is None:
                continue
            try:
                frozen_facts = VideoArtifactCurrencyFacts.from_dict(record.get("artifact_video_currency"))
            except (TypeError, ValueError):
                withdrawn.append(key)
                continue
            if frozen_facts.video_basis.digest == target.basis_digest:
                continue
            facts = _migrated_video_facts(frozen_facts, migrated, description)
            if (
                frozen_facts.video_basis.digest != old_digest
                or facts is None
                or facts.video_basis.digest != target.basis_digest
            ):
                withdrawn.append(key)
                continue
            record["artifact_video_currency"] = facts.to_dict()
            changed = True
            continue
        resource_type = "grids" if key.kind is ArtifactKind.EPISODE_GRID else "storyboards"
        record = _selected_version_record(versions, resource_type, resource_id)
        if record is None:
            continue
        try:
            frozen = parse_image_version_basis(resource_type, resource_id, record)
        except (TypeError, ValueError):
            withdrawn.append(key)
            continue
        if frozen.digest == target.basis_digest:
            continue
        basis = after.bases.get(key)
        if frozen.digest != old_digest or basis is None or basis.digest != target.basis_digest:
            withdrawn.append(key)
            continue
        record[IMAGE_ARTIFACT_BASIS_FIELD] = basis.to_evidence_dict()
        changed = True
    return _VersionRewrite(changed=changed, withdrawn=tuple(withdrawn))


def _withdrawn_skip(key: ArtifactKey, entry: ArtifactManifestEntry) -> MigrationSkippedArtifact:
    episode = key.components[0]
    return MigrationSkippedArtifact(
        kind=key.kind.value,
        episode=episode if type(episode) is int else None,
        resource_id=str(key.components[-1]),
        artifact_path=entry.artifact_path,
        reason="selected version record does not match the registered basis; style description was not backfilled",
    )


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


def _read_bytes_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def migrate_v13_to_v14(project_dir: Path) -> ArtifactBackfillOutcome | None:
    """v13->v14 file-level migration. / v13→v14 文件级迁移。"""
    project_dir = Path(project_dir)
    pj = project_dir / "project.json"
    if not pj.exists():
        return None
    project_bytes = pj.read_bytes()
    data = json.loads(project_bytes)
    if not isinstance(data, dict):
        raise ValueError("project.json 必须是对象")
    if parse_project_schema_version(data) >= TARGET_SCHEMA_VERSION:
        return None
    migrated = migrate_project_dict(data)
    migrated["schema_version"] = TARGET_SCHEMA_VERSION
    description = normalize_style_value(data.get("style_description"))
    if migrated.get("style") == data.get("style") and not description:
        # Projects whose style value is already normalized and whose description is empty have byte-identical bases: the
        # upgrade shouldn't pay for a full target-state plan for them.
        # 风格值已归一、描述为空的项目依据逐字不变：升级不该为它们付出完整目标态规划的代价。
        atomic_write_json(pj, migrated)
        return None

    with project_metadata_lock(project_dir):
        allow_stale = bool(description)
        before = _plan(project_dir, data, allow_stale=allow_stale)
        after = _plan(project_dir, migrated, allow_stale=allow_stale)
        plans = [before.plan, after.plan]
        for plan in plans:
            assert_artifact_target_state_plan_unchanged(project_dir, plan, expected_project_bytes=project_bytes)
        adapter = ProjectArtifactManifestAdapter(project_dir)
        stored = adapter.snapshot_entries()
        rebased = _rebase_entries(stored, before.plan.entries, after.plan.entries)
        versions_path = project_dir / "versions" / "versions.json"
        versions_bytes = _read_bytes_or_none(versions_path)
        versions: dict[str, Any] = {}
        rewrite = _VersionRewrite(changed=False, withdrawn=())
        if rebased and description and versions_bytes is not None:
            loaded = json.loads(versions_bytes)
            if isinstance(loaded, dict):
                versions = loaded
                rewrite = _rewrite_selected_versions(versions, stored, rebased, after, migrated, description)
        for key in rewrite.withdrawn:
            del rebased[key]
        if rebased:
            _commit_rebase(
                project_dir,
                adapter,
                expected={key: stored[key] for key in rebased},
                rebased=rebased,
                plans=plans,
                project_bytes=project_bytes,
                versions=versions if rewrite.changed else None,
                versions_bytes=versions_bytes,
            )
        skipped = [*after.plan.skipped, *(_withdrawn_skip(key, stored[key]) for key in rewrite.withdrawn)]
        outcome = (
            ArtifactBackfillOutcome.from_entries(adapter.snapshot_entries(), skipped)
            if rebased or skipped or migrated.get("style") != data.get("style")
            else None
        )
        atomic_write_json(pj, migrated)
    return outcome


def _commit_rebase(
    project_dir: Path,
    adapter: ProjectArtifactManifestAdapter,
    *,
    expected: Mapping[ArtifactKey, ArtifactManifestEntry],
    rebased: Mapping[ArtifactKey, ArtifactManifestEntry],
    plans: list[ArtifactTargetStatePlan],
    project_bytes: bytes,
    versions: dict[str, Any] | None,
    versions_bytes: bytes | None,
) -> None:
    """Commit the rewrite version records first, then the manifest; if any step fails, roll back to pre-migration.

    When the manifest rollback is incomplete, version records stay rewritten: both stop at the new basis together, and on
    rerun the manifest registrations already equal the rewritten targets, leaving no split state where the manifest
    judges current and the player judges stale.

    先版本记录后清单地提交改写，任一步失败回滚到迁移前。

    清单回滚不完整时版本记录保持已改写：两者一起停在新口径，重跑时清单登记已等于改写后目标，
    不会留下清单判时新、播放器判过期的分裂状态。
    """
    ensure_versioned_backup(project_dir / MANIFEST_FILENAME, TARGET_SCHEMA_VERSION - 1)
    versions_path = project_dir / "versions" / "versions.json"
    if versions is not None:
        ensure_versioned_backup(versions_path, TARGET_SCHEMA_VERSION - 1)
        atomic_write_json(versions_path, versions)
        new_versions_bytes = versions_path.read_bytes()
        plans = [_with_versions_bytes(plan, versions_path, new_versions_bytes) for plan in plans]
    try:
        if not adapter.replace_entries_if_matches_atomically(expected=expected, replacements=rebased):
            raise RuntimeError("artifact manifest changed while rebasing legacy style bases")
    except BaseException:
        if versions is not None and versions_bytes is not None:
            atomic_write_bytes(versions_path, versions_bytes)
        raise
    try:
        for plan in plans:
            assert_artifact_target_state_plan_unchanged(project_dir, plan, expected_project_bytes=project_bytes)
    except BaseException as original_error:
        restored = adapter.replace_entries_if_matches_atomically(expected=rebased, replacements=expected)
        if not restored and any(adapter.get_entry(key) != entry for key, entry in expected.items()):
            raise RuntimeError("artifact manifest dependency drifted and rollback was incomplete") from original_error
        if versions is not None and versions_bytes is not None:
            atomic_write_bytes(versions_path, versions_bytes)
        raise


__all__ = ["LEGACY_STYLE_MAP", "TARGET_SCHEMA_VERSION", "migrate_project_dict", "migrate_v13_to_v14"]
