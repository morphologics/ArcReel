"""Core logic of script_plan->prompt_authoring content review: applicability, script_plan paths, content fingerprints,
review status derivation, and the single write exit of the reference-to-video formal script_plan
(``script_plan_write_lock`` / ``write_script_plan_locked``).

The gate is consumed by the web router / service (view / edit / confirm the structured intermediate state; confirming
converts it to the final script), the Agent confirm tool and workflow status. Status derivation is a pure computation
over the script_plan file + project dict; the write exit additionally holds the per-path lock of
``ProjectManager.file_lock``, and all four write paths (Web save, re-split, promotion, migration write-back) funnel
into it, so locking, optimistic concurrency comparison and prompt_authoring draft cleanup exist in one place only.

The only stored truth is the "confirmation fingerprint" in project.json ``episodes[i].script_plan_review``; pending /
confirmed is derived at read time by comparing against the live script_plan content fingerprint (following the
"compute rather than store" read-time convention). So rerunning normalize, the Agent rewriting script_plan via draft
promotion, and manual web edits of script_plan all drift the fingerprint and automatically wait for review again,
without each write path having to report.

Scope (the three content/visual two-stage paths that have a structured script_plan intermediate state):
- drama / narration image-to-video / grid paths: script_plan_normalized_script.json / script_plan_segments.json;
- the reference_video path (across narration / drama content_mode): script_plan_reference_units.json.
The script_plan variant of all three is decided uniformly by ``script_plan_kind`` (reference_video takes precedence
by project generation mode). ad has no script_plan and is not part of the gate.

script_plan→prompt_authoring 内容确认的核心逻辑：适用性判定、script_plan 路径、内容指纹、确认状态派生，
以及参考生视频正式 script_plan 的单一写盘出口（``script_plan_write_lock`` / ``write_script_plan_locked``）。

gate 由 web router / service（结构化中间态查看 / 编辑 / 确认，确认即转为正式脚本）、Agent 确认工具与
工作流状态消费。状态派生只依赖 script_plan 文件 + project dict
的纯计算；写盘出口另持 ``ProjectManager.file_lock`` 的 per-path 锁，四条写路径（Web 端保存、
重拆分、晋升、迁移回写）全部汇入，锁、乐观并发比对与 prompt_authoring 草稿清理只存在一处。

真值只存「确认指纹」于 project.json ``episodes[i].script_plan_review``；pending / confirmed 由读时
比对 live script_plan 内容指纹派生（沿「能算不存」的读时计算约定）。因此重跑 normalize、Agent
经草稿晋升改写 script_plan、web 手改 script_plan 都会让指纹漂移、自动重新等待确认，无需各写入路径各自
上报。

适用范围（拥有结构化 script_plan 中间态的三条内容/视觉两段式路径）：
- drama / narration 的图生 / 宫格路径：script_plan_normalized_script.json / script_plan_segments.json；
- reference_video 路径（跨 narration / drama content_mode）：script_plan_reference_units.json。
三者的 script_plan 变体由 ``script_plan_kind`` 统一判定（reference_video 按项目生成模式优先）。ad 无 script_plan，
不纳入 gate。
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from arcreel_market_core.validation_messages import ValidationMessage
from lib.artifacts.formal_write import formal_write_transaction, project_metadata_lock
from lib.episode.episode_paths import (
    REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME,
    SCRIPT_PLAN_FILENAMES,
    episode_drafts_dir,
    episode_script_filename,
)
from lib.infra.content_digest import canonical_json_digest, prefixed, unprefixed
from lib.infra.json_io import atomic_write_json, load_json_or_none
from lib.infra.path_safety import try_safe_join
from lib.infra.validation_messages import default_translate
from lib.project import project_trash
from lib.project.project_activity import record_project_activity
from lib.project.project_manager import ProjectManager, find_episode, is_reference_video_project
from lib.script.draft_quarantine import (
    DRAFT_OWNER_AGENT,
    QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
    QUARANTINE_KIND_NARRATION_SCRIPT_PLAN,
    QUARANTINE_KIND_PROMPT_AUTHORING,
    QUARANTINE_KIND_SCRIPT_PLAN,
    draft_owner,
    quarantine_path,
    read_quarantine,
)
from lib.script.reference_video.duration_migration import migrate_unit_durations
from lib.script.script_editor import ScriptEditError, resolve_items
from lib.script.script_models import get_generated_assets
from lib.script.script_plan_entries import (
    ScriptPlanKind as ScriptPlanKind,
)

if TYPE_CHECKING:
    from lib.artifacts.artifact_manifest import ArtifactBasis

logger = logging.getLogger(__name__)

#: Content review status: not_applicable = the episode doesn't use the gate; no_script_plan = applicable but no script_plan produced;
#: pending_review = script_plan produced but not confirmed (or content changed after confirmation); confirmed = confirmed (converted to the final script).
#: 内容确认状态：not_applicable=该集不走 gate；no_script_plan=适用但 script_plan 未产出；
#: pending_review=script_plan 已产出但未经确认（或确认后内容又变）；confirmed=已确认（已转为正式脚本）。
ReviewStatus = Literal["not_applicable", "no_script_plan", "pending_review", "confirmed"]

#: Field name of the confirmation record on the episode entry: ``{"fingerprint": str, "confirmed_at": ISO8601}``.
#: 确认记录在 episode 条目上的字段名：``{"fingerprint": str, "confirmed_at": ISO8601}``。
REVIEW_FIELD = "script_plan_review"


#: A stale ledger entry records the content fingerprint of the old script_plan at replan submission; a changed live fingerprint proves script_plan was rebuilt per the new ledger.
#: stale 账本条目记录重规划提交时旧 script_plan 的内容指纹；live 指纹变化即证明 script_plan 已按新账本重建。
STALE_SCRIPT_PLAN_REVISION_FIELD = "stale_script_plan_revision"

#: Fact that the script_plan of a stale episode finished rebuilding. The fingerprint may equal the old content, so whether a rebuild ran can't be inferred from content change alone.
#: stale 分集的 script_plan 重建完成事实。指纹可能与旧内容相同，不能仅以内容变化推断是否执行过重建。
STALE_SCRIPT_PLAN_REBUILT_REVISION_FIELD = "stale_script_plan_rebuilt_revision"

# The literal definition of ScriptPlanKind lives in lib.script.script_plan_entries and is re-exported by the import at the top of this module.
# ScriptPlanKind 的字面量定义在 lib.script.script_plan_entries，由本模块顶部的 import 再导出。


def script_plan_kind(project: dict[str, Any]) -> ScriptPlanKind | None:
    """The project's script_plan variant; None when there is no structured script_plan intermediate state (e.g. ad).

    reference_video is a generation_mode dimension across content_mode (narration / drama both possible), decided first
    by project generation mode; otherwise content_mode falls to drama / narration. A content_mode not in
    SCRIPT_PLAN_FILENAMES (ad) has no script_plan, and reference_video doesn't apply either. The variant is uniquely
    determined by the project's two axes and doesn't vary by episode number.

    项目的 script_plan 变体；无结构化 script_plan 中间态（如 ad）时返回 None。

    reference_video 是 generation_mode 维度、跨 content_mode（narration / drama 均可），按项目
    生成模式优先判定；否则按 content_mode 落 drama / narration。content_mode 非
    SCRIPT_PLAN_FILENAMES 成员（ad）即无 script_plan，reference_video 亦不适用。变体由项目两轴唯一决定，
    不随集号变化。
    """
    content_mode = project.get("content_mode")
    if content_mode not in SCRIPT_PLAN_FILENAMES:
        return None
    if is_reference_video_project(project):
        return "reference_video"
    return content_mode  # "drama" | "narration" (members of SCRIPT_PLAN_FILENAMES) / "drama" | "narration"（SCRIPT_PLAN_FILENAMES 成员）


def is_applicable(project: dict[str, Any]) -> bool:
    """Whether the gate applies to the project: it has a structured script_plan variant (drama / narration / reference_video).

    gate 是否适用于该项目：拥有结构化 script_plan 变体（drama / narration / reference_video）。"""
    return script_plan_kind(project) is not None


def script_plan_path(project_path: Path, project: dict[str, Any], episode: int) -> Path | None:
    """Path of the episode's structured script_plan intermediate file; None when the gate doesn't apply.

    该集结构化 script_plan 中间态文件路径；不适用 gate 时返回 None。"""
    kind = script_plan_kind(project)
    if kind is None:
        return None
    filename = REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME if kind == "reference_video" else SCRIPT_PLAN_FILENAMES[kind]
    return episode_drafts_dir(project_path, episode) / filename


#: script_plan variant -> the variant's draft source. Each of the three routes has one slot; absence means "this variant has no draft slot",
#: and content review and the generation side don't block on it (ad has no structured script_plan and gets no variant anyway).
#: script_plan 变体 → 该变体的草稿来源。三条路线各有一位；缺席即「该变体无草稿位」，
#: 内容确认与生成侧据此不阻塞（ad 无结构化 script_plan，本就取不到变体）。
_SCRIPT_PLAN_QUARANTINE_KIND: dict[str, str] = {
    "reference_video": QUARANTINE_KIND_SCRIPT_PLAN,
    "drama": QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
    "narration": QUARANTINE_KIND_NARRATION_SCRIPT_PLAN,
}


def script_plan_quarantine_kind(project: dict[str, Any]) -> str | None:
    """Draft source for the project's script_plan variant; None when the variant has no draft slot.

    该项目 script_plan 变体对应的草稿来源；该变体无草稿位时返回 None。"""
    kind = script_plan_kind(project)
    return _SCRIPT_PLAN_QUARANTINE_KIND.get(kind) if kind is not None else None


def script_plan_quarantine_path(project_path: Path, project: dict[str, Any], episode: int) -> Path | None:
    """Path of the episode's script_plan draft; None when the variant has no draft slot.

    Only returns the path without checking existence -- existence is checked in ``script_plan_quarantined``; they are
    separate so callers can get the path when they need it for an error message.

    该集 script_plan 草稿的路径；该变体无草稿位时返回 None。

    只回路径、不判存在性——存在性判断在 ``script_plan_quarantined``，两者分开是为了让调用方在
    需要报错文案时能拿到路径。
    """
    quarantine_kind = script_plan_quarantine_kind(project)
    if quarantine_kind is None:
        return None
    return quarantine_path(project_path, episode, quarantine_kind)


def script_plan_quarantined(project_path: Path, project: dict[str, Any], episode: int) -> bool:
    """Whether a draft of the episode's script_plan is present -- the blocking criterion of the gate and prompt_authoring.

    A pending draft is independent of "the content fingerprint of the formal script_plan": while an invalid draft or
    editable draft is present the formal file stays unchanged, so checking only the fingerprint would wrongly let
    prompt_authoring through. A present draft therefore blocks on its own.

    Drafts are resolved per the project's current generation mode (see ``script_plan_quarantine_path``); leftover drafts
    of other generation modes don't count, otherwise the episode would be stuck forever on a file no current writer cleans up.

    该集 script_plan 是否有草稿在场——gate 与 prompt_authoring 的阻塞判据。

    待处置草稿与「正式 script_plan 的内容指纹」相互独立：待修复草稿或可编辑草稿在场时正式文件
    保持不变，仅检查指纹会错误放行 prompt_authoring。草稿在场因此独立阻塞。

    草稿按项目当前生成模式解析（见 ``script_plan_quarantine_path``）；其他生成模式的遗留草稿
    不参与判定，否则该集会被一份没有当前写入方清理的文件永久卡死。
    """
    path = script_plan_quarantine_path(project_path, project, episode)
    return path is not None and path.exists()


def content_fingerprint_of_data(data: object) -> str:
    """Fingerprint of a parsed JSON object: sha256 of a canonical dump (key order / whitespace changes don't change it).

    Lets callers fingerprint an object already in memory (e.g. a pre-migration snapshot) without a second disk read -- the
    fingerprint must correspond to the very content the caller holds. Same canonicalization as the JSON branch of
    ``content_fingerprint``, only the input changes from a path to a parsed object, so both give the same value for the
    same content.

    已解析 JSON 对象的指纹：规范化 dump（键序 / 空白重排不改指纹）的 sha256。

    供调用方对已读入内存的对象直接取指纹（如迁移前 snapshot），不经二次磁盘读取——指纹须
    对应调用方手里这份内容本身。与 ``content_fingerprint`` 的 JSON 分支同一套规范化逻辑，
    仅入参从路径换成已解析对象，故对同一份内容两者取值相同。
    """
    return canonical_json_digest(data)


def content_fingerprint(path: Path) -> str | None:
    """script_plan content fingerprint: valid JSON takes the sha256 of a canonical dump (key order / whitespace changes don't change it, only semantic changes do),
    non-JSON degrades to the sha256 of raw bytes; None when the file doesn't exist (FileNotFoundError).

    Only "file does not exist" degrades to None (-> no_script_plan, gate lets through); other OSErrors such as missing
    permission, a directory in its place or transient I/O are all raised, so real file system faults aren't silently
    treated as "script_plan not produced" and wrongly let prompt_authoring through.

    To fingerprint an object already in memory (e.g. a pre-migration snapshot) use ``content_fingerprint_of_data``; don't
    call this function again on the same file -- a concurrent write between two independent reads would be swallowed by
    this function's second read, see its docstring.

    script_plan 内容指纹：合法 JSON 取规范化 dump 的 sha256（键序 / 空白重排不改指纹、语义变更才改），
    非 JSON 退化为原始字节 sha256；文件不存在（FileNotFoundError）时 None。

    只把「文件不存在」降级为 None（→ no_script_plan、gate 放行）；权限不足、目录占位、短暂 I/O 等其它
    OSError 一律向上抛，避免把真实文件系统故障静默当成「script_plan 未产出」而误放行 prompt_authoring。

    对已读入内存的对象取指纹（如迁移前 snapshot）用 ``content_fingerprint_of_data``，不要对同一
    文件再调一次本函数——两次独立读取之间的并发写入会被此函数的第二次读取吞掉，见其 docstring。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return hashlib.sha256(raw).hexdigest()
    return content_fingerprint_of_data(parsed)


def published_revision(fingerprint: str | None) -> str | None:
    """External revision form of the content fingerprint: with the ``sha256-v1:`` prefix, the same representation as other revisions on the read surface.

    The fingerprint itself (persisted baselines, draft meta, project records) remains a bare hexdigest; the prefix is only
    added when handed to clients, and inputs are stripped via ``fingerprint_of_revision`` before comparing with fingerprints.

    内容指纹对外的 revision 写法：带 ``sha256-v1:`` 前缀，与读取面其余 revision 同一种表示。

    指纹本身（落盘的基线、草稿 meta、项目记录）仍是裸 hexdigest；只在交给客户端时加前缀，
    入参经 ``fingerprint_of_revision`` 去前缀后再与指纹比对。
    """
    return prefixed(fingerprint) if fingerprint is not None else None


def fingerprint_of_revision(revision: str | None) -> str | None:
    """Revision sent back by the client -> content fingerprint: both the prefixed and bare hexdigest forms are accepted.

    客户端传回的 revision → 内容指纹：带前缀与裸 hexdigest 两种写法都接受。"""
    return unprefixed(revision) if revision is not None else None


class _UncheckedFingerprint(enum.Enum):
    """``write_script_plan_locked`` 的 ``expected_fingerprint`` 哨兵类型：区分「不做基线比对」与
    「基线是 None（写入方取基线时正式文件不存在）」——两者都要能表达，``None`` 只够表达后者。"""

    TOKEN = 0


#: "No baseline comparison" sentinel: passed when the writer has no baseline (re-split overwriting wholesale, read-modify-write in the same critical section).
#: 「不做基线比对」哨兵：写入方没有基线可言（重拆分整份覆盖、同临界区读改写）时传它。
UNCHECKED_FINGERPRINT = _UncheckedFingerprint.TOKEN


class ScriptPlanWriteConflict(Exception):
    """Optimistic concurrency conflict on the formal script_plan: the writer's baseline fingerprint differs from the value on disk.

    Carries the value on disk (``current_content``, None for invalid JSON) and both fingerprints, so the editor can render
    a conflict report and merge against the latest content -- the single write exit compares under the lock and raises, so
    the later writer gets this exception instead of silently overwriting the earlier one.

    正式 script_plan 的乐观并发冲突：写入方的基线指纹与盘上现值不一致。

    携带盘上现值（``current_content``，非法 JSON 时 None）与两侧指纹，供编辑方渲染冲突报告、
    对照最新内容合并——单一写盘出口在锁内比对后抛出，后写方收到本异常而非静默覆盖先写方。
    """

    def __init__(self, *, expected: str | None, actual: str | None, current_content: dict[str, Any] | None):
        super().__init__(f"script_plan concurrency conflict: base fingerprint {expected}, on-disk fingerprint {actual}")
        self.expected = expected
        self.actual = actual
        self.current_content = current_content


def mark_ledger_stale(project_path: Path, project: dict[str, Any], entry: dict[str, Any], episode: int) -> None:
    """Mark an episode's episode planning status as stale, with the current script_plan content fingerprint as the rebuild baseline.

    When an already stale episode is changed again, the baseline is reset and the previous round's rebuild-done record is
    cleared.

    把一集的集规划状态标为 stale，以当前 script_plan 的内容指纹为重建基线。

    已是 stale 的集再次被改动时重设基线，并清掉上一轮的重建完成记录。
    """
    entry["ledger_status"] = "stale"
    path = script_plan_path(project_path, project, episode)
    entry[STALE_SCRIPT_PLAN_REVISION_FIELD] = content_fingerprint(path) if path is not None else None
    entry.pop(STALE_SCRIPT_PLAN_REBUILT_REVISION_FIELD, None)


class ScriptPlanRebuildCompletionError(ValueError):
    """A stale script_plan rebuild cannot be recorded against the current ledger state."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def complete_stale_script_plan_rebuild(
    pm: ProjectManager,
    project_name: str,
    episode: int,
    expected_stale_revision: str | None,
) -> str:
    """Record preprocessing completion for a stale entry, including byte-identical rebuilds."""
    if isinstance(episode, bool) or episode < 1:
        raise ScriptPlanRebuildCompletionError("invalid_episode", "episode must be a positive integer")

    project_path = pm.get_project_path(project_name)
    committed: dict[str, str] = {}

    def _commit(project: dict[str, Any]) -> None:
        entry = find_episode(project, episode)
        if entry is None or entry.get("ledger_status") != "stale":
            raise ScriptPlanRebuildCompletionError("not_stale", "episode is not awaiting a stale script_plan rebuild")
        if STALE_SCRIPT_PLAN_REVISION_FIELD not in entry:
            raise ScriptPlanRebuildCompletionError("missing_baseline", "stale episode has no rebuild baseline")
        if entry.get(STALE_SCRIPT_PLAN_REVISION_FIELD) != fingerprint_of_revision(expected_stale_revision):
            raise ScriptPlanRebuildCompletionError(
                "baseline_conflict", "stale script_plan baseline changed; refresh workflow status"
            )
        path = script_plan_path(project_path, project, episode)
        revision = content_fingerprint(path) if path is not None else None
        if revision is None:
            raise ScriptPlanRebuildCompletionError("script_plan_missing", "rebuilt script_plan file is missing")
        entry[STALE_SCRIPT_PLAN_REBUILT_REVISION_FIELD] = revision
        committed["revision"] = revision

    pm.update_project(project_name, _commit)
    return committed["revision"]


def assert_base_fingerprint(path: Path, expected: str | _UncheckedFingerprint | None) -> None:
    """Optimistic concurrency comparison: when ``expected`` differs from the value of ``path`` on disk, raise ``ScriptPlanWriteConflict`` and don't write.

    ``expected`` is the file fingerprint when the writer took its baseline (``None`` means the file didn't exist then);
    ``UNCHECKED_FINGERPRINT`` skips the comparison. All three script_plan variants share this function, so the comparison
    semantics exist in one place only.

    The caller must already hold the exclusive lock on the path: if comparison and the subsequent write aren't in the same
    critical section, the comparison is just a stale read.

    乐观并发比对：``expected`` 与 ``path`` 盘上现值不一致时抛 ``ScriptPlanWriteConflict``、不落盘。

    ``expected`` 是写入方取基线时的文件指纹（``None`` 表示彼时文件不存在），
    ``UNCHECKED_FINGERPRINT`` 跳过比对。三个 script_plan 变体共用本函数，比对语义只存在这一处。

    调用方须已持该路径的排他锁：比对与随后的写盘不在同一临界区内，比对就只是一次过期读。
    """
    if isinstance(expected, _UncheckedFingerprint):
        return
    actual = content_fingerprint(path)
    if expected == actual:
        return
    current = load_json_or_none(path)
    raise ScriptPlanWriteConflict(
        expected=expected,
        actual=actual,
        current_content=current if isinstance(current, dict) else None,
    )


def official_reference_script_plan_path(project_path: Path, episode: int) -> Path:
    """Path of the episode's reference-to-video formal script_plan (``drafts/episode_N/script_plan_reference_units.json``).

    Difference from ``script_plan_path``: that one decides the file name by project variant and returns None when not
    applicable; this function is the path source of truth for the rv write exit and doesn't depend on the project dict.

    该集参考生视频正式 script_plan 的路径（``drafts/episode_N/script_plan_reference_units.json``）。

    与 ``script_plan_path`` 的区别：后者按项目变体判定文件名、不适用时 None；本函数是 rv 写盘出口
    的路径真相源，不依赖 project dict。"""
    return episode_drafts_dir(project_path, episode) / REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME


@contextmanager
def formal_script_plan_lock(project_path: Path, episode: int, path: Path) -> Generator[Path]:
    """Write critical section of any variant's formal script_plan: create dirs + per-path exclusive lock, yield the formal file path.

    Shares the same ``ProjectManager.file_lock`` (per-path, exclusive across processes, non-reentrant) with migration
    read-modify-write, Web saves and re-split / promotion. Every operation that "reads the formal file and then writes to
    disk or writes a draft based on it" should be wrapped entirely in this critical section -- reading and writing
    separately outside the lock is a concurrent-overwrite window.

    The path is passed by the caller per variant (``script_plan_path`` / ``official_reference_script_plan_path``): the
    three script_plan variants have different formal file names, and the lock granularity is the file itself, so variants
    can't each make their own lock.

    任一变体正式 script_plan 的写临界区：建目录 + per-path 排他锁，yield 该正式文件路径。

    与迁移读改写、Web 端保存、重拆分 / 晋升共用同一把 ``ProjectManager.file_lock``（per-path，
    进程间排他、不可重入）。凡要「读正式文件后据此写盘或写草稿」的操作都应整段包在本
    临界区内——读与写拆开在锁外各做一次，就是并发覆盖窗口。

    路径由调用方按变体传入（``script_plan_path`` / ``official_reference_script_plan_path``）：三个 script_plan
    变体的正式文件名不同，锁的粒度是文件本身，不能按变体各自造一把。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pm = ProjectManager.for_project_dir(project_path)
    with pm.file_lock(path):
        yield path


@contextmanager
def script_plan_write_lock(project_path: Path, episode: int) -> Generator[Path]:
    """Write critical section of the reference-to-video formal script_plan (named entry of ``formal_script_plan_lock`` bound to that variant's path).

    参考生视频正式 script_plan 的写临界区（``formal_script_plan_lock`` 绑定该变体路径的具名入口）。"""
    with formal_script_plan_lock(
        project_path, episode, official_reference_script_plan_path(project_path, episode)
    ) as path:
        yield path


@contextmanager
def formal_script_plan_write_transaction(
    project_path: Path,
    episode: int,
    *paths: Path,
    basis: ArtifactBasis | None = None,
    record_activity: bool = True,
) -> Generator[None]:
    """Commit formal script_plan files and their active Manifest claim as one unit.

    Callers own the canonical per-path lock.  Every Python write path for a
    drama, narration, or reference-video script_plan enters this context so a
    successful write refreshes the same typed claim, while registration
    failure restores every supplied formal file byte-for-byte.

    A committed write moves the project's last activity forward: script_plan
    files are JSON, whose modification time does not count as activity.  Pass
    ``record_activity=False`` for a write that leaves the content unchanged.
    Exits called inside the context pass their own activity recording up to
    this commit: the ledger is project-global and stays out of the rollback
    set, so other writers' records survive a failed commit.
    """

    with project_metadata_lock(project_path), formal_write_transaction(*paths):
        yield
        from lib.artifacts.artifact_activation import (
            register_current_artifact,
            register_current_artifact_if_provable,
        )
        from lib.artifacts.artifact_manifest import ArtifactKey

        # A successful no-op write can still repair a missing claim after a
        # temporarily unavailable source made activation skip this target.
        key = ArtifactKey.episode_script_plan(episode)
        if basis is None:
            register_current_artifact_if_provable(project_path, key)
        else:
            if not paths:
                raise ValueError("a frozen script_plan basis requires its formal artifact path")
            register_current_artifact(
                project_path,
                key,
                artifact_path=paths[0].relative_to(project_path).as_posix(),
                basis=basis,
            )
    if record_activity:
        record_project_activity(project_path)


def delete_script_plan_file(project_path: Path, episode: int, path: Path) -> bool:
    """Move the formal script_plan into the project trash and remove its artifact registration in the same transaction.

    把正式 script_plan 移进项目回收站，并在同一事务里撤掉它的产物登记。"""

    pm = ProjectManager.for_project_dir(project_path)
    with pm.file_lock(path):
        if not path.exists():
            return False
        trash_script_plan_locked(project_path, episode, path)
    return True


def trash_script_plan_locked(project_path: Path, episode: int, path: Path) -> None:
    """Locked body of :func:`delete_script_plan_file`: the caller already holds the file's per-path lock.

    :func:`delete_script_plan_file` 的锁内主体：调用方已持有该文件的 per-path 锁。"""
    from lib.script.planning_draft_trash import trash_planning_draft

    record = None
    try:
        with formal_script_plan_write_transaction(project_path, episode, path):
            record = trash_planning_draft(project_path, episode, path)
    except BaseException:
        # If unregistration fails, the transaction has written the file back byte for byte and the record is removed; if the write-back also fails, the record stays and no content is lost.
        # 撤登记失败时事务已把文件按字节写回，记录随之撤掉；写回也失败时记录留着，内容不丢。
        if record is not None and path.is_file():
            project_trash.purge(project_path, record.id)
        raise


def write_formal_script_plan_locked(
    project_path: Path,
    episode: int,
    path: Path,
    content: dict[str, Any],
    *,
    expected_fingerprint: str | _UncheckedFingerprint | None = UNCHECKED_FINGERPRINT,
    dependent_quarantine: str | None = None,
    clear_dependent_quarantine: bool = True,
    basis: ArtifactBasis | None = None,
) -> bool:
    """The **single write exit** of any variant's formal script_plan: baseline comparison (OCC) -> atomic write -> on content change, clear the invalidated
    downstream draft. Returns whether the content changed.

    The caller must already hold the file's exclusive lock (``formal_script_plan_lock``, or ``ProjectManager.file_lock``
    on the same path -- the lock is non-reentrant, callers already in the critical section can't nest another); when
    ``dependent_quarantine`` is given, the draft lock must be held first too, with the uniform lock order "downstream draft
    -> formal script_plan". All write paths of the three variants (Web save, re-split / re-normalize, promotion, migration
    write-back) funnel into this function. The formal script_plan is write-forbidden to the Agent precisely because writes
    only happen at this one locked exit.

    ``expected_fingerprint`` is the formal file fingerprint when the writer took its baseline (``None`` means the file
    didn't exist then); when it differs from the value on disk, raise ``ScriptPlanWriteConflict`` and don't write -- the
    later writer takes the conflict report to merge, and the earlier writer's content isn't silently overwritten. Pass
    ``UNCHECKED_FINGERPRINT`` to skip the comparison: re-split is a deliberate wholesale rebuild, and read-modify-write in
    the same critical section (migration, confirmation) has no concurrency window between read and write anyway.

    ``dependent_quarantine`` is the downstream draft source based on this file (only reference-to-video prompt_authoring;
    drama / narration script_plan have no downstream draft, pass None). It joins the transaction with this file: if the
    write fails both roll back byte for byte, never leaving the half state "formal file is old, draft already cleared".
    It is invalidated only when the base really changed -- migration write-back is mechanical format absorption, not a
    content edit, so callers pass ``clear_dependent_quarantine=False`` to keep it.

    任一变体正式 script_plan 的**单一写盘出口**：基线比对（OCC）→ 原子写 → 内容变化时清作废的
    下游草稿。返回内容是否发生变化。

    调用方须已持有该文件的排他锁（``formal_script_plan_lock``，或同一路径的
    ``ProjectManager.file_lock``——锁不可重入，已在临界区内的调用方不能再套一层）；指定
    ``dependent_quarantine`` 时还须先持有该草稿锁，统一锁序为「下游草稿 → 正式 script_plan」。三个变体
    的全部写路径（Web 端保存、重拆分 / 重规范化、晋升、迁移回写）汇入本函数。正式 script_plan
    之所以对 Agent 写禁，正是因为写盘只发生在这一个持锁的出口。

    ``expected_fingerprint`` 是写入方取基线时的正式文件指纹（``None`` 表示彼时文件不存在）；
    与盘上现值不一致时抛 ``ScriptPlanWriteConflict``、不落盘——后写方拿冲突报告去合并，先写方的
    内容不被静默覆盖。传 ``UNCHECKED_FINGERPRINT`` 跳过比对：重拆分是刻意的整份重建，
    同临界区读改写（迁移、确认）则读写之间本就无并发窗口。

    ``dependent_quarantine`` 是以本文件为基底的下游草稿来源（只有参考生视频的 prompt_authoring；
    drama / narration script_plan 没有下游草稿，传 None）。它随本文件一并进事务：写盘失败时两者都
    按字节回滚，不会留下「正式文件是旧的、草稿已被清掉」的半场。基底真的变了才作废它——迁移回写是机械
    格式收编、不是内容编辑，调用方传 ``clear_dependent_quarantine=False`` 保留。
    """
    assert_base_fingerprint(path, expected_fingerprint)
    previous = load_json_or_none(path)
    changed = previous != content
    quarantine = None if dependent_quarantine is None else quarantine_path(project_path, episode, dependent_quarantine)
    paths = (path,) if quarantine is None else (path, quarantine)
    from lib.script.planning_draft_trash import trash_planning_draft

    record = None
    try:
        with formal_script_plan_write_transaction(project_path, episode, *paths, basis=basis, record_activity=changed):
            atomic_write_json(path, content)
            if changed and clear_dependent_quarantine and quarantine is not None:
                # Invalidate only when content changed: the draft goes into the project trash, accounted by the transaction per content change after commit
                # 内容变了才作废：草稿进项目回收站，提交后由事务按内容变化记账
                record = trash_planning_draft(project_path, episode, quarantine)
    except BaseException:
        # The transaction has written the draft back byte for byte, and the record is removed.
        # shortcut: when an outer formal_write_transaction wraps the draft and fails after this function returns, the outer layer writes the draft back while the record stays (one extra recoverable record, no content lost); when strictness is needed, create the record in the outermost transaction.
        # 事务已把草稿按字节写回，记录随之撤掉。
        # shortcut: 外层另有 formal_write_transaction 包住草稿、在本函数返回后失败时，外层写回草稿而记录留着（多一条可恢复的记录，内容不丢）；需要严格时把记录建在最外层事务里。
        if record is not None and quarantine is not None and quarantine.is_file():
            project_trash.purge(project_path, record.id)
        raise
    return changed


def write_script_plan_locked(
    project_path: Path,
    episode: int,
    content: dict[str, Any],
    *,
    expected_fingerprint: str | _UncheckedFingerprint | None = UNCHECKED_FINGERPRINT,
    clear_prompt_authoring_quarantine: bool = True,
    basis: ArtifactBasis | None = None,
) -> bool:
    """Write exit of the reference-to-video formal script_plan (named entry of ``write_formal_script_plan_locked`` bound to that variant's path and
    its downstream prompt_authoring draft).

    参考生视频正式 script_plan 的写盘出口（``write_formal_script_plan_locked`` 绑定该变体路径与其
    下游 prompt_authoring 草稿的具名入口）。"""
    return write_formal_script_plan_locked(
        project_path,
        episode,
        official_reference_script_plan_path(project_path, episode),
        content,
        expected_fingerprint=expected_fingerprint,
        dependent_quarantine=QUARANTINE_KIND_PROMPT_AUTHORING,
        clear_dependent_quarantine=clear_prompt_authoring_quarantine,
        basis=basis,
    )


def write_script_plan(
    project_path: Path,
    episode: int,
    content: dict[str, Any],
    *,
    expected_fingerprint: str | _UncheckedFingerprint | None = UNCHECKED_FINGERPRINT,
    clear_prompt_authoring_quarantine: bool = True,
    basis: ArtifactBasis | None = None,
    before_lock: Callable[[], None] | None = None,
) -> bool:
    """Run the reference script_plan transaction in global lock order: prompt_authoring draft, then formal script_plan."""
    prompt_authoring_path = quarantine_path(project_path, episode, QUARANTINE_KIND_PROMPT_AUTHORING)
    pm = ProjectManager.for_project_dir(project_path)
    if before_lock is not None:
        before_lock()
    with pm.file_lock(prompt_authoring_path), script_plan_write_lock(project_path, episode):
        return write_script_plan_locked(
            project_path,
            episode,
            content,
            expected_fingerprint=expected_fingerprint,
            clear_prompt_authoring_quarantine=clear_prompt_authoring_quarantine,
            basis=basis,
        )


def stored_review(project: dict[str, Any], episode: int) -> dict[str, Any]:
    """The episode's stored confirmation record (``episodes[i].script_plan_review``); empty dict when missing or malformed.

    该集已存的确认记录（``episodes[i].script_plan_review``），缺失或形状坏时返回空 dict。"""
    ep = find_episode(project, episode)
    review = ep.get(REVIEW_FIELD) if ep else None
    return review if isinstance(review, dict) else {}


@dataclass(frozen=True, slots=True)
class OverwrittenScriptEntry:
    """One final script entry an overwriting confirmation will remove, plus the artifacts and attributions under it that can't be restored within the project.

    覆盖式确认将移除的一条正式脚本条目，及其名下无法在项目内恢复的产物与归属。"""

    entry_id: str
    has_storyboard: bool
    has_video: bool
    has_narration_audio: bool
    has_end_frame: bool
    grid_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.entry_id,
            "has_storyboard": self.has_storyboard,
            "has_video": self.has_video,
            "has_narration_audio": self.has_narration_audio,
            "has_end_frame": self.has_end_frame,
            "grid_id": self.grid_id,
        }


@dataclass(frozen=True, slots=True)
class FormalScriptOverwrite:
    """The final script content review will overwrite: after confirmation all old entries are removed, artifacts are unregistered the same way as shot removal and moved, with version history, into the project trash.

    ``fingerprint`` is the final script content fingerprint at read time, exposed as ``revision``: callers send it back
    when acknowledging the overwrite; if it doesn't match the final script read at confirmation, it counts as not
    acknowledged and is rejected again with the new list; the overwrite write also uses it as baseline, so the
    acknowledgment only covers this one listed content.

    内容确认将覆盖的正式脚本：确认后旧条目全部移除，产物按分镜移除的口径撤登记、连同版本历史移进项目回收站。

    ``fingerprint`` 是读取时正式脚本的内容指纹，对外作 ``revision``：调用方认可覆盖时回传它，
    与确认时读到的正式脚本不符即视为未认可、按新清单重新拒绝；覆盖写入也以它为基线，认可只对应
    这一份被列出的内容。
    """

    fingerprint: str
    entries: tuple[OverwrittenScriptEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": published_revision(self.fingerprint),
            "entries": [entry.to_dict() for entry in self.entries],
            "storyboard_count": sum(entry.has_storyboard for entry in self.entries),
            "video_count": sum(entry.has_video for entry in self.entries),
            "narration_audio_count": sum(entry.has_narration_audio for entry in self.entries),
            "end_frame_count": sum(entry.has_end_frame for entry in self.entries),
            "grid_member_count": sum(entry.grid_id is not None for entry in self.entries),
            "grid_count": len({entry.grid_id for entry in self.entries if entry.grid_id is not None}),
        }


#: Artifact kinds listed per entry in the overwrite list: entry key of ``to_dict`` -> message key. Order is the text order.
#: 覆盖清单里逐条目列出的产物种类：``to_dict`` 的条目键 → 文案 key。顺序即行文顺序。
_OVERWRITE_ENTRY_KINDS: tuple[tuple[str, str], ...] = (
    ("has_storyboard", "script_overwrite_kind_storyboard"),
    ("has_video", "script_overwrite_kind_video"),
    ("has_narration_audio", "script_overwrite_kind_narration_audio"),
    ("has_end_frame", "script_overwrite_kind_end_frame"),
    ("grid_id", "script_overwrite_kind_grid"),
)

#: Summary items of the overwrite list: (count key, message key). Items with count 0 don't appear. / 覆盖清单的汇总项：（计数键，文案 key）。计数为 0 的项不出现。
_OVERWRITE_LOSS_TOTALS: tuple[tuple[str, str], ...] = (
    ("storyboard_count", "script_overwrite_loss_storyboard"),
    ("video_count", "script_overwrite_loss_video"),
    ("narration_audio_count", "script_overwrite_loss_narration_audio"),
    ("end_frame_count", "script_overwrite_loss_end_frame"),
    ("grid_member_count", "script_overwrite_loss_grid"),
)


def render_overwrite_loss_text(overwrite: Mapping[str, Any], translate: Callable[..., str]) -> str:
    """Render ``FormalScriptOverwrite.to_dict()`` into the loss list text.

    The Web confirm dialog and the Agent's ``script_overwrite_required`` receipt both take this one text; messages are
    only registered in the i18n key table, and callers no longer assemble the list themselves.

    把 ``FormalScriptOverwrite.to_dict()`` 渲染成丢失清单文本。

    Web 确认框与 Agent 的 ``script_overwrite_required`` 回执都取这一份文本，文案只登记在 i18n key 表里；
    调用方不再各自拼装清单。
    """
    entries: list[Mapping[str, Any]] = list(overwrite.get("entries") or ())
    separator = translate("script_overwrite_separator")
    lines = [translate("script_overwrite_summary", count=len(entries))]
    totals = [
        translate(key, count=count, grids=overwrite.get("grid_count", 0))
        for count_key, key in _OVERWRITE_LOSS_TOTALS
        if (count := int(overwrite.get(count_key) or 0)) > 0
    ]
    if totals:
        lines.append(translate("script_overwrite_loss", items=separator.join(totals)))
    if entries:
        lines.append(translate("script_overwrite_history"))
        rendered: list[str] = []
        for entry in entries:
            kinds = [translate(key) for field, key in _OVERWRITE_ENTRY_KINDS if entry.get(field)]
            rendered.append(
                translate("script_overwrite_entry", id=entry.get("id", ""), kinds=separator.join(kinds))
                if kinds
                else str(entry.get("id", ""))
            )
        lines.append(translate("script_overwrite_entries", entries=separator.join(rendered)))
    return "\n".join(lines)


def overwrite_with_text(overwrite: Mapping[str, Any] | None, translate: Callable[..., str]) -> dict[str, Any] | None:
    """Overwrite list with the rendered loss list text (``text``) attached; None when nothing is overwritten.

    覆盖清单附上渲染好的丢失清单文本（``text``）；无覆盖时 None。"""
    if overwrite is None:
        return None
    return {**overwrite, "text": render_overwrite_loss_text(overwrite, translate)}


def _bound_script_filename(project: Mapping[str, Any], episode: int) -> str | None:
    """Script file name bound in project.json for the episode (normalized to the name under ``scripts/``); None when unbound.

    该集 project.json 绑定的剧本文件名（归一到 ``scripts/`` 下的名字）；未绑定时 None。"""

    entry = find_episode(project, episode)
    binding = entry.get("script_file") if isinstance(entry, dict) else None
    if not isinstance(binding, str) or not binding:
        return None
    return ProjectManager.normalize_script_filename(binding) or None


class ForeignFormalScriptError(ValueError):
    """The episode has no readable/writable final script location: the binding isn't on disk, and the file at the canonical path isn't this episode's script.

    Carries the episode ID and the occupying file name so callers can give a locatable hint. Subclasses ``ValueError`` so
    call sites not yet handling this shape separately end as "reject" rather than "success".

    该集没有可读写的正式脚本位置：绑定不在盘上，而规范路径上那份文件不是本集剧本。

    携带集 ID 与占位的文件名，供调用方给出可定位的提示。继承 ``ValueError`` 让尚未单独处置这一形态
    的调用点也按「拒绝」而不是按「成功」收场。
    """

    def __init__(self, episode: int, filename: str) -> None:
        super().__init__(
            f"Episode (id={episode}): the canonical script path scripts/{filename} holds another episode's script,"
            f" so it cannot be read or written as this episode's final script"
        )
        self.episode = episode
        self.filename = filename


def formal_script_filename(project_path: Path, project: Mapping[str, Any], episode: int) -> str:
    """File name of the episode's final script under ``scripts/``.

    Takes the binding when the ``script_file`` bound in project.json points to a file on disk; falls back to the
    canonical file name ``episode_N.json`` when the binding is missing, out of bounds or the file doesn't exist. Content
    review reads the overwrite list and writes the final script through here, so both are always the same file.

    The fallback only holds when the canonical path is empty or holds this episode's script: episodes skipped by
    migration are bound to a vanished old path while the canonical path holds another episode's script, and an
    unconditional fallback would make reads get another episode's content and writes rebuild another episode's live
    script wholesale and bind this episode to it. This shape raises ``ForeignFormalScriptError``; the ownership criterion
    (not readable as an object, or internal episode number mismatch) matches the check before the v14->v15 rename.

    该集正式脚本在 ``scripts/`` 下的文件名。

    project.json 绑定的 ``script_file`` 指向盘上文件时取绑定；绑定缺失、越界或文件不存在时回落规范
    文件名 ``episode_N.json``。内容确认读覆盖清单与写正式脚本都经这里，两者始终是同一份文件。

    回落只在规范路径空着、或那上面确是本集剧本时成立：迁移跳过的集绑着已消失的旧路径，而规范路径
    上放着别集剧本，无条件回落会让读拿到别集内容、让写整份重建别集的在世剧本并把本集绑上去。这一
    形态抛 ``ForeignFormalScriptError``；归属判据（读不成对象或内部集号不符）与 v14→v15 改名前的
    那次校验一致。
    """
    filename = _bound_script_filename(project, episode)
    if filename is not None:
        path = try_safe_join(project_path / "scripts", filename)
        if path is not None and path.is_file():
            return filename
    canonical = episode_script_filename(episode)
    canonical_path = project_path / "scripts" / canonical
    if canonical_path.is_file():
        script = load_json_or_none(canonical_path)
        # Episode number is checked strictly as a positive integer: the script is read raw, JSON ``true`` becomes Python ``True``, which is both ``int``
        # and equal to ``1``, so comparing with ``!=`` would let a dirty file pass the ownership check posing as episode 1.
        # 集号按正整数严格判：剧本是裸读进来的，JSON ``true`` 变成 Python ``True``，它既是 ``int``
        # 又等于 ``1``，按 ``!=`` 比会让脏文件冒充第 1 集通过归属校验。
        recorded = script.get("episode") if isinstance(script, dict) else None
        if type(recorded) is not int or recorded != episode:
            raise ForeignFormalScriptError(episode, canonical)
    return canonical


def formal_script_overwrite(
    project_path: Path, project: Mapping[str, Any], episode: int
) -> FormalScriptOverwrite | None:
    """Read the final script content review will overwrite; None when the episode has no final script yet.

    Reads the script resolved by ``formal_script_filename``, i.e. the file the confirmation conversion will write. If the
    file exists but can't be read as a script (invalid JSON, broken entry array), it still counts as an existing final
    script with an empty entry list: overwriting it still needs acknowledgment.

    When the canonical path holds another episode's script, return None as if no script -- that content isn't this
    episode's and can't pad this episode's overwrite list; the confirmation conversion rejects with
    ``ForeignFormalScriptError`` before writing.

    读取内容确认将覆盖的正式脚本；该集尚无正式脚本时返回 None。

    读的是 ``formal_script_filename`` 解析出的那份剧本，即确认转换将写入的文件。文件存在但读不成
    剧本（非法 JSON、条目数组损坏）时照样算已有正式脚本、条目列表为空：覆盖它仍需认可。

    规范路径上是别集剧本时按无剧本返回 None——那份内容不属本集，不能列进本集的覆盖清单充数；
    确认转换会在写盘前以 ``ForeignFormalScriptError`` 拒绝。
    """
    try:
        filename = formal_script_filename(project_path, project, episode)
    except ForeignFormalScriptError:
        return None
    path = project_path / "scripts" / filename
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return FormalScriptOverwrite(fingerprint=hashlib.sha256(raw).hexdigest(), entries=())
    fingerprint = content_fingerprint_of_data(parsed)
    if not isinstance(parsed, dict):
        return FormalScriptOverwrite(fingerprint=fingerprint, entries=())
    try:
        items, id_field, _kind = resolve_items(parsed)
    except ScriptEditError:
        items, id_field = [], ""
    entries: list[OverwrittenScriptEntry] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(id_field), str) or not item[id_field]:
            continue
        assets = get_generated_assets(item)
        grid_id = assets.get("grid_id")
        entries.append(
            OverwrittenScriptEntry(
                entry_id=item[id_field],
                has_storyboard=bool(assets.get("storyboard_image")),
                has_video=bool(assets.get("video_clip")),
                has_narration_audio=bool(assets.get("narration_audio")),
                has_end_frame=bool(item.get("end_frame_image")),
                grid_id=grid_id if isinstance(grid_id, str) and grid_id else None,
            )
        )
    return FormalScriptOverwrite(fingerprint=fingerprint, entries=tuple(entries))


def prompt_authoring_generated(project_path: Path, project: dict[str, Any], episode: int) -> bool:
    """Whether the episode's prompt_authoring artifact (the generated script JSON) already exists.

    When a binding is present only the bound file counts, without the canonical path fallback of
    ``formal_script_filename``: a missing bound file with another episode's file at the canonical path is exactly the
    skipped shape migration records in its report, and that other episode's file must not count as "already produced".
    When unbound, judge by the canonical path.

    该集 prompt_authoring 产物（生成的剧本 JSON）是否已存在。

    绑定在场时只认绑定的那份文件，不走 ``formal_script_filename`` 的规范路径回落：绑定文件缺席而
    规范路径上是别集文件，正是迁移记进报告的跳过形态，不能据那份别集文件判成「已有产出」。未绑定时
    按规范路径判。
    """
    filename = _bound_script_filename(project, episode) or episode_script_filename(episode)
    path = try_safe_join(project_path / "scripts", filename)
    return path is not None and path.is_file()


def review_status(project_path: Path, project: dict[str, Any], episode: int) -> ReviewStatus:
    """Derive the episode's content review status.

    - no script_plan (or gate not applicable): not_applicable / no_script_plan;
    - has a confirmation fingerprint: matches the live script_plan content fingerprint -> confirmed, differs (script_plan
      changed) -> pending_review;
    - no confirmation fingerprint: pending_review. Same even with a final script present -- without a fingerprint there's
      no way to tell it came from the current plan.

    派生该集内容确认状态。

    - 无 script_plan（或 gate 不适用）：not_applicable / no_script_plan；
    - 有确认指纹：与 live script_plan 内容指纹一致 → confirmed，不一致（script_plan 改过）→ pending_review；
    - 无确认指纹：pending_review。正式脚本在场也一样——没有指纹就无从判断它出自眼前这份规划。
    """
    path = script_plan_path(project_path, project, episode)
    if path is None:
        return "not_applicable"
    # A present draft is checked before the fingerprint: an output failing constraints hasn't been handled yet, and whether the formal file is missing, old or confirmed,
    # the episode doesn't yet have a script_plan that can be let through. It's judged pending_review rather than a new status -- gate consumers
    # (blocking prompt_authoring, web status display) want exactly the "not let through" bit, and a new status would ripple to all consumers.
    # 草稿在场先于指纹判定：未满足约束的产物尚未处置，无论正式文件是缺失、旧版还是已确认，
    # 该集都还没有一份「可放行」的 script_plan。判 pending_review 而非新增状态——gate 的消费方
    # （阻塞 prompt_authoring、web 状态展示）要的正是「未放行」这一位，加状态会波及全部消费点。
    if script_plan_quarantined(project_path, project, episode):
        return "pending_review"
    live = content_fingerprint(path)
    if live is None:
        return "no_script_plan"
    return "confirmed" if _formal_script_plan_confirmed(project_path, project, episode, live) else "pending_review"


def _formal_script_plan_confirmed(project_path: Path, project: dict[str, Any], episode: int, live: str) -> bool:
    stored_fingerprint = stored_review(project, episode).get("fingerprint")
    if stored_fingerprint is not None:
        return stored_fingerprint == live
    # No confirmation fingerprint: we don't know which plan the final script came from, so the current one was never confirmed. The confirmation baseline of
    # legacy episodes is backfilled by the v14->v15 migration; when starting from scratch the confirmation record is deleted, which also lands here.
    # 没有确认指纹：不知道正式脚本出自哪一份规划，眼前这份就没有被确认过。存量集的确认基线由
    # v14→v15 迁移补记；从空白开始时确认记录被删去，同样落在这里。
    return False


def formal_script_plan_confirmed(project_path: Path, project: dict[str, Any], episode: int) -> bool:
    """Whether the formal script plan is the confirmed one; if so it is read-only, and content edits happen on the final script.

    Same criterion as ``review_status`` but ignoring invalid drafts: while a draft left by rerunning the script plan is
    present, the formal script plan is still the confirmed one and still read-only; once the rerun writes new content the
    fingerprint no longer matches and it becomes editable again.

    正式脚本规划是否就是已确认的那一份；是则只读，内容修改改在正式脚本上做。

    与 ``review_status`` 同一判据，但不看待修复草稿：重跑脚本规划留下的草稿在场时，正式脚本规划
    仍是已确认的那一份，照样只读；重跑写出新内容后指纹不再一致，即恢复可编辑。
    """
    path = script_plan_path(project_path, project, episode)
    if path is None:
        return False
    live = content_fingerprint(path)
    return live is not None and _formal_script_plan_confirmed(project_path, project, episode, live)


def formal_script_plan_agent_owned(project_path: Path, project: dict[str, Any], episode: int) -> bool:
    """While an editable draft fetched by the Agent is present, the formal script plan is read-only; callers check this inside the formal plan lock.

    Agent 取回的可编辑草稿在场时，正式脚本规划只读；调用方在正式规划锁内判断。"""
    kind = script_plan_quarantine_kind(project)
    draft = read_quarantine(project_path, episode, kind) if kind is not None else None
    return draft is not None and draft_owner(draft) == DRAFT_OWNER_AGENT


def apply_confirmation(project: dict[str, Any], episode: int, fingerprint: str, confirmed_at: str) -> bool:
    """Write the confirmation record in place into project ``episodes[i].script_plan_review``; return False when the episode entry doesn't exist.

    For the service layer to call inside the RMW callback of ProjectManager.update_project; the single source of truth for
    the persisted shape of the confirmation fingerprint lives here.

    就地把确认记录写入 project ``episodes[i].script_plan_review``；集条目不存在返回 False。

    供 service 层在 ProjectManager.update_project 的 RMW 回调内调用，确认指纹的持久化 shape
    单一真相源在此。
    """
    ep = find_episode(project, episode)
    if ep is None:
        return False
    ep[REVIEW_FIELD] = {"fingerprint": fingerprint, "confirmed_at": confirmed_at}
    return True


def carry_confirmation_through_migration(project: dict[str, Any], episode: int, before: str, after: str) -> bool:
    """Duration absorption migration of legacy script_plan drafts is mechanical format absorption, not a content edit, but the write-back drifts the content fingerprint.

    If the episode's confirmation fingerprint records exactly the pre-migration content (``before``), shift it to the
    post-migration value (``after``), so a long-confirmed episode doesn't wait for review again just because it was
    migrated; when the fingerprint doesn't match anyway (script_plan really was changed outside migration), leave it,
    return False, and treat it as pending review as usual.

    Only applies when migration has no warnings (pure format absorption, durations not rewritten by clamp): callers must
    skip this function when migration produced warnings and let the confirmation lapse as usual -- in that case the
    durations in ``after`` are no longer what the user saw when confirming, and shifting the confirmation would tacitly
    approve an unreviewed content change on the user's behalf.

    For callers to call inside the locked callback of ``ProjectManager.update_project`` (so the comparison is against the
    latest project re-read after locking) -- the two migration write-back entries in
    ``server/services/project/script_review.py`` and ``lib/script/script_generator.py`` share this function instead of
    each repeating this check. Cascading rewrites for asset renames, derivative renames and merges likewise only change
    names, not content, and also shift via this function (``ProjectManager._carry_script_plan_confirmations``).

    存量 script_plan 草稿的时长收编迁移是机械格式收编、不是内容编辑，但回写会让内容指纹漂移。

    若该集确认指纹恰好记的是迁移前内容（``before``），就把它平移到迁移后的值（``after``），
    避免一个早已确认的分集仅因被迁移回写就重新等待确认；指纹本就对不上（script_plan 在迁移外确实被
    改过）时不动，返回 False，照常按待确认处理。

    仅适用于迁移无 warnings 的情形（纯格式收编，时长取值未被 clamp 改写）：调用方须在迁移
    产生 warnings 时跳过本函数，让确认照常失效——那种情形下 ``after`` 携带的时长已不是用户
    确认时看到的值，平移确认等于替用户默许了一次未经审阅的内容变更。

    供调用方在 ``ProjectManager.update_project`` 的锁内回调中调用（确保比对的是加锁后重读的
    最新 project）——``server/services/project/script_review.py`` 与 ``lib/script/script_generator.py`` 的两处
    迁移写回入口共用本函数，不各自重复这段判断。资产改名、衍生改名与合并的级联改写同样只换名字、
    不改内容，也经本函数平移（``ProjectManager._carry_script_plan_confirmations``）。
    """
    stored = stored_review(project, episode)
    if stored.get("fingerprint") != before:
        return False
    apply_confirmation(project, episode, after, str(stored.get("confirmed_at") or ""))
    return True


def migrate_script_plan_draft_in_place(
    project_path: Path,
    content: object,
    *,
    episode: int,
    update_project: Callable[[Callable[[dict[str, Any]], None]], dict[str, Any]],
    supported_durations: Sequence[int] | None = None,
) -> tuple[dict[str, Any] | None, list[ValidationMessage]]:
    """Run the one-time duration absorption migration in place on a script_plan draft already in memory and write it back; return ``(最新 project, warnings)`` (latest project, warnings).

    The caller must hold two exclusive locks in the order "prompt_authoring draft -> formal script_plan"; the write-back
    goes via the single write exit ``write_script_plan_locked``, the same per-path lock as Web saves / re-split writes.
    Migration is mechanical format absorption, not a content edit, so it doesn't invalidate the prompt_authoring draft;
    read-modify-write in the same critical section has no concurrency window either, so no baseline comparison. When no
    migration happened nothing is written back and ``(None, [])`` is returned.

    Migration is mostly mechanical format absorption, but the write-back drifts the content fingerprint: via
    ``update_project`` the episode's confirmation fingerprint is shifted under the lock to the post-migration value
    (``carry_confirmation_through_migration``), so a confirmed episode doesn't wait for review again just because it was
    loaded. Non-empty ``warnings`` means migration clamp-rewrote actual duration values per options / structural range --
    that is a content change, the confirmation is not shifted, and confirmed episodes wait for review again via the
    fingerprint comparison as usual; episodes that never stored a fingerprint explicitly record the fingerprint of the
    pre-migration content, so they likewise mismatch and wait for review.
    Persisting this marker doesn't distinguish dry-run from real generation: after the migration lands idempotently,
    retries produce no more warnings, and only the persisted marker guarantees later generation stays blocked by content
    review.

    The project-side confirmation marker is persisted first and the draft after: if interrupted between the two writes, the
    draft still has pre-migration content and rerunning the migration on next load self-heals. In the reverse order the
    draft would have lost the old fields, the rerun would judge ``changed=False``, and the marker would be missing forever.

    When ``supported_durations`` is given (the prompt_authoring loading side holds model options), the absorption result
    snaps to options directly; when omitted (web gate side, capability resolution is async + DB and options can't be
    obtained synchronously), only structural range clamp is done.

    The two script_plan read entries in ``lib.script.script_generator.ScriptGenerator`` and
    ``server.services.project.script_review`` share this function, so migration and confirmation shifting checks aren't
    repeated in each.

    对已读入内存的 script_plan 草稿就地做一次性时长收编迁移并回写；返回 ``(最新 project, warnings)``。

    调用方须按「prompt_authoring 草稿 → 正式 script_plan」顺序持有两把排他锁；回写经单一写盘出口
    ``write_script_plan_locked``，与 Web 端保存 / 重拆分写盘同一把 per-path 锁。迁移是机械格式
    收编、不是内容编辑，不作废 prompt_authoring 草稿；同临界区读改写也无并发窗口，不做基线比对。
    未发生迁移时不回写，返回 ``(None, [])``。

    迁移多数情况下是机械格式收编，回写会让内容指纹漂移：经 ``update_project`` 在锁内把该集
    确认指纹平移到迁移后的值（``carry_confirmation_through_migration``），避免已确认分集仅因
    被加载就重新等待确认。``warnings`` 非空说明迁移按档位 / 结构区间 clamp 改写了实际时长取值——
    那是内容变更，不平移确认，已确认分集经指纹比对照常重新等待确认；从未存过指纹的集则显式记下
    迁移前内容的指纹，使其同样失配、等待确认。
    该标记的持久化不区分 dry-run 与真实生成：迁移幂等落盘后重试不再产生 warnings，只有落盘
    的标记能保证后续生成仍被内容确认阻塞。

    project 侧的确认标记先落盘、草稿后落盘：两次写之间中断时草稿仍是迁移前内容，下次加载
    重跑迁移即自愈。反序则草稿已丢失旧字段、重跑判 ``changed=False``，标记永久缺失。

    ``supported_durations`` 给定时（prompt_authoring 加载侧持有模型档位）收编结果直接取档；缺省
    （web gate 侧，能力解析是 async + DB、同步拿不到档位）只做结构区间 clamp。

    ``lib.script.script_generator.ScriptGenerator`` 与 ``server.services.project.script_review`` 的两处
    script_plan 读取入口共用本函数，迁移与确认平移的判断不各自重复。
    """
    if not isinstance(content, dict):
        return None, []
    before = content_fingerprint_of_data(content)
    changed, warnings = migrate_unit_durations(content.get("units"), supported_durations=supported_durations)
    for message in warnings:
        logger.warning(
            "script_plan 草稿 %s 时长收编迁移: %s",
            REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME,
            message.render(default_translate),
        )
    if not changed:
        return None, []

    if warnings:

        def _invalidate_unconfirmed(p: dict[str, Any]) -> None:
            # Episodes that already stored a fingerprint are left alone: if the confirmed content was pre-migration, the fingerprint already mismatches naturally; if it was
            # other content, review_status already judges pending_review.
            # 已存过指纹的分集不插手：确认的是迁移前内容时指纹已自然失配，确认的是别的
            # 内容时 review_status 本就判 pending_review。
            stored = stored_review(p, episode)
            if not stored.get("fingerprint"):
                apply_confirmation(p, episode, before, str(stored.get("confirmed_at") or ""))

        updated = update_project(_invalidate_unconfirmed)
    else:
        after = content_fingerprint_of_data(content)

        def _carry(p: dict[str, Any]) -> None:
            carry_confirmation_through_migration(p, episode, before, after)

        updated = update_project(_carry)

    write_script_plan_locked(project_path, episode, content, clear_prompt_authoring_quarantine=False)
    return updated, warnings
