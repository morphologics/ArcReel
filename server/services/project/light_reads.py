"""Light reads: one asset, one episode's shot list, the project list with covers.

``GET /projects/{name}`` returns the whole project plus every episode script and every media fingerprint; a client
that only shows one asset card, one episode's board or a project lobby pays for all of it. The reads here load only
what they show:

- one asset: ``project.json`` + the artifact manifest + ``versions/versions.json``;
- one episode's shots: ``project.json`` + that episode's script + the manifest + ``versions/versions.json``, one
  ``stat`` per media file;
- the project list: per project ``project.json`` + its scripts (for the cover and the shot count), without the
  project summary projection and without walking the project directory.

Artifact status uses the same two conventions as the project summary (``lib.workflow.workflow_state``):
``registered`` (default: registered in the manifest and present; a registered but outdated artifact reads as
``current``) or ``verified`` (compared with its canonical state, so ``stale`` is reported; hashes images, slower).

轻量读取：单个资产、一集的分镜列表、带封面的项目列表。

``GET /projects/{name}`` 返回整个项目、每一集的剧本与全部媒体指纹；只显示一张资产卡、一集的画板或项目大厅的客户端
也要为这些全部买单。这里的读取只加载要显示的部分：

- 单个资产：``project.json`` + 产物清单 + ``versions/versions.json``；
- 一集的分镜：``project.json`` + 该集剧本 + 清单 + ``versions/versions.json``，每个媒体文件一次 ``stat``；
- 项目列表：每个项目的 ``project.json`` + 其剧本（用于封面与分镜计数），不跑项目摘要投影，也不遍历项目目录。

产物状态沿用项目摘要（``lib.workflow.workflow_state``）的两种口径：``registered``（默认：清单已登记且文件在场；
已登记但过期的产物报 ``current``）或 ``verified``（与规范状态比对，会报 ``stale``；要哈希图片，较慢）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from lib.artifacts.artifact_activation import ArtifactComparer, ArtifactCurrencyResolver, RegisteredArtifactResolver
from lib.artifacts.artifact_manifest import ArtifactKey, ArtifactManifestError, ArtifactStatus
from lib.artifacts.version_manager import VersionManager
from lib.infra.path_safety import try_safe_join
from lib.project.asset_types import ASSET_SPECS, DERIVATIVES_FIELD
from lib.project.project_activity import recorded_project_activity
from lib.project.project_manager import ProjectManager
from lib.project.resource_paths import CHARACTER_DERIVATIVE_RESOURCE_TYPE
from lib.script.script_models import get_generated_assets
from lib.script.script_skeleton import SKELETON_ENTITY_TYPES, SKELETONS, resolve_kind_items
from server.services.admission.asset_sheet_batch import project_asset_sheet_units
from server.services.project.project_cover import resolve_project_cover

logger = logging.getLogger(__name__)

ArtifactCurrency = Literal["registered", "verified"]
ArtifactState = Literal["current", "stale", "missing", "blocked"]

#: Asset type -> version resource type (``GET /projects/{p}/versions/{resource_type}/{id}``).
#: 资产类型 → 版本资源类型（``GET /projects/{p}/versions/{resource_type}/{id}``）。
_ASSET_VERSION_TYPES: dict[str, str] = {
    "character": "characters",
    "scene": "scenes",
    "prop": "props",
    "product": "products",
}


class AssetNotFound(LookupError):
    """The project has no asset of this type and name. / 项目里没有该类型与名字的资产。"""


class EpisodeNotFound(LookupError):
    """The project has no such episode, or the episode has no script yet. / 项目没有该集，或该集还没有剧本。"""


class MediaRef(BaseModel):
    """One project media file: its project-relative path and a ``/api/v1/files`` URL.

    ``url`` carries ``?v=<mtime_ns>`` when the file is present (immutable caching, the same version key as
    ``asset_fingerprints``); append ``&w=320`` (160 / 320 / 640 / 1280) for a WebP thumbnail. ``present`` is false
    when the path is recorded but the file is not on disk.

    一个项目媒体文件：项目内相对路径与 ``/api/v1/files`` URL。文件在场时 ``url`` 带 ``?v=<mtime_ns>``；
    追加 ``&w=320`` 取 WebP 缩略图。路径有记录但文件不在盘上时 ``present`` 为 false。
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    url: str
    present: bool
    version_key: int | None = Field(default=None, description="mtime_ns of the file (the ?v= value)")


