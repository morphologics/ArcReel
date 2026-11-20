"""
Project file manager

Manages the video project's directory structure, shot script reads/writes, and status tracking.

项目文件管理器

管理视频项目的目录结构、分镜剧本读写、状态追踪。
"""

import asyncio
import copy
import errno
import functools
import hashlib
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import time
import unicodedata
from collections import Counter
from collections.abc import AsyncGenerator, Callable, Generator, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar, Literal, TypeGuard, cast

import portalocker
from pydantic import BaseModel, Field

from lib.agent.agent_profile import agent_profile_dir
from lib.agent.profile_manifest import (
    VALID_CONTENT_MODES,
    ContentMode,
    ProfileEmptyError,
    ProfileMisconfiguredError,
    ProfileMissingError,
    get_profile_status,
    sync_profile_to_project,
)
from lib.agent.profile_manifest import (
    force_resync_profile as _force_resync_profile,
)
from lib.artifacts.artifact_manifest import ArtifactBasisDescriptor
from lib.artifacts.formal_write import formal_write_transaction, project_metadata_lock
from lib.episode.episode_ids import raise_episode_id_high_water
from lib.episode.episode_ledger import SOURCE_TEXT_SUFFIXES
from lib.episode.episode_paths import (
    REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME,
    SCRIPT_PLAN_FILENAMES,
    episode_script_filename,
    episode_script_relpath,
)
from lib.episode.episode_sources import (
    SOURCE_ORIGIN_FIELD,
    WHOLE_SOURCE_FILES_KEY,
    SourceOrigin,
    record_source_remaining,
    source_planning_inputs,
)
from lib.episode.episode_target_duration import (
    EPISODE_TARGET_DURATION_FIELD,
    MAX_EPISODE_TARGET_DURATION,
    MIN_EPISODE_TARGET_DURATION,
    is_valid_episode_target_duration,
)
from lib.episode.source_kinds import project_overview_source_kind
from lib.infra.app_data_dir import app_data_dir
from lib.infra.async_thread import run_sync_transaction
from lib.infra.content_digest import canonical_json_digest
from lib.infra.data_root_layout import PROJECT_FILENAME, PROJECT_NAME_PATTERN, DataRootLayout, list_project_dirs
from lib.infra.json_io import atomic_write_bytes, atomic_write_json, load_json, load_json_or_none
from lib.infra.path_safety import PathTraversalError, safe_join
from lib.infra.schema_guards import is_int, is_shape, is_str
from lib.infra.validation_messages import ValidationResult, default_translate
from lib.project.asset_rename import (
    AssetDeletionPreview,
    AssetEpisodeReferences,
    AssetRenameConflictError,
    AssetRenameNotFoundError,
    AssetRenameReport,
    owns_named_file,
    plan_asset_file_renames,
    rewrite_entry_paths,
    rewrite_grid_reference_images,
    rewrite_payload_references,
)
from lib.project.asset_types import (
    ALIASES_FIELD,
    ASSET_SPECS,
    DERIVATIVES_FIELD,
    ProjectAssetNameConflictError,
    asset_name_comparison_key,
    build_asset_entry,
    ensure_project_asset_name_available,
    ensure_project_asset_namespace,
    find_project_asset_name,
    normalize_asset_bucket,
    normalize_asset_name,
    record_asset_aliases,
    rekey_equivalent_entries,
    resolve_asset_key,
    validate_asset_name,
)
from lib.project.project_change_hints import emit_project_change_hint
from lib.project.project_schema import parse_project_schema_version
from lib.project.script_entry_cleanup import trash_entries
from lib.project.task_project_claim import ensure_task_project_claim
from lib.references.reference_catalog import derivative_reference
from lib.script.draft_quarantine import QUARANTINE_FILENAMES
from lib.script.item_ids import (
    ItemIdIssuer,
    episode_item_id_high_water,
    max_item_number,
    raise_item_id_high_water,
)
from lib.script.reference_video.duration_migration import migrate_script_unit_durations
from lib.script.script_editor import ScriptEditError, resolve_items
from lib.script.script_models import get_generated_assets
from lib.speech.audio_utils import resolve_audio_ref_path, resolve_stale_reference_audio
from lib.speech.narration_config import (
    NARRATION_CONFIG_FIELDS,
    NARRATION_DELIVERY_FIELD,
    POST_PRODUCTION,
    validate_project_narration_config,
)

if TYPE_CHECKING:
    from lib.project.asset_merge import AssetMergeReport
    from lib.script.grid.models import GridGeneration

logger = logging.getLogger(__name__)

PROJECT_SLUG_SANITIZER = re.compile(r"[^a-zA-Z0-9]+")

# Generation mode (generation_mode): a required binary, set at creation and immutable afterwards (mutability is
# enforced by the PATCH model structure). Grid is not a generation mode: it is expressed by the separate
# grid_storyboard boolean and only meaningful in the `storyboard` generation mode. The legacy third value "grid" has
# been re-encoded by the v4->v5 migration as storyboard + grid_storyboard=true.
# 生成模式（generation_mode）：二值必填，创建即定、之后不可变（可变性由 PATCH 模型结构保证）。
# 宫格不是生成模式：它由独立的 grid_storyboard 布尔表达，仅 `storyboard` 生成模式有意义。
# 存量三值 "grid" 已由 v4→v5 迁移重编码为 storyboard + grid_storyboard=true。
VALID_GENERATION_MODES: frozenset[str] = frozenset({"storyboard", "reference_video"})
_DEFAULT_GENERATION_MODE = "storyboard"


class _Unset:
    """Sentinel: distinguishes "before not passed" (the unified write entry point reads the pre-change state from disk
    itself) from "explicitly passed None" (no pre-change state).

    哨兵：区分「未传 before」（写盘统一入口自行读盘取改前）与「显式传 None」（无改前）。"""


_UNSET = _Unset()


class ScriptWriteConflict(Exception):
    """A formal script changed after an optimistic-concurrency baseline was captured."""

    def __init__(self, *, expected: str | None, actual: str | None, current_content: dict[str, Any] | None):
        super().__init__(f"script changed: expected {expected}, actual {actual}")
        self.expected = expected
        self.actual = actual
        self.current_content = current_content


