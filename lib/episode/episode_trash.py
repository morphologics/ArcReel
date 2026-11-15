"""Restore a deleted episode (``docs/adr/0107``): it keeps its original episode ID and always goes to the end of the episode order as an episode with its own source.

The episode source from before deletion becomes this episode's source: an episode cut from the whole source no
longer occupies its original range, and that stretch of source stays unsplit. An episode that had no episode source
is still an episode without source.
Scripts, planning drafts, edit timelines and final cuts, grids and media are added back at their original paths;
version history and artifact manifest registrations are taken from the migrated temporary project, artifacts are
judged by artifact freshness as usual, and those whose inputs changed after the deletion read as stale.

Objects such as edit timelines and shots whose episode was deleted can only be restored with their original
structure after the episode is restored (:func:`require_live_episode`).

恢复删除的集（``docs/adr/0107``）：沿用原集 ID，一律作为自带原文的集排到播出顺序末尾。

删除前的集原文成为这一集的源文：切自整本源文的集不再占用原范围，那段原文保持未切分。没有集原文的集仍是无原文的集。
剧本、规划草稿、剪辑时间线与成片、宫格、媒体按原路径加回；版本历史与产物清单的登记取自迁移后的临时项目，
产物照常按产物时效判定，删除之后输入变过的读为过期。

所属集已删除的剪辑时间线、分镜等对象，在集恢复之后才能按原结构恢复（:func:`require_live_episode`）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lib.artifacts.artifact_manifest import ArtifactManifestEntry, ProjectArtifactManifestAdapter
from lib.artifacts.artifact_registration import register_artifact_entries_atomically
from lib.artifacts.formal_write import formal_write_transaction
from lib.artifacts.version_manager import VersionManager
from lib.episode.episode_deletion import EPISODE_KIND, episode_content, episode_trash_record
from lib.episode.episode_paths import episode_source_path
from lib.episode.episode_sources import SOURCE_ORIGIN_FIELD, SourceOrigin
from lib.episode.source_kinds import SOURCE_KIND_FIELD, entry_source_kind, record_episode_kind
from lib.infra.api_errors import ConflictError
from lib.project.project_change_hints import emit_project_change_hint
from lib.project.project_manager import ProjectManager, find_episode
from lib.project.trash_restore import Landing, Restoration, RestoreBlocked

__all__ = ["EPISODE_KIND", "require_live_episode", "take_episode"]


def take_episode(project_dir: Path, staged: Path, obj: Mapping[str, Any]) -> Restoration:
    """Take the ledger entry, this episode's files, version records and artifact manifest registrations by episode ID from the migrated temporary project.

    按集 ID 从迁移后的临时项目取出账本条目、这一集的文件、版本记录与产物清单登记。"""
    episode = obj.get("id")
    staged_project = json.loads((staged / "project.json").read_text(encoding="utf-8"))
    entry = find_episode(staged_project, episode if isinstance(episode, int) else None)
    if entry is None:
        raise ConflictError("trash_restore_failed", id=str(episode))
    number: int = entry["episode"]
    content = episode_content(staged, staged_project, number)
    claims = {
        key: ArtifactManifestEntry(claim.artifact_path, claim.basis_digest)
        for key, claim in ProjectArtifactManifestAdapter(staged).snapshot_entries().items()
        if key.episode_number == number
    }
    source = episode_source_path(staged, number)
    own = source.is_file() and not source.is_symlink()
    restored = {key: value for key, value in entry.items() if key != "source_range"}
    restored[SOURCE_ORIGIN_FIELD] = (SourceOrigin.OWN if own else SourceOrigin.NONE).value
    kind = entry_source_kind(staged_project, entry)
    if not own:
        restored.pop(SOURCE_KIND_FIELD, None)
    projects = ProjectManager.for_project_dir(project_dir)

    def append(project: dict[str, Any]) -> None:
        if find_episode(project, number) is not None:
            # Episode IDs are not reused, so the original ID is always free; if it is still taken, do not merge into another episode.
            # 集 ID 不复用，原 ID 总是空闲；仍被占用时不合并进别的集。
            raise ConflictError("trash_restore_failed", id=str(number))
        if own:
            record_episode_kind(project, restored, kind)
        project["episodes"] = [*(project.get("episodes") or []), restored]

    def land(landing: Landing) -> Mapping[str, Any]:
        blocked = next((rel for rel in content.files if not landing.free(rel)), None)
        if blocked is not None:
            raise ConflictError("trash_restore_failed", id=str(number))
        versions = VersionManager(project_dir)
        with formal_write_transaction(project_dir / "project.json", versions.versions_file):
            for rel in content.files:
                landing.place(staged / rel, rel, original=rel)
            versions.attach_resources(content.versions)
            projects.update_project_locked(project_dir.name, append)
            if claims:
                # Re-record: registrations are taken from the manifest from before deletion (after migration); the basis digests are unchanged, and those whose inputs changed since read as stale as usual.
                # 补录：登记取自删除前（迁移后）的清单，依据摘要不变，输入此后变过的照常读为过期。
                register_artifact_entries_atomically(project_dir, claims)
        return obj

    return Restoration(
        land=land,
        after=lambda: emit_project_change_hint(project_dir.name, changed_paths=[ProjectManager.PROJECT_FILE]),
    )


def require_live_episode(project_dir: Path, episode: object, identity: str) -> None:
    """Before restoring an object attached to an episode, check that its episode still exists.

    When the episode is also in the project trash, raises :class:`RestoreBlocked`: it can only be restored with the
    episode; restore the episode first, then this record; the record is not downgraded.
    When the episode is gone and has no deletion record (permanently deleted), raises ``trash_restore_failed``,
    downgraded as an object that cannot be taken out.

    恢复挂在集上的对象前核对所属的集仍在。

    集也在项目回收站里时抛 :class:`RestoreBlocked`：只能随集恢复，先恢复集、再恢复这条记录，记录不降级。
    集已不在、也没有删除记录（已彻底删除）时抛 ``trash_restore_failed``，按取不出对象降级。
    """
    number = episode if isinstance(episode, int) and not isinstance(episode, bool) else None
    project = ProjectManager.for_project_dir(project_dir).load_project(project_dir.name)
    if find_episode(project, number) is not None:
        return
    record = episode_trash_record(project_dir, number) if number is not None else None
    if record is None:
        raise ConflictError("trash_restore_failed", id=identity)
    title = record.object.get("title")
    if isinstance(title, str) and title:
        raise RestoreBlocked("trash_restore_needs_episode", name=title)
    raise RestoreBlocked("trash_restore_needs_episode_untitled", episode=number)