class ArtifactRef(BaseModel):
    """One generated artifact of a stage (asset sheet, storyboard image, video, narration audio).

    一个阶段的生成产物（资产图、分镜图、视频、旁白音频）。"""

    model_config = ConfigDict(extra="forbid")

    status: ArtifactState
    file: MediaRef | None = None
    version: int | None = Field(default=None, description="current version number in the version history")
    versions: int = Field(default=0, description="number of versions in the history")


class AssetDerivativeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    sheet: ArtifactRef


class ProjectAssetResponse(BaseModel):
    """``GET /projects/{p}/assets/{type}/{name}``."""

    model_config = ConfigDict(extra="forbid")

    project: str
    asset_type: Literal["character", "scene", "prop", "product"]
    name: str
    fields: dict[str, Any] = Field(description="the stored entry without its sheet field and derivative table")
    sheet: ArtifactRef
    derivatives: list[AssetDerivativeSummary] = Field(default_factory=list)
    currency: ArtifactCurrency


class ShotPrompts(BaseModel):
    """Prompt presence (always) and the prompts themselves (only with ``include=prompts``).

    提示词是否存在（总是给出）与提示词本身（仅 ``include=prompts`` 时）。"""

    model_config = ConfigDict(extra="forbid")

    has_image_prompt: bool
    has_video_prompt: bool
    image_prompt: Any | None = None
    video_prompt: Any | None = None
    text: str | None = Field(default=None, description="reference-video units: the unit body (their only prompt)")


class EpisodeShot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    number: int = Field(description="1-based position in the episode")
    kind: str = Field(description="segment | drama_scene | shot | reference_unit")
    duration_seconds: int | None = None
    storyboard: ArtifactRef | None = Field(default=None, description="null for reference-video units")
    video: ArtifactRef
    narration: ArtifactRef | None = Field(default=None, description="only when the item has narration audio")
    thumbnail: MediaRef | None = Field(default=None, description="video first frame, else the storyboard image")
    prompts: ShotPrompts
    needs_replan: bool = False


class EpisodeShotList(BaseModel):
    """``GET /projects/{p}/episodes/{n}/shots``."""

    model_config = ConfigDict(extra="forbid")

    project: str
    episode: int
    script_file: str
    skeleton: str = Field(description="segments | scenes | shots | video_units")
    total_duration_seconds: int
    shots: list[EpisodeShot]
    currency: ArtifactCurrency


class ProjectCoverItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    content_mode: str | None = None
    generation_mode: str | None = None
    cover: str | None = Field(default=None, description="files URL of the cover (same pick as the lobby thumbnail)")
    episodes: int
    shots: int
    updated_at: str | None = Field(
        default=None, description="latest business write (project / script metadata, activity ledger), ISO UTC"
    )


class ProjectCoverList(BaseModel):
    """``GET /projects?covers=1``."""

    model_config = ConfigDict(extra="forbid")

    projects: list[ProjectCoverItem]


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _media_ref(project_name: str, project_dir: Path, rel: object) -> MediaRef | None:
    if not isinstance(rel, str) or not rel:
        return None
    url = f"/api/v1/files/{quote(project_name)}/{quote(rel.lstrip('/'), safe='/')}"
    resolved = try_safe_join(project_dir, rel, require_file=True)
    if resolved is None:
        return MediaRef(path=rel, url=url, present=False)
    try:
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError:
        return MediaRef(path=rel, url=url, present=False)
    return MediaRef(path=rel, url=f"{url}?v={mtime_ns}", present=True, version_key=mtime_ns)