def _file_content_fingerprint(path: Path) -> tuple[str | None, dict[str, Any] | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return hashlib.sha256(raw).hexdigest(), None
    return canonical_json_digest(parsed), parsed if isinstance(parsed, dict) else None


def grid_storyboard_enabled(project: dict[str, Any]) -> bool:
    """Whether the project produces storyboard images via grids.

    Grid is a way of producing storyboard images within storyboard-to-video: reference-to-video has no storyboard image
    step, so even a leftover grid_storyboard=true doesn't activate the grid branch.

    项目是否按宫格生产分镜图。

    宫格是分镜图生视频内的分镜图生产方式：参考生视频无分镜图步骤，
    即使残留 grid_storyboard=true 也不激活宫格分支。
    """
    return project.get("generation_mode") == "storyboard" and bool(project.get("grid_storyboard"))


def find_episode(project: Mapping[str, Any], episode: int | None) -> dict[str, Any] | None:
    """Return the entry in project.json ``episodes[]`` with ``episode == N``, or None if missing.

    When ``episode`` is None (episode number unknown) no entry matches.

    返回 project.json ``episodes[]`` 中 ``episode == N`` 的条目，缺失则 None。

    ``episode`` 为 None（集号未知）时不匹配任何条目。
    """
    if episode is None:
        return None
    for ep in project.get("episodes") or []:
        if isinstance(ep, dict) and ep.get("episode") == episode:
            return ep
    return None


def resolve_episode_script_binding(
    project: Mapping[str, Any],
    episode: int,
    expected_script_file: str,
    *,
    require_indexed: bool = False,
) -> str | None:
    """Return the live binding when it still denotes the expected episode script.

    Legacy projects without an ``episodes`` index use the submitted script as
    their binding unless ``require_indexed`` is true. Indexed projects must
    retain a matching normalized binding; a missing episode or a concurrent
    rebind returns ``None``.
    """

    entry = find_episode(project, episode)
    current_binding = entry.get("script_file") if isinstance(entry, dict) else None
    if current_binding is None and not require_indexed and not (project.get("episodes") or []):
        return expected_script_file
    if isinstance(current_binding, str) and (
        ProjectManager.normalize_script_filename(current_binding)
        == ProjectManager.normalize_script_filename(expected_script_file)
    ):
        return current_binding
    return None


def is_episode_number(value: object) -> TypeGuard[int]:
    """Whether this value is an episode number.

    Episode numbers are positive integers. Both project.json and script JSON may have been read raw without structural
    validation: ``True`` is both an ``int`` and equal to ``1``, while ``0`` and negatives correspond to no episode.
    Checking by ``isinstance`` or only by magnitude would let these values masquerade as episode numbers all the way
    into the ledger. This is the only episode-number criterion, shared by ledger reads, write validation and episode
    metadata sync.

    这个值是不是一个集号。

    集号是正整数。project.json 与剧本 JSON 都可能是裸读进来的，没过结构校验：``True`` 既是
    ``int`` 又等于 ``1``，``0`` 与负数则不对应任何一集。按 ``isinstance`` 或只比大小都会让这些
    值一路冒充集号写进账本。集号的判据只此一处，读账本、写盘校验与集元数据同步共用。
    """

    return type(value) is int and value >= 1


def is_reference_video_project(project: Mapping[str, Any]) -> bool:
    """Whether the project uses reference-to-video.

    project.json's ``generation_mode`` is the single source of truth for this decision: the mode is set at creation
    and immutable afterwards, and the whole project generates in the same generation mode; ad/short script skeletons
    don't carry a script-level ``generation_mode`` stamp either (see ``script_generator``), so reference-to-video can't
    be determined from the script alone.

    项目是否使用参考生视频。

    project.json 的 ``generation_mode`` 是该判定的唯一真相源：模式创建即定、之后不可变，
    整个项目按同一生成模式生成；广告/短片的剧本骨架也不携带剧本级 ``generation_mode`` 戳
    （见 ``script_generator``），只看剧本判不出参考生视频。
    """
    return project.get("generation_mode") == "reference_video"


def _resolve_items_or_warn(script: dict, *, script_filename: str | None = None) -> list[Any]:
    """Dirty-data degradation on the read path: distinguish the three script structures via `resolve_items`; on dirty data
    (key present but value not a list) log a warning + return [].

    Sharing `resolve_items` with the write path (`update_scene_asset` / `batch_update_scene_assets`) keeps read and
    write discrimination of the three script structures consistent, so reference-to-video doesn't silently fall into
    the drama fallback and return []. The write side should fail loudly (let ScriptEditError propagate, the worker
    fails explicitly, the upper API returns 5xx signaling data corruption); the read side returns [] on dirty data so
    UI rendering isn't blocked, but the warning gives ops an observable signal to fix it manually, so the degradation
    doesn't become invisible.

    读取路径的脏数据降级：基于 `resolve_items` 判别三种剧本结构，
    脏数据（键存在但值非 list）下 log warning + 返回 []。

    与写入路径（`update_scene_asset` / `batch_update_scene_assets`）共用 `resolve_items` 判别
    保证三种剧本结构的读写判别一致，避免参考生视频静默落到 drama 兜底返回 []。
    写入侧应该 fail-loud（让 ScriptEditError 上冒，worker 显式失败，
    上层 API 5xx 告知数据损坏）；读取侧在脏数据下返回 [] 不阻塞 UI 渲染，但 warning 给运维
    可观测信号去人工修复，不让降级变隐形。
    """
    try:
        items, _id_field, _kind = resolve_items(script)
        return items
    except ScriptEditError as e:
        logger.warning(
            "剧本 %s 数据损坏（%s），读取降级为空列表——请人工修复",
            script_filename or "<unknown>",
            e,
        )
        return []


class EpisodeScriptReboundError(RuntimeError):
    """The episode->script_file binding changed between before and after locking (concurrent PATCH rebinding); the caller
    should retry.

    加锁前后 episode→script_file 绑定发生变化（并发 PATCH 改绑），调用方应重试。"""


class EmptySourceError(ValueError):
    """The source directory is empty, so no overview can be generated; distinguished from configuration errors like "no
    usable text provider", so the routing layer doesn't misjudge it as a user action.

    source 目录为空，无法生成概述；与「无可用文本供应商」等配置错误区分，避免路由层误判用户操作。"""


# ==================== Data models ==================== / ==================== 数据模型 ====================


class ProjectOverview(BaseModel):
    """项目概述数据模型，用于 Gemini Structured Outputs"""

    synopsis: str = Field(description="故事梗概，200-300字，概括主线剧情")
    genre: str = Field(description="题材类型，如：古装宫斗、现代悬疑、玄幻修仙")
    theme: str = Field(description="核心主题，如：复仇与救赎、成长与蜕变")
    world_setting: str = Field(description="时代背景和世界观设定，100-200字")
    # language is an archived field of the LLM output; the single source of truth is the top-level
    # project["source_language"], written in sync by generate_overview at write time. All measuring/splitting callers
    # read the top-level field; don't read overview.language directly.
    # language 是 LLM 输出存档字段；唯一真相源是顶层 project["source_language"]，
    # 由 generate_overview 在落盘时同步写入。所有度量/切分调用方一律读顶层字段，
    # 不要直接读 overview.language。
    language: Literal["zh", "en", "vi", "ru"] = Field(description="小说源语言代码")


def _rename_agnostic_errors(
    result: ValidationResult, old_name: str, new_name: str
) -> dict[tuple[str, tuple[tuple[str, str], ...]], str]:
    """Compress validation errors into fingerprints independent of "the identity being renamed", mapped to readable text.

    The "no worse" criterion of an asset rename compares the error sets before and after the rewrite, and many
    validation messages name the asset (data format errors, invalid field types, etc.), so the asset name appears in
    parameter slots. Taking a set difference of rendered text directly would treat a pre-existing legacy error as new
    after the rewrite -- the name changed, so the text changed -- and refuse a rename that isn't actually worse.
    Fingerprints are built from the structured message (key + params), folding the new name in params back to the old
    name.

    Folding only recognizes two definite forms: a param value that is exactly the new name, and the bracketed segment
    in field paths like ``characters[newname].xxx``. No arbitrary substring replacement -- if the new name happens to be
    a substring of unrelated text, a generalized replacement would fold a genuinely new error onto an existing
    fingerprint and silently swallow it. If the narrowing misses some embedded-name form, the failure direction is
    conservative: a legacy error is treated as new, and the rename is refused rather than let through.

    把校验错误压成与「被改名的那个身份」无关的指纹，映射到可读文本。

    资产改名的「不更坏」判据是比对改写前后的错误集合，而不少校验消息会点名是哪个资产
    （数据格式错误、字段类型非法等），参数位上因此带着资产名。按渲染文本直接做集合差，
    会把一条原就存在的历史遗留错误当成改写后新增的——名字变了，文本就变了——从而拒绝
    一次本不更坏的改名。指纹按结构化消息（key + params）构造，并把参数里的新名折回旧名。

    折叠只认两种确定形态：参数值整体就是新名，以及 ``characters[新名].xxx`` 这类字段路径里
    的方括号段。不做任意子串替换——新名若恰是某段无关文本的子串，泛化替换会把一条真正新增
    的错误折到已有指纹上、被静默吞掉。收窄后若漏认某种嵌名形态，失败方向是保守的：历史遗留
    错误被当作新增，改名被拒而非被放行。
    """

    def fold(value: str) -> str:
        if normalize_asset_name(value) == normalized_new:
            return old_name
        return value.replace(f"[{new_name}]", f"[{old_name}]")

    normalized_new = normalize_asset_name(new_name)
    folded: dict[tuple[str, tuple[tuple[str, str], ...]], str] = {}
    for message in result.error_messages:
        params = tuple(
            sorted(
                (name, fold(value) if isinstance(value, str) else repr(value)) for name, value in message.params.items()
            )
        )
        folded[(message.key, params)] = message.render(default_translate)
    return folded


#: Rewrite target when the deletion preview borrows the rename scan for counting: no asset name will ever equal it, so
#: all hit references are counted.
#: 删除预览借重命名扫描计数时的改写目标：任何资产名都不会与它判等，命中的引用一律计入。
_DELETION_SCAN_TARGET = "\x00"


@dataclass
class _AssetReferenceScan:
    """Result of one asset reference scan: the rewritten scripts and drafts, and reference counts attributed per episode.

    一次资产引用扫描的结果：改写后的剧本与草稿，以及按集归属的引用处数。"""

    #: ``(script file name, rewritten payload, pre-rewrite payload)`` / ``(剧本文件名, 改写后载荷, 改写前载荷)``
    scripts: list[tuple[str, dict, dict]] = field(default_factory=list)
    #: ``(draft path, rewritten payload, pre-rewrite fingerprint)`` / ``(草稿路径, 改写后载荷, 改写前指纹)``
    drafts: list[tuple[Path, dict, str]] = field(default_factory=list)
    #: Episode directory name (``episode_N``) -> reference count; scripts use the file stem, drafts the containing
    #: directory name.
    #: 集目录名（``episode_N``）→ 引用处数；剧本取文件 stem，草稿取所在目录名。
    episode_references: Counter[str] = field(default_factory=Counter)

    @property
    def references(self) -> int:
        return self.episode_references.total()


class ProjectManager:
    """Video project manager / 视频项目管理器"""

    # Project subdirectory structure / 项目子目录结构
    SUBDIRS: ClassVar[list[str]] = [
        "source",
        "scripts",
        "drafts",
        "characters",
        "scenes",
        "props",
        "products",
        "storyboards",
        "videos",
        "audio",
        "subtitles",
        "presentations",
        "thumbnails",
        "output",
        "grids",
    ]

    # Project metadata file name / 项目元数据文件名
    PROJECT_FILE = PROJECT_FILENAME

    @staticmethod
    def normalize_project_name(name: str) -> str:
        """Validate and normalize a project identifier."""
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Project identifier must not be empty")
        if not PROJECT_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("Project identifier may only contain English letters, digits and hyphens")
        return normalized

    @staticmethod
    def _slugify_project_title(title: str) -> str:
        """Build a filesystem-safe slug prefix from the project title.

        A CJK title loses all its Han characters after NFKD + ascii ignore; if the result contains no letters (empty /
        only stray digits from the title, e.g. "第1集" -> "1"), fall back to the neutral prefix ``proj``, avoiding a slug
        like ``1-<hex>`` that looks meaningful but is misleading.

        Note that truncation must happen before the letter check: something like ``"1-2345...23abc"`` (>24 chars, letters
        at the end) leaves only digits after cutting to 24, and such a result should likewise collapse to ``proj``.

        CJK 标题经 NFKD + ascii ignore 后会丢光汉字;若结果不包含任何字母
        （纯空 / 仅夹在标题里的孤立数字,如「第1集」→ "1"),退化为中性
        前缀 ``proj``,避免产生 ``1-<hex>`` 这种看似有意义实则误导的 slug。

        注意 truncate 必须在 letter 校验之前:像 ``"1-2345...23abc"``(>24 字符,
        字母在尾部) 截前 24 后会只剩数字,这种结果同样应塌成 ``proj``。
        """
        ascii_text = unicodedata.normalize("NFKD", str(title).strip()).encode("ascii", "ignore").decode("ascii")
        slug = PROJECT_SLUG_SANITIZER.sub("-", ascii_text).strip("-_").lower()[:24]
        if not slug or not any(c.isalpha() for c in slug):
            return "proj"
        return slug

    def generate_project_name(self, title: str | None = None) -> str:
        """Generate a unique internal project identifier."""
        prefix = self._slugify_project_title(title or "")
        while True:
            candidate = f"{prefix}-{secrets.token_hex(4)}"
            if not (self.projects_dir / candidate).exists():
                return candidate

    @classmethod
    def for_project_dir(cls, project_dir: str | Path) -> "ProjectManager":
        """Build the ProjectManager for the data root containing a project directory (the data root is derived via the layout
        module).

        由一个项目目录构造其所在数据根的 ProjectManager（数据根经布局模块推导）。"""
        return cls(DataRootLayout.for_project_dir(Path(project_dir)).root)

    def __init__(
        self,
        data_root: str | Path,
        *,
        script_reader: Callable[[Path], dict] | None = None,
        script_writer: Callable[[Path, dict], None] | None = None,
    ):
        """
        Initialize the project manager

        Args:
            data_root: data root; locations such as the projects directory come from the data-root layout
            script_reader: script JSON read seam; reads from the file system by default.
            script_writer: script JSON atomic write seam; uses atomic_write_json by default.

        初始化项目管理器

        Args:
            data_root: 数据根；项目目录等位置由数据根布局给出
            script_reader: 剧本 JSON 读取 seam；缺省时从文件系统读取。
            script_writer: 剧本 JSON 原子写入 seam；缺省时使用 atomic_write_json。
        """
        self.layout = DataRootLayout(Path(data_root))
        self.data_root = self.layout.root
        self.projects_dir = self.layout.projects_dir
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._script_reader = script_reader
        self._script_writer = script_writer

    def list_projects(self) -> list[str]:
        """List all projects (criteria in ``list_project_dirs``). / 列出所有项目（判定见 ``list_project_dirs``）。"""
        return [d.name for d in list_project_dirs(self.projects_dir)]

    def get_global_assets_root(self) -> Path:
        """Return the global assets root directory, ensuring the character/scene/prop subdirectories exist.

        返回全局资产根目录，并确保 character/scene/prop 子目录存在。"""
        root = self.layout.global_assets_dir
        root.mkdir(parents=True, exist_ok=True)
        for sub in ("character", "scene", "prop"):
            (root / sub).mkdir(exist_ok=True)
        return root

    def create_project(
        self,
        name: str,
        content_mode: ContentMode = "narration",
        *,
        publish: bool = True,
    ) -> Path:
        """
        Create a new project

        Args:
            name: project identifier (globally unique, used in URLs and the file system)
            content_mode: content mode (narration / drama), determines which variant profile materialization picks
            publish: whether to write a minimal project.json immediately; a composite creation flow should publish
                after the full metadata is ready

        Returns:
            project directory path

        创建新项目

        Args:
            name: 项目标识（全局唯一，用于 URL 和文件系统）
            content_mode: 创作类型（narration / drama），影响 profile 物化时选哪份变体
            publish: 是否立即写入最小 project.json；组合创建流程应在完整元数据就绪后再发布

        Returns:
            项目目录路径
        """
        name = self.normalize_project_name(name)
        project_dir = self.projects_dir / name

        try:
            project_dir.mkdir()
        except FileExistsError as exc:
            raise FileExistsError(f"Project '{name}' already exists") from exc

        # A single-step call persists content_mode by default; a composite creation flow can defer to publishing the full
        # metadata at once.
        # 单步调用默认持久化 content_mode；组合创建流程可延迟到完整 metadata 一次发布。
        try:
            for subdir in self.SUBDIRS:
                (project_dir / subdir).mkdir(exist_ok=True)
            if publish:
                atomic_write_json(project_dir / self.PROJECT_FILE, {"content_mode": content_mode})
            self.sync_agent_profile(project_dir, content_mode=content_mode)
        except Exception:
            # Roll back project_dir when sync fails, so a half-built directory doesn't block retries (a same-name create would
            # hit FileExistsError)
            # sync 失败时回滚 project_dir，避免残缺目录阻塞重试（同名 create 撞 FileExistsError）
            # remove the just-created project directory when project creation fails
            shutil.rmtree(project_dir, ignore_errors=True)  # hard-delete: rollback -- 创建项目失败时移除刚建的项目目录
            raise

        return project_dir

    # A _project_lock triggered by a concurrent reader (e.g. ProjectEventService's polling scan) may re-touch the lock
    # file after rmtree has emptied the directory contents but before removing the directory itself: on POSIX this shows
    # up as rmdir failing because the directory isn't empty (ENOTEMPTY); on Windows the lock file is still held by the
    # peer process's file lock, showing up as access denied (PermissionError / EACCES). Both are platform-specific
    # symptoms of the same race; retrying lets the lock file be deleted in the next cleanup round.
    # 并发读者（如 ProjectEventService 的轮询扫描）触发的 _project_lock 可能在
    # rmtree 清空目录内容之后、移除目录本身之前重新 touch 出锁文件：POSIX 上表现为
    # rmdir 因目录非空失败（ENOTEMPTY），Windows 上锁文件仍被对端进程持有的文件锁
    # 占用，表现为访问被拒（PermissionError / EACCES）。两者都是同一竞态的平台
    # 特定症状，重试让锁文件在下一轮清理中一并删除。
    _DELETE_RETRYABLE_ERRNOS = (errno.ENOTEMPTY, errno.EACCES)

    def delete_project_directory(self, name: str) -> None:
        """Hard-delete a project directory, only to undo a just-created project; tolerates transient errors from races with
        concurrent scans and deletions.

        Creator project deletion goes through deleted projects (``lib.project.deleted_projects``), not here.

        硬删除项目目录，只用于撤销刚建出的项目；容忍并发扫描与删除操作竞态产生的临时性错误。

        创作者删除项目走已删除项目（``lib.project.deleted_projects``），不经这里。
        """
        project_dir = self.get_project_path(name)
        attempts = 5
        for attempt in range(attempts):
            try:
                shutil.rmtree(project_dir)  # hard-delete: rollback -- undo a project whose creation failed / 撤销创建失败的项目
                return
            except FileNotFoundError:
                # The directory no longer exists -- a previous retry already succeeded, or another concurrent deletion completed;
                # the goal of deletion is reached, no need to keep retrying or report an error. If the directory is still there, a
                # concurrent residue cleanup (remove_project_directory_residue) deleted its empty directories first; retry.
                # 目录已不存在——上一次重试已经成功,或并发的另一次删除已经完成,
                # 删除目的已达成,无需继续重试或报错。目录还在时，是并发的残余目录清理
                # （remove_project_directory_residue）先删掉了其中的空目录，接着重试。
                if not project_dir.exists():
                    return
                if attempt == attempts - 1:
                    raise
            except OSError as exc:
                if exc.errno not in self._DELETE_RETRYABLE_ERRNOS or attempt == attempts - 1:
                    raise
                time.sleep(0.05)

    def remove_project_directory_residue(self, name: str) -> None:
        """Delete the residue of a same-name project directory that holds only empty directories.

        Tasks still running after a project was deleted may recreate empty subdirectories under the old name via
        ``mkdir(parents=True)``. If the directory has a project.json, leave it alone -- that's a newly created or imported
        project of the same name; only empty directories are deleted, and every file is kept as-is.

        删除同名项目目录里只剩空目录的残余。

        项目删除后仍在执行的任务，可能经 ``mkdir(parents=True)`` 按旧名补建出空的子目录。目录下
        有 project.json 时不动，那是同名新建或导入的项目；只删空目录，任何文件都原样保留。
        """
        project_dir = safe_join(self.projects_dir, self.normalize_project_name(name))
        if (project_dir / self.PROJECT_FILE).exists():
            return
        for directory, _subdirs, _files in os.walk(project_dir, topdown=False):
            try:
                os.rmdir(directory)  # hard-delete: infra -- empty directories recreated under a deleted project's name / 已删除项目同名下被重新建出的空目录
            except OSError:
                continue

    def sync_agent_profile(
        self,
        project_dir: Path,
        *,
        content_mode: ContentMode | None = None,
    ) -> dict:
        """Materialize agent_runtime_profile into the project directory's .claude / CLAUDE.md.

        When ``content_mode=None`` it is read from ``project_dir/project.json``; if project.json or the ``content_mode``
        field is missing -> fall back to ``"narration"`` + log info. An explicit invalid ``content_mode`` value -> raise
        ``ValueError``.

        See ``lib.agent.profile_manifest.sync_profile_to_project`` for details: the manifest-driven materialization flow
        uses sha256 to distinguish built-in skill upgrades (propagated automatically) / user modifications (kept) / user
        deletions (not resurrected); when the profile deletes upstream, unmodified copies in the project are removed; a
        15-row decision table fully covers naming collisions / state-machine backflow, etc.

        Returns:
            dict with backward-compatible ``created/repaired/skipped/errors`` + finer-grained stat keys

        物化 agent_runtime_profile 到项目目录的 .claude / CLAUDE.md。

        ``content_mode=None`` 时从 ``project_dir/project.json`` 读取；
        project.json 缺失或 ``content_mode`` 字段缺失 → 回退到 ``"narration"`` + log info。
        ``content_mode`` 显式非法值 → 抛 ``ValueError``。

        详见 ``lib.agent.profile_manifest.sync_profile_to_project``：manifest-driven
        物化流程用 sha256 区分内置 skill 升级（自动传播）/ 用户修改（保留）/ 用户主动
        删除（不复活）；profile 上游删除时移除项目内未改副本；命名碰撞 /
        状态机回流等 15 行决策表完整覆盖。

        Returns:
            含向后兼容 ``created/repaired/skipped/errors`` + 细分 stat key 的字典
        """
        if content_mode is None:
            content_mode = self._resolve_content_mode(project_dir)
        profile_dir = agent_profile_dir()
        return sync_profile_to_project(profile_dir, project_dir, content_mode)

    def force_resync_profile(
        self,
        project_dir: Path,
        *,
        paths: list[str] | None = None,
        content_mode: ContentMode | None = None,
    ) -> dict:
        """Force-overwrite the corresponding project files from the profile and refresh the manifest.

        Used for explicitly triggered cases such as the UI "restore built-in skill" button. ``paths=None`` means all; if a
        file in the given paths has been deleted from the profile, it is skipped + log warn (not counted as an error).

        When ``content_mode=None`` it has the same semantics as ``sync_agent_profile``, resolved automatically from
        project.json.

        强制按 profile 覆盖项目内对应文件并刷新 manifest。

        用于 UI"恢复内置 skill"按钮等显式触发的场景。``paths=None`` 表示全量；
        指定 paths 中若某文件 profile 已删，会 skip + log warn（不算 error）。

        ``content_mode=None`` 时与 ``sync_agent_profile`` 同语义，自动从 project.json 解析。
        """
        if content_mode is None:
            content_mode = self._resolve_content_mode(project_dir)
        profile_dir = agent_profile_dir()
        return _force_resync_profile(profile_dir, project_dir, content_mode, paths=paths)

    def get_agent_profile_status(self, project_dir: Path) -> dict[str, object]:
        """Describe project-local Agent Profile customizations for settings UI."""
        content_mode = self._resolve_content_mode(project_dir)
        return get_profile_status(agent_profile_dir(), project_dir, content_mode)

    def _resolve_content_mode(self, project_dir: Path) -> ContentMode:
        """Read content_mode from project_dir/project.json; fall back to narration when missing.

        ``project.json`` missing or lacking the ``content_mode`` field -> fall back to narration (compatible with old
        projects). File present but read/parse fails -> raise, so the upper sync_all_agent_profiles takes the
        failed_projects branch; silently falling back to narration would make a drama project trigger a destructive reset
        due to the mode mismatch recorded in the manifest, wrongly switching the profile back to the narration variant.

        从 project_dir/project.json 读 content_mode；缺失回退 narration。

        ``project.json`` 不存在或缺 ``content_mode`` 字段 → 回退 narration（兼容
        老项目）。文件存在但读取/解析失败 → raise，让上层 sync_all_agent_profiles
        走 failed_projects 分支；若静默回退到 narration，drama 项目会因 manifest
        记录的 mode 不匹配触发破坏性 reset，把 profile 错误切回旁白/解说变体。
        """
        pj_path = project_dir / self.PROJECT_FILE
        try:
            data = load_json(pj_path)
        except FileNotFoundError:
            logger.info("project.json missing under %s, defaulting content_mode=narration", project_dir)
            return "narration"
        mode = data.get("content_mode") if isinstance(data, dict) else None
        if mode is None:
            logger.info("project.json has no content_mode under %s, defaulting narration", project_dir)
            return "narration"
        if not isinstance(mode, str) or mode not in VALID_CONTENT_MODES:
            raise ValueError(
                f"project {project_dir.name}: invalid content_mode={mode!r} "
                f"(must be one of {sorted(VALID_CONTENT_MODES)})"
            )
        return cast(ContentMode, mode)

    def sync_all_agent_profiles(self) -> dict:
        """Scan all project directories and materialize agent_runtime_profile (for the startup hook).

        Per-project failure isolation: catch ordinary exceptions and continue with the next project (``failed_projects``
        count). ``ProfileMissingError`` / ``ProfileEmptyError`` are deployment-level errors; skip everything and set
        ``aborted=True``, to avoid silently emptying every project's .claude.

        Returns:
            includes backward-compatible ``created/repaired/skipped/errors`` + finer-grained stats + fallback
            ``failed_projects`` / ``aborted`` fields

        扫描所有项目目录，物化 agent_runtime_profile（启动 hook 用）。

        单项目失败隔离：捕获普通异常后继续下一项目（``failed_projects`` 计数）。
        ``ProfileMissingError`` / ``ProfileEmptyError`` 是部署级错误，全部跳过
        并设 ``aborted=True``，避免静默把所有项目的 .claude 删空。

        Returns:
            含向后兼容 ``created/repaired/skipped/errors`` + 细分 stat + 兜底
            ``failed_projects`` / ``aborted`` 字段
        """
        totals = {
            "created": 0,
            "repaired": 0,
            "skipped": 0,
            "errors": 0,
            "failed_projects": 0,
            "aborted": False,
        }
        _STAT_KEYS_TO_AGGREGATE = (
            "created",
            "repaired",
            "skipped",
            "errors",
            "upgraded",
            "user_modified",
            "user_only",
            "pruned",
            "orphaned",
            "deleted_user",
            "tombstoned",
            "unchanged",
            "collision",
            "migrated_total",
        )
        for project_dir in list_project_dirs(self.projects_dir):
            try:
                result = self.sync_agent_profile(project_dir)
                for key in _STAT_KEYS_TO_AGGREGATE:
                    if key in result:
                        totals[key] = totals.get(key, 0) + result[key]
            except (ProfileMissingError, ProfileEmptyError, ProfileMisconfiguredError) as e:
                # Deployment-level errors (wrong profile path / volume mount failure) -> skip everything; don't fall back to the
                # destructive behavior of "pretending the profile is empty"
                # 部署级错误（profile 路径错 / volume 挂载失败）→ 全部跳过，
                # 不要 fallback 到"假装 profile 是空"的破坏行为
                logger.error("profile sync ABORTED for ALL projects: %s", e)
                totals["aborted"] = True
                break
            except ValueError as e:
                # A single project's invalid content_mode -> skip it without affecting other projects / 单个项目 content_mode 非法 → 跳过，不影响其它项目
                logger.warning("Skip sync for %s: %s", project_dir.name, e)
                totals["failed_projects"] += 1
            except Exception:
                logger.exception("profile sync failed for %s", project_dir.name)
                totals["failed_projects"] += 1
        return totals

    def get_project_path(self, name: str) -> Path:
        """Get the project path (with path-traversal protection).

        Within a running task's context, raise ``ProjectDeletedDuringTaskError`` when the project was deleted during task
        execution: a same-name directory may by now belong to another newly created or imported project.

        获取项目路径（含路径遍历防护）。

        在执行任务的上下文里，项目已在任务执行期间删除时抛 ``ProjectDeletedDuringTaskError``：
        同名目录此时可能已属于新建或导入的另一个项目。
        """
        name = self.normalize_project_name(name)
        ensure_task_project_claim(name)
        try:
            project_dir = safe_join(self.projects_dir, name)
        except PathTraversalError as exc:
            raise ValueError(f"Invalid project name: '{name}'") from exc
        if not project_dir.exists():
            raise FileNotFoundError(f"Project '{name}' does not exist")
        return project_dir

    @staticmethod
    def _safe_subpath(base_dir: Path, filename: str) -> str:
        """Check that filename joined to base_dir doesn't escape it; return the normalized absolute path string.

        校验 filename 拼接后不逃出 base_dir，返回规范化的绝对路径字符串。"""
        try:
            return str(safe_join(base_dir, filename))
        except PathTraversalError as exc:
            raise ValueError(f"Invalid file name: '{filename}'") from exc

    def get_project_status(self, name: str) -> dict[str, Any]:
        """
        Get the project status

        Returns:
            dict with the completion status of each stage

        获取项目状态

        Returns:
            包含各阶段完成情况的字典
        """
        project_dir = self.get_project_path(name)

        status = {
            "name": name,
            "path": str(project_dir),
            "source_files": [],
            "scripts": [],
            "characters": [],
            "scenes": [],
            "props": [],
            "products": [],
            "storyboards": [],
            "videos": [],
            "outputs": [],
            "current_stage": "empty",
        }

        # Check the contents of each directory / 检查各目录内容
        for subdir in self.SUBDIRS:
            subdir_path = project_dir / subdir
            if subdir_path.exists():
                files = list(subdir_path.glob("*"))
                if subdir == "source":
                    status["source_files"] = [f.name for f in files if f.is_file()]
                elif subdir == "scripts":
                    status["scripts"] = [f.name for f in files if f.suffix == ".json"]
                elif subdir == "characters":
                    status["characters"] = [f.name for f in files if f.suffix in [".png", ".jpg", ".jpeg"]]
                elif subdir == "scenes":
                    status["scenes"] = [f.name for f in files if f.suffix in [".png", ".jpg", ".jpeg"]]
                elif subdir == "props":
                    status["props"] = [f.name for f in files if f.suffix in [".png", ".jpg", ".jpeg"]]
                elif subdir == "products":
                    status["products"] = [f.name for f in files if f.suffix in [".png", ".jpg", ".jpeg"]]
                elif subdir == "storyboards":
                    status["storyboards"] = [f.name for f in files if f.suffix in [".png", ".jpg", ".jpeg"]]
                elif subdir == "videos":
                    status["videos"] = [f.name for f in files if f.suffix in [".mp4", ".webm"]]
                elif subdir == "output":
                    status["outputs"] = [f.name for f in files if f.suffix in [".mp4", ".webm"]]

        # Determine the current stage / 确定当前阶段
        if status["outputs"]:
            status["current_stage"] = "completed"
        elif status["videos"]:
            status["current_stage"] = "videos_generated"
        elif status["storyboards"]:
            status["current_stage"] = "storyboards_generated"
        elif status["characters"]:
            status["current_stage"] = "characters_generated"
        elif status["scripts"]:
            status["current_stage"] = "script_created"
        elif status["source_files"]:
            status["current_stage"] = "source_ready"
        else:
            status["current_stage"] = "empty"

        return status

    # ==================== Shot script operations ==================== / ==================== 分镜剧本操作 ====================

    def save_script(
        self,
        project_name: str,
        script: dict,
        filename: str,
        *,
        validate: bool = True,
        artifact_basis: ArtifactBasisDescriptor | None = None,
        expected_fingerprint: str | _Unset | None = _UNSET,
        project_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        """
        Save a shot script

        Args:
            project_name: project name
            script: script dict
            filename: script file name; only the canonical name `episode_N.json` is accepted (with optional `scripts/`
                prefix); other file names are refused by the write consistency check
            validate: whether to run the "no worse" structural validation (default True, fail-safe). A direct save
                holds no pre-change script; the unified write entry point reads the pre-change state from disk as needed
                (if it exists: no worse; for a brand-new save: strict validation).
            artifact_basis: script source basis frozen before the generation call started; ordinary edits don't pass
                it and it is resolved from current values at commit time.
            expected_fingerprint: optional baseline of the final script content; the write is refused if it doesn't
                match under the script lock.
            project_update: extra changes to project.json within the same write transaction as the script and episode
                index; only for scripts with an episode number.

        Returns:
            saved file path

        保存分镜剧本

        Args:
            project_name: 项目名称
            script: 剧本字典
            filename: 剧本文件名；只接受规范名 `episode_N.json`（可带 `scripts/` 前缀），
                其余文件名在写盘一致性校验处拒绝
            validate: 是否做「不更坏」结构校验（默认 True，fail-safe）。直连保存不持有
                改前剧本，由写盘统一入口按需读盘取改前（已存在则不更坏，全新保存则严格校验）。
            artifact_basis: 生成调用开始前冻结的剧本来源 basis；普通编辑不传，按提交时现值解析。
            expected_fingerprint: 可选的正式剧本内容基线；在剧本锁内不匹配时拒绝写入。
            project_update: 与剧本、集索引同一写事务内对 project.json 的额外修改；仅带集号的剧本可用。

        Returns:
            保存的文件路径
        """
        filename = self.normalize_script_filename(filename)
        episode = script.get("episode")

        with self._script_lock(project_name, filename):
            if not isinstance(expected_fingerprint, _Unset):
                script_path = Path(self._safe_subpath(self.get_project_path(project_name) / "scripts", filename))
                actual_fingerprint, current_content = _file_content_fingerprint(script_path)
                if actual_fingerprint != expected_fingerprint:
                    raise ScriptWriteConflict(
                        expected=expected_fingerprint,
                        actual=actual_fingerprint,
                        current_content=current_content,
                    )
            prepare_on_commit: Callable[[], Callable[[Path], None] | None] | None = None
            before_script: dict | _Unset | None = _UNSET
            if type(episode) is int and episode > 0 and self.project_exists(project_name):
                from lib.artifacts.artifact_activation import prepare_episode_script_manifest_commit

                project_path = self.get_project_path(project_name)
                project_file = self._get_project_file_path(project_name)
                script_path = Path(self._safe_subpath(project_path / "scripts", filename))
                before_script = self._load_script_or_none(script_path)
                items, id_field, script_kind = resolve_items(script)
                resource_ids = tuple(
                    resource_id
                    for item in items
                    if isinstance(item, Mapping)
                    and isinstance((resource_id := item.get(id_field)), str)
                    and resource_id
                )
                previous_resource_ids: tuple[str, ...] = ()
                previous_kind = script_kind
                if before_script is not None:
                    try:
                        previous_items, previous_id_field, previous_kind = resolve_items(before_script)
                    except ScriptEditError:
                        pass
                    else:
                        previous_resource_ids = tuple(
                            resource_id
                            for item in previous_items
                            if isinstance(item, Mapping)
                            and isinstance((resource_id := item.get(previous_id_field)), str)
                            and resource_id
                        )

                def _prepare_manifest_commit() -> Callable[[Path], None] | None:
                    manifest_commit = prepare_episode_script_manifest_commit(
                        project_path,
                        episode=episode,
                        artifact_path=f"scripts/{filename}",
                        resource_ids=resource_ids,
                        removed_resource_ids=tuple(set(previous_resource_ids) - set(resource_ids)),
                        basis=artifact_basis,
                    )
                    # Within the project lock, before writing: the deletion record's snapshot needs the pre-rewrite script and
                    # project.json.
                    # 项目锁内、写入之前：删除记录的快照要改写前的剧本与 project.json。
                    before_files = {
                        rel: path.read_bytes()
                        for rel, path in ((f"scripts/{filename}", script_path), (self.PROJECT_FILE, project_file))
                        if path.is_file()
                    }

                    def _register_manifest(_script_path: Path) -> None:
                        # Removed old entries move into the project trash, then are unregistered; the script lock is still held, so the new
                        # entries' media can't be written first. On overwrite, entries in the old script all get records as structural
                        # removals (ADR 0080), so even those without media can be recovered.
                        # 被移除的旧条目移进项目回收站，再撤登记；仍持剧本锁，新条目的媒体不能先落盘。
                        # 覆盖时旧剧本里的条目都按结构移除建记录（ADR 0080），没有媒体的也能找回。
                        trash_entries(
                            project_path,
                            episode=episode,
                            script_file=filename,
                            entries=dict.fromkeys(
                                (rid for rid in previous_resource_ids if rid not in resource_ids),
                                previous_kind,
                            ),
                            before=before_files,
                            removed=previous_resource_ids,
                            then=manifest_commit,
                        )

                    return _register_manifest

                prepare_on_commit = _prepare_manifest_commit

            return self._commit_script_unlocked(
                project_name,
                script,
                filename,
                validate=validate,
                before=before_script,
                prepare_on_commit=prepare_on_commit,
                project_update=project_update,
            )

    def _commit_script_unlocked(
        self,
        project_name: str,
        script: dict,
        filename: str,
        *,
        validate: bool,
        before: dict | _Unset | None = _UNSET,
        on_commit: Callable[[Path], None] | None = None,
        prepare_on_commit: Callable[[], Callable[[Path], None] | None] | None = None,
        project_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        """Commit a script, its project index, and an optional sidecar hook together.

        The caller holds the canonical script lock.  An episode index, or a
        ``prepare_on_commit`` callback, also keeps the project lock through the
        formal write and hook so rollback cannot overwrite a concurrent project
        edit. The prepare callback runs only after that lock is held, so a
        schema-dependent sidecar plan cannot race with project activation.
        """

        if on_commit is not None and prepare_on_commit is not None:
            raise ValueError("on_commit and prepare_on_commit are mutually exclusive")

        scripts_dir = self.get_project_path(project_name) / "scripts"
        output_path = Path(self._safe_subpath(scripts_dir, filename))
        project_file = self._get_project_file_path(project_name)
        sync_project = project_file.is_file() and isinstance(script.get("episode"), int)
        lock_project = project_file.is_file() and (sync_project or prepare_on_commit is not None)
        if project_update is not None and not sync_project:
            raise ValueError("project_update requires an episode script of an existing project")

        if lock_project:
            with self._project_lock(project_name):
                prepared_on_commit = prepare_on_commit() if prepare_on_commit is not None else on_commit
                transaction_paths = (output_path, project_file) if sync_project else (output_path,)
                with formal_write_transaction(*transaction_paths):
                    output = self._write_script_unlocked(
                        project_name,
                        script,
                        filename,
                        sync_project=False,
                        validate=validate,
                        before=before,
                        emit_change=False,
                    )
                    if sync_project:
                        project = self._read_project_raw_unlocked(project_name)
                        if self._requires_unique_asset_namespace(project):
                            ensure_project_asset_namespace(project)
                        self._apply_episode_sync(project, script, filename)
                        if project_update is not None:
                            project_update(project)
                        self._migrate_legacy_resolution_on_save(project)
                        self._touch_metadata(project)
                        if self._requires_unique_asset_namespace(project):
                            ensure_project_asset_namespace(project)
                        atomic_write_json(project_file, project)
                    if prepared_on_commit is not None:
                        prepared_on_commit(output)
            changed_paths = [f"scripts/{output_path.name}", *([self.PROJECT_FILE] if sync_project else [])]
        else:
            prepared_on_commit = prepare_on_commit() if prepare_on_commit is not None else on_commit
            with formal_write_transaction(output_path):
                output = self._write_script_unlocked(
                    project_name,
                    script,
                    filename,
                    sync_project=False,
                    validate=validate,
                    before=before,
                    emit_change=False,
                )
                if prepared_on_commit is not None:
                    prepared_on_commit(output)
            changed_paths = [f"scripts/{output_path.name}"]

        emit_project_change_hint(project_name, changed_paths=changed_paths)
        return output

    def _write_script_unlocked(
        self,
        project_name: str,
        script: dict,
        filename: str,
        sync_project: bool = True,
        *,
        validate: bool = True,
        before: dict | _Unset | None = _UNSET,
        emit_change: bool = True,
    ) -> Path:
        """Main body of a script write: validate + update metadata + atomic write + sync project.json.

        **Does not acquire `_script_lock`** -- the caller must already hold it (see `save_script` / `locked_script`),
        otherwise concurrency protection is lost. It's factored out so that `locked_script` reusing `save_script` doesn't
        acquire the same flock twice and self-deadlock within the process (same reason `update_project` inlines
        `atomic_write_json` instead of reusing `save_project`). filename must already have the `scripts/` prefix stripped
        and not be None.

        With `sync_project=False`, `sync_episode_from_script` is skipped: that sync acquires `_project_lock` again via
        `update_project`, so callers already holding the project lock (see `locked_episode_script`) must pass False to
        avoid self-deadlock within the process. Skipping the sync has no side effects when only script content is written
        and episode metadata isn't changed.

        With `validate=True` (default, fail-safe) structural validation uses "no worse" semantics: only when the data to
        write turns a previously valid script invalid does it `raise ScriptStructureValidationError`; old scripts that were
        already invalid before the change pass as usual. Read-modify-write flows (the `locked_script` family) already hold
        the pre-change script and should pass it as `before` for zero extra disk reads; direct saves don't pass `before`,
        and this function reads the pre-change state from disk as needed (strict validation when there is none). Hot paths
        such as asset write-back that only touch `generated_assets` pass `validate=False` for a full exemption.

        剧本写盘主体：校验 + 更新元数据 + 原子写 + 同步 project.json。

        **不获取 `_script_lock`**——调用方必须已持有该锁（见 `save_script` / `locked_script`），
        否则会丧失并发保护。独立抽出是为了避免 `locked_script` 复用 `save_script` 时二次获取
        同一把 flock 造成同进程自死锁（与 `update_project` 内联 `atomic_write_json` 而不复用
        `save_project` 同理）。filename 须已去除 `scripts/` 前缀且非 None。

        `sync_project=False` 时跳过 `sync_episode_from_script`：该同步会经 `update_project`
        再次获取 `_project_lock`，故已持有项目锁的调用方（见 `locked_episode_script`）须传 False
        以免同进程自死锁。仅写脚本内容、不改 episode 元数据的场景跳过同步无副作用。

        `validate=True`（默认，fail-safe）时按「不更坏」语义做结构校验：仅当待写数据把一个
        原本合法的剧本改成非法时才 `raise ScriptStructureValidationError`，改前就已非法的旧
        剧本照常放行。读-改-写流程（`locked_script` 一族）已持有改前剧本，应作 `before` 传入
        以零额外读盘；直连保存不传 `before`，由本函数按需读盘取改前（无改前则按严格校验）。
        资产回写等只动 `generated_assets` 的热路径传 `validate=False` 整体豁免。
        """
        scripts_dir = self.get_project_path(project_name) / "scripts"
        real = self._safe_subpath(scripts_dir, filename)
        output_path = Path(real)

        # Structural validation guard ("no worse" semantics), placed before the write so dirty data doesn't lurk until it
        # surfaces in the worker execution layer.
        # 结构校验守卫（「不更坏」语义），置于落盘前，避免脏数据潜伏到 worker 执行层才暴露。
        if validate:
            before_script = self._load_script_or_none(output_path) if isinstance(before, _Unset) else before
            self._guard_no_worse(before_script, script)

        # Then check filename/internal episode consistency, so it isn't only raised in the sync stage after writing, causing
        # a partial commit of "script file written, project.json not synced".
        # 再做 filename/内部 episode 一致性校验，避免写盘后才在 sync 阶段抛错，
        # 造成"脚本文件已落盘、project.json 未同步"的部分提交。
        self.require_filename_episode_consistency(script, filename)

        # Update metadata (compatible with old scripts: metadata may be missing, or narration uses segments)
        # 更新元数据（兼容旧脚本：可能缺少 metadata，或 narration 使用 segments）
        now = datetime.now(UTC).isoformat()
        metadata = script.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            script["metadata"] = metadata
        metadata.setdefault("created_at", now)
        metadata.setdefault("status", "draft")
        metadata["updated_at"] = now

        # Atomic write (with path-traversal protection; output_path was already resolved before the guard), so concurrent
        # PATCHes don't corrupt the JSON
        # 原子写（含路径遍历防护，output_path 已在守卫前解析），避免并发 PATCH 导致 JSON 损坏
        self._persist_script_json(output_path, script)

        # Sync to project.json so the script write and metadata sync are a single transaction (sync uses `_project_lock`, a
        # different lock from the outer `_script_lock`, so no conflict).
        # 同步到 project.json，保证 script 写入与元数据同步是单一事务
        # （sync 走的是 `_project_lock`，与外层 `_script_lock` 不同锁，不会冲突）。
        if sync_project and self.project_exists(project_name) and isinstance(script.get("episode"), int):
            self.sync_episode_from_script(project_name, filename)

        if emit_change:
            emit_project_change_hint(
                project_name,
                changed_paths=[f"scripts/{output_path.name}"],
            )

        return output_path

    @staticmethod
    def normalize_script_filename(script_filename: str) -> str:
        """Strip the `scripts/` prefix and collapse `./` segments, normalizing to the same identity as
        `_script_lock`/`_safe_subpath`.

        `episode_1.json`, `scripts/episode_1.json`, `./episode_1.json`, `./scripts/episode_1.json` are valid aliases of the
        same script -- `_safe_subpath` resolves by real path underneath and they all share the same file lock; callers
        that compare across calls or use it as a key (e.g. concurrency markers grouped by script) should pass through here
        first, so the different spellings don't each create their own identity invisible to the others. Path segments must
        be collapsed before stripping the prefix -- in the reverse order, `./scripts/x.json` wouldn't match the prefix (there
        is still `./` before it) and only becomes `scripts/x.json` after collapsing, which `load_script`/`_script_lock`
        would treat as a bare file name without prefix and join with the `scripts/` directory again, producing the double
        prefix `scripts/scripts/x.json`. `..` segments are kept as-is for `_safe_subpath` to reject; no bounds check here.

        剥离 `scripts/` 前缀并折叠 `./` 片段，归一到与 `_script_lock`/`_safe_subpath` 一致的身份。

        `episode_1.json`、`scripts/episode_1.json`、`./episode_1.json`、`./scripts/episode_1.json`
        是指向同一剧本的合法别名——`_safe_subpath` 底层按真实路径解析，均共享同一把文件锁；调用方
        需要跨调用比较或做 key（如按剧本分组的并发标记）时应先过这里，避免几种写法各自生成一份
        身份、互相看不见对方。须先折叠路径片段再剥前缀——顺序反过来的话，`./scripts/x.json` 先剥
        前缀不命中（前缀前面还有 `./`），折叠后才变回 `scripts/x.json`，会被 `load_script`/
        `_script_lock` 当成不含前缀的裸文件名再次拼接 `scripts/` 目录，产生 `scripts/scripts/x.json`
        双重前缀。保留 `..` 片段原样交给 `_safe_subpath` 拒绝，这里不做越界判断。
        """
        normalized = posixpath.normpath(script_filename).removeprefix("scripts/")
        return "" if normalized == "." else normalized

    @contextmanager
    def locked_script(
        self,
        project_name: str,
        script_filename: str,
        *,
        validate: bool = True,
        on_commit: Callable[[Path], None] | None = None,
        prepare_on_commit: Callable[[dict], Callable[[Path], None] | None] | None = None,
    ):
        """Complete a script load -> mutate -> save read-modify-write within a single `_script_lock`.

        Yields the script dict for the caller to modify in place; written back on normal exit; if the with body raises
        (e.g. target scene/unit not found) the write-back is skipped and the lock released as usual. Symmetric with
        `update_project`, eliminating the lost-update race of "overwritten by a concurrent write between read and write".

        With `validate=True` (default) the "pre-change" script is snapshotted before yield and the write-back goes through
        "no worse" structural validation (zero extra disk reads). Asset write-back hot paths that only touch
        `generated_assets` pass `validate=False` for a full exemption. ``prepare_on_commit`` receives the modified script
        under the project lock and returns the sync hook for after the formal write.

        在单一 `_script_lock` 内完成剧本的 load → mutate → save 读-改-写。

        yield 出剧本字典供调用方就地修改；正常退出时写回，with 体内抛异常（如目标 scene/unit
        未找到）则跳过写回、照常释放锁。与 `update_project` 对称，消除"读改写之间被并发写覆盖"
        的 lost-update 竞态。

        `validate=True`（默认）时在 yield 前快照「改前」剧本，写回走「不更坏」结构校验（零额外
        读盘）。只动 `generated_assets` 的资产回写热路径传 `validate=False` 整体豁免。
        ``prepare_on_commit`` 在项目锁内接收修改后的剧本，并返回正式写入后的同步 hook。
        """
        norm = self.normalize_script_filename(script_filename)
        with self._script_lock(project_name, norm):
            # Lock already held; use the unlocked variant to get the script (migration results are written along with the
            # write-back)
            # 已持锁，走 unlocked 变体取剧本（迁移结果随写回落盘）
            script, _migrated = self._read_script_unlocked(project_name, norm)
            before = copy.deepcopy(script) if validate else None
            yield script
            self._commit_script_unlocked(
                project_name,
                script,
                norm,
                validate=validate,
                before=before,
                on_commit=on_commit,
                prepare_on_commit=(lambda: prepare_on_commit(script)) if prepare_on_commit is not None else None,
            )

    def script_lock(self, project_name: str, script_filename: str) -> AbstractContextManager[None]:
        """Exclusive lock of the script (non-reentrant), for callers that take the script lock outside the project lock first
        and then call :meth:`update_script_locked` within it.

        剧本的排他锁（不可重入），供要在项目锁之外先取剧本锁、再在锁内调用 :meth:`update_script_locked` 的调用方。"""
        return self._script_lock(project_name, script_filename)

    def update_script_locked(self, project_name: str, script_filename: str, mutate_fn: Callable[[dict], None]) -> dict:
        """The caller holds the script lock then the project lock (e.g. the restore pipeline's landing rules): read-modify-write
        the script and sync the episode index; return the written-back script.

        The write-back goes through "no worse" structural validation; like :meth:`update_project_locked` it does no
        rollback on failure and sends no change hints.

        调用方依次持剧本锁与项目锁（如恢复流水线的落地规则）：读-改-写剧本并同步集索引，返回写回的剧本。

        写回经「不更坏」结构校验；与 :meth:`update_project_locked` 一样不做失败回滚，也不发变更提示。
        """
        norm = self.normalize_script_filename(script_filename)
        script, _migrated = self._read_script_unlocked(project_name, norm)
        before = copy.deepcopy(script)
        mutate_fn(script)
        self._write_script_unlocked(
            project_name, script, norm, sync_project=False, validate=True, before=before, emit_change=False
        )
        project = self._read_project_raw_unlocked(project_name)
        if isinstance(script.get("episode"), int):
            self._apply_episode_sync(project, script, norm)
        self._touch_metadata(project)
        atomic_write_json(self._get_project_file_path(project_name), project)
        return script

    @contextmanager
    def locked_project_script_snapshot(self, project_name: str, script_filename: str):
        """Yield a read-only project/script snapshot under the canonical write locks.

        Artifact selection must compare execution-frozen inputs with one coherent
        current snapshot, but a read-only comparison must not rewrite the script
        or touch project metadata.  The lock order mirrors
        :meth:`locked_episode_script` (script, then project), so callers can keep
        the comparison and a guarded downstream commit serialized with both
        source edit paths.
        """

        norm = self.normalize_script_filename(script_filename)
        with self._script_lock(project_name, norm), self._project_lock(project_name):
            project = self._read_project_raw_unlocked(project_name)
            script, _migrated = self._read_script_unlocked(project_name, norm)
            yield project, script

    @contextmanager
    def locked_project_snapshot(self, project_name: str):
        """Yield current project metadata under its canonical write lock."""

        with self._project_lock(project_name):
            project = self._read_project_raw_unlocked(project_name)
            yield project

    def _read_project_raw_unlocked(self, project_name: str) -> dict:
        """Raw-read project.json (no lock, no migration). Only for re-check calls already holding `_project_lock`.

        裸读 project.json（不取锁、不迁移）。仅供已持 `_project_lock` 的复核调用。"""
        project_file = self._get_project_file_path(project_name)
        with open(project_file, encoding="utf-8") as f:
            return json.load(f)

    @contextmanager
    def locked_episode_script(
        self,
        project_name: str,
        resolve_script_file: Callable[[dict], str],
        *,
        validate: bool = True,
        on_commit: Callable[[Path], None] | None = None,
    ):
        """Under the unified "script lock -> project lock" order, resolve episode->script_file and read-modify-write the
        script.

        `resolve_script_file(project) -> script_file`: a resolver supplied by the caller that finds the target episode in
        project.json, validates it, and returns its bound script file name (it may raise itself, e.g. 404/409).

        Resolve candidate -> lock -> re-check binding -> write all happen inside the critical section holding
        `_project_lock`, eliminating the TOCTOU of "script_file read outside the lock, rebound by a concurrent PATCH, write
        lands on the old script". The lock acquisition order matches worker write-back (`locked_script` -> sync): script
        lock -> project lock, avoiding ABBA deadlock.

        Writing the script uses `sync_project=False` to skip the sync inside `_write_script_unlocked` that would take the
        project lock again (avoiding self-deadlock within the process); instead the episode metadata sync and project.json
        write-back are done inline within the already-held project lock, behaving the same as the old `locked_script` ->
        sync path (refreshing episodes metadata and `updated_at`).

        If the binding points to different scripts before and after locking (concurrent rebinding), raise
        `EpisodeScriptReboundError` for the caller to retry.

        ``on_commit`` runs after the script and project index are written and before the lock is released, for the same
        domain commit to append final steps such as the Artifact Manifest. Both JSON files are snapshotted byte for byte
        before committing; if any of the script, project or hook steps fails, the old bytes are restored atomically in the
        same critical section. The hook must design its own writes to "not raise after succeeding", otherwise this method
        can't infer how to undo external state it has already written.

        统一「脚本锁 → 项目锁」顺序下，解析 episode→script_file 并对剧本做读-改-写。

        `resolve_script_file(project) -> script_file`：调用方提供的解析器，从 project.json
        找到目标 episode、做校验、返回其绑定的脚本文件名（可自行抛异常，如 404/409）。

        解析候选 → 加锁 → 复核绑定 → 写入全程在持 `_project_lock` 的临界区内完成，消除
        「锁外读 script_file 后被并发 PATCH 改绑、写入落到旧脚本」的 TOCTOU。锁获取顺序与
        worker 回写（`locked_script` → sync）保持一致的 脚本锁 → 项目锁，避免 ABBA 死锁。

        写脚本经 `sync_project=False` 跳过 `_write_script_unlocked` 内会二次取项目锁的 sync
        （避免同进程自死锁）；改在已持有的项目锁内联完成集元数据同步与 project.json 写回，
        与旧 `locked_script` → sync 路径行为一致（刷新 episodes 元数据与 `updated_at`）。

        若加锁前后绑定指向了不同脚本（并发改绑），抛 `EpisodeScriptReboundError` 让调用方重试。

        ``on_commit`` 在脚本与 project 索引写入后、锁释放前执行，供同一领域提交追加 Artifact
        Manifest 等最后一步。提交前逐字快照两份 JSON；脚本、project 或 hook 任一步失败都在同一
        临界区原子恢复旧字节。hook 必须把自身写入设计为「成功后不再抛错」，否则它已经落下的
        外部状态无法由本方法推断如何撤销。
        """
        # Candidate resolution only determines the script lock identity and only reads outside the lock; if the command is
        # then refused by revision / schema pre-checks, project.json must remain byte-for-byte unchanged. On a successful
        # commit, migration is written within the project lock below together with the script and index, compensated by the
        # same old-bytes snapshot.
        # 候选解析只用于确定脚本锁身份，锁外只读不写；命令若随后因 revision / schema 等预检
        # 被拒，project.json 必须保持逐字不变。成功提交时，迁移会在下方项目锁内与脚本、索引
        # 一起落盘并受同一份旧字节快照补偿。
        candidate = resolve_script_file(self.load_project(project_name))
        norm = self.normalize_script_filename(candidate)
        with self._script_lock(project_name, norm), self._project_lock(project_name):
            project = self._read_project_raw_unlocked(project_name)
            if self._requires_unique_asset_namespace(project):
                ensure_project_asset_namespace(project)
            current = resolve_script_file(project)
            cur_norm = self.normalize_script_filename(current)
            if cur_norm != norm:
                raise EpisodeScriptReboundError(f"episode script binding changed: {norm} -> {cur_norm}")
            script_path = Path(self._safe_subpath(self.get_project_path(project_name) / "scripts", norm))
            project_path = self._get_project_file_path(project_name)
            script, _migrated = self._read_script_unlocked(project_name, norm)
            before = copy.deepcopy(script) if validate else None
            yield script
            with formal_write_transaction(script_path, project_path):
                self._write_script_unlocked(
                    project_name,
                    script,
                    norm,
                    sync_project=False,
                    validate=validate,
                    before=before,
                    emit_change=False,
                )
                # Sync project.json inline within the already-held project lock (equivalent to update_project's write path, without
                # taking the lock again)
                # 在已持项目锁内联同步 project.json（等价 update_project 写路径，但不二次取锁）
                if isinstance(script.get("episode"), int):
                    self._apply_episode_sync(project, script, norm)
                self._migrate_legacy_resolution_on_save(project)
                self._touch_metadata(project)
                if self._requires_unique_asset_namespace(project):
                    ensure_project_asset_namespace(project)
                atomic_write_json(project_path, project)
                if on_commit is not None:
                    on_commit(script_path)
            emit_project_change_hint(
                project_name,
                changed_paths=[f"scripts/{script_path.name}", self.PROJECT_FILE],
            )

    @staticmethod
    def filename_episode(script_filename: str) -> int | None:
        """Episode number implied by the file name (`episode[-_\\s]*N`, case-insensitive); None when the file name has no
        episode number.

        Non-positive parse results like `episode_0.json` are always treated as "no episode number": 0 corresponds to no
        episode, and accepting it as ownership would make episode metadata sync create an `episode: 0` entry in the ledger.

        文件名隐含的集号（`episode[-_\\s]*N`，忽略大小写）；文件名不含集号时返回 None。

        `episode_0.json` 这种非正数解析结果一律按「不含集号」处理：0 不对应任何一集，认它作归属
        会让集元数据同步往账本里新建一条 `episode: 0`。
        """
        base_name = ProjectManager.normalize_script_filename(script_filename)
        filename_match = re.search(r"episode[-_\s]*(\d+)", base_name, re.IGNORECASE)
        if filename_match is None:
            return None
        episode = int(filename_match.group(1))
        return episode if is_episode_number(episode) else None

    @staticmethod
    def canonical_script_episode(script_filename: str) -> int | None:
        """Which episode this script belongs to: episode N when the file name is exactly the canonical name
        `episode_N.json`; anything else is unrecognized.

        Ownership depends only on the file name, not on the ledger binding: since v15 `script_file` can only be the
        episode's canonical path (see `lib.project.project_migrations.v14_to_v15_formal_script_truth`), so files like
        `episode_1_old.json` or `custom.json` belong to no episode.

        这份剧本归属哪一集：文件名正是规范名 `episode_N.json` 时为第 N 集，其余一律认不出。

        归属只看文件名，与账本绑定无关：`script_file` 自 v15 起只能是该集的规范路径
        （见 `lib.project.project_migrations.v14_to_v15_formal_script_truth`），`episode_1_old.json`、
        `custom.json` 这类文件不属于任何一集。
        """
        base_name = ProjectManager.normalize_script_filename(script_filename)
        episode = ProjectManager.filename_episode(base_name)
        if episode is None or base_name != episode_script_filename(episode):
            return None
        return episode

    @staticmethod
    def require_filename_episode_consistency(script: dict, script_filename: str) -> None:
        """Check that the file name is an episode's canonical name and the script's internal `episode` field matches it;
        otherwise raise ValueError.

        Scripts with non-canonical names belong to no episode and are always refused; they are not written or registered as
        an episode binding based on the script's internal `episode`. When the script has no int `episode`, only a canonical
        file name is required.

        校验文件名是某一集的规范名，且脚本内 `episode` 字段与之一致；否则 raise ValueError。

        非规范名的剧本不属于任何一集，一律拒绝，不按脚本内 `episode` 写盘或登记为集绑定。
        脚本内无 `episode` int 时只要求文件名是规范名。
        """
        base_name = ProjectManager.normalize_script_filename(script_filename)
        owner_episode = ProjectManager.canonical_script_episode(base_name)
        if owner_episode is None:
            raise ValueError(
                f"Script {base_name} does not have a canonical file name (expected episode_N.json); "
                "refusing to avoid corrupting project.json"
            )
        script_episode = script.get("episode")
        if not is_episode_number(script_episode):
            return
        if script_episode != owner_episode:
            raise ValueError(
                f"Script {base_name} has internal episode={script_episode}, which does not match its owning "
                f"episode={owner_episode}; refusing to avoid corrupting project.json"
            )

    def _persist_script_json(self, path: Path, script: dict) -> None:
        """The single exit for writing script JSON to disk: atomic write by default, the injected implementation when
        ``script_writer`` is injected.

        剧本 JSON 落盘的单一出口：缺省原子写，注入了 ``script_writer`` 时改走注入实现。"""
        if self._script_writer is None:
            atomic_write_json(path, script)
        else:
            self._script_writer(path, script)

    def _load_script_or_none(self, path: Path) -> dict | None:
        """Raw-read script JSON for the "pre-change" snapshot; return None when the script doesn't exist or is corrupt (->
        handled with strict validation).

        Shares the ``script_reader`` seam with ``_read_script_unlocked``, so the pre-change snapshot and the formal read
        come from the same source. An injected reader signals an unobtainable script with ``OSError`` (missing or
        unreadable) or ``ValueError`` (corrupt content), which this function normalizes to None; other exceptions propagate
        as usual.

        裸读剧本 JSON 取「改前」快照；剧本不存在或损坏时返回 None（→ 按严格校验处理）。

        与 ``_read_script_unlocked`` 共用 ``script_reader`` seam，改前快照与正式读取取自同一
        来源。注入的 reader 以 ``OSError``（不存在或不可读）或 ``ValueError``（内容损坏）表达
        取不到剧本，本函数据此归一为 None；其余异常照常上抛。
        """
        if self._script_reader is None:
            loaded = load_json_or_none(path)
        else:
            try:
                loaded = self._script_reader(path)
            except (OSError, ValueError):
                return None
        return loaded if isinstance(loaded, dict) else None

    @staticmethod
    def _guard_no_worse(before: dict | None, after: dict) -> None:
        """"No worse" guard: refuse only when the data to write introduces new structural errors.

        Valid after change -> allow; invalid after change: valid before or no pre-change state -> refuse (`raise`); already
        invalid before -> allow (not taking the blame for legacy problems). The validator is imported lazily inside the
        function, breaking the import cycle project_manager -> validator -> data_validator -> project_manager.

        「不更坏」守卫：仅当待写数据引入新结构错误时拒绝。

        改后合法 → 放行；改后非法时：改前合法或无改前 → 拒绝（`raise`）；改前已非法 → 放行
        （不为历史遗留背锅）。校验器经函数内延迟 import，打破 project_manager → 校验器 →
        data_validator → project_manager 的导入环。
        """
        from lib.script.script_structure_validator import (
            ScriptStructureValidationError,
            validate_script_structure,
        )

        after_result = validate_script_structure(after)
        if after_result.valid:
            return
        if before is not None and not validate_script_structure(before).valid:
            return
        raise ScriptStructureValidationError(after_result)

    @staticmethod
    def resolve_episode_from_script(script: dict, script_filename: str) -> int:
        """Resolve the episode number from the script.

        Prefer the script's top-level `episode` field (source of truth), falling back to the file-name regex
        `episode[-_\\s]*(\\d+)` (supports underscore/space/hyphen separators); raise ValueError when neither exists.

        Replaces callers repeatedly passing the `--episode` CLI argument, which risks mismatches.

        `bool` is a subclass of `int`, so it's excluded explicitly: a script with `episode: true` (dirty data) is treated
        as a missing field and goes to the file name, not silently treated as episode 1.

        从剧本解析集号。

        优先使用 script 顶层 `episode` 字段（真相源），fallback 到文件名正则
        `episode[-_\\s]*(\\d+)`（支持下划线/空格/连字符分隔）；两者都无则抛 ValueError。

        用于替代调用方重复传入 `--episode` CLI 参数造成的错配风险。

        `bool` 是 `int` 的子类，故显式排除：剧本 `episode: true`（脏数据）当作字段缺失走文件名，
        不静默当成第 1 集。
        """
        ep = script.get("episode")
        if isinstance(ep, int) and not isinstance(ep, bool):
            return ep
        match = re.search(r"episode[-_\s]*(\d+)", script_filename, re.IGNORECASE)
        if match:
            return int(match.group(1))
        raise ValueError(f"Cannot determine the episode number: the script has no episode field and the file name "
            f"{script_filename} does not contain an episodeN pattern")

    def sync_episode_from_script(self, project_name: str, script_filename: str) -> dict:
        """
        Sync episode info from the script file into project.json

        The Agent must call this after writing a script to make sure the WebUI shows the episode list correctly.

        Args:
            project_name: project name
            script_filename: script file name (e.g. episode_1.json)

        Returns:
            updated project dict

        Raises:
            ValueError: when the file name isn't the canonical name `episode_N.json`, or the file name's episode number
                doesn't match the script's internal `episode` field, so wrong script data doesn't overwrite the real
                episode entry (e.g. episode_10.json mistakenly containing episode=1 would overwrite episode 1).

        从剧本文件同步集数信息到 project.json

        Agent 写入剧本后必须调用此方法以确保 WebUI 能正确显示剧集列表。

        Args:
            project_name: 项目名称
            script_filename: 剧本文件名（如 episode_1.json）

        Returns:
            更新后的 project 字典

        Raises:
            ValueError: 当文件名不是规范名 `episode_N.json`，或文件名的集号与脚本内
                `episode` 字段不一致时抛出，避免错误的脚本数据覆盖真实集号条目
                （例如 episode_10.json 内部错写为 episode=1，会覆盖第 1 集）。
        """
        # Use the unlocked variant: this method is called by the unified write entry point while holding `_script_lock`, and
        # `load_script`'s migration write-back would take the same lock again and self-deadlock.
        # 走 unlocked 变体：本方法被写盘统一入口在持有 `_script_lock` 时调用，
        # `load_script` 的迁移回写会二次取同一把锁而自死锁。
        script, _migrated = self._read_script_unlocked(project_name, script_filename)
        return self.update_project_reconciling_episode_bindings(
            project_name, lambda project: self._apply_episode_sync(project, script, script_filename)
        )

    def _apply_episode_sync(self, project: dict, script: dict, script_filename: str) -> None:
        """Sync the script's episode number/title/script_file into `project` (in place, no lock, no write).

        Shared by `sync_episode_from_script` (inside the `update_project` lock) and `locked_episode_script` (inside the
        critical section already holding `_project_lock`), avoiding duplicate implementations of the episode metadata sync.

        把剧本的集号/标题/script_file 同步进 `project`（就地修改，不取锁、不写盘）。

        供 `sync_episode_from_script`（在 `update_project` 锁内）与 `locked_episode_script`
        （在已持 `_project_lock` 的临界区内）共用，避免重复实现集元数据同步逻辑。
        """
        base_name = self.normalize_script_filename(script_filename)
        # Defense in depth: the SSE scan path calls this function directly (not via save_script), so it also needs validation
        # 防御纵深：SSE 扫描路径直接调用此函数（不经 save_script），同样需要校验
        self.require_filename_episode_consistency(script, base_name)

        script_episode = script.get("episode")
        episode_num = script_episode if is_episode_number(script_episode) else self.canonical_script_episode(base_name)
        episode_title = script.get("title", "")
        script_file = f"scripts/{base_name}"

        # Find or create the episode entry (the whole RMW happens within a single _project_lock, so concurrent syncs aren't
        # lost)
        # 查找或创建 episode 条目（整段 RMW 在单一 _project_lock 内完成，避免并发同步丢失）
        episodes = project.setdefault("episodes", [])
        episode_entry: dict[str, Any] | None = next((ep for ep in episodes if ep["episode"] == episode_num), None)
        if episode_entry is None:
            # Episodes whose script was written before their ledger entry have never registered episode source text / 剧本先于账本条目落盘的集没有登记过集原文
            episode_entry = {"episode": episode_num, SOURCE_ORIGIN_FIELD: SourceOrigin.NONE.value}
            episodes.append(episode_entry)
        # Sync core metadata (excluding statistics fields, which are computed at read time by the project summary)
        # 同步核心元数据（不包含统计字段，统计字段由项目摘要读时计算）
        episode_entry["title"] = episode_title
        episode_entry["script_file"] = script_file
        # Entry numbers are only issued, never reused: every number that appeared in a written script is recorded into the
        # episode's largest used number
        # 条目编号只分配、不复用：落盘的剧本里出现过的编号都记进本集用过的最大号
        try:
            items, id_field, _kind = resolve_items(script)
        except ScriptEditError:
            items, id_field = [], ""
        if episode_num is not None:
            raise_item_id_high_water(
                episode_entry,
                max_item_number((item.get(id_field) for item in items if isinstance(item, Mapping)), episode_num),
            )

        logger.info("已同步剧集信息: Episode %d - %s", episode_num, episode_title)

    def item_id_floor(self, project: Mapping[str, Any], script: Mapping[str, Any], episode: int) -> int:
        """The largest main sequence number already used for entries in this episode: the max of the ledger's recorded maximum
        and the numbers currently in the script.

        本集条目编号已用过的最大主序号：账本记录的最大号与剧本现存编号取大。"""
        try:
            items, id_field, _kind = resolve_items(dict(script))
        except ScriptEditError:
            items, id_field = [], ""
        return max(
            episode_item_id_high_water(project, episode),
            max_item_number((item.get(id_field) for item in items if isinstance(item, Mapping)), episode),
        )

    def reserve_item_ids(self, project_name: str, episode: int, kind: str, count: int) -> list[str]:
        """Issue ``count`` new numbers for write points that take numbers before the script (script plan drafts), advancing
        the episode's maximum to the last one.

        When the episode isn't in the ledger the maximum isn't recorded: the entry is created when the script is first
        written, and the numbers in the script are recorded then.

        为剧本之外先占号的写入点（脚本规划草稿）发 ``count`` 个新编号，并把本集最大号推进到最后一个。

        集不在账本里时不记最大号：剧本首次落盘时建条目，届时按剧本里的编号补记。
        """
        issued: list[str] = []

        def _mutate(project: dict) -> None:
            entry = find_episode(project, episode)
            script: Mapping[str, Any] = {}
            script_file = entry.get("script_file") if entry is not None else None
            if isinstance(script_file, str):
                scripts_dir = self.get_project_path(project_name) / "scripts"
                script_path = Path(self._safe_subpath(scripts_dir, self.normalize_script_filename(script_file)))
                script = self._load_script_or_none(script_path) or {}
            issuer = ItemIdIssuer(episode, kind, self.item_id_floor(project, script, episode))
            issued[:] = [issuer.issue() for _ in range(count)]
            if entry is not None and issued:
                raise_item_id_high_water(entry, issuer.last)

        self.update_project(project_name, _mutate)
        return issued

    def load_script(self, project_name: str, filename: str) -> dict:
        """
        Load a shot script (including legacy migration; migration results are written back under the script lock)

        Args:
            project_name: project name
            filename: script file name

        Returns:
            script dict

        加载分镜剧本（含存量迁移，迁移结果在剧本锁内回写落盘）

        Args:
            project_name: 项目名称
            filename: 剧本文件名

        Returns:
            剧本字典
        """
        norm = self.normalize_script_filename(filename)
        # Read once without the lock first: the vast majority of scripts were absorbed long ago, so this path takes no lock
        # and creates no lock file; read concurrency and file-system side effects stay the same as before migration (a
        # missing script also fails loudly before a lock file is created).
        # 先无锁读一次：绝大多数剧本早已完成收编，这条路径不取锁、不建 lock 文件，读剧本的
        # 并发度与文件系统副作用与迁移前一致（剧本不存在也在建锁文件之前就 fail-loud）。
        script, migrated = self._read_script_unlocked(project_name, norm)
        if not migrated:
            return script
        with self._script_lock(project_name, norm):
            # Re-read once under the lock before writing back: read-modify-write must happen within the same script lock,
            # otherwise content written by a concurrent writer between the lock-free read and the write-back would be overwritten
            # by the migration result. Not via _write_script_unlocked: migration is only format absorption and shouldn't refresh
            # metadata.updated_at or trigger project.json sync and change hints.
            # 锁内重读一次再回写：读-改-写须在同一把剧本锁内完成，否则并发写者在无锁读与回写
            # 之间落盘的内容会被迁移结果覆盖。不走
            # _write_script_unlocked：迁移只是格式收编，不应刷新 metadata.updated_at、
            # 不触发 project.json 同步与变更提示。
            script, migrated = self._read_script_unlocked(project_name, norm)
            if migrated:
                real = Path(self._safe_subpath(self.get_project_path(project_name) / "scripts", norm))
                self._persist_script_json(real, script)
        return script

    def load_script_readonly(self, project_name: str, filename: str) -> dict:
        """Load a script with in-memory compatibility migrations but never persist them."""
        norm = self.normalize_script_filename(filename)
        script, _migrated = self._read_script_unlocked(project_name, norm)
        return script

    def _read_script_unlocked(self, project_name: str, filename: str) -> tuple[dict, bool]:
        """Raw-read the script and run legacy migration in place; return ``(script, whether migration occurred)``; **does not
        take the script lock**.

        Locking and write-back are the caller's responsibility; shared by three kinds of callers:
        - Read-modify-write already holding `_script_lock` (the `locked_script` family, the unified write entry point's
          episode metadata sync) -- they write back on exit as usual, and the migration result is written with it; taking
          the same flock again here would self-deadlock within the process.
        - `load_script`'s lock-free probe -- returns directly when no migration happened, introducing no lock contention for
          reading scripts.
        - `load_script`'s re-read after taking the lock -- fetch the latest content again under the lock before writing
          back.

        裸读剧本并就地跑存量迁移，返回 ``(剧本, 是否发生迁移)``；**不取剧本锁**。

        取锁与回写的责任在调用方，三类调用方共用：
        - 已持有 `_script_lock` 的读-改-写（`locked_script` 一族、写盘统一入口的集元数据同步）
          ——它们退出时照常写回，迁移结果随之落盘；此处二次取同一把 flock 会同进程自死锁。
        - `load_script` 的无锁探测——未发生迁移就直接返回，不为读剧本引入锁竞争。
        - `load_script` 取锁后的重读——回写前在锁内重新取一次最新内容。
        """
        project_dir = self.get_project_path(project_name)
        filename = self.normalize_script_filename(filename)
        real = Path(self._safe_subpath(project_dir / "scripts", filename))

        if self._script_reader is None:
            # The existence check only holds for the file-system path; whether the script exists is decided by the actual reader.
            # 存在性检查只对文件系统这条路径成立；剧本是否存在由实际读取方判定。
            if not real.exists():
                raise FileNotFoundError(f"Script file does not exist: {real}")
            with open(real, encoding="utf-8") as f:
                script = json.load(f)
        else:
            script = self._script_reader(real)

        migrated, warnings = migrate_script_unit_durations(script)
        for message in warnings:
            logger.warning("剧本 %s 时长收编迁移: %s", real.name, message.render(default_translate))
        return script, migrated

    def list_scripts(self, project_name: str) -> list[str]:
        """List all scripts in the project / 列出项目中的所有剧本"""
        project_dir = self.get_project_path(project_name)
        scripts_dir = project_dir / "scripts"
        return [f.name for f in scripts_dir.glob("*.json")]

    def _canonical_episode_scripts(self, project_name: str) -> list[str]:
        """Scripts under ``scripts/`` with canonical file names, sorted: the candidate set for batch reference rewrites.

        Ownership depends only on the file name (see ``canonical_script_episode``), the same criterion before and after
        locking, so no need to recompute under the lock: JSON with non-canonical names belongs to no episode; rewriting it
        has no basis and would raise ``ValueError`` at the write's episode-number consistency check, aborting the whole
        rename.

        ``scripts/`` 下文件名为规范名的剧本，排序后返回：批量引用改写的候选集。

        归属只看文件名（见 ``canonical_script_episode``），取锁前后是同一份判据，无需在锁内
        重算：非规范名的 JSON 不属于任何一集，改写它既无依据，也会在写盘的集号一致性校验处抛
        ``ValueError`` 中断整次改名。
        """
        return sorted(
            name for name in self.list_scripts(project_name) if self.canonical_script_episode(name) is not None
        )

    # ==================== Character management ==================== / ==================== 角色管理 ====================

    def update_character_sheet(self, project_name: str, script_filename: str, name: str, sheet_path: str) -> dict:
        """Update the character sheet path / 更新角色资产图路径"""
        # Asset write-back hot path: only touches runtime fields, the structure can't get worse from it, so exempt from
        # structural validation.
        # 资产回写热路径：只动运行时字段，结构不可能因此变坏，豁免结构校验。
        with self.locked_script(project_name, script_filename, validate=False) as script:
            key = resolve_asset_key(script.get("characters"), name)
            if key is None:
                # Raised within the lock; locked_script skips the write-back / 在锁内抛出，locked_script 跳过写回
                raise KeyError(f"Character '{name}' not found")
            script["characters"][key]["character_sheet"] = sheet_path
        return script

    # ==================== Data structure normalization ==================== / ==================== 数据结构标准化 ====================

    @staticmethod
    def create_generated_assets(content_mode: str = "narration") -> dict:
        """
        Create the standard generated_assets structure

        Args:
            content_mode: content mode ('narration' or 'drama')

        Returns:
            standard generated_assets dict

        创建标准的 generated_assets 结构

        Args:
            content_mode: 创作类型（'narration' 或 'drama'）

        Returns:
            标准的 generated_assets 字典
        """
        return {
            "storyboard_image": None,
            "storyboard_last_image": None,
            "video_clip": None,
            "video_thumbnail": None,
            "video_uri": None,
            "narration_audio": None,
            "grid_id": None,
            "grid_cell_index": None,
            "status": "pending",
            "video_generated_at": None,
        }

    @staticmethod
    def create_scene_template(scene_id: str, duration_seconds: int = 8) -> dict:
        """
        Create a standard scene object template

        Args:
            scene_id: shot ID (e.g. "E1S01"), the episode number is encoded in the ID
            duration_seconds: shot duration (seconds)

        Returns:
            standard scene dict

        创建标准场景对象模板

        Args:
            scene_id: 分镜 ID（如 "E1S01"），集号已编码在 ID 中
            duration_seconds: 分镜时长（秒）

        Returns:
            标准的场景字典
        """
        return {
            "scene_id": scene_id,
            "duration_seconds": duration_seconds,
            "segment_break": False,
            "characters_in_scene": [],
            "scenes": [],
            "props": [],
            "visual": {
                "description": "",
                "shot_type": "medium shot",
                "camera_movement": "static",
                "lighting": "",
                "mood": "",
            },
            "action": "",
            "dialogue": {"speaker": "", "text": "", "emotion": "neutral"},
            "audio": {"dialogue": [], "narration": "", "sound_effects": []},
            "generated_assets": ProjectManager.create_generated_assets(),
        }

    def normalize_scene(self, scene: dict) -> dict:
        """
        Fill in missing fields in a single scene

        Args:
            scene: scene dict

        Returns:
            completed scene dict

        补全单个场景中缺失的字段

        Args:
            scene: 场景字典

        Returns:
            补全后的场景字典
        """
        template = self.create_scene_template(
            scene_id=scene.get("scene_id", "000"),
            duration_seconds=scene.get("duration_seconds", 8),
        )

        # Merge visual fields / 合并 visual 字段
        if "visual" not in scene:
            scene["visual"] = template["visual"]
        else:
            for key in template["visual"]:
                if key not in scene["visual"]:
                    scene["visual"][key] = template["visual"][key]

        # Merge audio fields / 合并 audio 字段
        if "audio" not in scene:
            scene["audio"] = template["audio"]
        else:
            for key in template["audio"]:
                if key not in scene["audio"]:
                    scene["audio"][key] = template["audio"][key]

        # Fill in the generated_assets field. generated_assets comes from on-disk script JSON and external edits may corrupt
        # it into a non-dict (None/string etc.); normalize via get_generated_assets first, so the membership checks/assignments
        # below don't raise TypeError.
        # 补全 generated_assets 字段
        # generated_assets 来自磁盘剧本 JSON，外部编辑可能损坏成非 dict（None/字符串等）；
        # 先经 get_generated_assets 归一化，避免下面的成员检查/赋值抛 TypeError。
        assets = get_generated_assets(scene)
        assets_template = self.create_generated_assets()
        for key in assets_template:
            if key not in assets:
                assets[key] = assets_template[key]
        scene["generated_assets"] = assets

        # Fill in other top-level fields / 补全其他顶层字段
        top_level_defaults = {
            "segment_break": False,
            "characters_in_scene": [],
            "scenes": [],
            "props": [],
            "action": "",
            "dialogue": template["dialogue"],
        }

        for key, default_value in top_level_defaults.items():
            if key not in scene:
                scene[key] = default_value

        # Update status / 更新状态
        self.update_scene_status(scene)

        return scene

    def update_scene_status(self, scene: dict) -> str:
        """
        Update and return the scene status based on generated_assets content

        Status values:
        - pending: not started
        - storyboard_ready: storyboard image done
        - completed: video done

        Args:
            scene: scene dict

        Returns:
            updated status value

        根据 generated_assets 内容更新并返回场景状态

        状态值:
        - pending: 未开始
        - storyboard_ready: 分镜图完成
        - completed: 视频完成

        Args:
            scene: 场景字典

        Returns:
            更新后的状态值
        """
        # generated_assets comes from on-disk script JSON and external edits may corrupt it into a non-dict (None/string
        # etc.); get_generated_assets normalizes it to an empty dict treated as "not generated", so chained .get() access
        # doesn't raise AttributeError.
        # generated_assets 来自磁盘剧本 JSON，外部编辑可能损坏成非 dict（None/字符串等）；
        # get_generated_assets 归一化为空 dict 按「未生成」处理，避免 .get() 链式访问抛 AttributeError。
        assets = get_generated_assets(scene)

        has_image = bool(assets.get("storyboard_image"))
        has_video = bool(assets.get("video_clip"))

        if has_video:
            status = "completed"
        elif has_image:
            status = "storyboard_ready"
        else:
            status = "pending"

        assets["status"] = status
        scene["generated_assets"] = assets
        return status

    # ==================== Scene management ==================== / ==================== 场景管理 ====================

    def update_scene_asset(
        self,
        project_name: str,
        script_filename: str,
        scene_id: str,
        asset_type: str,
        asset_path: str,
        *,
        on_commit: Callable[[Path], None] | None = None,
    ) -> dict:
        """
        Update a scene's generated resource path

        Args:
            project_name: project name
            script_filename: script file name
            scene_id: shot ID
            asset_type: resource type ('storyboard_image' or 'video_clip')
            asset_path: resource path

        Returns:
            updated script

        更新场景的生成资源路径

        Args:
            project_name: 项目名称
            script_filename: 剧本文件名
            scene_id: 分镜 ID
            asset_type: 资源类型 ('storyboard_image' 或 'video_clip')
            asset_path: 资源路径

        Returns:
            更新后的剧本
        """
        # Asset write-back hot path: only touches generated_assets, the structure can't get worse from it, so exempt from
        # structural validation. But "shot array key corrupted (e.g. segments: null)" is more severe corruption, and the
        # write side must fail loudly -- a silent no-op would hide data loss: the worker writes N video_clips thinking it
        # succeeded, but the UI sees no write-back at all. Let ScriptEditError propagate; the worker layer handles
        # degradation (record task failure, manual repair). `resolve_items`'s discrimination of the three script structures
        # shares one source with `_write_script_unlocked` / the read helpers, avoiding the path where reference-to-video
        # falls into the drama fallback, takes the "scenes" key, silently returns [], and a KeyError "shot not found" masks
        # the root cause.
        # 资产回写热路径：只动 generated_assets，结构不可能因此变坏，豁免结构校验。
        # 但「分镜数组键损坏（如 segments: null）」是更严重的损坏，写入侧必须 fail-loud——
        # 静默 no-op 等于把数据丢失藏起来：worker 写完 N 个 video_clip 还以为成功了，UI 却
        # 看不到任何回写。让 ScriptEditError 上冒，worker 层负责降级（记 task 失败、人工修复）。
        # `resolve_items` 对三种剧本结构的判别与 `_write_script_unlocked` / 读取 helper 共用同一源，
        # 避免参考生视频落到 drama 兜底
        # 取 "scenes" 键、静默返回 [] 然后 KeyError 报"分镜不存在"的根因被掩盖路径。
        with self.locked_script(
            project_name,
            script_filename,
            validate=False,
            on_commit=on_commit,
        ) as script:
            self._set_scene_asset_in_script(script, scene_id, asset_type, asset_path)
        return script

    def _set_scene_asset_in_script(
        self,
        script: dict[str, Any],
        scene_id: str,
        asset_type: str,
        asset_path: str,
    ) -> dict[str, Any]:
        """Mutate one loaded script; the caller owns its canonical script lock."""

        content_mode = script.get("content_mode", "narration")
        items, id_field, _kind = resolve_items(script)
        for item in items:
            # Non-dict elements of a corrupted script are skipped (mirroring the isinstance guard of script_editor._find_index),
            # so item.get(id_field) doesn't raise AttributeError; a miss still goes to the KeyError fail-loud below.
            # 损坏脚本的非 dict 元素跳过（镜像 script_editor._find_index 的 isinstance 守卫），
            # 避免 item.get(id_field) 抛 AttributeError；未命中仍走下方 KeyError fail-loud。
            if not isinstance(item, dict):
                continue
            if str(item.get(id_field)) != str(scene_id):
                continue
            assets = item.get("generated_assets")
            if not isinstance(assets, dict):
                assets = {}
                item["generated_assets"] = assets
            for key, default_value in self.create_generated_assets(content_mode).items():
                if key not in assets:
                    assets[key] = default_value
            assets[asset_type] = asset_path
            self.update_scene_status(item)
            return item
        raise KeyError(f"Storyboard item '{scene_id}' not found")

    def update_scene_asset_across_scripts(
        self,
        project_name: str,
        script_filenames: Sequence[str],
        scene_id: str,
        asset_type: str,
        asset_path: str,
        *,
        on_commit: Callable[[], None] | None = None,
        on_miss: Callable[[], None] | None = None,
    ) -> tuple[str, ...]:
        """Update every matching script and a final sidecar under one lock set.

        Restore operations can touch several episode scripts before registering
        one Manifest identity.  Holding every canonical script lock plus the
        project lock through rollback prevents a failed final registration from
        restoring blanket snapshots over a concurrent script edit.
        """

        # All named scripts with canonical file names are locked and rewritten; the rest belong to no episode and are left
        # untouched.
        # 点名的剧本里文件名为规范名的全部上锁并改写；其余不属于任何一集，不动。
        normalized = tuple(
            sorted(
                {
                    name
                    for name in map(self.normalize_script_filename, script_filenames)
                    if self.canonical_script_episode(name) is not None
                }
            )
        )
        project_path = self.get_project_path(project_name)
        scripts_dir = project_path / "scripts"
        script_paths = [Path(self._safe_subpath(scripts_dir, name)) for name in normalized]
        project_file = self._get_project_file_path(project_name)
        changed: list[str] = []

        with ExitStack() as locks:
            for name in normalized:
                locks.enter_context(self._script_lock(project_name, name))
            locks.enter_context(self._project_lock(project_name))
            with formal_write_transaction(*script_paths, project_file):
                project = self._read_project_raw_unlocked(project_name)
                for name in normalized:
                    try:
                        script, _migrated = self._read_script_unlocked(project_name, name)
                        before = copy.deepcopy(script)
                        self._set_scene_asset_in_script(script, scene_id, asset_type, asset_path)
                    except KeyError:
                        continue
                    except ScriptEditError as exc:
                        logger.warning("跨集同步元数据跳过脏脚本 %s: %s", name, exc)
                        continue
                    except OSError as exc:
                        logger.warning("跨集同步元数据 sibling 集 %s IO 失败: %s", name, exc)
                        continue
                    self._write_script_unlocked(
                        project_name,
                        script,
                        name,
                        sync_project=False,
                        validate=False,
                        before=before,
                        emit_change=False,
                    )
                    # Only sync the episode the ledger actually binds to this script: the write doesn't use the occasion to rebind.
                    # 只同步账本确实绑着这份剧本的那一集：写盘不借恢复改绑。
                    script_episode = script.get("episode")
                    if isinstance(script_episode, int) and resolve_episode_script_binding(
                        project, script_episode, name, require_indexed=True
                    ):
                        self._apply_episode_sync(project, script, name)
                    changed.append(name)

                if changed:
                    self._migrate_legacy_resolution_on_save(project)
                    self._touch_metadata(project)
                    if self._requires_unique_asset_namespace(project):
                        ensure_project_asset_namespace(project)
                    atomic_write_json(project_file, project)
                if changed and on_commit is not None:
                    on_commit()
                if not changed and on_miss is not None:
                    on_miss()

        if changed:
            emit_project_change_hint(
                project_name,
                changed_paths=[*(f"scripts/{name}" for name in changed), self.PROJECT_FILE],
            )
        return tuple(changed)

    def batch_update_scene_assets(
        self,
        project_name: str,
        script_filename: str,
        updates: list[tuple[str, str, Any]],
        *,
        on_commit: Callable[[Path], None] | None = None,
        prepare_on_commit: Callable[[dict], Callable[[Path], None] | None] | None = None,
    ) -> dict:
        """Batch-update the generated resource paths of multiple scenes (single read/write).

        Args:
            project_name: project name
            script_filename: script file name
            updates: list, each item is (scene_id, asset_type, asset_path)
            on_commit: sync hook run after the script and project.json are written, before the formal transaction exits
            prepare_on_commit: callback preparing ``on_commit`` from the modified script under the project lock; mutually
                exclusive with ``on_commit``

        Returns:
            updated script

        批量更新多个场景的生成资源路径（单次读写）。

        Args:
            project_name: 项目名称
            script_filename: 剧本文件名
            updates: 列表，每项为 (scene_id, asset_type, asset_path)
            on_commit: 剧本与 project.json 写入后、正式事务退出前执行的同步 hook
            prepare_on_commit: 项目锁内基于修改后剧本准备 ``on_commit`` 的回调；与
                ``on_commit`` 互斥

        Returns:
            更新后的剧本
        """
        if not updates:
            return {}

        # Asset write-back hot path: only touches generated_assets, the structure can't get worse from it, so exempt from
        # structural validation. Both kinds of errors -- corrupted shot array key (resolve_items raises ScriptEditError) and
        # id misses -- fail loudly: a silent no-op would discard the N clip paths the worker wrote while SSE still broadcasts
        # "all updated", and the UI stays pending forever. Id misses are collected for a full round and raised together, so
        # the worker sees the complete failure set rather than only the first; if locked_script's with body raises, nothing
        # is written back (aligned with the single-item update_scene_asset). resolve_items lets the reference-to-video worker
        # index video_units by unit_id correctly too.
        # 资产回写热路径：只动 generated_assets，结构不可能因此变坏，豁免结构校验。
        # 分镜数组键损坏（resolve_items 抛 ScriptEditError）与 id 未命中两类错误都 fail-loud：
        # 静默 no-op 等于把 worker 写完的 N 个 clip 路径丢弃但 SSE 仍广播「all updated」、UI
        # 永远 pending。id 未命中收集一轮再统一抛，让 worker 看到完整失败集合而不是只看到首个；
        # locked_script 在 with 体内抛异常时整体不写回（与 update_scene_asset 单个版本对齐）。
        # resolve_items 让参考生视频 worker 也能正确按 unit_id 索引 video_units。
        with self.locked_script(
            project_name,
            script_filename,
            validate=False,
            on_commit=on_commit,
            prepare_on_commit=prepare_on_commit,
        ) as script:
            content_mode = script.get("content_mode", "narration")
            items, id_field, _kind = resolve_items(script)

            # Build a scene_id -> item index to avoid O(N*M) lookup. Non-dict elements of a corrupted script are filtered out
            # (mirroring script_editor.new_item_ids); updates hitting such ids land in missing -> KeyError fail-loud.
            # 建立 scene_id → item 索引，避免 O(N*M) 查找。损坏脚本的非 dict 元素过滤掉
            # （镜像 script_editor.new_item_ids），命中这类 id 的 update 会落 missing → KeyError fail-loud。
            item_by_id: dict[str, dict] = {str(item.get(id_field)): item for item in items if isinstance(item, dict)}
            missing: list[str] = []

            for scene_id, asset_type, asset_path in updates:
                item = item_by_id.get(str(scene_id))
                if item is None:
                    missing.append(str(scene_id))
                    continue

                assets = item.get("generated_assets")
                if not isinstance(assets, dict):
                    assets = {}
                    item["generated_assets"] = assets

                assets_template = self.create_generated_assets(content_mode)
                for key, default_value in assets_template.items():
                    if key not in assets:
                        assets[key] = default_value

                assets[asset_type] = asset_path
                self.update_scene_status(item)

            if missing:
                raise KeyError(f"Batch write-back failed: these storyboard items do not exist {sorted(set(missing))}")
        return script

    def get_pending_scenes(self, project_name: str, script_filename: str, asset_type: str) -> list[dict]:
        """
        Get the list of pending shots

        Args:
            project_name: project name
            script_filename: script file name
            asset_type: resource type

        Returns:
            list of pending shots

        获取待处理的分镜列表

        Args:
            project_name: 项目名称
            script_filename: 剧本文件名
            asset_type: 资源类型

        Returns:
            待处理分镜列表
        """
        script = self.load_script(project_name, script_filename)

        # `_resolve_items_or_warn` discriminates the three script structures uniformly and degrades dirty data with
        # warn-and-skip -- silence on the read side is safer than silence on the write side (rendering an empty list in the
        # UI beats a 5xx blocking the page), but the warning gives an observable signal; the write side
        # (update_scene_asset / batch_update_scene_assets) uses `resolve_items` to raise ScriptEditError directly, ensuring
        # data corruption always has an explicit signal. Reference-to-video correctly returns video_units too, not silently
        # falling into the drama fallback and losing reference-to-video data.
        # `_resolve_items_or_warn` 对三种剧本结构统一判别，并对脏数据 warn-and-skip 降级——读取侧 silent
        # 比写入侧 silent 安全（UI 渲染空列表好过 5xx 阻塞页面），但 warning 给可观测信号；
        # 写入侧（update_scene_asset / batch_update_scene_assets）则用 `resolve_items` 直接
        # 抛 ScriptEditError 保证数据损坏永远有显式信号。参考生视频也能正确返回
        # video_units，不会静默落到 drama 兜底丢失参考生视频数据。
        items = _resolve_items_or_warn(script, script_filename=script_filename)

        # item.generated_assets missing / null / non-dict all count as "not generated" -- dirty-data tolerance on the read
        # side, normalized via get_generated_assets, mirroring the isinstance check of the write-side update_scene_asset.
        # item.generated_assets 缺失 / null / 非 dict 一律视为"未生成"——读取侧脏数据容错，
        # 经 get_generated_assets 归一化，与写入侧 update_scene_asset 的 isinstance check mirror。
        def _missing(item: dict) -> bool:
            return not get_generated_assets(item).get(asset_type)

        # Non-dict elements of a corrupted script are dropped outright (mirroring the filter of script_editor.new_item_ids),
        # so the UI doesn't render junk items.
        # 损坏脚本的非 dict 元素直接剔除（镜像 script_editor.new_item_ids 的过滤），UI 不渲染垃圾项。
        return [item for item in items if isinstance(item, dict) and _missing(item)]

    # ==================== File path tools ==================== / ==================== 文件路径工具 ====================

    def get_source_path(self, project_name: str, filename: str) -> Path:
        """Get the source file path / 获取源文件路径"""
        return self.get_project_path(project_name) / "source" / filename

    def get_character_path(self, project_name: str, filename: str) -> Path:
        """Get the character sheet path / 获取角色资产图路径"""
        return self._get_asset_path("character", project_name, filename)

    def get_storyboard_path(self, project_name: str, filename: str) -> Path:
        """Get the storyboard image path / 获取分镜图片路径"""
        return self.get_project_path(project_name) / "storyboards" / filename

    def get_video_path(self, project_name: str, filename: str) -> Path:
        """Get the video path / 获取视频路径"""
        return self.get_project_path(project_name) / "videos" / filename

    def get_output_path(self, project_name: str, filename: str) -> Path:
        """Get the output path / 获取输出路径"""
        return self.get_project_path(project_name) / "output" / filename

    def get_scenes_needing_storyboard(self, project_name: str, script_filename: str) -> list[dict]:
        """
        Get the list of shots needing storyboard images (unified logic for both modes)

        Args:
            project_name: project name
            script_filename: script file name

        Returns:
            list of shots needing storyboard images

        获取需要生成分镜图的分镜列表（两种模式统一逻辑）

        Args:
            project_name: 项目名称
            script_filename: 剧本文件名

        Returns:
            需要生成分镜图的分镜列表
        """
        script = self.load_script(project_name, script_filename)

        # Same as get_pending_scenes: resolve_items three-mode discrimination + warn-and-skip degradation +
        # get_generated_assets normalization tolerance.
        # 同 get_pending_scenes：resolve_items 三模式判别 + warn-and-skip 降级 +
        # get_generated_assets 归一化容错。
        items = _resolve_items_or_warn(script, script_filename=script_filename)

        def _missing_storyboard(item: dict) -> bool:
            return not get_generated_assets(item).get("storyboard_image")

        # Same as get_pending_scenes: drop non-dict elements, mirroring script_editor.new_item_ids.
        # 同 get_pending_scenes：非 dict 元素剔除，镜像 script_editor.new_item_ids。
        return [item for item in items if isinstance(item, dict) and _missing_storyboard(item)]

    # ==================== Project-level metadata management ==================== / ==================== 项目级元数据管理 ====================

    def _get_project_file_path(self, project_name: str) -> Path:
        """Get the project metadata file path / 获取项目元数据文件路径"""
        return self.get_project_path(project_name) / self.PROJECT_FILE

    def project_exists(self, project_name: str) -> bool:
        """Check whether the project metadata file exists / 检查项目元数据文件是否存在"""
        try:
            return self._get_project_file_path(project_name).exists()
        except FileNotFoundError:
            return False

    def load_project(self, project_name: str) -> dict:
        """
        Load project metadata: no lock, no migration, no write; returns an in-memory snapshot of project.json

        Args:
            project_name: project name

        Returns:
            project metadata dict

        加载项目元数据：不取锁、不迁移、不写盘，返回 project.json 的内存快照

        Args:
            project_name: 项目名称

        Returns:
            项目元数据字典
        """
        project_file = self._get_project_file_path(project_name)

        if not project_file.exists():
            raise FileNotFoundError(f"Project metadata file does not exist: {project_file}")

        with open(project_file, encoding="utf-8") as f:
            return json.load(f)

    @contextmanager
    def _project_lock(self, project_name: str):
        """Acquire an exclusive lock on the project file via a hidden lock file.

        Uses a separate .project.json.lock rather than the data file itself, avoiding the lock becoming invalid after
        os.replace swaps the inode.

        通过隐藏 lock file 获取项目文件的排他锁。

        使用独立的 .project.json.lock 而非数据文件本身，避免 os.replace
        更换 inode 后锁失效的问题。
        """
        project_file = self._get_project_file_path(project_name)
        with project_metadata_lock(project_file.parent):
            yield

    @contextmanager
    def locked_source_mutation(self, project_name: str) -> Generator[Path]:
        """Serialize source-file mutations with project transactions.

        Source writers must use this context so a source-file change and its matching
        project.json commit observe one immutable source snapshot.
        """
        project_path = self.get_project_path(project_name)
        with self._project_lock(project_name):
            source_dir = project_path / "source"
            if source_dir.is_symlink() or source_dir.is_junction():
                raise ValueError("The source directory must not be a symlink or junction")
            source_dir.mkdir(parents=True, exist_ok=True)
            yield source_dir

    @contextmanager
    def locked_source_registration(self, project_name: str) -> Generator[tuple[Path, dict, ExitStack]]:
        """Registration variant of :meth:`locked_source_mutation`: source files and their registration in project.json are
        changed under the same project lock.

        Yields ``(source_dir, project, undo)``; the block modifies ``project`` in place (whole-source list, episode ledger);
        on normal exit it first re-records from on-disk source whether source text remains unplanned, and writes back
        ``project.json`` if ``project`` changed. For each on-disk file the caller changes, it registers an undo callback in
        ``undo``; if the block raises or writing back ``project.json`` fails, nothing is written back and these callbacks
        run under the lock in reverse registration order.

        :meth:`locked_source_mutation` 的登记变体：源文文件与 project.json 的登记在同一把项目锁内改。

        产出 ``(source_dir, project, undo)``；块内就地修改 ``project``（整本源文清单、分集账本），块正常
        退出时先按盘上源文重记源文是否还有未规划的原文，``project`` 有变化时写回 ``project.json``。调用方
        每改一处盘上文件，就把它的撤销回调登记进 ``undo``；块内抛错或写回 ``project.json`` 失败时不写回，
        并在锁内按登记的逆序执行这些回调。
        """
        project_file = self._get_project_file_path(project_name)
        changed = False
        with self.locked_source_mutation(project_name) as source_dir:
            project = self._read_project_raw_unlocked(project_name)
            before = json.dumps(project, sort_keys=True, ensure_ascii=False)
            undo = ExitStack()
            try:
                yield source_dir, project, undo
                # The block may have changed only the source text without touching the registration: whether source text remains
                # unplanned is re-recorded from on-disk source every time
                # 块内可能只改了源文文本、没动登记：源文是否还有未规划的原文每次都按盘上源文重记
                record_source_remaining(source_dir.parent, project)
                if json.dumps(project, sort_keys=True, ensure_ascii=False) != before:
                    self._apply_project_mutation_unlocked(project_name, project, lambda _project: None)
                    atomic_write_json(project_file, project)
                    changed = True
            except BaseException:
                try:
                    undo.close()
                except Exception:
                    logger.exception("撤销源文改动失败: %s", project_name)
                raise
            undo.pop_all()
        if changed:
            emit_project_change_hint(project_name, changed_paths=[self.PROJECT_FILE])

    @asynccontextmanager
    async def async_file_lock(
        self,
        path: Path,
    ) -> AsyncGenerator[None]:
        """Cancellation-safe async counterpart of :meth:`file_lock`."""
        lock_path = path.parent / f".{path.name}.lock"

        # Register the handle as soon as it is opened: if cancellation lands during the open, the return value is lost, and
        # this is what closes it.
        # 句柄一打开就登记：取消落在打开那一程时返回值会丢，靠它关闭。
        handles: list[BinaryIO] = []

        def _open_lock_file() -> BinaryIO:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
            handles.append(handle)
            return handle

        try:
            handle = await run_sync_transaction(_open_lock_file)
        except BaseException:
            for opened in handles:
                opened.close()
            raise
        acquired = False
        try:
            while not acquired:
                try:
                    portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                    acquired = True
                except portalocker.AlreadyLocked:
                    await asyncio.sleep(0.05)
            yield
        finally:
            try:
                if acquired:
                    portalocker.unlock(handle)
            finally:
                handle.close()

    @contextmanager
    def file_lock(self, path: Path):
        """Acquire an exclusive lock on any file via a hidden lock file; public so modules can share the same mutex.

        The lock file is named `.{basename}.lock` (starting with `.`), in the file's directory, same convention as
        `_project_lock` / `_script_lock`, automatically excluded by directory globs. Use it when one file has several
        read-modify-write entry points (such as the migration write-back and body save of a script_plan draft) that need
        to serialize with each other -- each entry point takes this lock on the same real path to be mutually exclusive,
        without needing to know about the others.

        通过隐藏 lock file 获取任意文件的排他锁，公开供跨模块共享同一把互斥。

        lock 文件命名为 `.{basename}.lock`（以 `.` 开头），位于该文件所在目录，
        与 `_project_lock` / `_script_lock` 同一约定，自动被目录 glob 过滤排除。
        供同一份文件存在多个读-改-写入口（如 script_plan 草稿的迁移写回与正文保存）
        且需要相互串行化时使用——各入口对同一 real path 取本锁即可互斥，不必
        知道彼此的存在。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / f".{path.name}.lock"
        lock_path.touch(exist_ok=True)
        with portalocker.Lock(lock_path, flags=portalocker.LOCK_EX):
            yield

    @contextmanager
    def _script_lock(self, project_name: str, script_filename: str):
        """Acquire an exclusive lock on a script file via a hidden lock file.

        **Key**: normalize filename with `_safe_subpath` before deriving the lock key, avoiding the aliasing problem where
        `./episode_1.json` and `episode_1.json` resolve to the same real path but get different locks, bypassing mutual
        exclusion.

        通过隐藏 lock file 获取剧本文件的排他锁。

        **关键**：用 `_safe_subpath` 规范化 filename 再派生 lock key，避免
        `./episode_1.json` 与 `episode_1.json` 解析到同一个 real path 却拿到
        不同锁、从而绕过互斥的别名问题。
        """
        scripts_dir = self.get_project_path(project_name) / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_filename = self.normalize_script_filename(script_filename)
        real = Path(self._safe_subpath(scripts_dir, script_filename))
        with self.file_lock(real):
            yield

    def save_project(self, project_name: str, project: dict) -> Path:
        """
        Save project metadata

        Args:
            project_name: project name
            project: project metadata dict

        Returns:
            saved file path

        保存项目元数据

        Args:
            project_name: 项目名称
            project: 项目元数据字典

        Returns:
            保存的文件路径
        """
        project_file = self._get_project_file_path(project_name)

        self._migrate_legacy_resolution_on_save(project)
        self._touch_metadata(project)

        with self._project_lock(project_name):
            if self._requires_unique_asset_namespace(project):
                ensure_project_asset_namespace(project)
            atomic_write_json(project_file, project)

        emit_project_change_hint(
            project_name,
            changed_paths=[self.PROJECT_FILE],
        )

        return project_file

    def update_project(
        self,
        project_name: str,
        mutate_fn: Callable[[dict], None],
        *,
        on_commit: Callable[[Path], None] | None = None,
        formal_paths: Sequence[Path] = (),
    ) -> dict:
        """Update project.json atomically: file lock -> read -> modify -> atomic write-back.

        Avoids lost-update races between concurrent tasks (e.g. generating several character images at once). Returns the
        written-back project metadata dict, so the caller needn't load_project again.

        Args:
            project_name: project name
            mutate_fn: callback receiving the project dict and modifying it in place

        Returns:
            written-back project metadata dict (same structure as load_project returns)

        原子性地更新 project.json：加文件锁 → 读 → 修改 → 原子写回。

        避免并发任务（如同时生成多张角色图片）之间的 lost-update 竞态。
        返回写回后的项目元数据 dict，调用方无需再 load_project 一次。

        Args:
            project_name: 项目名称
            mutate_fn: 接收 project dict 并就地修改的回调函数

        Returns:
            写回后的项目元数据字典（与 load_project 返回结构一致）
        """
        project_file = self._get_project_file_path(project_name)

        with self._project_lock(project_name), ExitStack() as transaction:
            if on_commit is not None or formal_paths:
                transaction.enter_context(formal_write_transaction(project_file, *formal_paths))
            project = self.update_project_locked(project_name, mutate_fn)
            if on_commit is not None:
                on_commit(project_file)

        emit_project_change_hint(
            project_name,
            changed_paths=[self.PROJECT_FILE],
        )

        return project

    def update_project_locked(self, project_name: str, mutate_fn: Callable[[dict], None]) -> dict:
        """The in-lock step of :meth:`update_project`: the caller already holds the project lock (non-reentrant), e.g. the
        restore pipeline's landing rules.

        Read, modify, normalize, then atomically write back and return the written-back project metadata; no rollback on
        failure, no change hints.

        :meth:`update_project` 的锁内一步：调用方已持有项目锁（不可重入），如恢复流水线的落地规则。

        读、改、规整后原子写回并返回写回的项目元数据；不做失败回滚，也不发变更提示。
        """
        project = self._read_project_raw_unlocked(project_name)
        self._apply_project_mutation_unlocked(project_name, project, mutate_fn)
        atomic_write_json(self._get_project_file_path(project_name), project)
        return project

    def update_asset_entry(
        self,
        asset_type: str,
        project_name: str,
        name: str,
        mutate_fn: Callable[[dict], None],
        *,
        on_commit: Callable[[Path], None] | None = None,
        formal_paths: Sequence[Path] = (),
    ) -> dict:
        """Update one asset and reconcile a moved formal sheet claim.

        Every asset router shares this project-lock/formal-write boundary.  A
        metadata-only input change keeps the current sheet claim so currency
        comparison can report it stale; clearing or replacing the canonical
        sheet path removes the old claim in the same durable commit.

        ``on_commit`` runs inside that same commit, after the sheet claim is
        reconciled, so a mutation that also retires sub-resources (a derivative
        and its image) can retire their claims without a second transaction.
        """

        from lib.artifacts.artifact_activation import reconcile_artifact_target_claims
        from lib.artifacts.artifact_manifest import ArtifactKey

        spec = ASSET_SPECS[asset_type]
        project_dir = self.get_project_path(project_name)
        canonical_name: str | None = None
        sheet_path_changed = False
        result: dict[str, Any] = {}

        def _mutate(project: dict) -> None:
            nonlocal canonical_name, sheet_path_changed
            bucket = project.get(spec.bucket_key)
            key = resolve_asset_key(bucket, name)
            if not isinstance(bucket, dict) or key is None:
                raise KeyError(name)
            entry = bucket[key]
            if not isinstance(entry, dict):
                raise ValueError(f"project asset {spec.bucket_key}/{key} must be an object")
            previous_sheet_path = entry.get(spec.sheet_field)
            mutate_fn(entry)
            canonical_name = key
            sheet_path_changed = entry.get(spec.sheet_field) != previous_sheet_path
            result.update(entry)

        def _reconcile_claim(_project_file: Path) -> None:
            if not sheet_path_changed:
                return
            if canonical_name is None:  # pragma: no cover - mutation contract
                raise RuntimeError("asset update did not resolve a canonical identity")
            reconcile_artifact_target_claims(
                project_dir,
                (ArtifactKey.asset_sheet(asset_type, canonical_name),),
            )

        def _commit(project_file: Path) -> None:
            _reconcile_claim(project_file)
            if on_commit is not None:
                on_commit(project_file)

        self.update_project(project_name, _mutate, on_commit=_commit, formal_paths=formal_paths)
        return result

    def update_project_reconciling_episode_bindings(
        self,
        project_name: str,
        mutate_fn: Callable[[dict], None],
    ) -> dict:
        """Update project metadata and forget claims unowned after rebinding.

        The selected script is the ownership boundary for every episode-scoped
        formal artifact.  When a mutation changes that binding, reconcile all
        existing claims for the affected episode through the canonical target
        resolver and one Manifest compare-and-swap.
        """

        from lib.artifacts.artifact_activation import reconcile_artifact_target_claims
        from lib.artifacts.artifact_manifest import ProjectArtifactManifestAdapter

        project_dir = self.get_project_path(project_name)
        changed_episodes: set[int] = set()

        def _bindings(project: Mapping[str, Any]) -> dict[int, str | None]:
            raw_episodes = project.get("episodes")
            if not isinstance(raw_episodes, list):
                return {}
            bindings: dict[int, str | None] = {}
            for raw_episode in raw_episodes:
                if not isinstance(raw_episode, Mapping):
                    continue
                episode = raw_episode.get("episode")
                if type(episode) is not int or episode < 1:
                    continue
                script_file = raw_episode.get("script_file")
                bindings[episode] = (
                    self.normalize_script_filename(script_file)
                    if isinstance(script_file, str) and script_file
                    else None
                )
            return bindings

        def _mutate(project: dict) -> None:
            before = _bindings(project)
            mutate_fn(project)
            after = _bindings(project)
            changed_episodes.update(
                episode for episode in before.keys() | after.keys() if before.get(episode) != after.get(episode)
            )

        def _reconcile_claims(_project_file: Path) -> None:
            if not changed_episodes:
                return
            adapter = ProjectArtifactManifestAdapter(project_dir)
            claimed_keys = tuple(key for key in adapter.snapshot_entries() if key.episode_number in changed_episodes)
            reconcile_artifact_target_claims(project_dir, claimed_keys, adapter=adapter)

        return self.update_project(project_name, _mutate, on_commit=_reconcile_claims)

    def _apply_project_mutation_unlocked(
        self, project_name: str, project: dict, mutate_fn: Callable[[dict], None]
    ) -> None:
        """Apply one mutation plus the canonical save-time normalizations.

        The caller owns the project lock and is responsible for the durable
        write. Keeping this sequence shared lets compound file transactions
        make their final existence decision and project mutation under one
        lock without re-entering :meth:`update_project`.
        """

        if self._requires_unique_asset_namespace(project):
            ensure_project_asset_namespace(project)
        # The change may move entries out of the ledger: first let the historical highest number record the episode IDs
        # before the change
        # 变更可能把条目移出账本：先让历史最高号记下变更前的集 ID
        raise_episode_id_high_water(project)
        planning_inputs = source_planning_inputs(project)
        mutate_fn(project)
        # Episode planning, manual splitting, adopting a replan, reset and episode deletion all change the ledger through
        # here: when the ledger or source registration changed, re-record from on-disk source whether source text remains
        # unplanned; the project list reads that instead of the source
        # 分集规划、手工切分、重新规划采纳、重置与删集都经由这里改账本：账本或源文登记变了，
        # 就按盘上源文重记源文是否还有未规划的原文，项目列表读它而不读源文
        if source_planning_inputs(project) != planning_inputs:
            record_source_remaining(self.get_project_path(project_name), project)
        if self._requires_unique_asset_namespace(project):
            ensure_project_asset_namespace(project)
        self._migrate_legacy_resolution_on_save(project)
        self._touch_metadata(project)

    def update_project_with_file_copies(
        self,
        project_name: str,
        mutate_fn: Callable[[dict], None],
        copies: list[tuple[Path, Path]],
        *,
        on_commit: Callable[[Path], None] | None = None,
    ) -> dict:
        """Commit file copies, project.json and an optional sidecar hook as one transaction under the project lock.

        在项目锁内把文件复制、project.json 与可选 sidecar hook 作为一个事务提交。"""

        return self._update_project_with_files(
            project_name,
            mutate_fn,
            copies=copies,
            on_commit=on_commit,
        )

    def _update_project_with_files(
        self,
        project_name: str,
        mutate_fn: Callable[[dict], None],
        *,
        copies: list[tuple[Path, Path]] | None = None,
        writes: list[tuple[bytes, Path]] | None = None,
        on_commit: Callable[[Path], None] | None = None,
    ) -> dict:
        """Commit file changes and the project.json write-back as one rollback-able transaction under the project lock.

        ``mutate_fn`` may finish final name planning under the lock and append operations to both lists. If the callback
        raises, no files are installed; all copies/byte writes are staged first, then targets are replaced one by one. If
        installation or the JSON write-back fails, restore in reverse order; restore failures are only logged and the
        original exception kept; backups are cleaned up after a successful commit. All targets must be distinct, to avoid
        ambiguity from operation order within the same transaction.

        在项目锁内把文件变更与 project.json 写回作为一个可回滚事务提交。

        ``mutate_fn`` 可在锁内完成最终名称规划并向两个列表追加操作。回调抛错时不会安装
        任何文件；所有复制/字节写入先完成暂存，再逐个替换目标。安装或 JSON 写回失败时按
        相反顺序恢复，恢复失败仅记录日志并保留原始异常；提交成功后清理备份。全部目标必须
        互不重复，避免同一事务内操作顺序产生歧义。
        """
        project_file = self._get_project_file_path(project_name)
        copies = copies if copies is not None else []
        writes = writes if writes is not None else []

        token = secrets.token_hex(8)
        staged: list[tuple[Path, Path]] = []
        installed: list[tuple[Path, Path | None]] = []
        committed = False
        with self._project_lock(project_name), ExitStack() as transaction:
            if on_commit is not None:
                transaction.enter_context(formal_write_transaction(project_file))
            try:
                with open(project_file, encoding="utf-8") as f:
                    project = json.load(f)
                if self._requires_unique_asset_namespace(project):
                    ensure_project_asset_namespace(project)
                mutate_fn(project)
                if self._requires_unique_asset_namespace(project):
                    ensure_project_asset_namespace(project)
                self._migrate_legacy_resolution_on_save(project)
                self._touch_metadata(project)

                # mutate_fn may finish final name planning and fill copies under the lock; so target uniqueness must also be checked
                # after the callback, while still holding the same project lock.
                # mutate_fn 可在锁内完成最终名称规划并填充 copies；因此目标唯一性也必须
                # 在回调之后、仍持有同一把项目锁时校验。
                replacement_destinations = [destination for _source, destination in copies] + [
                    destination for _content, destination in writes
                ]
                if len(set(replacement_destinations)) != len(replacement_destinations):
                    raise ValueError("The project file transaction contains duplicate target paths")
                for index, (source, destination) in enumerate(copies):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_name(f".{destination.name}.{token}-{index}.tmp")
                    shutil.copyfile(source, temporary)
                    staged.append((temporary, destination))
                for offset, (content, destination) in enumerate(writes, start=len(staged)):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_name(f".{destination.name}.{token}-{offset}.tmp")
                    atomic_write_bytes(temporary, content)
                    staged.append((temporary, destination))

                for index, (temporary, destination) in enumerate(staged):
                    backup: Path | None = None
                    if destination.exists() or destination.is_symlink():
                        backup = destination.with_name(f".{destination.name}.{token}-{index}.bak")
                        os.replace(destination, backup)
                    installed.append((destination, backup))
                    os.replace(temporary, destination)

                atomic_write_json(project_file, project)
                if on_commit is not None:
                    on_commit(project_file)
                committed = True
            except BaseException:
                for destination, backup in reversed(installed):
                    try:
                        if destination.exists() or destination.is_symlink():
                            # remove the files newly installed this time when the write fails
                            destination.unlink()  # hard-delete: rollback -- 写入失败时移除本次新装的文件
                        if backup is not None:
                            os.replace(backup, destination)
                    except OSError:
                        logger.exception("恢复项目文件事务失败: %s", destination)
                raise
            finally:
                for temporary, _destination in staged:
                    try:
                        temporary.unlink(missing_ok=True)  # hard-delete: temp -- staged write file / 写入暂存文件
                    except OSError:
                        logger.warning("清理项目文件事务暂存失败: %s", temporary)
                if committed:
                    for _destination, backup in installed:
                        if backup is None:
                            continue
                        try:
                            backup.unlink(missing_ok=True)  # hard-delete: temp -- .bak backup after commit / 提交后的 .bak 备份
                        except OSError:
                            logger.warning("清理项目文件事务备份失败: %s", backup)

        emit_project_change_hint(
            project_name,
            changed_paths=[self.PROJECT_FILE],
        )
        return project

    @staticmethod
    def _touch_metadata(project: dict) -> None:
        """Finishing step before every project.json write: refresh the update time, and let the historical highest number
        cover the episode IDs in the ledger.

        每次写 project.json 前的收尾：刷新更新时间，并让历史最高号覆盖账本里的集 ID。"""
        raise_episode_id_high_water(project)
        now = datetime.now(UTC).isoformat()
        if "metadata" not in project:
            project["metadata"] = {"created_at": now, "updated_at": now}
        else:
            project["metadata"]["updated_at"] = now

    @staticmethod
    def _requires_unique_asset_namespace(project: dict) -> bool:
        """From schema v6, ordinary business writes may no longer operate on a corrupted asset name space.

        schema v6 起禁止普通业务写继续操作已损坏的资产名称空间。"""
        return parse_project_schema_version(project) >= 6

    @staticmethod
    def _migrate_legacy_resolution_on_save(project: dict) -> None:
        """If project.model_settings contains resolution, clear the matching legacy entries in video_model_settings.

        Rule: for each new model_settings key (shaped "<provider>/<model>"), if its resolution is set, remove the
        resolution field from video_model_settings[<model>]; delete that key if the entry becomes empty; delete
        video_model_settings entirely when the legacy dict becomes empty.

        若 project.model_settings 含 resolution，清除 video_model_settings 中命中的 legacy 条目。

        规则：对每个 new model_settings key（形如 "<provider>/<model>"），若其 resolution 已设置，
        则从 video_model_settings[<model>] 中移除 resolution 字段；如该条目变空则删除该 key；
        legacy dict 变空时整体删除 video_model_settings。
        """
        model_settings = project.get("model_settings") or {}
        legacy = project.get("video_model_settings") or {}
        if not model_settings or not legacy:
            return
        for composite_key, entry in model_settings.items():
            if "/" not in composite_key:
                continue
            _, model_id = composite_key.split("/", 1)
            if not entry.get("resolution"):
                continue
            legacy_entry = legacy.get(model_id)
            if not legacy_entry:
                continue
            legacy_entry.pop("resolution", None)
            if not legacy_entry:
                legacy.pop(model_id, None)
        if not legacy:
            project.pop("video_model_settings", None)

    # Ad/short projects are always single-episode: episodes is always a single entry for episode 1, and the script is
    # episode 1's script file
    # 广告/短片项目恒单集：episodes 恒为第 1 集单条，剧本即第 1 集脚本文件
    AD_SINGLE_EPISODE: ClassVar[dict[str, str | int]] = {
        "episode": 1,
        "title": "",
        "script_file": episode_script_relpath(1),
        SOURCE_ORIGIN_FIELD: SourceOrigin.NONE.value,
    }
    # Data-layer fallback when the create entry point didn't pass target_duration (same value as the creation wizard's
    # default option)
    # 创建入口未传 target_duration 时的数据层兜底（与创建向导默认档位同值）
    AD_DEFAULT_TARGET_DURATION = 60

    def create_project_metadata(
        self,
        project_name: str,
        title: str | None = None,
        style: str | None = None,
        content_mode: str | None = "narration",
        aspect_ratio: str | None = "9:16",
        default_duration: int | None = None,
        episode_target_duration: int | None = None,
        style_template_id: str | None = None,
        extras: dict | None = None,
        target_duration: int | None = None,
        brief: str | None = None,
        narration: Mapping[str, object] | None = None,
    ) -> dict:
        """
        Create a new project metadata file

        `extras` is for writing optional model/backend fields (e.g. video_backend / image_provider_t2i /
        image_provider_i2i / text_backend_{script,overview,style}). The caller is responsible for removing empty values;
        this method only writes the keys present in extras literally -- the retired single field image_backend is out of
        scope (the resolution chain doesn't read it and the write boundary already rejects it), so callers shouldn't pass it.

        `target_duration` / `brief` are only available with content_mode=ad; ad projects don't hold `default_duration` or
        `episode_target_duration`, and episodes is always a single entry for episode 1.

        `episode_target_duration` is the per-episode target duration (seconds), with the range given in
        `lib.episode.episode_target_duration`; a soft preference, only injected into the script plan prompt and the review
        panel comparison, never blocking.

        `narration` is the narration delivery config field (delivery method and TTS snapshot, see
        `lib.speech.narration_config`), defaulting to post-production voiceover; TTS voiceover must carry a complete
        snapshot.

        创建新的项目元数据文件

        `extras` 用于写入可选的模型/后端等字段（如 video_backend / image_provider_t2i /
        image_provider_i2i / text_backend_{script,overview,style}）。调用方负责剔除空值，
        本方法只按字面写入 extras 中已有的键——退役的单字段 image_backend 不在写入范围
        （解析链不读取、写边界已拒绝），调用方不应再传入。

        `target_duration` / `brief` 仅 content_mode=ad 可用；ad 项目不持有
        `default_duration` 与 `episode_target_duration`，且 episodes 恒为第 1 集单条。

        `episode_target_duration` 为单集目标时长（秒），取值区间见
        `lib.episode.episode_target_duration`；软偏好，只注入脚本规划提示词与审核面板对比，不做阻断。

        `narration` 是旁白交付配置字段（交付方式与 TTS 快照，见 `lib.speech.narration_config`），
        缺省为后期配音；TTS 配音必须带完整快照。
        """
        project_name = self.normalize_project_name(project_name)
        project_title = str(title).strip() if title is not None else ""
        resolved_mode = content_mode or "narration"

        # Data-layer guard: mode-specific fields are mutually exclusive. The routing layer already returns 400; this is an
        # extra backstop for non-route callers.
        # 数据层守卫：模式专属字段互斥。路由层已返回 400，这里再兜一道防非路由调用方。
        if resolved_mode == "ad":
            if default_duration is not None:
                raise ValueError("Ad/short projects do not hold default_duration "
                    "(shot durations are planned per shot from the target_duration budget)")
            if episode_target_duration is not None:
                raise ValueError("Ad/short projects do not hold episode_target_duration "
                    "(the episode size is planned from the target_duration budget)")
            if target_duration is not None and not is_int(target_duration, minimum=1):
                raise ValueError(f"target_duration must be a positive integer of seconds, got {target_duration!r}")
            if brief is not None and not is_str(brief):
                raise ValueError(f"brief must be a string, got {brief!r}")
        else:
            if target_duration is not None:
                raise ValueError("target_duration is only available for ad/short projects (content_mode=ad)")
            if brief is not None:
                raise ValueError("brief is only available for ad/short projects (content_mode=ad)")

        # schema_version aligns with CURRENT_SCHEMA_VERSION: a new project is already in the latest shape, so startup
        # migration doesn't mishandle it (e.g. v0->v1 wrongly clearing scenes/props when "no clues field").
        # schema_version 与 CURRENT_SCHEMA_VERSION 对齐：新项目即最新形态，
        # 避免被启动迁移误处理（如 v0→v1 在"未含 clues 字段"时误清空 scenes/props）。
        from lib.project.project_migrations import CURRENT_SCHEMA_VERSION

        project = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            # Empty string allowed: the frontend falls back to displaying the i18n "Untitled project", avoiding freezing a
            # slug-style project_name as the user-visible title.
            # 允许空字符串:前端会以 i18n「未命名项目」兜底显示,避免把 slug
            # 风格的 project_name 固化为用户可见的标题。
            "title": project_title,
            "content_mode": resolved_mode,
            "aspect_ratio": aspect_ratio or "9:16",
            "style": style or "",
            "episodes": [],
            WHOLE_SOURCE_FILES_KEY: [],
            "characters": {},
            "scenes": {},
            "props": {},
            NARRATION_DELIVERY_FIELD: POST_PRODUCTION,
            "metadata": {
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        }
        if narration is not None:
            unknown = set(narration) - set(NARRATION_CONFIG_FIELDS)
            if unknown:
                raise ValueError(f"narration only accepts narration delivery settings fields, got: {sorted(unknown)}")
            project.update(narration)
            validate_project_narration_config(project)
        if resolved_mode == "ad":
            project["target_duration"] = (
                target_duration if target_duration is not None else self.AD_DEFAULT_TARGET_DURATION
            )
            project["brief"] = brief if brief is not None else ""
            project["episodes"] = [dict(self.AD_SINGLE_EPISODE)]
        if default_duration is not None:
            project["default_duration"] = default_duration
        if episode_target_duration is not None:
            if not is_valid_episode_target_duration(episode_target_duration):
                raise ValueError(
                    f"episode_target_duration must be an integer of {MIN_EPISODE_TARGET_DURATION}–"
                    f"{MAX_EPISODE_TARGET_DURATION} seconds, got {episode_target_duration!r}"
                )
            project[EPISODE_TARGET_DURATION_FIELD] = episode_target_duration
        if style_template_id is not None:
            project["style_template_id"] = style_template_id
        if extras:
            # Data-layer guard: the retired single field image_backend must not be written (the resolution chain doesn't read it,
            # and writing it back would only recreate the silently ignored legacy shape). The routing layer already returns 400;
            # this is an extra backstop for non-route callers.
            # 数据层守卫：退役的单字段 image_backend 不得写入（解析链不读取，写回只会
            # 重新制造被静默忽略的 legacy 形态）。路由层已返回 400，这里再兜一道防非路由调用方。
            if "image_backend" in extras:
                raise ValueError("image_backend is deprecated; use image_provider_t2i / image_provider_i2i instead")
            # extras may only append optional fields and must not overwrite the core fields validated/built above -- otherwise
            # non-route callers could bypass the mode mutual-exclusion guard via extras (e.g. an ad project writing back
            # default_duration).
            # extras 只许追加可选字段，不得覆盖上方已校验/已构造的核心字段——
            # 否则非路由调用方可借 extras 绕过模式互斥守卫（如 ad 项目写回 default_duration）。
            reserved = set(project) | {
                "default_duration",
                EPISODE_TARGET_DURATION_FIELD,
                "style_template_id",
                "target_duration",
                "brief",
            }
            forbidden = reserved & set(extras)
            if forbidden:
                raise ValueError(f"extras may not override core fields: {sorted(forbidden)}")
            project.update(extras)

        # Generation mode and grid switch: the routing layer already enforces the required binary and the ad mutual exclusion
        # (400/422); this is an extra backstop for non-route callers; when not passed, explicit values are back-filled from
        # the data-layer defaults, so a new project has both fields on disk from the start (aligned with the schema v5 shape).
        # 生成模式与宫格开关：路由层已做必填二值校验与 ad 互斥（400/422），这里再兜一道防非路由
        # 调用方；未传时按数据层默认补写显式值，保证新项目落盘即含两字段（与 schema v5 形态对齐）。
        generation_mode = project.setdefault("generation_mode", _DEFAULT_GENERATION_MODE)
        if not isinstance(generation_mode, str) or generation_mode not in VALID_GENERATION_MODES:
            raise ValueError(
                f"Invalid generation_mode value: {generation_mode!r}, must be one of {sorted(VALID_GENERATION_MODES)}",
            )
        grid_storyboard = project.setdefault("grid_storyboard", False)
        if not isinstance(grid_storyboard, bool):
            raise ValueError(f"grid_storyboard must be a boolean, got {grid_storyboard!r}")
        if resolved_mode == "ad" and grid_storyboard:
            raise ValueError("Ad/short projects do not support grid storyboards (grid_storyboard)")

        self.save_project(project_name, project)
        return project

    def add_episode(self, project_name: str, episode: int, title: str, script_file: str) -> dict:
        """
        Add an episode to the project

        Args:
            project_name: project name
            episode: episode number
            title: episode title
            script_file: script file relative path

        Returns:
            updated project metadata

        向项目添加剧集

        Args:
            project_name: 项目名称
            episode: 集数
            title: 剧集标题
            script_file: 剧本文件相对路径

        Returns:
            更新后的项目元数据
        """

        def _mutate(project: dict) -> None:
            # Update if it exists, otherwise append (the whole RMW happens within a single _project_lock)
            # 已存在则更新，否则追加（整段 RMW 在单一 _project_lock 内完成）
            for ep in project["episodes"]:
                if ep["episode"] == episode:
                    ep["title"] = title
                    ep["script_file"] = script_file
                    return
            # A new episode goes at the end of the episode order (excluding statistics fields, computed at read time by the
            # project summary)
            # 新剧集接在播出顺序末尾（不包含统计字段，由项目摘要读时计算）
            project["episodes"].append({"episode": episode, "title": title, "script_file": script_file})

        return self.update_project(project_name, _mutate)

    def sync_project_status(self, project_name: str) -> dict:
        """
        [Deprecated] Sync project status

        This method is deprecated. Statistics fields such as status, progress and item_count are computed at read time by
        the project summary and not stored in the JSON file.

        Kept only for backward compatibility; it performs no writes.

        Args:
            project_name: project name

        Returns:
            project metadata (without statistics fields, which are injected by the project summary)

        [已废弃] 同步项目状态

        此方法已废弃。status、progress、item_count 等统计字段
        由项目摘要读时计算，不存储在 JSON 文件中。

        保留此方法仅为向后兼容，实际不执行任何写入操作。

        Args:
            project_name: 项目名称

        Returns:
            项目元数据（不含统计字段，统计字段由项目摘要注入）
        """
        import warnings

        warnings.warn(
            "sync_project_status() is deprecated. status and other statistics fields are now computed when the "
            "project summary is read.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Only return project data, perform no writes / 仅返回项目数据，不执行任何写入
        return self.load_project(project_name)

    # ================ Unified project-level asset API (character / scene / prop / product) ================
    #
    # The 6 private methods in this section are driven by lib.project.asset_types.ASSET_SPECS and uniformly handle
    # bucket-level reads/writes of the character / scene / prop project-level asset types. The public methods below
    # (add_project_scene / add_prop / get_scene / update_*_sheet etc.) all delegate to these private methods, with
    # signatures and exceptions 100% compatible with old callers.
    # ================ 项目级资产统一 API（character / scene / prop / product） ================
    #
    # 这一节的 6 个私有方法按 lib.project.asset_types.ASSET_SPECS 驱动，统一处理 character /
    # scene / prop 三类项目级资产的桶级读写。下方的 public 方法（add_project_scene /
    # add_prop / get_scene / update_*_sheet 等）全部委托给这些私有方法，签名与异常
    # 100% 兼容旧调用方。

    def _add_asset(self, asset_type: str, project_name: str, name: str, entry: dict) -> bool:
        """Add an entry to project[bucket][name]. Returns False when one of the same type already exists.

        Completes the read-modify-write within a single file lock via update_project, avoiding lost-update races on
        concurrent adds.

        Raises:
            ProjectAssetNameConflictError: the name is already taken by another asset type, or differs from an existing
                asset only in case.

        新增 entry 到 project[bucket][name]。同类型已存在时返回 False。

        通过 update_project 在单一文件锁内完成 read-modify-write，避免并发新增时的
        lost-update 竞态。

        Raises:
            ProjectAssetNameConflictError: 名称已被其它资产类型占用，或与已有资产只差大小写。
        """
        name = validate_asset_name(name)
        spec = ASSET_SPECS[asset_type]
        added = False

        def _mutate(project):
            nonlocal added
            bucket = project.setdefault(spec.bucket_key, {})
            # Existing keys may be NFD; check conflicts after resolving by coordinates, to avoid a second visually identical
            # asset
            # 存量 key 可能是 NFD，按坐标系解析后判冲突，避免视觉同名的第二条资产
            if resolve_asset_key(bucket, name) is not None:
                logger.debug("%s '%s' 已存在于 project.json，跳过", spec.label_zh, name)
                return
            ensure_project_asset_name_available(project, name, requested_asset_type=asset_type)
            bucket[name] = entry
            added = True

        self.update_project(project_name, _mutate)
        if added:
            logger.info("添加%s: %s", spec.label_zh, name)
        return added

    def _add_assets_batch(self, asset_type: str, project_name: str, entries: dict[str, dict]) -> int:
        """Batch-add entries. Existing names are skipped; returns the number added.

        Completes the read-modify-write within a single file lock via update_project, avoiding lost-update races on
        concurrent batch adds. If any name conflicts with another asset type, nothing in the batch is written.

        Raises:
            ProjectAssetNameConflictError: any name is already taken by another asset type, or differs from an existing
                asset only in case.

        批量新增 entries。已存在的 name 跳过，返回新增数量。

        通过 update_project 在单一文件锁内完成 read-modify-write，避免并发批量新增时
        的 lost-update 竞态。任一名称与其它资产类型冲突时整批不落盘。

        Raises:
            ProjectAssetNameConflictError: 任一名称已被其它资产类型占用，或与已有资产只差大小写。
        """
        spec = ASSET_SPECS[asset_type]
        # Same basis as upsert_assets: keys equivalent after normalization (strip + NFC) ({"李白", "  李白  "} or the
        # NFC/NFD dual forms) must not silently overwrite each other; the whole batch fails loudly with nothing written, so
        # the caller notices the collision and de-duplicates.
        # 与 upsert_assets 同口径：规范化（strip + NFC）后等价的 key（{"李白", "  李白  "}
        # 或 NFC/NFD 双形态）不允许静默互相覆盖，整批 fail-loud 不落盘，让调用方感知
        # collision 并去重。
        normalized_entries: dict[str, dict] = {}
        raw_keys_by_normalized: dict[str, str] = {}
        for raw_name, entry in entries.items():
            name = validate_asset_name(raw_name)
            if name in normalized_entries:
                raise ValueError(
                    f"{spec.bucket_key} entries contain name {name!r} that conflicts after normalization: "
                    f"raw keys {raw_keys_by_normalized[name]!r} and {raw_name!r} are equal after normalization "
                    "(strip + NFC)"
                )
            normalized_entries[name] = entry
            raw_keys_by_normalized[name] = raw_name
        entries = normalized_entries
        added = 0

        def _mutate(project):
            nonlocal added
            bucket = project.setdefault(spec.bucket_key, {})
            for name, entry in entries.items():
                if resolve_asset_key(bucket, name) is not None:
                    logger.debug("%s '%s' 已存在，跳过", spec.label_zh, name)
                    continue
                ensure_project_asset_name_available(project, name, requested_asset_type=asset_type)
                bucket[name] = entry
                added += 1
                logger.info("添加%s: %s", spec.label_zh, name)

        if entries:
            self.update_project(project_name, _mutate)
        return added

    def upsert_assets(self, project_name: str, table: str, entries: dict[str, dict]) -> dict[str, Any]:
        """Upsert assets by table (characters/scenes/props/products) + name: add if absent, update fields if present.

        Completes the read-modify-write within `update_project`'s single file lock; after applying and before writing, the
        resulting project dict is structurally validated at payload level and decided by **"no worse" semantics**: only
        when the upsert turns a previously valid project invalid does it raise and **write nothing** (when the mutation
        raises, `update_project` doesn't run atomic_write); if it was already invalid before (legacy dirty data, such as
        an empty `style`), it passes as usual -- otherwise projects with historical problems would find the whole
        patch_project path unusable (the old `add_assets.py` reporting validation errors didn't block writes either). Same
        origin as `_guard_no_worse` of the unified script write entry point. Extends "add only" to "can update".

        Returns a **diagnostic dict** (not project metadata): ``added`` (list of newly created entry names), ``merged``
        (list of merged existing entry names), ``dropped_fields`` (disallowed fields dropped by the whitelist,
        {name: [field names]}), ``dropped_legacy`` (removed legacy fields such as type/importance, {name: [field names]}).
        The caller (MCP tool layer) uses it to build explicit feedback for the Agent -- silent drop is the design intent
        (least privilege), but pure silence makes the Agent believe reference_image / sheet_field were written
        successfully; returning diagnostics lets the tool layer tell the Agent why things were ignored, so it doesn't keep
        retrying fields that will be dropped anyway.

        按 table（characters/scenes/props/products）+ name upsert 资产：不存在则新增、存在则改字段。

        在 `update_project` 的单一文件锁内完成 read-modify-write；apply 后、落盘前对结果
        project dict 做 payload 级结构校验，按**「不更坏」语义**裁决：仅当该 upsert 把原本
        合法的 project 改成非法时才 raise 且**不落盘**（mutation 抛错时 `update_project` 不执行
        atomic_write）；改前已非法（历史遗留脏数据，如空 `style`）则照常放行——否则带历史问题的
        项目会整条 patch_project 路径不可用（旧 `add_assets.py` 报告校验错误也不阻断写入）。
        与剧本写盘统一入口的 `_guard_no_worse` 同源。把「只能加」扩为「可改」。

        返回**诊断 dict**（不是 project 元数据）：``added``（新建条目名列表）、``merged``
        （合并已有条目名列表）、``dropped_fields``（被白名单丢弃的非允许字段，{name: [字段名]}）、
        ``dropped_legacy``（被剔除的历史字段如 type/importance，{name: [字段名]}）。caller
        （MCP tool 层）据此构造对 Agent 的明确反馈——silent drop 是设计意图（least privilege），
        但纯 silent 让 Agent 误以为 reference_image / sheet_field 写入成功；返回诊断让工具层
        把忽略原因明示给 Agent，避免 Agent 重复尝试同样会被丢的字段。
        """
        # data_validator imports this module at module level (VALID_GENERATION_MODES), so import lazily to break the cycle.
        # data_validator 在模块级 import 本模块（VALID_GENERATION_MODES），故惰性 import 破环。
        from lib.project.asset_derivatives import merge_agent_derivatives, normalize_agent_derivatives
        from lib.project.data_validator import DataValidator

        asset_type = self._resolve_asset_type(table)
        # A wrong entries type and an empty object need different hints, so the Agent can fix its input precisely.
        # entries 类型错误与空对象需要不同提示，便于 Agent 精确修正输入。
        if not is_shape(entries, dict):
            raise ValueError(f"entries must be an object (dict), got {type(entries).__name__}")
        if not entries:
            raise ValueError("entries must not be empty (at least one name → attrs entry is required)")
        # Normalize name: non-empty after strip + NFC, and must be a path-safe single component (validate_asset_name; names
        # are joined into file paths and single-segment route parameters). An Agent mistakenly passing a name with spaces like
        # "  李白  " would make later name-indexed lookups (character generation etc.) mismatch on whitespace. Invalid names
        # fail loudly. Also detect post-normalization conflicts: {"李白": {...}, "  李白  ": {...}} or NFC/NFD dual forms with
        # the same key after normalization -> the latter would silently overwrite the former; fail loudly so the Agent clearly
        # notices the collision and de-duplicates.
        # 规范化 name：strip + NFC 后非空，且须是路径安全的单段组件（validate_asset_name，
        # 名称会被拼进文件路径与单段路由参数）。Agent 误传 "  李白  " 这种带空格的 name 会让
        # 后续按 name 索引查找（角色生成等）因空格差异 mismatch。非法 name fail-loud。
        # 同时检测规范化后冲突：{"李白": {...}, "  李白  ": {...}} 或 NFC/NFD 双形态规范化后
        # key 相同 → 后者会 silent overwrite 前者；fail-loud 让 Agent 明确感知 collision 并去重。
        normalized_entries: dict[str, dict] = {}
        raw_keys_by_normalized: dict[str, str] = {}
        for raw_name, attrs in entries.items():
            try:
                name = validate_asset_name(raw_name)
            except ValueError as exc:
                raise ValueError(f"{table}: {exc}") from None
            if not is_shape(attrs, dict):
                raise ValueError(f"The content of {table} '{name}' must be an object")
            if name in normalized_entries:
                raise ValueError(
                    f"{table} entries contain name {name!r} that conflicts after normalization: "
                    f"raw keys {raw_keys_by_normalized[name]!r} and {raw_name!r} are equal after normalization "
                    "(strip + NFC)"
                )
            normalized_entries[name] = attrs
            raw_keys_by_normalized[name] = raw_name

        spec = ASSET_SPECS[asset_type]
        # The field whitelist uses the spec's "Agent permission dimension" `agent_editable_extra_fields` and **does not
        # reuse** the schema dimension `extra_string_fields` -- the latter includes system/user path fields such as
        # `reference_image` (same nature as sheet_field, updated via the dedicated `update_character_reference_image` API),
        # which the Agent's patch_project shouldn't change directly. Disallowed fields likewise include `sheet_field`
        # (character_sheet / scene_sheet / prop_sheet, written back by the asset generation pipeline via the dedicated
        # `_update_asset_sheet` API once the image is ready) and any key outside the spec. `_strip_legacy_asset_fields`
        # handles legacy fields such as type/importance; this layer adds the whitelist to form "least privilege".
        # `derivatives` in the whitelist (only for types with that capability) is normalized and merged per the Agent write
        # rules of `lib.project.asset_derivatives`: only change descriptions are writable; sheet paths are kept by the
        # generation pipeline.
        # 字段白名单走 spec 的「Agent 权限维度」`agent_editable_extra_fields`，**不复用** schema 维度
        # `extra_string_fields`——后者包括 `reference_image` 这类系统/用户路径字段（与 sheet_field
        # 同性质，更新走 `update_character_reference_image` 专用 API），不该被 Agent patch_project 直改。
        # 不允许的字段同样含 `sheet_field`（character_sheet / scene_sheet / prop_sheet，资产生成流水线
        # 在图像就绪后通过 `_update_asset_sheet` 专用 API 回写）以及 spec 之外的任意 key。
        # `_strip_legacy_asset_fields` 处理 type/importance 等历史字段，这层再加白名单形成「最小特权」。
        # 白名单里的 `derivatives`（开启该能力的类型才有）归一与合并走 `lib.project.asset_derivatives` 的
        # Agent 写入口径：只写得动变化描述，资产图路径由生成流水线保留。
        allowed_fields = spec.agent_writable_fields
        # Collect fields dropped by the whitelist / legacy fields removed for the caller to tell the Agent explicitly. Silent
        # drop is still the design intent, but the returned dict tells the tool layer explicitly "what was dropped", and the
        # tool layer informs the Agent accordingly, so the LLM doesn't keep retrying.
        # 收集白名单丢字段 / 历史字段丢弃 给 caller 用于明示 Agent。silent drop 仍是设计意图,
        # 但通过返回 dict 把"被丢了什么"显式告诉工具层,工具层据此告知 Agent,避免 LLM 重复尝试。
        cleaned: dict[str, dict[str, Any]] = {}
        dropped_fields: dict[str, list[str]] = {}  # name -> [fields dropped by the whitelist] / name → [被白名单丢的字段]
        # name -> [fields removed by _LEGACY_ASSET_FIELDS]
        dropped_legacy: dict[str, list[str]] = {}  # name → [被 _LEGACY_ASSET_FIELDS 剔除的字段]
        for name, attrs in normalized_entries.items():
            legacy_keys = sorted(set(attrs) & self._LEGACY_ASSET_FIELDS)
            if legacy_keys:
                dropped_legacy[name] = legacy_keys

            entry_clean: dict[str, Any] = {}
            non_allowed: list[str] = []
            for k, v in self._strip_legacy_asset_fields(attrs).items():
                if k in allowed_fields:
                    entry_clean[k] = v
                else:
                    non_allowed.append(k)
                    logger.debug(
                        "upsert_assets: %s '%s' 的字段 %r 不在 Agent 可编辑白名单 %s,已忽略",
                        table,
                        name,
                        k,
                        sorted(allowed_fields),
                    )
            if non_allowed:
                dropped_fields[name] = sorted(non_allowed)
            if DERIVATIVES_FIELD in entry_clean:
                entry_clean[DERIVATIVES_FIELD] = normalize_agent_derivatives(
                    entry_clean[DERIVATIVES_FIELD], spec=spec, field_path=f"{table} {name!r}"
                )
            cleaned[name] = entry_clean

        added: list[str] = []
        merged: list[str] = []
        noop: list[str] = []

        def _mutate(project: dict) -> None:
            validator = DataValidator(str(self.projects_dir))
            before_errors = set(validator.validate_project_payload(project).errors)  # pre-change snapshot / 改前快照
            bucket = project.setdefault(spec.bucket_key, {})
            if not isinstance(bucket, dict):
                # Legacy dirty data: bucket_key exists but isn't a dict (e.g. list/str). Continuing would make bucket.get /
                # bucket[name].update below raise a vague AttributeError, so fail loudly first with the unexpected type and the
                # offending key (a mutation physically can't be applied to a non-dict; unrelated to "no worse").
                # 历史脏数据：bucket_key 已存在却非 dict（如 list/str）。继续会让下方
                # bucket.get/bucket[name].update 抛含糊的 AttributeError，故先 fail-loud
                # 给出意外类型与 offending key（mutation 物理上无法对非 dict 施加，与「不更坏」无关）。
                raise ValueError(f"project[{spec.bucket_key!r}] must be an object, got {type(bucket).__name__}")
            for name, attrs in cleaned.items():
                # An existing entry's key may be NFD: resolve the real key and update in place, rather than creating a second entry
                # under the NFC name
                # 存量 entry 的 key 可能是 NFD：解析真实 key 就地更新，而非按 NFC 名新建第二条
                match = find_project_asset_name(project, name)
                if match is not None and not match.is_entry(asset_type, name):
                    raise ProjectAssetNameConflictError(name, match, asset_type)
                key = match.name if match is not None else name
                existing = isinstance(bucket.get(key), dict)
                # The derivative table is merged by name (same name updates the description, new names are added, unmentioned ones
                # stay); whole-table replacement would wipe the sheets of unmentioned derivatives; other fields of the base-look entry
                # are still overwritten as whole values. An empty table doesn't count as a change, same no-op as "all fields dropped".
                # 衍生表按名合并（同名改描述、新名加入、未提及的留下），整表替换会抹掉
                # 未提及衍生的资产图；本体条目的其余字段仍是整值覆盖。空表不算改动，
                # 与「全字段被丢空」同归 no-op。
                derivatives = attrs.get(DERIVATIVES_FIELD) or {}
                body_attrs = {k: v for k, v in attrs.items() if k != DERIVATIVES_FIELD}
                # Only detect no-op for existing entries: when all fields were dropped by the whitelist/legacy strip, update({})
                # changes nothing, so count it as noop rather than merged to avoid a false "merged 1". A new entry is created as usual
                # even if cleaned is empty, with description defaulting to an empty string -- the description is only required when
                # generating a sheet.
                # 仅对已存在 entry 检测 no-op:全字段被白名单/legacy strip 丢空时 update({})
                # 实际不变,归到 noop 而非 merged 避免「合并 1 个」误报。新 entry 即使
                # cleaned 空也照常建条目,description 缺省记为空串——描述只在生成资产图时必需。
                if existing and not body_attrs and not derivatives:
                    noop.append(name)
                    continue
                if existing:
                    # update: merge fields, keeping existing fields such as the sheet path
                    bucket[key].update(body_attrs)  # 改：合并字段，保留 sheet 路径等既有字段
                    merged.append(name)
                else:
                    bucket[key] = self._build_asset_entry(asset_type, body_attrs.get("description", ""), body_attrs)
                    added.append(name)
                if derivatives:
                    merge_agent_derivatives(bucket[key], derivatives)
            after_errors = set(validator.validate_project_payload(project).errors)
            # "No worse" is judged by error set diff: after must not have any errors that before didn't.
            #   - valid before, invalid after -> new_errors = all after errors -> refuse
            #   - dirty before, same dirt after -> new_errors = empty -> allow (projects with legacy dirty data can keep patching)
            #   - dirty before, new errors after (e.g. an entry's description isn't a string) -> new_errors non-empty -> refuse
            #   - dirty before, partly fixed after -> new_errors = empty -> allow (patches may improve legacy dirty data)
            # Stricter than just comparing the valid flag: blocks "structural errors of a new entry piggybacking onto disk in a
            # project with legacy dirty data".
            # 「不更坏」按 error set diff 判定：after 不应比 before 多任何 errors。
            #   - 改前合法、改后非法 → new_errors=全部 after errors → 拒
            #   - 改前已脏、改后相同脏 → new_errors=∅ → 放行（允许带历史脏数据的项目继续 patch）
            #   - 改前已脏、改后引入新错误（如 entries 的 description 不是字符串）→ new_errors≠∅ → 拒
            #   - 改前已脏、改后修复了部分 → new_errors=∅ → 放行（允许 patch 改进历史脏数据）
            # 比单纯比 valid 标志更严：堵住「带历史脏数据的项目里新 entry 的结构错误 piggyback 落盘」。
            new_errors = after_errors - before_errors
            if new_errors:
                raise ValueError("project.json structure validation failed: " + "; ".join(sorted(new_errors)))

        self.update_project(project_name, _mutate)
        return {
            "added": added,
            "merged": merged,
            "noop": noop,
            "dropped_fields": dropped_fields,
            "dropped_legacy": dropped_legacy,
        }

    def delete_asset(self, project_name: str, table: str, name: str) -> dict:
        """Delete a project asset: the entry, sheet, original images and reference audio, version history and its derivatives
        together become one deletion record.

        The asset entry owns the identity of the sheet's manifest registration; leaving the registration would make the
        manifest point at a nonexistent asset, so all callers share this one place; project.json, version records and the
        manifest are rewritten in the same formal write transaction and roll back together if the deletion record can't be
        created.

        删除一个项目资产：条目、资产图、原图与参考音频、版本历史与名下衍生一起成为一条删除记录。

        资产条目拥有资产图清单登记的身份，登记留着会让清单指向不存在的资产，所以全部调用方共用这一处；
        project.json、版本记录与清单在同一个正式写入事务里改写，删除记录建不成时一起回滚。
        """

        from lib.project import asset_trash

        asset_type = self._resolve_asset_type(table)
        spec = ASSET_SPECS[asset_type]
        project_dir = self.get_project_path(project_name)
        deleted_name: str | None = None
        deleted_entry: dict[str, Any] = {}
        before: dict[str, bytes] = {}

        def _mutate(project: dict) -> None:
            nonlocal deleted_name
            bucket = project.get(spec.bucket_key)
            key = resolve_asset_key(bucket, name)
            if not isinstance(bucket, dict) or key is None:
                raise KeyError(f"{spec.asset_type.capitalize()} '{name}' not found")
            before.update(asset_trash.state_before(project_dir))
            deleted_name = key
            entry = bucket[key]
            if isinstance(entry, dict):
                deleted_entry.update(entry)
            del bucket[key]

        def _trash(_project_file: Path) -> None:
            if deleted_name is None:  # pragma: no cover - mutate contract
                raise RuntimeError("asset deletion did not resolve a canonical identity")
            asset_trash.trash_asset(project_dir, spec, deleted_name, deleted_entry, before)

        return self.update_project(
            project_name, _mutate, on_commit=_trash, formal_paths=asset_trash.state_paths(project_dir)
        )

    def preview_asset_deletion(self, project_name: str, table: str, name: str) -> AssetDeletionPreview:
        """Reference preview before deleting an asset (read-only): how many references in scripts and drafts would dangle
        after deletion.

        Deletion itself doesn't rewrite these references (see :meth:`delete_asset`), and dangling references are only
        stopped at generation time; the preview is the only chance to tell the user in advance. The scan borrows the same
        rewrite as rename, so reference counts match the rename preview, and derivative references (``角色/衍生``,
        character/derivative) are counted along with the base look.

        Raises:
            FileNotFoundError: the project doesn't exist.
            KeyError: the asset doesn't exist.

        删除资产前的引用预览（只读）：剧本与草稿里有多少处引用会在删除后悬空。

        删除本身不改写这些引用（见 :meth:`delete_asset`），悬空引用要到生成时才被拦下；预览是提前
        告诉用户的唯一机会。扫描借用重命名的同一套改写，引用数与重命名预览一致，衍生引用
        （``角色/衍生``）随本体一并计入。

        Raises:
            FileNotFoundError: 项目不存在。
            KeyError: 资产不存在。
        """
        asset_type = self._resolve_asset_type(table)
        spec = ASSET_SPECS[asset_type]
        if not self.project_exists(project_name):
            raise FileNotFoundError(f"Project does not exist: {project_name}")
        with self._asset_cascade_locks(project_name) as (script_files, draft_files):
            bucket = self._read_project_raw_unlocked(project_name).get(spec.bucket_key)
            key = resolve_asset_key(bucket, name)
            if key is None:
                raise KeyError(f"{spec.asset_type.capitalize()} '{name}' not found")
            scan = self._scan_asset_references(
                project_name, script_files, draft_files, asset_type, key, _DELETION_SCAN_TARGET
            )
        by_episode: Counter[int] = Counter()
        for episode_dir, count in scan.episode_references.items():
            episode = self.filename_episode(episode_dir)
            if episode is not None:
                by_episode[episode] += count
        return AssetDeletionPreview(
            table=table,
            name=key,
            references=scan.references,
            episodes=tuple(
                AssetEpisodeReferences(episode=episode, references=count)
                for episode, count in sorted(by_episode.items())
            ),
        )

    # bucket_key (characters/scenes/props/products) -> asset type, derived once from the static ASSET_SPECS.
    # bucket_key（characters/scenes/props/products）→ 资产类型，从静态 ASSET_SPECS 派生一次。
    _BUCKET_TO_ASSET_TYPE: ClassVar[dict[str, str]] = {spec.bucket_key: t for t, spec in ASSET_SPECS.items()}

    @classmethod
    def _resolve_asset_type(cls, table: str) -> str:
        """bucket_key -> asset type; an unknown table name raises ValueError (the message lists the allowed values).

        bucket_key → 资产类型；未知表名抛 ValueError（message 列出合法取值）。"""
        asset_type = cls._BUCKET_TO_ASSET_TYPE.get(table)
        if asset_type is None:
            raise ValueError(f"Unknown asset table: {table!r}, must be one of {sorted(cls._BUCKET_TO_ASSET_TYPE)}")
        return asset_type

    _LEGACY_ASSET_FIELDS = frozenset({"type", "importance"})

    @classmethod
    def _strip_legacy_asset_fields(cls, attrs: dict) -> dict:
        """Strip the old-style type/importance fields (left over from schema evolution); return a new dict.

        剔除旧式 type/importance 字段（schema 演进遗留），返回新 dict。"""
        return {k: v for k, v in attrs.items() if k not in cls._LEGACY_ASSET_FIELDS}

    #: The script_plan final content, editable draft and invalid draft file names that cascading rename must also rewrite
    #: (structured JSON -- they carry reference arrays / ``@[name]`` body text and flow back into final content after
    #: promotion). The draft part takes the full registry of ``lib.script.draft_quarantine`` instead of listing each:
    #: missing one source would leave that route's drafts with the old name, judged a violation for "unregistered
    #: reference" on promotion, unresolvable until the draft is edited by hand. Legacy ``.md`` free-text aliases aren't
    #: listed: the read layer only accepts them for browsing, and the write and generation sides no longer accept them.
    #: 级联重命名须一并改写的 script_plan 正式内容、可编辑草稿与待修复草稿文件名（结构化 JSON——它们承载
    #: 引用数组 / ``@[名称]`` 正文，晋升后会回流为正式内容）。草稿部分取
    #: ``lib.script.draft_quarantine`` 的登记表全集而非逐个列举：漏一种来源就会让那条路线的草稿留着
    #: 旧名，晋升时被「引用未登记」判违约、直到人工改草稿才解得开。旧版 ``.md`` 自由文本别名
    #: 不在列：读取层仅兼认浏览，写盘与生成侧已不认。
    _RENAME_DRAFT_FILENAMES = frozenset(
        {
            *SCRIPT_PLAN_FILENAMES.values(),
            REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME,
            *QUARANTINE_FILENAMES,
        }
    )

    @contextmanager
    def _asset_cascade_locks(self, project_name: str) -> Generator[tuple[list[str], list[Path]]]:
        """Hold the locks needed to cascade-rewrite all scripts and drafts; yields ``(script file names, draft paths)``.

        The lock order is "all script locks (sorted by file name) -> draft file locks -> project lock", consistent with
        ``locked_episode_script``'s script lock -> project lock, avoiding ABBA deadlock. Shared by rename, derivative rename
        and merge.

        持有级联改写全部剧本与草稿所需的锁，产出 ``(剧本文件名, 草稿路径)``。

        锁序为「全部剧本锁（按文件名排序）→ 草稿文件锁 → 项目锁」，与 ``locked_episode_script`` 的
        脚本锁 → 项目锁 一致，避免 ABBA 死锁。重命名、衍生改名与合并共用。
        """
        script_files = self._canonical_episode_scripts(project_name)
        drafts_root = self.get_project_path(project_name) / "drafts"
        draft_files = (
            sorted(p for p in drafts_root.glob("episode_*/*.json") if p.name in self._RENAME_DRAFT_FILENAMES)
            if drafts_root.is_dir()
            else []
        )
        with ExitStack() as stack:
            for filename in script_files:
                stack.enter_context(self._script_lock(project_name, filename))
            for path in draft_files:
                stack.enter_context(self.file_lock(path))
            stack.enter_context(self._project_lock(project_name))
            yield script_files, draft_files

    @staticmethod
    def _artifact_manifest_adapter(project_dir: Path):
        """The artifact manifest adapter when the project already has a manifest, otherwise ``None`` (a rename doesn't create
        a manifest out of thin air).

        项目已有产物清单时给出它的适配器，没有则 ``None``（改名不凭空建清单）。"""
        from lib.artifacts.artifact_manifest import MANIFEST_FILENAME, ProjectArtifactManifestAdapter

        manifest_path = project_dir / MANIFEST_FILENAME
        if manifest_path.exists() or manifest_path.is_symlink():
            return ProjectArtifactManifestAdapter(project_dir)
        return None

    def _scan_asset_references(
        self,
        project_name: str,
        script_files: Sequence[str],
        draft_files: Sequence[Path],
        asset_type: str,
        old_name: str,
        new_name: str,
    ) -> _AssetReferenceScan:
        """Within ``_asset_cascade_locks``, rewrite references to *old_name* in scripts and drafts to *new_name* (in-memory
        payloads only).

        The rename preview, rename execution and pre-deletion reference preview share this one scan, so their reference
        counts agree.

        在 ``_asset_cascade_locks`` 内把剧本与草稿里指向 *old_name* 的引用改写为 *new_name*（只改内存载荷）。

        重命名的预览与执行、删除前的引用预览共用这一次扫描，三者的引用数因此一致。
        """
        scan = _AssetReferenceScan()
        for filename in script_files:
            script, _migrated = self._read_script_unlocked(project_name, filename)
            before = copy.deepcopy(script)
            changes = rewrite_payload_references(script, asset_type, old_name, new_name)
            if changes:
                scan.scripts.append((filename, script, before))
                scan.episode_references[Path(filename).stem] += changes
        for path in draft_files:
            payload = load_json_or_none(path)
            if not isinstance(payload, dict):
                continue
            fingerprint = canonical_json_digest(payload)
            changes = rewrite_payload_references(payload, asset_type, old_name, new_name)
            if changes:
                scan.drafts.append((path, payload, fingerprint))
                scan.episode_references[path.parent.name] += changes
        return scan

    def _carry_script_plan_confirmations(
        self, project_dir: Path, project: dict, changed_drafts: Sequence[tuple[Path, dict, str]]
    ) -> None:
        """Cascading rewrites only change asset names, not plan content: for episodes whose confirmation is exactly the
        pre-rewrite content, the confirmation fingerprint is carried over.

        ``changed_drafts`` is ``(path, rewritten payload, pre-rewrite fingerprint)``; the fingerprint uses the same basis
        as confirmation records (a normalized hash of the whole JSON, see ``content_fingerprint_of_data``); only each
        episode's formal script plan takes part in the confirmation decision. ``project`` is the project.json payload to be
        committed with this batch of rewrites. Without carrying over, the confirmation fingerprint wouldn't match the
        rewritten plan; the user only changed a name, yet that episode would fall back to pending confirmation and be
        steered to the confirmation action that overwrites the final script.

        级联改写只换资产的名字，不改规划内容：确认的恰是改写前内容的集，确认指纹随之平移。

        ``changed_drafts`` 是 ``(路径, 改写后载荷, 改写前指纹)``，指纹与确认记录同一口径（整份 JSON 的
        规范化哈希，见 ``content_fingerprint_of_data``）；只有各集的正式脚本规划参与确认判定。
        ``project`` 是将随这批改写提交的 project.json 载荷。不平移时确认指纹与改写后的规划对不上，
        用户只改了个名字，那一集就退回待确认、被引向覆盖正式脚本的确认操作。
        """
        # script_review imports this module at module level; import lazily to break the cycle. / script_review 在模块级 import 本模块，惰性 import 破环。
        from lib.script.script_review import (
            carry_confirmation_through_migration,
            content_fingerprint_of_data,
            script_plan_path,
        )

        for path, payload, before in changed_drafts:
            episode = self.filename_episode(path.parent.name)
            if episode is not None and script_plan_path(project_dir, project, episode) == path:
                carry_confirmation_through_migration(project, episode, before, content_fingerprint_of_data(payload))

    def rename_asset(
        self, project_name: str, table: str, old_name: str, new_name: str, *, dry_run: bool = False
    ) -> AssetRenameReport:
        """Single transactional entry point for cascading asset rename (shared by UI and Agent, see docs/adr/0057).

        Within "all script locks (sorted by file name) -> draft file locks -> project lock" it does everything at once:
        scan name references in all episode scripts and script_plan drafts, plan associated file moves, and run "no worse"
        structural validation on the project.json change; with ``dry_run=True`` it stops there and only returns the impact
        report (preview and execution share the same scan, so numbers always match); otherwise it writes in the order
        scripts -> drafts -> associated files -> version history -> project.json -> Artifact Manifest. The manifest is
        re-keyed last with one CAS on the whole file; if the process exits after project.json is committed, rerunning the
        same rename completes the remaining CAS from the old claim and the new project binding. The lock acquisition order
        is consistent with ``locked_episode_script``'s script lock -> project lock, avoiding ABBA deadlock.

        Raises:
            ValueError: unknown table or invalid new name (``validate_asset_name``) / structural validation failed.
            AssetRenameNotFoundError: the old name doesn't exist (the message includes an idempotent-recovery hint).
            AssetRenameConflictError: the new name conflicts with an existing same-type asset under normalization.
            AssetRenameFileCollisionError: an associated file's move target is already taken by an orphan file.
            AssetRenameHistoryCollisionError: the new name already has version history belonging to another asset.

        资产级联重命名的单一事务入口（UI 与 Agent 共用，见 docs/adr/0057）。

        在「全部剧本锁（按文件名排序）→ 草稿文件锁 → 项目锁」内一次完成：扫描全部剧集
        剧本与 script_plan 草稿的名称引用、规划关联文件迁移、对 project.json 变更做「不更坏」
        结构校验；``dry_run=True`` 时到此为止只返回影响报告（预览与执行共用同一套扫描，
        数字必然一致），否则按 剧本 → 草稿 → 关联文件 → 版本历史 → project.json →
        Artifact Manifest 的顺序落盘。Manifest 以整份文件的一次 CAS 最后重键；若进程在
        project.json 提交后退出，重跑同一次重命名会凭旧 claim 与新项目绑定完成剩余 CAS。
        锁获取顺序与
        ``locked_episode_script`` 的 脚本锁 → 项目锁 一致，避免 ABBA 死锁。

        Raises:
            ValueError: table 未知或新名非法（``validate_asset_name``）/ 结构校验失败。
            AssetRenameNotFoundError: 旧名不存在（message 含幂等恢复提示）。
            AssetRenameConflictError: 新名与既有同类型资产归一化判定冲突。
            AssetRenameFileCollisionError: 某个关联文件的迁移目标已被孤儿文件占用。
            AssetRenameHistoryCollisionError: 新名下已有属于别的资产的版本历史。
        """
        # data_validator imports this module at module level; import lazily to break the cycle (same as upsert_assets).
        # data_validator 在模块级 import 本模块，惰性 import 破环（与 upsert_assets 同理）。
        from lib.artifacts.version_manager import VersionManager
        from lib.project.asset_derivative_rename import (
            owner_derivative_renames,
            plan_derivative_sheet_relocation,
            rewrite_derivative_sheet_paths,
        )
        from lib.project.data_validator import DataValidator

        asset_type = self._resolve_asset_type(table)
        spec = ASSET_SPECS[asset_type]
        new_clean = validate_asset_name(new_name)
        if not self.project_exists(project_name):
            raise FileNotFoundError(f"Project does not exist: {project_name}")
        project_dir = self.get_project_path(project_name)

        with self._asset_cascade_locks(project_name) as (script_files, draft_files):
            project = self._read_project_raw_unlocked(project_name)
            bucket = project.get(spec.bucket_key)
            from lib.artifacts.artifact_manifest import ArtifactKey, ArtifactManifest

            manifest_adapter = self._artifact_manifest_adapter(project_dir)

            def _plan_manifest_rekey(
                source_name: str,
                target_name: str,
                source_sheet: object,
                target_sheet: object,
            ):
                if manifest_adapter is None:
                    return None
                path_rewrites = (
                    {source_sheet: target_sheet}
                    if isinstance(source_sheet, str)
                    and source_sheet
                    and isinstance(target_sheet, str)
                    and target_sheet
                    and source_sheet != target_sheet
                    else {}
                )
                return ArtifactManifest(manifest_adapter).plan_entry_rekey(
                    ArtifactKey.asset_sheet(asset_type, source_name),
                    ArtifactKey.asset_sheet(asset_type, target_name),
                    artifact_path_rewrites=path_rewrites,
                )

            old_key = resolve_asset_key(bucket, old_name)
            if old_key is None:
                completed_key = resolve_asset_key(bucket, new_clean)
                if completed_key is not None:
                    completed_entry = bucket.get(completed_key) if isinstance(bucket, dict) else None
                    source_claim = (
                        manifest_adapter.get_entry(ArtifactKey.asset_sheet(asset_type, normalize_asset_name(old_name)))
                        if manifest_adapter is not None
                        else None
                    )
                    target_sheet = (
                        completed_entry.get(spec.sheet_field) if isinstance(completed_entry, Mapping) else None
                    )
                    if source_claim is not None and isinstance(target_sheet, str) and target_sheet:
                        recovery_plan = _plan_manifest_rekey(
                            normalize_asset_name(old_name),
                            completed_key,
                            source_claim.artifact_path,
                            target_sheet,
                        )
                        if not dry_run:
                            assert recovery_plan is not None
                            recovery_plan.commit()
                        return AssetRenameReport(
                            table=table,
                            old_name=normalize_asset_name(old_name),
                            new_name=completed_key,
                            episodes=0,
                            references=0,
                            files=0,
                            dry_run=dry_run,
                        )
                hint = (
                    "; the new name already exists in the asset table - the previous rename may have succeeded "
                    "(cascading rename is safe to retry)"
                    if completed_key is not None
                    else ""
                )
                raise AssetRenameNotFoundError(f"No asset named {old_name!r} exists in {table}{hint}")
            conflict_key = resolve_asset_key(bucket, new_clean)
            if conflict_key is not None and conflict_key != old_key:
                raise AssetRenameConflictError(conflict_key)
            ensure_project_asset_name_available(
                project,
                new_clean,
                requested_asset_type=asset_type,
                exclude_asset_type=asset_type,
                exclude_name=old_key,
            )

            # -- Scan (dry-run preview and execution share the same logic) -- / —— 扫描（dry-run 预览与执行共用同一套逻辑）——
            scan = self._scan_asset_references(project_name, script_files, draft_files, asset_type, old_key, new_clean)
            changed_scripts = scan.scripts
            changed_drafts = scan.drafts

            moves = plan_asset_file_renames(project_dir, spec, old_key, new_clean)
            version_manager = VersionManager(project_dir)
            version_files = version_manager.rename_resource(spec.bucket_key, old_key, new_clean, dry_run=True)
            # Derivative images, version snapshots and manifest keys under the asset all have the base name as their first
            # segment and move along with the base look (see lib/project/asset_derivative_rename); with no derivatives the plan
            # is empty and nothing is done at write time.
            # 名下衍生的图、版本快照与清单键都以本体名为第一段，随本体一起搬（见
            # lib/project/asset_derivative_rename）；无衍生时规划为空，落盘期什么也不做。
            derivative_renames = (
                owner_derivative_renames(project[spec.bucket_key][old_key]) if spec.supports_derivatives else ()
            )
            derivatives = plan_derivative_sheet_relocation(
                project_dir,
                manifest_adapter=manifest_adapter,
                old_owner=old_key,
                new_owner=new_clean,
                renames=derivative_renames,
            )

            # The project.json change is first applied to a copy and checked for "no worse": if validation fails it is rejected
            # as a whole and nothing is written anywhere.
            # project.json 变更先在副本上应用并做「不更坏」校验：校验失败整体拒绝、任何一处不落盘。
            mutated = copy.deepcopy(project)
            validator = DataValidator(str(self.projects_dir))
            before_errors = _rename_agnostic_errors(validator.validate_project_payload(mutated), old_key, new_clean)
            entry = rekey_equivalent_entries(mutated[spec.bucket_key], old_key, new_clean)
            if isinstance(entry, dict):
                rewrite_entry_paths(entry, spec, old_key, new_clean)
                if ALIASES_FIELD in spec.extra_list_fields:
                    record_asset_aliases(entry, (old_key,), asset_name=new_clean)
                rewrite_derivative_sheet_paths(
                    entry, old_owner=old_key, new_owner=new_clean, renames=derivative_renames
                )
            if self._requires_unique_asset_namespace(mutated):
                ensure_project_asset_namespace(mutated)
            after_errors = _rename_agnostic_errors(validator.validate_project_payload(mutated), old_key, new_clean)
            new_errors = {after_errors[fingerprint] for fingerprint in after_errors.keys() - before_errors.keys()}
            if new_errors:
                raise ValueError("project.json structure validation failed: " + "; ".join(sorted(new_errors)))

            manifest_rekey_plan = None
            if manifest_adapter is not None:
                source_asset_entry = project[spec.bucket_key][old_key]
                old_sheet = (
                    source_asset_entry.get(spec.sheet_field) if isinstance(source_asset_entry, Mapping) else None
                )
                new_sheet = entry.get(spec.sheet_field) if isinstance(entry, Mapping) else None
                manifest_rekey_plan = _plan_manifest_rekey(old_key, new_clean, old_sheet, new_sheet)

            report = AssetRenameReport(
                table=table,
                old_name=old_key,
                new_name=new_clean,
                episodes=len(scan.episode_references),
                references=scan.references,
                files=len(moves) + version_files + derivatives.files,
                dry_run=dry_run,
            )
            if dry_run:
                return report

            # -- Write (each step idempotent; after a mid-way failure, rerunning the same rename converges) --
            # —— 落盘（每步幂等，中途失败重跑同一次重命名即可收敛）——
            for filename, script, before in changed_scripts:
                self._write_script_unlocked(
                    project_name,
                    script,
                    filename,
                    sync_project=False,
                    before=before,
                )
            self._carry_script_plan_confirmations(project_dir, mutated, changed_drafts)
            for path, payload, _fingerprint in changed_drafts:
                atomic_write_json(path, payload)
            self._rename_grid_references(
                project_dir, asset_type, ((old_key, new_clean),), moves=(*moves, *derivatives.moves)
            )
            for src, dst in moves:
                if src.exists():
                    os.replace(src, dst)
            version_manager.rename_resource(spec.bucket_key, old_key, new_clean)
            derivatives.relocate()
            self._touch_metadata(mutated)
            project_file = self._get_project_file_path(project_name)
            with formal_write_transaction(project_file):
                atomic_write_json(project_file, mutated)
                if manifest_rekey_plan is not None:
                    manifest_rekey_plan.commit()
                derivatives.commit_manifest()

        emit_project_change_hint(project_name, changed_paths=[self.PROJECT_FILE])
        return report

    @staticmethod
    def _rename_grid_references(
        project_dir: Path,
        asset_type: str,
        renames: Sequence[tuple[str, str]],
        *,
        moves: Iterable[tuple[Path, Path]],
    ) -> None:
        """Reference images frozen in grid records have their names and paths rewritten with the rename (rules in
        ``rewrite_grid_reference_images``).

        *renames* are this rewrite's (old reference name -> new reference name): one pair each for rename and derivative
        rename; for an asset merge, one pair per derivative moved with its image under the kept asset. Written before the
        file moves: when rerunning the same rename or merge after a mid-way failure, already rewritten records no longer
        match the old name, and the file moves for not-yet-rewritten records are still in the plan, so both cases converge.
        Unreadable records are left as-is.

        宫格记录冻结的参考图随改名改写名字与路径（规则见 ``rewrite_grid_reference_images``）。

        *renames* 是本次改写的（旧引用名 → 新引用名）：改名与衍生改名各一对，资产合并是每个随图迁到
        保留方名下的衍生一对。在文件迁移之前写：中途失败重跑同一次改名或合并时，已改写的记录不再命中
        旧名，未改写的记录对应的文件迁移也还在规划里，两种情况都能收敛。读不出的记录原样留下。
        """
        from lib.script.grid.grid_manager import GridManager

        grids_dir = project_dir / "grids"
        if not grids_dir.is_dir():
            return
        path_rewrites = {
            unicodedata.normalize("NFC", src.relative_to(project_dir).as_posix()): dst.relative_to(
                project_dir
            ).as_posix()
            for src, dst in moves
        }

        def rewrite(grid: "GridGeneration") -> int:
            return sum(
                rewrite_grid_reference_images(
                    grid.reference_images or [], asset_type, old_name, new_name, path_rewrites
                )
                for old_name, new_name in renames
            )

        def mutate(grid: "GridGeneration") -> None:
            rewrite(grid)

        manager = GridManager(project_dir)
        for record in sorted(grids_dir.glob("grid_*.json")):
            grid_id = record.name.removesuffix(".json")
            try:
                grid = manager.get(grid_id)
            except (OSError, KeyError, TypeError, ValueError):
                continue
            if grid is not None and rewrite(grid):
                manager.update(grid_id, mutate, ignore_invalid=True)

    def rename_asset_derivative(
        self, asset_type: str, project_name: str, entry_name: str, old_name: str, new_name: str
    ) -> dict:
        """Cascading transaction for a derivative rename: the derivative table key in the base-look entry, plus
        ``本体名/旧衍生名`` (base/old derivative name) in all scripts / drafts.

        Same lock order (scripts -> drafts -> project) and same reference rewriting (``rewrite_payload_references``, with
        the old name passed as ``本体名/旧衍生名``) as :meth:`rename_asset`: a derivative shares the base look's name and
        identity, and its reference form has only one more segment (see ``docs/adr/0072``), so there's no reason for it to
        use a different rewrite basis. Returns the renamed base-look entry.

        Raises:
            FileNotFoundError: the project doesn't exist.
            KeyError: the base-look entry doesn't exist.
            AssetRenameNotFoundError: the base look has no such derivative.
            AssetRenameConflictError: the new derivative name is already taken by another derivative of the same base
                look (case-only differences count).
            ValueError: invalid new name (``validate_asset_name``) or the entry structure isn't an object.

        衍生改名的级联事务：本体条目内的衍生表 key，加上全部剧本 / 草稿里的 ``本体名/旧衍生名``。

        与 :meth:`rename_asset` 同一把锁序（剧本 → 草稿 → 项目）与同一套引用改写
        （``rewrite_payload_references``，旧名传 ``本体名/旧衍生名``）：衍生共享本体的名字与
        身份，引用形态只多一段（见 ``docs/adr/0072``），没有理由让它走另一条改写口径。
        返回改名后的本体条目。

        Raises:
            FileNotFoundError: 项目不存在。
            KeyError: 本体条目不存在。
            AssetRenameNotFoundError: 该本体下没有这个衍生。
            AssetRenameConflictError: 新衍生名已被同一本体下的另一个衍生占用（只差大小写也算）。
            ValueError: 新名非法（``validate_asset_name``）或条目结构不是对象。
        """
        from lib.project.asset_derivative_rename import plan_derivative_sheet_relocation, rewrite_derivative_sheet_paths
        from lib.project.asset_derivatives import find_derivative_name

        spec = ASSET_SPECS[asset_type]
        new_clean = validate_asset_name(new_name)
        if not self.project_exists(project_name):
            raise FileNotFoundError(f"Project does not exist: {project_name}")
        project_dir = self.get_project_path(project_name)

        with self._asset_cascade_locks(project_name) as (script_files, draft_files):
            project = self._read_project_raw_unlocked(project_name)
            bucket = project.get(spec.bucket_key)
            base_key = resolve_asset_key(bucket, entry_name)
            if not isinstance(bucket, dict) or base_key is None:
                raise KeyError(entry_name)
            entry = bucket[base_key]
            if not isinstance(entry, dict):
                raise ValueError(f"project asset {spec.bucket_key}/{base_key} must be an object")
            table = entry.get(DERIVATIVES_FIELD)
            if not isinstance(table, dict):
                raise AssetRenameNotFoundError(f"{base_key} has no registered derivatives")
            old_key = resolve_asset_key(table, old_name)
            if old_key is None:
                raise AssetRenameNotFoundError(f"No derivative named {old_name!r} exists under {base_key}")
            taken = find_derivative_name(table, new_clean, exclude=old_key)
            if taken is not None:
                raise AssetRenameConflictError(taken)

            old_reference = derivative_reference(base_key, old_key)
            new_reference = derivative_reference(base_key, new_clean)

            # -- Scan (same shape as rename_asset: change everything in memory first, then write scripts -> drafts ->
            # project.json) --
            # —— 扫描（与 rename_asset 同形：先在内存改齐，再按 剧本 → 草稿 → project.json 落盘）——
            changed_scripts: list[tuple[str, dict, dict]] = []
            for filename in script_files:
                script, _migrated = self._read_script_unlocked(project_name, filename)
                before = copy.deepcopy(script)
                if rewrite_payload_references(script, asset_type, old_reference, new_reference):
                    changed_scripts.append((filename, script, before))
            changed_drafts: list[tuple[Path, dict, str]] = []
            for path in draft_files:
                payload = load_json_or_none(path)
                if not isinstance(payload, dict):
                    continue
                fingerprint = canonical_json_digest(payload)
                if rewrite_payload_references(payload, asset_type, old_reference, new_reference):
                    changed_drafts.append((path, payload, fingerprint))

            # The derivative sheet's three coordinates (image, version snapshots, manifest key) all include the derivative name
            # and move along with the entry key; planning precedes any write, so conflicts are rejected here as a whole with zero
            # bytes written.
            # 衍生资产图的三样坐标（图、版本快照、清单键）都含衍生名，与条目键一起搬；
            # 规划先于任何写入，冲突在此整体拒绝、零字节落盘。
            relocation = plan_derivative_sheet_relocation(
                project_dir,
                manifest_adapter=self._artifact_manifest_adapter(project_dir),
                old_owner=base_key,
                new_owner=base_key,
                renames=((old_key, new_clean),),
            )

            rekey_equivalent_entries(table, old_key, new_clean)
            rewrite_derivative_sheet_paths(
                entry, old_owner=base_key, new_owner=base_key, renames=((old_key, new_clean),)
            )

            for filename, script, before in changed_scripts:
                self._write_script_unlocked(
                    project_name,
                    script,
                    filename,
                    sync_project=False,
                    before=before,
                )
            self._carry_script_plan_confirmations(project_dir, project, changed_drafts)
            for path, payload, _fingerprint in changed_drafts:
                atomic_write_json(path, payload)
            self._rename_grid_references(
                project_dir, asset_type, ((old_reference, new_reference),), moves=relocation.moves
            )
            relocation.relocate()
            self._touch_metadata(project)
            project_file = self._get_project_file_path(project_name)
            with formal_write_transaction(project_file):
                atomic_write_json(project_file, project)
                relocation.commit_manifest()
            result = dict(entry)

        emit_project_change_hint(project_name, changed_paths=[self.PROJECT_FILE])
        return result

    def merge_asset(
        self,
        project_name: str,
        table: str,
        source_name: str,
        target_name: str,
        *,
        as_derivative: bool = False,
        dry_run: bool = False,
    ) -> "AssetMergeReport":
        """Single transactional entry point for asset merge (shared by UI and Agent): merge the merged-away asset into a
        same-type kept asset.

        Same lock set and same "scan first, then write" as :meth:`rename_asset`: references to the merged-away asset in all
        episode scripts and script_plan drafts are repointed to the kept asset (rules in :mod:`lib.project.asset_merge`);
        the kept asset stays unchanged, only gaining aliases (merge as base look) or a derivative (merge as derivative).
        The merged-away asset's description, sheet and version history, voice settings, original images and reference
        audio are not merged in, and together with the entry become one deletion record; its derivatives move under the
        kept asset, and those with the same name as an existing derivative of the kept asset (case-only differences count)
        are merged into that one. With ``dry_run=True`` only the impact report is returned; preview and execution share the
        same scan, so numbers always match.

        Write order: scripts -> drafts -> derivative sheet moves -> project.json, manifest and the merged-away asset's
        deletion record in the same formal write transaction. If it fails mid-way before project.json is committed,
        rerunning the same merge converges (already rewritten references are no longer counted).

        Raises:
            ValueError: unknown table / structural validation failed.
            AssetMergeRejectedError: type not mergeable, merging into itself, or non-character merged as derivative.
            AssetMergeNotFoundError: the merged-away or kept asset doesn't exist.
            AssetRenameFileCollisionError: the target of a derivative sheet move is already taken by an orphan file.
            AssetRenameHistoryCollisionError: the target id of a derivative move already has other version history.

        资产合并的单一事务入口（UI 与 Agent 共用）：把被并方并入同类型的保留方。

        与 :meth:`rename_asset` 同一组锁、同一套「先扫描、再落盘」：全部剧集剧本与 script_plan 草稿里
        指向被并方的引用改指保留方（规则见 :mod:`lib.project.asset_merge`），保留方原样不动，只追加
        别名（并为本体）或衍生（并为衍生）。被并方的描述、资产图及版本历史、声音设置、原图与参考音频
        不并入，连同条目成为一条删除记录；它名下的衍生迁到保留方，与保留方已有衍生同名（只差大小写也算）的并入那个。
        ``dry_run=True`` 时只返回影响报告，预览与执行共用同一次扫描，数字必然一致。

        落盘顺序：剧本 → 草稿 → 衍生资产图搬迁 → 同一个正式写入事务里的 project.json、清单与被并方的删除记录。
        project.json 提交前中途失败时，重跑同一次合并即可收敛（已改写的引用不再计数）。

        Raises:
            ValueError: table 未知 / 结构校验失败。
            AssetMergeRejectedError: 类型不可合并、并入自己，或非角色并为衍生。
            AssetMergeNotFoundError: 被并方或保留方不存在。
            AssetRenameFileCollisionError: 迁移衍生资产图的目标已被孤儿文件占用。
            AssetRenameHistoryCollisionError: 迁移衍生的目标 id 下已有别的版本历史。
        """
        # The derivative and artifact modules depend back on this module via artifact planning; import lazily to break the
        # cycle (same as rename_asset).
        # 衍生与产物模块经产物规划反向依赖本模块，惰性 import 破环（与 rename_asset 同理）。
        from lib.project import asset_trash
        from lib.project.asset_derivative_rename import (
            plan_derivative_sheet_relocation,
            rewrite_derivative_sheet_paths,
        )
        from lib.project.asset_derivatives import derivative_table, ensure_derivative_table
        from lib.project.asset_merge import (
            MERGEABLE_ASSET_TYPES,
            AssetMergeEpisodeImpact,
            AssetMergeNotFoundError,
            AssetMergeRejectedError,
            AssetMergeReport,
            DerivativeMergePlan,
            count_outdated_artifacts,
            current_artifact_probe,
            grid_merge_references,
            merge_payload_references,
            plan_derivative_merge,
            storyboard_reference_identities,
        )
        from lib.project.data_validator import DataValidator

        asset_type = self._resolve_asset_type(table)
        if asset_type not in MERGEABLE_ASSET_TYPES:
            raise AssetMergeRejectedError("type_not_mergeable", f"{table} does not support merging")
        spec = ASSET_SPECS[asset_type]
        if as_derivative and not spec.supports_derivatives:
            raise AssetMergeRejectedError(
                "derivative_needs_character",
                f"{table} has no derivatives; cannot merge as a derivative",
            )
        if not self.project_exists(project_name):
            raise FileNotFoundError(f"Project does not exist: {project_name}")
        project_dir = self.get_project_path(project_name)

        with self._asset_cascade_locks(project_name) as (script_files, draft_files):
            project = self._read_project_raw_unlocked(project_name)
            bucket = project.get(spec.bucket_key)
            source = resolve_asset_key(bucket, asset_name_comparison_key(source_name))
            if not isinstance(bucket, dict) or source is None:
                raise AssetMergeNotFoundError(source_name)
            target = resolve_asset_key(bucket, asset_name_comparison_key(target_name))
            if target is None:
                raise AssetMergeNotFoundError(target_name)
            if source == target:
                raise AssetMergeRejectedError("same_asset", f"Cannot merge {source!r} into itself")
            source_entry, target_entry = bucket[source], bucket[target]
            if not isinstance(source_entry, dict) or not isinstance(target_entry, dict):
                raise ValueError(f"project asset {spec.bucket_key} entries must be objects")

            # Derivatives of the merged character: those moved under the kept asset move together with their images, versions
            # and manifest keys; those merged into an existing same-name derivative are cleared entirely. Reference rewriting
            # needs the planned derivative names (when merged into an existing derivative differing only in case, its name is
            # used), so plan before scanning.
            # 被并角色的衍生：迁到保留方名下的随图、版本与清单键一起搬，并入已有同名衍生的整套清掉。
            # 引用改写要用到规划出的衍生名（并入只差大小写的已有衍生时改用它的名字），故先于扫描规划。
            derivative_plan = (
                plan_derivative_merge(source_entry, target_entry, source, as_derivative=as_derivative)
                if spec.supports_derivatives
                else DerivativeMergePlan()
            )

            # -- Scan (dry-run preview and execution share the same logic) -- / —— 扫描（dry-run 预览与执行共用同一套逻辑）——
            impacts: dict[int, dict[str, int]] = {}

            def tally(episode: int | None, **counts: int) -> None:
                if episode is None:
                    return
                totals = impacts.setdefault(episode, {})
                for key, value in counts.items():
                    totals[key] = totals.get(key, 0) + value

            is_current = current_artifact_probe(project_dir)
            storyboard_identities = storyboard_reference_identities(project_dir, project, asset_type)
            carried_references = derivative_plan.carried_references(source, target)
            outdated_grid_ids, grid_reference_names = grid_merge_references(
                project_dir, asset_type, source, derivative_plan.folded
            )
            for new_reference, old_reference in carried_references.items():
                if old_reference in storyboard_identities:
                    storyboard_identities[new_reference] = storyboard_identities[old_reference]
            changed_scripts: list[tuple[str, dict, dict]] = []
            for filename in script_files:
                script, _migrated = self._read_script_unlocked(project_name, filename)
                before = copy.deepcopy(script)
                changes = merge_payload_references(
                    script, asset_type, source, target, as_derivative=as_derivative, derivatives=derivative_plan
                )
                if changes.total:
                    changed_scripts.append((filename, script, before))
                episode = self.canonical_script_episode(filename)
                storyboards, videos = (
                    count_outdated_artifacts(
                        before,
                        script,
                        asset_type,
                        episode,
                        is_current,
                        storyboard_identities=storyboard_identities,
                        carried_references=carried_references,
                        outdated_grid_ids=outdated_grid_ids,
                        grid_reference_names=grid_reference_names,
                    )
                    if episode is not None
                    else (0, 0)
                )
                if not changes.total and not storyboards and not videos:
                    continue
                tally(
                    episode,
                    script=changes.names,
                    speaker=changes.speakers,
                    prompt_text=changes.mentions,
                    storyboards=storyboards,
                    videos=videos,
                )
            changed_drafts: list[tuple[Path, dict, str]] = []
            for path in draft_files:
                payload = load_json_or_none(path)
                if not isinstance(payload, dict):
                    continue
                fingerprint = canonical_json_digest(payload)
                changes = merge_payload_references(
                    payload, asset_type, source, target, as_derivative=as_derivative, derivatives=derivative_plan
                )
                if not changes.total:
                    continue
                changed_drafts.append((path, payload, fingerprint))
                category = "draft" if path.name in QUARANTINE_FILENAMES else "script_plan"
                tally(
                    self.filename_episode(path.parent.name),
                    **{category: changes.names},
                    speaker=changes.speakers,
                    prompt_text=changes.mentions,
                )

            moved_pairs = tuple((name, name) for name in derivative_plan.moved)
            relocation = plan_derivative_sheet_relocation(
                project_dir,
                manifest_adapter=self._artifact_manifest_adapter(project_dir),
                old_owner=source,
                new_owner=target,
                renames=moved_pairs,
                carry_unlisted=False,
            )

            # The project.json change is first applied to a copy and checked for "no worse": if validation fails it is rejected
            # as a whole and nothing is written anywhere.
            # project.json 变更先在副本上应用并做「不更坏」校验：校验失败整体拒绝、任何一处不落盘。
            mutated = copy.deepcopy(project)
            validator = DataValidator(str(self.projects_dir))
            # A merge doesn't rename any retained asset, so error fingerprints are compared on the original text (folding is the
            # identity).
            # 合并不改任何留存资产的名字，错误指纹按原文比对（折叠是恒等的）。
            before_errors = _rename_agnostic_errors(validator.validate_project_payload(mutated), target, target)
            merged_bucket = mutated[spec.bucket_key]
            discarded = merged_bucket.pop(source)
            kept = merged_bucket[target]

            def string_aliases(entry: Mapping[str, Any]) -> list[str]:
                aliases = entry.get(ALIASES_FIELD)
                return [alias for alias in aliases if isinstance(alias, str)] if isinstance(aliases, list) else []

            aliases_before = set(string_aliases(kept))
            if not as_derivative:
                record_asset_aliases(kept, (source, *string_aliases(discarded)), asset_name=target)
            if derivative_plan.created is not None or derivative_plan.moved:
                derivatives = ensure_derivative_table(kept)
                if derivative_plan.created is not None:
                    description = discarded.get("description")
                    derivatives[derivative_plan.created] = {
                        "description": description if isinstance(description, str) else "",
                        spec.sheet_field: "",
                    }
                source_derivatives = derivative_table(discarded)
                for name in derivative_plan.moved:
                    derivatives[name] = copy.deepcopy(source_derivatives[name])
                rewrite_derivative_sheet_paths(kept, old_owner=source, new_owner=target, renames=moved_pairs)
            if self._requires_unique_asset_namespace(mutated):
                ensure_project_asset_namespace(mutated)
            after_errors = _rename_agnostic_errors(validator.validate_project_payload(mutated), target, target)
            new_errors = {after_errors[fingerprint] for fingerprint in after_errors.keys() - before_errors.keys()}
            if new_errors:
                raise ValueError("project.json structure validation failed: " + "; ".join(sorted(new_errors)))

            report = AssetMergeReport(
                table=table,
                source=source,
                target=target,
                as_derivative=as_derivative,
                aliases_added=tuple(alias for alias in string_aliases(kept) if alias not in aliases_before),
                derivative_created=derivative_plan.created,
                derivatives_moved=derivative_plan.moved,
                derivatives_folded=derivative_plan.folded,
                episodes=tuple(
                    AssetMergeEpisodeImpact(episode=episode, **counts) for episode, counts in sorted(impacts.items())
                ),
                dry_run=dry_run,
            )
            if dry_run:
                return report

            # -- Write -- / —— 落盘 ——
            state_before = asset_trash.state_before(project_dir)
            for filename, script, before in changed_scripts:
                self._write_script_unlocked(project_name, script, filename, sync_project=False, before=before)
            self._carry_script_plan_confirmations(project_dir, mutated, changed_drafts)
            for path, payload, _fingerprint in changed_drafts:
                atomic_write_json(path, payload)
            # Derivatives moved with their images only changed names, so the names and paths frozen in grid records are rewritten
            # too, in the same cascade as rename; those merged into an existing derivative switched to a different image, so the
            # record isn't repointed and the grid is judged stale as usual.
            # 随图迁来的衍生只换了名字，宫格记录冻结的名字与路径一并改写，与改名同一条级联；
            # 并入已有衍生的换成了另一张图，记录不改指，宫格照常判过期。
            self._rename_grid_references(
                project_dir,
                asset_type,
                tuple((old_reference, new_reference) for new_reference, old_reference in carried_references.items()),
                moves=relocation.moves,
            )
            relocation.relocate()
            self._touch_metadata(mutated)
            project_file = self._get_project_file_path(project_name)
            # The merged-away asset's sheet, version history, name-based files and the derivatives merged into an existing
            # same-name derivative go into the project trash: leaving them in place would let a future same-name asset pick up
            # history that doesn't belong to it. Derivatives moved under the kept asset have already moved by now, and the record
            # doesn't include them.
            # 被并方的资产图、版本历史、按名命名的文件与并入已有同名衍生的那些衍生进项目回收站：留在原处
            # 会让以后同名的资产接上不属于它的历史。迁到保留方名下的衍生此时已搬走，记录不收它们。
            with formal_write_transaction(*asset_trash.state_paths(project_dir)):
                atomic_write_json(project_file, mutated)
                relocation.commit_manifest()
                asset_trash.trash_asset(
                    project_dir,
                    spec,
                    source,
                    discarded,
                    state_before,
                    merged_into=target,
                    derivatives_moved=list(derivative_plan.moved),
                )

        emit_project_change_hint(project_name, changed_paths=[self.PROJECT_FILE])
        return report

    def _update_asset_sheet(
        self,
        asset_type: str,
        project_name: str,
        name: str,
        sheet_path: str,
        *,
        on_commit: Callable[[Path], None] | None = None,
    ) -> dict:
        """Update the asset sheet field path. Raises KeyError if the asset doesn't exist.

        Completes the read-modify-write within a single file lock via update_project, avoiding lost-update races with
        concurrent add / update tasks.

        更新资产 sheet 字段路径。资产不存在抛 KeyError。

        通过 update_project 在单一文件锁内完成 read-modify-write，避免与并发 add /
        update 任务的 lost-update 竞态。
        """
        spec = ASSET_SPECS[asset_type]

        def _mutate(project):
            bucket = project.get(spec.bucket_key)
            key = resolve_asset_key(bucket, name)
            if not isinstance(bucket, dict) or key is None:
                raise KeyError(f"{spec.asset_type.capitalize()} '{name}' does not exist")
            bucket[key][spec.sheet_field] = sheet_path

        return self.update_project(project_name, _mutate, on_commit=on_commit)

    def install_asset_sheet_bytes(
        self,
        asset_type: str,
        project_name: str,
        name: str,
        sheet_path: str,
        content: bytes,
        *,
        on_commit: Callable[[Path], None] | None = None,
    ) -> dict | None:
        """Install sheet bytes after rechecking their asset under the project lock.

        Stable sheet paths may be uploaded before an asset definition exists.
        That case writes only unclaimed bytes while still holding the same lock
        used by asset creation. If the definition exists by the final check,
        the file, metadata pointer, and sidecar hook commit atomically instead.
        """

        project_dir = self.get_project_path(project_name)
        target = safe_join(project_dir, sheet_path)
        project_file = self._get_project_file_path(project_name)
        spec = ASSET_SPECS[asset_type]

        with self._project_lock(project_name):
            project = self._read_project_raw_unlocked(project_name)
            bucket = project.get(spec.bucket_key)
            if resolve_asset_key(bucket, name) is None:
                with formal_write_transaction(target):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(target, content)
                return None

            def _mutate(locked_project: dict) -> None:
                locked_bucket = locked_project.get(spec.bucket_key)
                key = resolve_asset_key(locked_bucket, name)
                if not isinstance(locked_bucket, dict) or key is None:
                    raise KeyError(f"{spec.asset_type.capitalize()} '{name}' does not exist")
                locked_bucket[key][spec.sheet_field] = sheet_path

            with formal_write_transaction(project_file, target):
                self._apply_project_mutation_unlocked(project_name, project, _mutate)
                atomic_write_json(project_file, project)
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(target, content)
                if on_commit is not None:
                    on_commit(target)

        emit_project_change_hint(
            project_name,
            changed_paths=[self.PROJECT_FILE],
        )
        return project

    def _get_asset(self, asset_type: str, project_name: str, name: str) -> dict:
        """Get the asset definition. Raises KeyError if it doesn't exist. / 获取资产定义。不存在抛 KeyError。"""
        spec = ASSET_SPECS[asset_type]
        project = self.load_project(project_name)
        bucket = project.get(spec.bucket_key)
        key = resolve_asset_key(bucket, name)
        if not isinstance(bucket, dict) or key is None:
            raise KeyError(f"{spec.asset_type.capitalize()} '{name}' does not exist")
        return bucket[key]

    def _get_asset_path(self, asset_type: str, project_name: str, filename: str) -> Path:
        """Get the absolute path of an asset file under the project directory. / 获取资产文件在项目目录下的绝对路径。"""
        spec = ASSET_SPECS[asset_type]
        return self.get_project_path(project_name) / spec.subdir / filename

    # ==================== Project-level character management ==================== / ==================== 项目级角色管理 ====================

    def add_project_character(
        self,
        project_name: str,
        name: str,
        description: str,
        voice_style: str | None = None,
        character_sheet: str | None = None,
    ) -> dict:
        """
        Add a character to the project (project-level)

        Args:
            project_name: project name
            name: character name
            description: character description
            voice_style: voice style
            character_sheet: character sheet path

        Returns:
            updated project metadata

        向项目添加角色（项目级）

        Args:
            project_name: 项目名称
            name: 角色名称
            description: 角色描述
            voice_style: 声音风格
            character_sheet: 角色资产图路径

        Returns:
            更新后的项目元数据
        """
        name = validate_asset_name(name)

        def _mutate(project: dict) -> None:
            bucket = project["characters"]
            # Overwrites must also land on the existing real key (possibly NFD), otherwise a visually identical old entry would
            # remain
            # 覆盖写也要落在存量真实 key 上（可能是 NFD），否则会残留视觉同名的旧条目
            match = find_project_asset_name(project, name)
            if match is not None and not match.is_entry("character", name):
                raise ProjectAssetNameConflictError(name, match, "character")
            key = match.name if match is not None else name
            entry = self._build_asset_entry(
                "character",
                description,
                {"voice_style": voice_style or "", "character_sheet": character_sheet or ""},
            )
            # Overwrites only replace base-look fields: derivatives hang inside the base-look entry, and replacing the whole entry
            # would wipe the registered derivatives too.
            # 覆盖写只换本体字段：衍生挂在本体条目内，整条替换会连同已登记的衍生一起抹掉。
            previous = bucket.get(key)
            if isinstance(previous, dict):
                inherited = previous.get(DERIVATIVES_FIELD)
                if isinstance(inherited, dict):
                    entry[DERIVATIVES_FIELD] = inherited
            bucket[key] = entry

        return self.update_project(project_name, _mutate)

    def update_project_character_sheet(self, project_name: str, name: str, sheet_path: str) -> dict:
        """Update the project-level character sheet path / 更新项目级角色资产图路径"""
        return self._update_asset_sheet("character", project_name, name, sheet_path)

    def update_character_reference_image(self, project_name: str, char_name: str, ref_path: str) -> dict:
        """
        Update the character's reference image path

        Args:
            project_name: project name
            char_name: character name
            ref_path: reference image relative path

        Returns:
            updated project data

        更新角色的参考图路径

        Args:
            project_name: 项目名称
            char_name: 角色名称
            ref_path: 参考图相对路径

        Returns:
            更新后的项目数据
        """

        def _mutate(project: dict) -> None:
            key = resolve_asset_key(project.get("characters"), char_name)
            if key is None:
                raise KeyError(f"Character '{char_name}' does not exist")
            project["characters"][key]["reference_image"] = ref_path

        return self.update_project(project_name, _mutate)

    def update_prop_reference_image(self, project_name: str, prop_name: str, ref_path: str) -> dict:
        """Set a prop's reference image path (an uploaded picture sent with its sheet's generation)."""

        def _mutate(project: dict) -> None:
            key = resolve_asset_key(project.get("props"), prop_name)
            if key is None:
                raise KeyError(f"Prop '{prop_name}' does not exist")
            project["props"][key]["reference_image"] = ref_path

        return self.update_project(project_name, _mutate)

    def clear_asset_reference_image(self, project_name: str, asset_type: str, name: str) -> dict:
        """Remove a character's or prop's reference image from its entry; the file stays (a new upload replaces it)."""

        if asset_type not in ("character", "prop"):
            raise ValueError(f"asset type has no single reference image: {asset_type}")
        bucket = ASSET_SPECS[asset_type].bucket_key

        def _mutate(project: dict) -> None:
            key = resolve_asset_key(project.get(bucket), name)
            if key is None:
                raise KeyError(f"{asset_type} '{name}' does not exist")
            project[bucket][key]["reference_image"] = ""

        return self.update_project(project_name, _mutate)

    def update_character_reference_audio(self, project_name: str, char_name: str, ref_path: str) -> dict:
        """
        Update the character's reference audio path (empty string means clear)

        Also mechanically stamps ``voice_updated_at`` (covering both the upload-overwrite and clear paths), compared against
        generated clips' ``generated_assets.video_generated_at`` to determine whether a clip predates the current voice
        settings -- no extra "dismissed" boolean is needed; the dismissed state is naturally expressed as "new version" by
        comparing the ``voice_notice_dismissed_at`` timestamp with this field.

        The other stamp point is in ``server/routers/assets.py::apply_to_project``: global asset library batch import
        writes characters within a single update_project and can't go through this method, so it stamps the same field in
        place.

        Args:
            project_name: project name
            char_name: character name
            ref_path: reference audio relative path

        Returns:
            updated project data

        更新角色的参考音频路径（空串表示清空）

        同时机械戳 ``voice_updated_at``（覆盖上传/清空两条路径），用于跟已生成片段的
        ``generated_assets.video_generated_at`` 比较，判定片段是否早于当前声音设置——
        无需额外「已关闭」布尔位，关闭态用 ``voice_notice_dismissed_at`` 时间戳与本字段
        比较即可自然表达「新版本」。

        另一处戳点在 ``server/routers/assets.py::apply_to_project``：全局资产库批量导入
        在单次 update_project 内一并写 characters，走不通本方法，故就地戳同一字段。

        Args:
            project_name: 项目名称
            char_name: 角色名称
            ref_path: 参考音频相对路径

        Returns:
            更新后的项目数据
        """

        def _mutate(project: dict) -> None:
            self._set_character_reference_audio(project, char_name, ref_path)

        return self.update_project(project_name, _mutate)

    @staticmethod
    def _set_character_reference_audio(project: dict, char_name: str, ref_path: str) -> None:
        key = resolve_asset_key(project.get("characters"), char_name)
        if key is None:
            raise KeyError(f"Character '{char_name}' does not exist")
        project["characters"][key]["reference_audio"] = ref_path
        project["characters"][key]["voice_updated_at"] = datetime.now(UTC).isoformat()

    def install_character_reference_audio(
        self,
        project_name: str,
        char_name: str,
        ref_path: str,
        content: bytes,
    ) -> dict:
        """Atomically install reference-audio bytes and point the character at them.

        Video currency selection hashes reference-audio bytes while holding the project lock.  Keeping every
        physical replacement in that same lock makes the project pointer and the bytes one coherent input snapshot.
        A replaced file with a different extension goes to the project trash inside the same commit.
        """

        project_dir = self.get_project_path(project_name)
        refs_audio_dir = project_dir / "characters" / "refs_audio"
        target = Path(self._safe_subpath(project_dir, ref_path))
        if os.path.realpath(target.parent) != os.path.realpath(refs_audio_dir):
            raise ValueError("reference audio target must be inside characters/refs_audio")
        stale: list[tuple[str, Path]] = []

        def _mutate(project: dict) -> None:
            key = resolve_asset_key(project.get("characters"), char_name)
            if key is None:
                raise KeyError(f"Character '{char_name}' does not exist")
            old_audio = project["characters"][key].get("reference_audio")
            stale_audio = resolve_stale_reference_audio(project_dir, refs_audio_dir, old_audio, target)
            self._set_character_reference_audio(project, char_name, ref_path)
            if stale_audio is not None:
                stale.append((key, stale_audio))

        return self._update_project_with_files(
            project_name,
            _mutate,
            writes=[(content, target)],
            on_commit=lambda _file: self._trash_stale_reference_audio(project_dir, *stale[0]) if stale else None,
        )

    @staticmethod
    def _trash_stale_reference_audio(project_dir: Path, character: str, stale: Path) -> None:
        """Move the replaced or cleared audio to the project trash when it is the character's own file.

        Reference audio is owned by name and never shared, so a file under another name is not this character's to
        remove; the caller holds the lock.
        """

        rel = f"characters/refs_audio/{stale.name}"
        if owns_named_file(ASSET_SPECS["character"], character, rel) and stale.is_file() and not stale.is_symlink():
            # The trash's landing rules depend on this module; a top-level import would create a cycle. / 回收站的落地规则依赖本模块，顶层导入会成环。
            from lib.project.media_trash import trash_reference_audio

            trash_reference_audio(project_dir, character, rel)

    def clear_character_reference_audio(self, project_name: str, char_name: str) -> dict:
        """Clear the reference and move the character's own audio file to the project trash in one commit."""

        project_dir = self.get_project_path(project_name)
        refs_audio_dir = project_dir / "characters" / "refs_audio"
        stale: list[tuple[str, Path]] = []

        def _mutate(project: dict) -> None:
            key = resolve_asset_key(project.get("characters"), char_name)
            if key is None:
                raise KeyError(f"Character '{char_name}' does not exist")
            old_audio = project["characters"][key].get("reference_audio")
            stale_audio = resolve_audio_ref_path(
                project_dir,
                refs_audio_dir,
                old_audio if isinstance(old_audio, str) else None,
            )
            self._set_character_reference_audio(project, char_name, "")
            if stale_audio is not None:
                stale.append((key, stale_audio))

        return self.update_project(
            project_name,
            _mutate,
            on_commit=lambda _file: self._trash_stale_reference_audio(project_dir, *stale[0]) if stale else None,
        )

    def get_project_character(self, project_name: str, name: str) -> dict:
        """Get the project-level character definition / 获取项目级角色定义"""
        return self._get_asset("character", project_name, name)

    # ==================== Scene management (scene) ==================== / ==================== 场景管理（scene） ====================

    def update_scene_sheet(self, project_name: str, name: str, sheet_path: str) -> dict:
        """Update the scene sheet path / 更新场景资产图路径"""
        return self._update_asset_sheet("scene", project_name, name, sheet_path)

    def get_scene(self, project_name: str, name: str) -> dict:
        """Get the scene definition / 获取场景定义"""
        return self._get_asset("scene", project_name, name)

    def get_scene_path(self, project_name: str, filename: str) -> Path:
        """Get the scene sheet path / 获取场景资产图路径"""
        return self._get_asset_path("scene", project_name, filename)

    # ==================== Prop management (prop) ==================== / ==================== 道具管理（prop） ====================

    def update_prop_sheet(self, project_name: str, name: str, sheet_path: str) -> dict:
        """Update the prop sheet path / 更新道具资产图路径"""
        return self._update_asset_sheet("prop", project_name, name, sheet_path)

    def get_prop(self, project_name: str, name: str) -> dict:
        """Get the prop definition / 获取道具定义"""
        return self._get_asset("prop", project_name, name)

    def get_prop_path(self, project_name: str, filename: str) -> Path:
        """Get the prop sheet path / 获取道具资产图路径"""
        return self._get_asset_path("prop", project_name, filename)

    # ==================== Product management (product) ==================== / ==================== 商品管理（product） ====================

    def update_product_sheet(self, project_name: str, name: str, sheet_path: str) -> dict:
        """Update the product standard reference image (product sheet) path / 更新商品标准参考图（product sheet）路径"""
        return self._update_asset_sheet("product", project_name, name, sheet_path)

    def get_product(self, project_name: str, name: str) -> dict:
        """Get the product definition / 获取商品定义"""
        return self._get_asset("product", project_name, name)

    def get_product_path(self, project_name: str, filename: str) -> Path:
        """Get the product image path / 获取商品图片路径"""
        return self._get_asset_path("product", project_name, filename)

    def add_product_reference_image(self, project_name: str, product_name: str, ref_path: str) -> dict:
        """Append an original image path to the product's reference_images list (not appended again if already present).

        Original images are the acceptance anchors of product fidelity, append-only; deletion/reordering goes through the
        asset PATCH channel.

        向商品的 reference_images 列表追加一张原图路径（已存在则不重复追加）。

        原图是商品保真的验收锚点，只增不改；删除/重排走资产 PATCH 通道。
        """

        def _mutate(project: dict) -> None:
            bucket = project.get("products")
            key = resolve_asset_key(bucket, product_name)
            if not isinstance(bucket, dict) or key is None:
                raise KeyError(f"Product '{product_name}' does not exist")
            refs = bucket[key].setdefault("reference_images", [])
            if not isinstance(refs, list):
                raise ValueError(
                    f"products['{product_name}'].reference_images must be a list, got {type(refs).__name__}"
                )
            if ref_path not in refs:
                refs.append(ref_path)

        return self.update_project(project_name, _mutate)

    # ==================== Direct project asset write tools ==================== / ==================== 项目资产直接写入工具 ====================

    @staticmethod
    def _build_asset_entry(asset_type: str, description: str, source: dict | None = None) -> dict:
        """Build a new entry per ASSET_SPECS, see :func:`lib.project.asset_types.build_asset_entry`.

        按 ASSET_SPECS 构造新条目，见 :func:`lib.project.asset_types.build_asset_entry`。"""
        return build_asset_entry(asset_type, description, source)

    def add_character(self, project_name: str, name: str, description: str, voice_style: str = "") -> bool:
        """Add a character directly to project.json; returns False if one of the same type exists, raises on cross-type
        conflict.

        直接添加角色到 project.json；同类型已存在返回 False，跨类型冲突则抛错。"""
        entry = self._build_asset_entry("character", description, {"voice_style": voice_style})
        return self._add_asset("character", project_name, name, entry)

    def add_project_scene(self, project_name: str, name: str, description: str) -> bool:
        """Add a scene directly to project.json; returns False if one of the same type exists, raises on cross-type conflict.

        直接添加场景到 project.json；同类型已存在返回 False，跨类型冲突则抛错。"""
        entry = self._build_asset_entry("scene", description)
        return self._add_asset("scene", project_name, name, entry)

    def add_prop(self, project_name: str, name: str, description: str) -> bool:
        """Add a prop directly to project.json; returns False if one of the same type exists, raises on cross-type conflict.

        直接添加道具到 project.json；同类型已存在返回 False，跨类型冲突则抛错。"""
        entry = self._build_asset_entry("prop", description)
        return self._add_asset("prop", project_name, name, entry)

    def add_product(self, project_name: str, name: str, description: str, brand: str = "") -> bool:
        """Add a product directly to project.json; returns False if one of the same type exists, raises on cross-type
        conflict.

        直接添加商品到 project.json；同类型已存在返回 False，跨类型冲突则抛错。"""
        entry = self._build_asset_entry("product", description, {"brand": brand})
        return self._add_asset("product", project_name, name, entry)

    def add_characters_batch(self, project_name: str, characters: dict[str, dict]) -> int:
        """Batch-add characters; same-type existing ones are skipped; on cross-type conflict nothing in the batch is written.

        批量添加角色；同类型已存在的跳过，跨类型冲突时整批不落盘。"""
        entries = {
            name: self._build_asset_entry("character", data.get("description", ""), data)
            for name, data in characters.items()
        }
        return self._add_assets_batch("character", project_name, entries)

    def add_scenes_batch(self, project_name: str, scenes: dict[str, dict]) -> int:
        """Batch-add scenes; same-type existing ones are skipped; on cross-type conflict nothing in the batch is written.

        批量添加场景；同类型已存在的跳过，跨类型冲突时整批不落盘。"""
        entries = {
            name: self._build_asset_entry("scene", data.get("description", ""), data) for name, data in scenes.items()
        }
        return self._add_assets_batch("scene", project_name, entries)

    def add_props_batch(self, project_name: str, props: dict[str, dict]) -> int:
        """Batch-add props; same-type existing ones are skipped; on cross-type conflict nothing in the batch is written.

        批量添加道具；同类型已存在的跳过，跨类型冲突时整批不落盘。"""
        entries = {
            name: self._build_asset_entry("prop", data.get("description", ""), data) for name, data in props.items()
        }
        return self._add_assets_batch("prop", project_name, entries)

    # ==================== Reference image collection tools ==================== / ==================== 参考图收集工具 ====================

    def collect_reference_images(self, project_name: str, scene: dict) -> list[Path]:
        """
        Collect all reference images needed by a scene

        Args:
            project_name: project name
            scene: scene dict

        Returns:
            list of reference image paths

        Asset names in scripts and asset bucket keys may be in either NFC or NFD form (the registration gate stores NFC;
        existing scripts and buckets need no migration), so they are normalized by the comparison coordinates of
        ``lib.project.asset_types`` before indexing.

        收集场景所需的所有参考图

        Args:
            project_name: 项目名称
            scene: 场景字典

        Returns:
            参考图路径列表

        剧本里的资产名与资产桶 key 可能是 NFC/NFD 中的任一形态（登记闸口落 NFC，存量剧本
        与桶均无需迁移），索引前按 ``lib.project.asset_types`` 的比对坐标系归一。
        """
        project = self.load_project(project_name)
        project_dir = self.get_project_path(project_name)
        refs = []

        characters = normalize_asset_bucket(project.get("characters"))
        props = normalize_asset_bucket(project.get("props"))

        # Character reference images / 角色参考图
        for char in scene.get("characters_in_scene", []):
            char_data = characters.get(normalize_asset_name(char), {})
            sheet = char_data.get("character_sheet")
            if sheet:
                sheet_path = project_dir / sheet
                if sheet_path.exists():
                    refs.append(sheet_path)

        # Prop reference images / 道具参考图
        for prop in scene.get("props_in_scene", []):
            prop_data = props.get(normalize_asset_name(prop), {})
            sheet = prop_data.get("prop_sheet")
            if sheet:
                sheet_path = project_dir / sheet
                if sheet_path.exists():
                    refs.append(sheet_path)

        return refs

    # ==================== Project overview generation ==================== / ==================== 项目概述生成 ====================

    def _read_source_files(self, project_name: str, max_chars: int = 50000) -> str:
        """
        Read the contents of all UTF-8 text files under the project's source directory.

        Non-UTF-8 files raise SourceDecodeError -- upload paths already normalize to UTF-8 and startup migration has fixed
        old projects; if non-UTF-8 is still encountered here, the user copied files directly bypassing the API, which
        needs an explicit error rather than a misleading "source directory is empty".

        读取项目 source 目录下的所有 UTF-8 文本文件内容。

        非 UTF-8 文件会抛 SourceDecodeError —— 上传路径已统一规范化为 UTF-8，
        启动迁移已修历史项目；这里若仍遇到非 UTF-8，说明用户绕过 API 直接拷贝
        文件，需显式报错而非"源目录为空"误导。
        """
        from lib.script.source_loader.errors import SourceDecodeError

        project_dir = self.get_project_path(project_name)
        source_dir = project_dir / "source"

        if not source_dir.exists():
            return ""

        contents = []
        total_chars = 0
        for file_path in sorted(source_dir.glob("*")):
            if not (file_path.is_file() and file_path.suffix.lower() in SOURCE_TEXT_SUFFIXES):
                continue

            raw = file_path.read_bytes()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SourceDecodeError(
                    filename=file_path.name,
                    tried_encodings=["utf-8"],
                ) from exc

            remaining = max_chars - total_chars
            if remaining <= 0:
                break
            if len(content) > remaining:
                content = content[:remaining]
            contents.append(f"--- {file_path.name} ---\n{content}")
            total_chars += len(content)

        return "\n\n".join(contents)

    async def generate_overview(self, project_name: str) -> dict:
        """
        Asynchronously generate the project overview using the Gemini API

        Args:
            project_name: project name

        Returns:
            generated overview dict, containing synopsis, genre, theme, world_setting, generated_at

        使用 Gemini API 异步生成项目概述

        Args:
            project_name: 项目名称

        Returns:
            生成的 overview 字典，包含 synopsis, genre, theme, world_setting, generated_at
        """
        from lib.artifacts.artifact_provenance import DEFAULT_SOURCE_LANGUAGE
        from lib.backends.providers import CallPurpose
        from lib.backends.text_backends.base import TextGenerationRequest, TextTaskType
        from lib.backends.text_generator import TextGenerator
        from lib.prompts.prompt_builders_script import build_overview_prompt

        # Read source file contents / 读取源文件内容
        source_content = await asyncio.to_thread(self._read_source_files, project_name)
        if not source_content:
            raise EmptySourceError("The source directory is empty; cannot generate an overview")

        # Create a TextGenerator (tracks usage automatically) / 创建 TextGenerator（自动追踪用量）
        generator = await TextGenerator.create(
            TextTaskType.OVERVIEW, project_name, purpose=CallPurpose.PROJECT_OVERVIEW
        )

        # Call TextGenerator (Structured Outputs). When all source files are screenplays, switch to "extract first": the
        # author's written creative-plan preface is used as-is first, falling back to summarizing from the body only when
        # missing; when mixed, use the novel basis.
        # 调用 TextGenerator（Structured Outputs）。全部源文都是剧本时翻为「提取优先」：
        # 作者写下的创作方案前言优先照用，缺失才退回从正文归纳；混合时按小说口径。
        project_data = await asyncio.to_thread(self.load_project, project_name)
        source_kind = project_overview_source_kind(project_data)
        # source_language comes from project.json and may be non-string dirty data; non-string or empty string falls back to
        # the default language
        # source_language 来自 project.json，可能是非字符串脏数据；非字符串或空串回退默认语言
        # YouReel (owner 2026-11-20): the fallback is the default locale's language (artifact_provenance), English in
        # production (upstream: "中文"). / YouReel：回退为默认语言（生产为英文；上游为「中文」）。
        raw_source_language = project_data.get("source_language")
        target_language = (
            raw_source_language
            if isinstance(raw_source_language, str) and raw_source_language.strip()
            else DEFAULT_SOURCE_LANGUAGE
        )
        prompt = build_overview_prompt(source_content, source_kind=source_kind, target_language=target_language)

        result = await generator.generate(
            TextGenerationRequest(
                prompt=prompt,
                response_schema=ProjectOverview,
            ),
            project_name=project_name,
        )
        response_text = result.text

        # Parse and validate the response / 解析并验证响应
        overview = ProjectOverview.model_validate_json(response_text)
        overview_dict = overview.model_dump()
        overview_dict["generated_at"] = datetime.now(UTC).isoformat()

        # Save to project.json (RMW within a single _project_lock, avoiding concurrent overwrites of other fields)
        # 保存到 project.json（RMW 在单一 _project_lock 内完成，避免并发覆盖其它字段）
        def _mutate(project: dict) -> None:
            project["overview"] = overview_dict
            project["source_language"] = overview_dict["language"]

        await run_sync_transaction(self.update_project, project_name, _mutate)

        logger.info("项目概述已生成并保存")
        return overview_dict


@functools.cache
def _project_manager_singleton() -> ProjectManager:
    # Construction ensures the projects directory exists; the no-arg cache guarantees it runs only once per process
    # 构造时会确保项目目录存在；无参缓存保证每个进程只执行一次
    return ProjectManager(app_data_dir())


def get_project_manager() -> ProjectManager:
    """Return the lazily loaded global ProjectManager singleton (standard projects root). / 返回懒加载的全局 ProjectManager 单例（标准项目根目录）。"""
    return _project_manager_singleton()


def reset_project_manager_for_tests() -> None:
    """Clear the :func:`get_project_manager` singleton; the next call rebuilds it from the current data root.

    清掉 :func:`get_project_manager` 的单例，下次调用按当前数据根重建。"""
    _project_manager_singleton.cache_clear()