def _resolver(project_dir: Path, project: Mapping[str, Any], currency: ArtifactCurrency) -> ArtifactComparer | None:
    """The manifest reader for ``currency``; ``None`` when the manifest cannot be read (every artifact: blocked).

    ``currency`` 对应的清单读取器；清单读不出时为 ``None``（每件产物：blocked）。"""
    try:
        if currency == "verified":
            return ArtifactCurrencyResolver(project_dir)
        return RegisteredArtifactResolver(project_dir, project)
    except (ArtifactManifestError, OSError, RuntimeError, TypeError, ValueError):
        logger.warning("light read: artifact manifest unreadable project=%s", project_dir.name, exc_info=True)
        return None


def _state(resolver: ArtifactComparer | None, key: ArtifactKey | None, path: object) -> ArtifactState:
    if not isinstance(path, str) or not path:
        return "missing"
    if resolver is None or key is None:
        return "blocked"
    try:
        status = resolver.compare(key, artifact_path=path).status
    except (ArtifactManifestError, OSError, RuntimeError, TypeError, ValueError):
        return "blocked"
    return cast(ArtifactState, (status or ArtifactStatus.MISSING).value)


def _artifact(
    project_name: str,
    project_dir: Path,
    resolver: ArtifactComparer | None,
    key: ArtifactKey | None,
    path: object,
    version_record: Mapping[str, Any] | None,
) -> ArtifactRef:
    versions = version_record.get("versions") if isinstance(version_record, Mapping) else None
    current = version_record.get("current_version") if isinstance(version_record, Mapping) else None
    return ArtifactRef(
        status=_state(resolver, key, path),
        file=_media_ref(project_name, project_dir, path),
        version=current if isinstance(current, int) and current > 0 else None,
        versions=len(versions) if isinstance(versions, list) else 0,
    )


def _version_records(project_dir: Path, resources: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """Version records of ``resources`` from one read of ``versions.json``; empty when it cannot be read.

    一次读取 ``versions.json`` 得到 ``resources`` 的版本记录；读不出时为空。"""
    if not resources:
        return {}
    try:
        return VersionManager(project_dir).resource_records(resources)
    except (OSError, ValueError):
        logger.warning("light read: versions.json unreadable project=%s", project_dir.name, exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# one asset
# ---------------------------------------------------------------------------


def read_project_asset(
    pm: ProjectManager,
    project_name: str,
    asset_type: Literal["character", "scene", "prop", "product"],
    name: str,
    *,
    currency: ArtifactCurrency = "registered",
) -> ProjectAssetResponse:
    """One asset with its sheet and derivative summaries. Raises ``FileNotFoundError`` (no project) or
    :class:`AssetNotFound`.

    单个资产及其资产图与衍生摘要。项目不存在抛 ``FileNotFoundError``，资产不存在抛 :class:`AssetNotFound`。"""
    spec = ASSET_SPECS[asset_type]
    project = pm.load_project(project_name)
    bucket = project.get(spec.bucket_key)
    entry = bucket.get(name) if isinstance(bucket, Mapping) else None
    if not isinstance(entry, Mapping):
        raise AssetNotFound(name)
    project_dir = pm.get_project_path(project_name)
    resolver = _resolver(project_dir, project, currency)

    units = [
        unit
        for unit in project_asset_sheet_units({spec.bucket_key: {name: entry}}, asset_type)
        if unit.name == name or unit.owner == name
    ]
    version_type = _ASSET_VERSION_TYPES[asset_type]
    resources = [
        (CHARACTER_DERIVATIVE_RESOURCE_TYPE, unit.name) if unit.is_derivative else (version_type, unit.name)
        for unit in units
    ]
    records = _version_records(project_dir, resources)

    sheet = ArtifactRef(status="missing")
    derivatives: list[AssetDerivativeSummary] = []
    for unit, (resource_type, resource_id) in zip(units, resources, strict=True):
        ref = _artifact(
            project_name,
            project_dir,
            resolver,
            unit.candidate.artifact_key,
            unit.candidate.artifact_path,
            records.get(resource_type, {}).get(resource_id),
        )
        if not unit.is_derivative:
            sheet = ref
            continue
        derivative_name = unit.name.split("/", 1)[1] if "/" in unit.name else unit.name
        derivatives.append(AssetDerivativeSummary(name=derivative_name, description=unit.description or "", sheet=ref))

    fields = {key: value for key, value in entry.items() if key not in (spec.sheet_field, DERIVATIVES_FIELD)}
    return ProjectAssetResponse(
        project=project_name,
        asset_type=asset_type,
        name=name,
        fields=fields,
        sheet=sheet,
        derivatives=derivatives,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# one episode's shots
# ---------------------------------------------------------------------------


def _has_prompt(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_prompt(item) for item in value.values())
    return False


def _episode_script_file(project: Mapping[str, Any], episode: int) -> str:
    for entry in project.get("episodes") or []:
        if isinstance(entry, Mapping) and entry.get("episode") == episode:
            script_file = entry.get("script_file")
            if isinstance(script_file, str) and script_file:
                return script_file
            break
    raise EpisodeNotFound(episode)


def read_episode_shots(
    pm: ProjectManager,
    project_name: str,
    episode: int,
    *,
    include_prompts: bool = False,
    currency: ArtifactCurrency = "registered",
) -> EpisodeShotList:
    """The episode's items in script order with per-stage artifact status. Raises ``FileNotFoundError`` (no project)
    or :class:`EpisodeNotFound` (no such episode, no script yet, or the script file is missing / unreadable).

    该集条目按剧本顺序，附各阶段产物状态。项目不存在抛 ``FileNotFoundError``；没有该集、还没有剧本、或剧本文件
    缺失 / 读不出时抛 :class:`EpisodeNotFound`。"""
    project = pm.load_project(project_name)
    script_file = _episode_script_file(project, episode)
    try:
        script = pm.load_script_readonly(project_name, script_file)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise EpisodeNotFound(episode) from exc
    if not isinstance(script, dict):
        raise EpisodeNotFound(episode)
    raw_items, id_field, kind = resolve_kind_items(script)
    items = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []

    project_dir = pm.get_project_path(project_name)
    resolver = _resolver(project_dir, project, currency)
    is_units = kind == "video_units"
    video_type = "reference_videos" if is_units else "videos"
    ids = [str(item.get(id_field) or "") for item in items]
    resources: list[tuple[str, str]] = []
    for resource_id in ids:
        if resource_id:
            resources.extend(((video_type, resource_id), ("audio", resource_id)))
            if not is_units:
                resources.append(("storyboards", resource_id))
    records = _version_records(project_dir, resources)

    def record(resource_type: str, resource_id: str) -> Mapping[str, Any] | None:
        return records.get(resource_type, {}).get(resource_id)

    shots: list[EpisodeShot] = []
    total = 0
    for number, (item, resource_id) in enumerate(zip(items, ids, strict=True), start=1):
        if not resource_id:
            continue
        assets = get_generated_assets(item)
        duration = item.get("duration_seconds")
        duration = duration if isinstance(duration, int) and not isinstance(duration, bool) else None
        total += duration or 0
        storyboard = (
            None
            if is_units
            else _artifact(
                project_name,
                project_dir,
                resolver,
                ArtifactKey.episode_storyboard(episode, resource_id),
                assets.get("storyboard_image"),
                record("storyboards", resource_id),
            )
        )
        video = _artifact(
            project_name,
            project_dir,
            resolver,
            ArtifactKey.episode_video(episode, resource_id),
            assets.get("video_clip"),
            record(video_type, resource_id),
        )
        if is_units and item.get("needs_replan") is True:
            # Same as the workflow status: a unit waiting for a replan has no usable video. / 与工作流状态一致：待重新规划的单元没有可用视频。
            video.status = "stale"
        narration_path = assets.get("narration_audio")
        narration = (
            _artifact(
                project_name,
                project_dir,
                resolver,
                ArtifactKey.episode_audio(episode, resource_id),
                narration_path,
                record("audio", resource_id),
            )
            if isinstance(narration_path, str) and narration_path
            else None
        )
        thumbnail = _media_ref(project_name, project_dir, assets.get("video_thumbnail")) or (
            storyboard.file if storyboard is not None else None
        )
        text = item.get("text") if is_units else None
        prompts = ShotPrompts(
            has_image_prompt=False if is_units else _has_prompt(item.get("image_prompt")),
            has_video_prompt=_has_prompt(text) if is_units else _has_prompt(item.get("video_prompt")),
        )
        if include_prompts:
            if is_units:
                prompts.text = text if isinstance(text, str) else None
            else:
                prompts.image_prompt = item.get("image_prompt")
                prompts.video_prompt = item.get("video_prompt")
        shots.append(
            EpisodeShot(
                id=resource_id,
                number=number,
                kind=SKELETON_ENTITY_TYPES.get(kind, kind),
                duration_seconds=duration,
                storyboard=storyboard,
                video=video,
                narration=narration,
                thumbnail=thumbnail,
                prompts=prompts,
                needs_replan=item.get("needs_replan") is True,
            )
        )
    return EpisodeShotList(
        project=project_name,
        episode=episode,
        script_file=script_file,
        skeleton=kind,
        total_duration_seconds=total,
        shots=shots,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# project list with covers
# ---------------------------------------------------------------------------


def _metadata_updated_at(doc: Mapping[str, Any]) -> datetime | None:
    metadata = doc.get("metadata")
    raw = metadata.get("updated_at") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        return None


def _count_items(script: Mapping[str, Any]) -> int:
    return sum(len(script[kind]) for kind in SKELETONS if isinstance(script.get(kind), list))


def _project_cover_item(pm: ProjectManager, name: str) -> ProjectCoverItem:
    project = pm.load_project(name)
    scripts: dict[str, dict[str, Any]] = {}
    episodes = [entry for entry in project.get("episodes") or [] if isinstance(entry, Mapping)]
    for entry in episodes:
        script_file = entry.get("script_file")
        if not isinstance(script_file, str) or not script_file:
            continue
        try:
            scripts[script_file] = pm.load_script_readonly(name, script_file)
        except (FileNotFoundError, OSError, ValueError):
            continue
    stamps = [stamp for stamp in (_metadata_updated_at(doc) for doc in (project, *scripts.values())) if stamp]
    recorded = recorded_project_activity(pm.get_project_path(name))
    if recorded is not None:
        stamps.append(recorded)
    updated = max(stamps, default=None)
    title = project.get("title")
    return ProjectCoverItem(
        name=name,
        title=title if isinstance(title, str) else "",
        content_mode=project.get("content_mode") if isinstance(project.get("content_mode"), str) else None,
        generation_mode=project.get("generation_mode") if isinstance(project.get("generation_mode"), str) else None,
        cover=resolve_project_cover(pm, name, project, preloaded_scripts=scripts),
        episodes=len(episodes),
        shots=sum(_count_items(script) for script in scripts.values() if isinstance(script, Mapping)),
        updated_at=updated.isoformat() if updated is not None else None,
    )


def list_project_covers(pm: ProjectManager) -> ProjectCoverList:
    """Every project with its cover, counts and last business write, most recently updated first.

    A project whose files cannot be read is listed with its name only, like the full list does.

    每个项目及其封面、计数与最近业务写入时间，最近更新的在前。文件读不出的项目只列名字，与完整列表一致。"""
    items: list[ProjectCoverItem] = []
    for name in pm.list_projects():
        try:
            if not pm.project_exists(name):
                continue
            items.append(_project_cover_item(pm, name))
        except Exception:
            logger.warning("light project list: cannot read project %s", name, exc_info=True)
            items.append(ProjectCoverItem(name=name, title="", episodes=0, shots=0))
    # Projects without a time go last, in listing order among themselves. / 没有时间的项目排在最后，彼此保持列举顺序。
    dated = sorted((item for item in items if item.updated_at), key=lambda item: item.updated_at or "", reverse=True)
    return ProjectCoverList(projects=[*dated, *(item for item in items if not item.updated_at)])
