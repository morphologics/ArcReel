"""Host-independent draft workflow implementation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.json_schema import SkipJsonSchema

from lib.artifacts.artifact_manifest import ArtifactBasis
from lib.catalog.errors import ModelNotInCatalogError
from lib.config.resolver import ConfigResolver
from lib.episode.episode_ids import episode_title
from lib.episode.episode_paths import SCRIPT_PLAN_FILENAMES, episode_drafts_dir, episode_script_filename
from lib.generation.video_request_facts import VideoRequestFactsError, VideoRequestFactsFailure
from lib.infra.async_thread import run_sync_transaction
from lib.infra.json_io import atomic_write_json, load_json_or_none
from lib.project.project_activity import record_project_activity
from lib.project.project_manager import ProjectManager, ScriptWriteConflict
from lib.script import script_review
from lib.script.draft_quarantine import (
    DOC_TYPE_TO_QUARANTINE_KIND,
    DRAFT_OWNER_AGENT,
    FORMAL_EDIT_META_KEY,
    PROMOTE_TOOL_NAME,
    QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
    QUARANTINE_KIND_NARRATION_SCRIPT_PLAN,
    QUARANTINE_KIND_PROMPT_AUTHORING,
    QUARANTINE_KIND_SCRIPT_PLAN,
    QUARANTINE_KIND_TO_DOC_TYPE,
    QuarantinedDraft,
    announce_draft_change,
    clear_quarantine,
    draft_owner,
    draft_payload,
    draft_revision,
    quarantine_and_report,
    quarantine_envelope,
    quarantine_exists,
    quarantine_path,
    read_quarantine,
    write_quarantine,
)
from lib.script.draft_violation import DraftViolation, schema_violations
from lib.script.plan_new_assets import NEW_ASSETS_FIELD, dedupe_new_assets, planning_project, with_new_assets
from lib.script.planning_draft_trash import discard_quarantine
from lib.script.script_generator import ScriptGenerator
from lib.script.script_models import (
    NarrationScriptPlanDraft,
    build_drama_normalized_script_model,
    build_reference_units_script_plan_model,
)
from lib.speech.speech_composition import admit_script_unit
from server.text_generation import (
    SOFT_VIOLATION_NOTE_QUARANTINED,
    ReferenceSplitCaps,
    SoftViolation,
    build_reference_units_from_flat,
    collect_drama_violations,
    collect_narration_violations,
    collect_reference_flat_violations,
    commit_single_script_plan,
    coverage_source_scope,
    drama_script_plan_result_text,
    fetch_reference_split_caps,
    fetch_storyboard_durations,
    load_novel_source,
    load_script_plan_source_with_basis,
    narration_script_plan_path,
    narration_script_plan_result_text,
    plan_item_ids,
    reference_result_text,
    reference_soft_violations,
    reissue_plan_item_ids,
    render_soft_violation_lines,
    render_soft_violation_section,
    uses_reference_video_units,
    video_facts_failure_text,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DraftContext:
    project_name: str
    data_root: Path
    pm: ProjectManager
    config_resolver: ConfigResolver | None = None

    @property
    def project_path(self) -> Path:
        return self.pm.get_project_path(self.project_name)

    async def aproject_path(self) -> Path:
        """Async entry for ``project_path``: locating the project directory calls stat, so offload it to a thread.

        ``project_path`` 的异步入口：定位项目目录会 stat，卸到线程。"""
        return await asyncio.to_thread(self.pm.get_project_path, self.project_name)


DraftDocType = Literal[
    "drama_script_plan", "narration_script_plan", "reference_script_plan", "reference_prompt_authoring"
]
PositiveEpisode = Annotated[int, Field(strict=True, ge=1)]
#: Shared description of the tool parameter ``episode_id``. The episode ID is only an internal identifier; talk to the
#: user in terms of title and episode order.
#: 工具参数 ``episode_id`` 的统一说明。集 ID 只是内部标识，对用户说标题与播出位置。
EPISODE_ID_DESCRIPTION = (
    "集 ID：一集的内部标识，取自项目详情 episodes[].episode 或制作计划的 episode_id，不是播出顺序中的第几集"
)


_DRAFT_REVISION_DESCRIPTION = "open_draft / 上次 patch_draft 返回的 revision；带 sha256-v1: 前缀与裸 hex 两种写法等价"


class _DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: PositiveEpisode = Field(description=EPISODE_ID_DESCRIPTION)
    doc_type: DraftDocType = Field(
        description=(
            "草稿对应的文档：drama_script_plan / narration_script_plan / reference_script_plan 为各创作类型的"
            " script_plan，reference_prompt_authoring 为参考生视频正式脚本的提示词编写稿"
        )
    )


class DraftLocator(_DraftRequest):
    source: str | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "可选小说源文件路径；仅在首次从正式 script_plan 创建草稿时用作重判来源，"
            "缺省为本集派生源文 source/episode_N.txt"
        ),
    )


class PatchDraftRequest(_DraftRequest):
    """``accept_formal_revision`` 与 ``source`` 以「是否出现」区分省略与显式 null：省略即不接受 / 不改。"""

    content: dict[str, Any] = Field(description="替换后的完整草稿正文；允许中间态不通过业务校验")
    base_revision: str = Field(description=_DRAFT_REVISION_DESCRIPTION)
    accept_formal_revision: str | None = Field(
        default=None,
        description=(
            "合并正式文档并发修改后，显式接受 open_draft 返回的 formal_revision（带 sha256-v1: 前缀与裸 hex "
            "两种写法等价）；正式文档已不存在时该值为 null，同样显式传 null。省略表示不接受"
        ),
    )
    source: str | None = Field(
        default=None,
        description="可选源文范围；仅在修正 script_plan 草稿的重判范围时提供，null 清除范围，省略则保持不变",
    )


class PromoteDraftRequest(_DraftRequest):
    base_revision: str = Field(description=_DRAFT_REVISION_DESCRIPTION)


class DiscardDraftRequest(_DraftRequest):
    base_revision: str = Field(description=_DRAFT_REVISION_DESCRIPTION)


class DraftWorkflowError(Exception):
    """``draft_refreshed`` true means promotion was blocked by violations and the violation report has been written
    back to the draft based on current values.

    ``failure`` non-empty means re-validation was blocked by video request facts (configuration problems such as
    unmet model capabilities or missing duration options, not violations of the draft itself): ``code`` is its issue
    code, and both Web and Agent present the real cause using the issue code, parameters and fix guidance.

    ``notice_key`` non-empty means the violation carries a user-facing text key with parameters ``notice_params``
    (e.g. a final script unit that failed the pre-check before promotion): ``code`` is unchanged, Web localizes the
    explanation by that key, and ``detail`` still targets the Agent.

    ``draft_refreshed`` 为真表示晋升被违约挡下、且违约报告已按现值写回草稿。

    ``failure`` 非空表示重判被视频请求事实挡下（模型能力不满足、档位缺失等配置问题，不是草稿本身的
    违约）：``code`` 即其问题码，Web 与 Agent 都按问题码、参数与修复指引呈现真实原因。

    ``notice_key`` 非空表示违约带有面向用户的文案 key 与参数 ``notice_params``（如晋升前正式剧本单元
    没过预判）：``code`` 不变，Web 按该 key 本地化说明原因，``detail`` 仍面向 Agent。
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        draft_refreshed: bool = False,
        failure: VideoRequestFactsFailure | None = None,
        notice_key: str | None = None,
        notice_params: dict[str, Any] | None = None,
    ):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.draft_refreshed = draft_refreshed
        self.failure = failure
        self.notice_key = notice_key
        self.notice_params = notice_params or {}

    @classmethod
    def video_facts(cls, failure: VideoRequestFactsFailure) -> DraftWorkflowError:
        return cls(failure.code, video_facts_failure_text(failure), failure=failure)


class ReferenceDraftRevalidation(NamedTuple):
    """Result of read-time re-validation of a script_plan draft.

    ``schema_failed`` explicitly separates two stages: True means the draft did not even pass the schema used at
    generation time (``flat_units`` is always empty, and the caller can only present ``draft.content`` as-is); when
    False, ``flat_units`` is the normalized flat output, and promotion is allowed when ``violations`` is empty. The
    two are handled differently (as-is vs normalized), so this is not inferred from whether ``flat_units`` is empty.

    ``soft_violations`` are structured entries for soft violations (voice degradation, unreferenced scenes), kept in
    a separate field from ``violations`` rather than merged: soft violations do not block promotion and do not enter
    the violation entries of the invalid draft; with one merged list, sooner or later some place would check
    "non-empty means blocked" and turn degradation notes into hard violations. Empty when the schema fails, since
    the body is not normalized yet and cannot be checked per unit.

    ``new_assets`` are this episode's new items after normalization and dedup, written back to the draft or to disk
    together with ``flat_units``.

    script_plan 草稿读时重判的结果。

    ``schema_failed`` 显式区分两个阶段：True 表示草稿连产出时的 schema 都没过（``flat_units``
    必为空，调用方只能按 ``draft.content`` 原样呈现）；False 时 ``flat_units`` 是收编后的扁平
    产出，``violations`` 为空即可晋升。两者的处置不同（原样 vs 收编），故不靠 ``flat_units``
    是否为空来反推。

    ``soft_violations`` 是软违约（声音降级、未引用场景）的结构化条目，与 ``violations``
    分属两个字段而非合流：软违约不阻断晋升、不进待修复草稿的违约条目，合成一个列表迟早会有一处
    按「非空即挡下」判定，把降级提示变成硬违约。schema 没过时为空——正文尚未收编，无从逐 unit 判。

    ``new_assets`` 是收编并去重后的本集新增项，随 ``flat_units`` 一起回写草稿或落盘。
    """

    violations: list[DraftViolation]
    flat_units: list[dict[str, Any]]
    caps: ReferenceSplitCaps
    schema_failed: bool
    basis: ArtifactBasis | None
    soft_violations: list[SoftViolation]
    new_assets: list[dict[str, Any]]

    def flat_content(self) -> dict[str, Any]:
        """Flat draft content after normalization.

        收编后的扁平草稿内容。"""
        return with_new_assets({"units": self.flat_units}, self.new_assets)


async def revalidate_reference_script_plan_draft(
    project_path: Path,
    project: dict[str, Any],
    episode: int,
    draft: QuarantinedDraft,
    *,
    config_resolver: ConfigResolver | None = None,
) -> ReferenceDraftRevalidation:
    """Fully re-validate a script_plan draft with the validators used at generation time; read-only, does not write to
    disk or clear the draft.

    Re-validation uses the same function as the split tool (``collect_reference_flat_violations``), not a simplified
    copy: the promotion rules, the read-time recheck in content review and the generation rules must share one piece
    of code, otherwise forks like "passes here, rejected at the next generation" reappear. Capabilities and source
    are both resolved again, since the user may have changed the model config or source while the draft exists, and
    re-validation must check against current values.

    Does not depend on ``DraftContext`` (``project_path`` / ``project`` are passed in by the caller instead of
    derived from ctx): the read-time recheck in content review (``server/services/project/script_review.py``) has no
    Agent tool ctx, only a ``ProjectManager``; both places share this function rather than each loading the project,
    and each caller loads it once.

    Raises ``ValueError`` when ``meta.source`` is missing (the draft was corrupted and cannot be re-validated).

    按产出时那套校验器全量重判 script_plan 草稿，只读、不写盘、不清草稿。

    重判走的是拆分工具用的同一个函数（``collect_reference_flat_violations``），不是它的简化
    副本：晋升口径、内容确认的读时重算与产出口径必须同一份代码，否则「这里放行、下次
    生成时被拒」这类分叉会重新出现。能力与源文都重新解析——草稿在场期间用户可能改过模型配置或
    源文，重判要对着现值判。

    不依赖 ``DraftContext``（``project_path`` / ``project`` 由调用方传入而非从 ctx 派生）：
    内容确认的读时重算（``server/services/project/script_review.py``）没有 Agent 工具的 ctx，
    只有 ``ProjectManager``；两处共用本函数而不各自加载 project，调用方各自加载一次即可。

    ``meta.source`` 缺失（草稿被改坏、无从重判）时抛 ``ValueError``。
    """
    # meta.source records the source range at generation time. A missing key means meta was corrupted: we must not
    # silently re-resolve with the default source; if a different source file was specified at generation time,
    # re-validating against another range would let through source-text anchors copied from elsewhere.
    # meta.source 记的是产出时的源文范围。缺键说明 meta 被改坏了：不能默默按缺省源文重解析
    # ——产出时若指定过别的源文件，换一份范围重判等于放行一份从别处抄来的原文锚。
    if "source" not in draft.meta:
        raise ValueError(
            f"draft {draft.path} is missing meta.source (the source range recorded at generation time); "
            "restore that field (the relative source path when a source was given, null when generated from this "
            "episode's derived source) and retry"
        )
    # The source can be several hundred KB; reading it synchronously inside this async function body would occupy the
    # event loop. The promotion tool runs on a separate session thread and is not sensitive, but the read-time recheck
    # in content review (same code) runs in the request coroutine, so offload to a thread to avoid slowing other
    # concurrent requests.
    # 源文可能达数百 KB，同步读盘直接放在这个 async 函数体里会占用
    # 事件循环——晋升工具走的是独立会话线程不敏感，但内容确认的读时重算（同一份代码）
    # 在请求协程里跑，卸到线程避免拖慢并发的其它请求。
    novel_text, _prompt_inputs, script_plan_basis = await asyncio.to_thread(
        load_script_plan_source_with_basis,
        project_path,
        draft.meta["source"],
        project,
        episode,
        "reference_video",
    )
    split_caps = await fetch_reference_split_caps(project, config_resolver=config_resolver)

    # A modified draft first goes through the schema used at generation time: on the split side, response_schema and
    # _parse_script_plan_json enforce the duration enum and non-empty fields; if the promotion side skipped this
    # layer, changing duration_seconds to a value outside the options, or deleting it entirely (becoming 0 seconds),
    # could be promoted all the way into the final file, which is exactly the "final file pollution" this mechanism
    # prevents. Schema violations on this path have no backend to retry (the content is written by the Agent), so
    # likewise return a report and let it keep editing.
    #
    # The outer shape (units missing / not an array / empty array) and per-unit field violations take the same report
    # path: both are mistakes the Agent makes when editing a draft, and if only the latter refreshed the report, the
    # former would throw it out of the "edit, then promote" loop.
    # 修改过的草稿先过产出时那份 schema：拆分侧由 response_schema 与 _parse_script_plan_json 卡住时长
    # 枚举与字段非空，晋升侧漏掉这一层的话，把 duration_seconds 改成非档位值、或整个删掉（收成
    # 0 秒）都能一路晋升进正式文件——正是本机制要防的「正式文件被污染」。schema 违约在这条路上
    # 没有 backend 可重试（内容是 Agent 写的），故同样回报告让它继续改。
    #
    # 外层形状（units 缺失 / 不是数组 / 空数组）与逐 unit 的字段违约走同一条报告路径：两者都是
    # Agent 编辑草稿时会犯的错，只有后者刷新报告的话，前者就把它甩出了「改完再晋升」的循环。
    raw_units = draft.content.get("units")
    schema = build_reference_units_script_plan_model(split_caps.durations)
    violations: list[DraftViolation] = []
    flat_units: list[dict[str, Any]] = []
    new_assets: list[dict[str, Any]] = []
    if not isinstance(raw_units, list) or not raw_units:
        logger.debug("草稿 content.units 形状非法: %s", type(raw_units).__name__)
        violations = [
            DraftViolation(
                "draft content.units must be a non-empty array of unit objects",
                code="schema_invalid",
            )
        ]
    else:
        try:
            flat = schema.model_validate(
                {"units": raw_units, NEW_ASSETS_FIELD: draft.content.get(NEW_ASSETS_FIELD, [])}
            ).model_dump()
        except ValidationError as exc:
            violations = schema_violations(exc, draft.content, "units")
        else:
            flat_units = flat["units"]
            new_assets = dedupe_new_assets(flat[NEW_ASSETS_FIELD])
    if violations:
        return ReferenceDraftRevalidation(
            violations, [], split_caps, schema_failed=True, basis=script_plan_basis, soft_violations=[], new_assets=[]
        )

    source_language = project.get("source_language")
    violations = await asyncio.to_thread(
        collect_reference_flat_violations,
        flat_units,
        project,
        project_path=project_path,
        episode=episode,
        novel_text=novel_text,
        caps=split_caps,
        source_language=source_language,
        new_assets=new_assets,
    )
    # Soft and hard violations are produced together in the same re-validation: they are computed even when there are
    # hard violations, and the report when promotion is blocked carries them too; otherwise the Agent would not see
    # degradation notes in any round of fixing the draft, and would only read them for the first time when the last
    # round promotes successfully.
    # 软违约与硬违约在同一次重判里一并产出：有硬违约时也要算，晋升被挡下时的报告同样带着它们，
    # 否则 Agent 修草稿的每一轮都看不见降级提示，直到最后一轮晋升成功才第一次读到。
    soft_violations = reference_soft_violations(
        [str(flat["text"]) for flat in flat_units],
        planning_project(project, new_assets),
        episode=episode,
        voice=split_caps.voice,
    )
    return ReferenceDraftRevalidation(
        violations,
        flat_units,
        split_caps,
        schema_failed=False,
        basis=script_plan_basis,
        soft_violations=soft_violations,
        new_assets=new_assets,
    )


def _project_relative(path: Path, project_path: Path) -> str:
    """Paths in the receipt are written only as project-relative paths, so the server filesystem layout is not leaked;
    outside the project directory only the file name is given.

    回执里的路径只写项目内相对路径，不外泄服务端文件系统布局；不在项目目录下时只给文件名。"""
    try:
        return path.relative_to(project_path).as_posix()
    except ValueError:
        return path.name


def _commit_reference_script_plan(
    project_path: Path,
    episode: int,
    content: dict[str, Any],
    expected_fingerprint: Any,
    basis: ArtifactBasis | None,
    before_commit: Callable[[], None] | None = None,
) -> None:
    if before_commit is not None:
        before_commit()
    script_review.write_script_plan(
        project_path,
        episode,
        content,
        expected_fingerprint=expected_fingerprint,
        basis=basis,
    )
    clear_quarantine(project_path, episode, QUARANTINE_KIND_SCRIPT_PLAN)


def _script_plan_confirmed_detail(episode: int) -> str:
    return (
        f"❌ The script plan of episode (id={episode}) is confirmed and read-only; it can no longer be changed. "
        "Edit the final script instead (patch_episode_script, or ask the user to edit it on the timeline); "
        "to redo the whole episode, re-run generate_script_plan: a new script plan returns the episode to "
        "awaiting confirmation."
    )


def _open_script_plan_draft(
    project_path: Path,
    episode: int,
    script_plan_path: Path,
    kind: str,
    source: str | None,
    to_draft_shape: Callable[[dict[str, Any]], dict[str, Any] | None],
    missing_detail: str,
) -> None:
    with script_review.formal_script_plan_lock(project_path, episode, script_plan_path):
        if quarantine_exists(project_path, episode, kind):
            return
        data = load_json_or_none(script_plan_path)
        content = to_draft_shape(data) if isinstance(data, dict) else None
        if content is None:
            raise DraftWorkflowError("draft_source_missing", missing_detail)
        write_quarantine(
            project_path,
            episode,
            kind,
            content=content,
            violations=[],
            meta={
                "source": source or None,
                "base_fingerprint": script_review.content_fingerprint_of_data(data),
                FORMAL_EDIT_META_KEY: True,
            },
        )


def _validate_open_source(project_path: Path, episode: int, source: str) -> None:
    load_novel_source(project_path, source, episode=episode)


def _rewrite_invalid_draft(
    project_path: Path,
    episode: int,
    kind: str,
    content: dict[str, Any],
    violations: list[DraftViolation],
    meta: dict[str, Any],
) -> str:
    return quarantine_and_report(
        project_path,
        episode,
        kind,
        content=content,
        violations=violations,
        meta=meta,
    )


def _soft_violation_section(revalidation: ReferenceDraftRevalidation) -> str:
    """The soft-violation section of the re-validation result, appended to the end of the report when promotion is
    blocked by violations.

    Shares ``render_soft_violation_section`` with the promotion / split receipts, only changing the handling note:
    here the draft still exists and these notes are not violations to fix; without a different note the Agent would
    treat degradation notes as unfixed violations and keep rewriting.

    重判结果里的软违约段，供晋升被违约挡下时拼在报告尾部。

    与晋升 / 拆分回执共用 ``render_soft_violation_section``，只换处置说明：此处草稿仍在场、
    这些提示不是要修的违约，说明不换的话 Agent 会把降级提示当成没修干净的违约反复重写。
    """
    return render_soft_violation_section(
        render_soft_violation_lines(revalidation.soft_violations), note=SOFT_VIOLATION_NOTE_QUARANTINED
    )


async def _promote_reference_script_plan(
    ctx: DraftContext,
    episode: int,
    draft: QuarantinedDraft,
    *,
    before_commit: Callable[[], None] | None = None,
) -> str:
    """Fully re-validate a script_plan draft with the validators used at generation time; on success, promote it to the
    final script_plan and clear the draft.

    Returns the promotion receipt text: the same exit as the split receipt (``reference_result_text``), with the
    soft-violation section after the save stats. Before promotion succeeds nobody tells the Agent about degradation
    notes; it first gets them when the main draft-fixing loop completes, so the success path cannot just return an
    "promoted" boolean.

    按产出时那套校验器全量重判 script_plan 草稿，通过则晋升为正式 script_plan 并清除草稿。

    返回晋升回执文本：与拆分回执同一出口（``reference_result_text``），落盘统计后接软违约段。
    晋升成功前无人向 Agent 交代降级提示——修草稿这条主回路走完时它才第一次拿到，故成功路径不能
    只返回一个「已晋升」的布尔。
    """
    project_path = await ctx.aproject_path()
    project = await asyncio.to_thread(ctx.pm.load_project, ctx.project_name)
    try:
        revalidation = await revalidate_reference_script_plan_draft(
            project_path,
            project,
            episode,
            draft,
            config_resolver=ctx.config_resolver,
        )
    except VideoRequestFactsError as exc:
        raise DraftWorkflowError.video_facts(exc.failure) from exc
    except ValueError as exc:
        raise DraftWorkflowError("draft_invalid", f"❌ {exc}") from exc
    violations, flat_units, split_caps = revalidation.violations, revalidation.flat_units, revalidation.caps
    if revalidation.schema_failed:
        # Schema violations: write back the content as the Agent has it, without normalizing; when fields are
        # corrupted, normalizing would reshape its original, and when it goes back by the report it would not match
        # what it wrote.
        # schema 违约：写回 Agent 手里那份原样内容，不做收编——字段被改坏时收编会把它的原稿
        # 改形，它照着报告回去看反而对不上自己写的东西。
        report = await run_sync_transaction(
            _rewrite_invalid_draft,
            project_path,
            episode,
            QUARANTINE_KIND_SCRIPT_PLAN,
            draft.content,
            violations,
            draft.meta,
        )
        raise DraftWorkflowError("draft_invalid", report + _soft_violation_section(revalidation), draft_refreshed=True)
    if violations:
        report = await run_sync_transaction(
            _rewrite_invalid_draft,
            project_path,
            episode,
            QUARANTINE_KIND_SCRIPT_PLAN,
            revalidation.flat_content(),
            violations,
            draft.meta,
        )
        raise DraftWorkflowError("draft_invalid", report + _soft_violation_section(revalidation), draft_refreshed=True)

    # Numbers are issued only by the server, allocated and never reused: keep the numbers the final plan on disk
    # already issued by position (so an unchanged promotion is byte-for-byte identical), and issue new numbers for
    # extra units; degradation notes in the receipt are located by the saved numbers.
    # 编号只由服务端发、只分配不复用：沿用盘上正式规划按位置已发的编号（原样晋升因此逐字不变），
    # 多出的单元另发新号；回执里的降级提示按落盘的编号定位。
    formal_ids = await asyncio.to_thread(
        plan_item_ids, script_review.official_reference_script_plan_path(project_path, episode)
    )
    unit_ids = formal_ids[: len(flat_units)]
    if len(flat_units) > len(unit_ids):
        unit_ids += await run_sync_transaction(
            ctx.pm.reserve_item_ids, ctx.project_name, episode, "video_units", len(flat_units) - len(unit_ids)
        )
    units = build_reference_units_from_flat(
        flat_units,
        planning_project(project, revalidation.new_assets),
        episode=episode,
        max_refs=split_caps.max_refs,
        unit_ids=unit_ids,
    )
    # Writing goes through a single exit (lib.script.script_review.write_script_plan_locked): the lock, baseline
    # comparison and prompt_authoring draft cleanup exist only there. The baseline fingerprint comes from
    # base_fingerprint recorded in meta at checkout / draft generation; if the final file was changed by another
    # writer (Web save, another split) after the draft was generated, promotion aborts and returns a conflict report
    # for the Agent to merge, rather than silently overwriting the other side's changes. Drafts without
    # base_fingerprint are promoted without a baseline.
    # 写盘经单一出口（lib.script.script_review.write_script_plan_locked）：锁、基线比对、prompt_authoring 草稿清理
    # 只存在那一处。基线指纹取自取回 / 草稿产出时记进 meta 的 base_fingerprint——正式文件在草稿
    # 产出后被其他写入方（Web 端保存、另一次拆分）改过时晋升中止、返回冲突报告让 Agent 合并，
    # 不静默覆盖对方的修改。缺少 base_fingerprint 的草稿按无基线晋升。
    expected = draft.meta.get("base_fingerprint", script_review.UNCHECKED_FINGERPRINT)
    try:
        await run_sync_transaction(
            _commit_reference_script_plan,
            project_path,
            episode,
            with_new_assets({"units": units}, revalidation.new_assets),
            expected,
            revalidation.basis,
            before_commit,
        )
    except script_review.ScriptPlanWriteConflict as conflict:
        raise DraftWorkflowError(
            "formal_revision_conflict",
            _render_script_plan_conflict_report(
                episode,
                draft,
                conflict,
                to_draft_shape=_reference_script_plan_draft_shape,
                field_hint="content.units",
            ),
        ) from conflict
    return reference_result_text(
        script_review.official_reference_script_plan_path(project_path, episode).relative_to(project_path),
        units,
        render_soft_violation_lines(
            [replace(soft, item_id=unit_ids[soft.item_index]) for soft in revalidation.soft_violations]
        ),
        action="晋升",
    )


def _render_script_plan_conflict_report(
    episode: int,
    draft: QuarantinedDraft,
    conflict: script_review.ScriptPlanWriteConflict,
    *,
    to_draft_shape: Callable[[dict[str, Any]], dict[str, Any] | None],
    field_hint: str,
) -> str:
    """Render the structured report returned to the Agent when promotion hits an optimistic concurrency conflict:
    latest content + merge guidance.

    The report must let the editor merge in place: attach the current on-disk value converted to the draft-layer
    shape (same shape as the draft ``content``, comparable entry by entry), and state that the merged final version
    must be explicitly accepted via ``patch_draft`` before re-promotion is allowed.

    ``to_draft_shape`` and ``field_hint`` are passed in by each variant: the draft-layer shape and editable fields
    differ per variant, and attaching "latest content" in a mismatched shape is more misleading than attaching
    nothing.

    渲染晋升遇乐观并发冲突时回给 Agent 的结构化报告：最新内容 + 合并指引。

    报告要让编辑方能就地合并：附上盘上现值转成草稿那一层的形状（与草稿 ``content`` 同形，可逐条
    对照），并指明通过 ``patch_draft`` 显式接受已合并的正式版本，之后重新晋升才会放行。

    ``to_draft_shape`` 与 ``field_hint`` 由各变体传入：草稿层的形状与可改字段按变体不同，
    附一份对不上形状的「最新内容」比不附更误导。
    """
    latest_content = to_draft_shape(conflict.current_content) if conflict.current_content is not None else None
    if latest_content is not None:
        latest = json.dumps(latest_content, ensure_ascii=False, indent=2)
        latest_block = f"Latest content of the current final script_plan (same shape as the draft content):\n{latest}"
    else:
        latest_block = (
            "The current final file does not exist or is not valid script_plan JSON, so its latest content "
            "cannot be attached; read that file yourself to check."
        )
    # The fingerprint is given as a JSON literal: when the final file has been deleted the current value is null;
    # writing "None" would make the Agent accept the wrong baseline, after which every re-promotion would mismatch and
    # return the same report, and the conflict could never be resolved.
    # 指纹按 JSON 字面量给：正式文件已被删除时现值是 null，写成 "None" 会让 Agent
    # 接受错误的基线，之后每次重晋升都比对不上、拿到同一份报告，冲突再也解不掉。
    actual_literal = json.dumps(script_review.published_revision(conflict.actual))
    doc_type = QUARANTINE_KIND_TO_DOC_TYPE[draft.kind]
    return (
        "❌ Promotion aborted (concurrent conflict): the final script_plan was changed by another writer (e.g. a save "
        "in the web app) after this draft was produced; promoting now would overwrite that change. Nothing was "
        "written and the draft is still in place.\n"
        f"Draft baseline fingerprint: {json.dumps(script_review.published_revision(conflict.expected))}; "
        f"current on-disk fingerprint: {actual_literal}\n\n"
        f"{latest_block}\n\n"
        "To resolve: call open_draft to read the current draft and formal_revision, merge "
        f"{field_hint} with the latest content above; "
        "then call patch_draft with the complete content and formal_revision as accept_formal_revision "
        "(pass null explicitly when it is null); "
        f'finally call {PROMOTE_TOOL_NAME}({{"episode_id": {episode}, "doc_type": "{doc_type}", '
        '"base_revision": "<new revision returned by patch_draft>"}) to promote again.'
    )


def _render_prompt_authoring_conflict_report(
    episode: int,
    draft: QuarantinedDraft,
    conflict: ScriptWriteConflict,
) -> str:
    latest = (
        json.dumps(conflict.current_content, ensure_ascii=False, indent=2)
        if conflict.current_content is not None
        else "null"
    )
    return (
        "❌ Promotion aborted (concurrent conflict): the final script was changed after this draft was produced. "
        "Nothing was written and the draft is still in place.\n"
        f"Draft baseline fingerprint: {json.dumps(script_review.published_revision(conflict.expected))}; "
        f"current on-disk fingerprint: {json.dumps(script_review.published_revision(conflict.actual))}\n\n"
        f"Latest content of the current final script:\n{latest}\n\n"
        "To resolve: call open_draft to read the current draft and formal_revision, merge the latest final content; "
        "then call patch_draft with the complete content and formal_revision as accept_formal_revision "
        "(pass null explicitly when it is null); "
        f'finally call {PROMOTE_TOOL_NAME}({{"episode_id": {episode}, "doc_type": "reference_prompt_authoring", '
        '"base_revision": "<new revision returned by patch_draft>"}) to promote again.'
    )


def _flatten_reference_script_plan_units(units: list[Any]) -> list[dict[str, Any]]:
    """Structured unit table of the final script_plan -> flat draft units (the inverse of
    ``build_reference_units_from_flat``).

    ``unit_id`` does not go into the draft: it is a derived value numbered mechanically by array index, and the
    draft is the layer the Agent edits; including derived fields opens the door to drift.

    When an on-disk unit has the wrong shape, do not fail loud: missing or mistyped fields are **carried over
    as-is** (missing ones filled with None / empty string) and left to the promotion-side schema re-validation to
    report to the Agent entry by entry. Carried over as-is rather than normalized to a valid value: if ``8.0`` were
    rewritten to ``0``, the Agent would see a duration in the draft it never wrote, and a report saying "duration
    not among the options" would not match the original value on disk either; keep the original value so it can see
    what is wrong. Non-dict units are not dropped either: an empty placeholder is kept at the corresponding array
    position so the promotion-side schema reports it as "invalid structure"; skipping it would shorten the array,
    and if the remaining units happened to pass validation, promotion would quietly overwrite the final file and
    lose this unit without anyone knowing.

    正式 script_plan 的结构化 unit 表 → 扁平草稿单元（``build_reference_units_from_flat`` 的逆向）。

    ``unit_id`` 不进草稿：它是按数组序号机械编号的派生物，草稿是给 Agent 改的那一层，带上
    派生字段等于给漂移开口子。

    盘上 unit 不合形状时不 fail-loud：字段缺失或类型不符时**原样带过**（缺失填 None / 空串），
    交由晋升侧的 schema 重判逐条报告给 Agent。原样带过而非归一化成合法值：``8.0`` 被改写成
    ``0`` 后，Agent 从草稿里看到的是一个它没写过的时长，报告说「时长不在档位内」也对不上盘
    上的原值——保留原值，让它自己看见错在哪。非 dict 的 unit 同样不丢弃：填空占位保留在数组
    对应位置，让晋升侧 schema 判它「结构非法」逐条报出——直接跳过会让数组变短，若剩余 unit
    恰好都能过校验，晋升会悄悄覆盖正式文件、丢失这个 unit 而无人知晓。
    """
    flat: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, dict):
            flat.append({"duration_seconds": None, "source_text": "", "text": ""})
            continue
        text = unit.get("text")
        flat.append(
            {
                "duration_seconds": unit.get("duration_seconds"),
                "source_text": unit.get("source_text", ""),
                "text": text if isinstance(text, str) else "",
            }
        )
    return flat


def _new_assets_of(content: dict[str, Any]) -> dict[str, Any]:
    """New items of this episode in the final script_plan, carried into the draft as-is; nothing is added when the key
    is absent.

    正式 script_plan 里的本集新增项，原样带进草稿；没有该键时不添。"""
    return {NEW_ASSETS_FIELD: content[NEW_ASSETS_FIELD]} if NEW_ASSETS_FIELD in content else {}


def _reference_script_plan_draft_shape(content: dict[str, Any]) -> dict[str, Any] | None:
    """Final reference script_plan content -> flat draft structure; returns None when it is not a valid script_plan.

    正式参考 script_plan 内容 → 扁平草稿结构；不是合法 script_plan 时返回 None。"""
    units = content.get("units")
    if not isinstance(units, list) or not units:
        return None
    return {"units": _flatten_reference_script_plan_units(units), **_new_assets_of(content)}


def _drama_script_plan_draft_shape(content: dict[str, Any]) -> dict[str, Any] | None:
    """Final drama script_plan content -> shot structure held by the editable draft; returns None when it is not a
    valid script_plan.

    Only ``needs_replan`` is stripped: it is a flag mechanically derived from dialogue-line admission, and letting
    the Agent edit derived values opens the door to drift; promotion re-derives it from the current ``content``
    anyway. Other fields are carried over as-is, including ``scene_id``: it is the alignment anchor of the
    prompt_authoring visual layer, and if it is broken in the draft the promotion-side schema must report it entry
    by entry; this layer must not fill it in.
    Non-dict shot items are also carried over as-is rather than dropped: skipping would shorten the array, and if
    the remaining shots happened to pass validation, promotion would quietly overwrite the final file and lose that
    shot without anyone knowing.

    正式 drama script_plan 内容 → 可编辑草稿装的分镜结构；不是合法 script_plan 时返回 None。

    只剥 ``needs_replan``：它是按台词准入机械派生的标记，让 Agent 编辑派生物等于给漂移开
    口子——晋升时照样按 ``content`` 现值重新派生。其余字段原样带过，包括 ``scene_id``：它是
    prompt_authoring 视觉层的对齐锚，草稿里写坏了要由晋升侧的 schema 逐条报出来，不能在这一层替它填。
    非 dict 的分镜项同样原样带过而非丢弃：跳过会让数组变短，若剩余分镜恰好都能过校验，晋升
    会悄悄覆盖正式文件、丢掉这一分镜而无人知晓。
    """
    scenes = content.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return None
    flat: list[Any] = [
        {k: v for k, v in scene.items() if k != "needs_replan"} if isinstance(scene, dict) else scene
        for scene in scenes
    ]
    return {"title": content.get("title", ""), "scenes": flat, **_new_assets_of(content)}


class SingleScriptPlanDraftRevalidation(NamedTuple):
    """Read-time re-validation result of the script_plan draft for the two routes with a single draft slot (drama /
    narration).

    ``schema_failed`` explicitly separates two stages: True means the draft did not even pass the schema used at
    generation time (``content`` is always an empty dict, and the caller can only present ``draft.content`` as-is);
    when False, ``content`` is the draft-layer shape normalized by the schema (drama's ``{title, scenes}``,
    narration's ``{segments}``), and promotion is allowed when ``violations`` is empty. The two are handled
    differently (as-is vs normalized), so this is not inferred from whether ``content`` is empty.

    The two routes share one type rather than each having its own: fields and semantics are identical, and splitting
    would only make the normalizing branch of ``revalidate_script_plan_draft`` write the same thing once per type.
    Reference-to-video has its own ``ReferenceDraftRevalidation``: its output is flat units and must carry back the
    duration options, so the shape is different anyway.

    只有一个草稿位的两条路线（drama / narration）的 script_plan 草稿读时重判结果。

    ``schema_failed`` 显式区分两个阶段：True 表示草稿连产出时的 schema 都没过（``content`` 必为
    空 dict，调用方只能按 ``draft.content`` 原样呈现）；False 时 ``content`` 是经 schema 收编后的
    草稿层形状（drama 的 ``{title, scenes}``、narration 的 ``{segments}``），``violations`` 为空即
    可晋升。两者的处置不同（原样 vs 收编），故不靠 ``content`` 是否为空来反推。

    两条路线共用一个类型而非各立一个：字段与语义逐字相同，分成两个只会让 ``revalidate_script_plan_draft``
    的归一分支按类型各写一遍同样的事。参考生视频另有 ``ReferenceDraftRevalidation``——它的产出是
    扁平 units 且要带回档位，形状本就不同。
    """

    violations: list[DraftViolation]
    content: dict[str, Any]
    schema_failed: bool
    basis: ArtifactBasis | None


async def revalidate_drama_script_plan_draft(
    project_path: Path,
    project: dict[str, Any],
    episode: int,
    draft: QuarantinedDraft,
    *,
    config_resolver: ConfigResolver | None = None,
) -> SingleScriptPlanDraftRevalidation:
    """Fully re-validate a drama script_plan draft with the validators used at generation time; read-only, does not
    write to disk or clear the draft.

    The validator is the very one used at generation time (``DramaNormalizedScript`` built from the current
    capability duration options), not a copy: the options change with the project config, a duration in the draft
    that was once valid may no longer be among the options today, and letting it through with the old enum would
    lock a duration the provider does not accept into the final file. ``needs_replan`` is likewise re-derived from
    current values, using the same rules as the generation side.

    Like the re-validators of the other two routes, this does not depend on ``DraftContext``: the promotion tool and
    the read-time recheck in content review share this function, and the latter has only a ``ProjectManager``, not
    the Agent tool's ctx.

    Raises ``ValueError`` when the source cannot be read (``meta.source`` points to a missing / renamed path).

    按产出时那套校验器全量重判 drama script_plan 草稿，只读、不写盘、不清草稿。

    校验器就是产出时那一个（按当前能力档位构造的 ``DramaNormalizedScript``），不是它的副本：
    档位随项目配置变化，草稿里那个曾经合法的秒数今天可能已不在档位内，用旧枚举放行等于把一份
    供应商不接的时长固化进正式文件。``needs_replan`` 同样按现值重新派生，与生成侧同一口径。

    与另两条路线的重判器同样不依赖 ``DraftContext``：晋升工具与内容确认的读时重算共用
    本函数，后者只有 ``ProjectManager``，没有 Agent 工具的 ctx。

    源文不可读（``meta.source`` 指向缺失 / 改名的路径）时抛 ``ValueError``。
    """
    # The source can be several hundred KB, so offload the synchronous read to a thread: the read-time recheck in
    # content review runs in the request coroutine, and reading directly would occupy the event loop and slow other
    # concurrent requests. Same rule as the other two routes.
    # 源文可能达数百 KB，同步读盘卸到线程：内容确认的读时重算在请求协程里跑，直接读会
    # 占用事件循环、拖慢并发的其它请求。与另两条路线同口径。
    _novel_text, _prompt_inputs, script_plan_basis = await asyncio.to_thread(
        load_script_plan_source_with_basis,
        project_path,
        draft.meta.get("source"),
        project,
        episode,
        "drama",
    )
    _default_duration, supported_durations = await fetch_storyboard_durations(project, config_resolver=config_resolver)
    schema = build_drama_normalized_script_model(supported_durations)
    try:
        content = schema.model_validate(draft.content).model_dump()
    except ValidationError as exc:
        return SingleScriptPlanDraftRevalidation(
            schema_violations(exc, draft.content, "scenes", "scene_id"),
            {},
            schema_failed=True,
            basis=script_plan_basis,
        )

    raw_scenes = content.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        violation = DraftViolation(
            "draft content.scenes must be a non-empty array of scene objects", code="schema_invalid"
        )
        return SingleScriptPlanDraftRevalidation([violation], {}, schema_failed=True, basis=script_plan_basis)
    for scene in raw_scenes:
        admission = admit_script_unit("scenes", scene, ignore_marker=True)
        if admission.allowed:
            scene.pop("needs_replan", None)
        else:
            scene["needs_replan"] = True
    content = with_new_assets(content, content[NEW_ASSETS_FIELD])
    violations = collect_drama_violations(raw_scenes, project=project, new_assets=content.get(NEW_ASSETS_FIELD))
    return SingleScriptPlanDraftRevalidation(violations, content, schema_failed=False, basis=script_plan_basis)


async def _promote_drama_script_plan(
    ctx: DraftContext,
    episode: int,
    draft: QuarantinedDraft,
) -> str:
    """Fully re-validate a drama script_plan draft with the validators used at generation time; on success, promote it
    to the final script_plan and clear the draft.

    Returns the promotion receipt text: the same exit as the generation receipt (``drama_script_plan_result_text``).

    按产出时那套校验器全量重判 drama script_plan 草稿，通过则晋升为正式 script_plan 并清除草稿。

    返回晋升回执文本：与产出回执同一出口（``drama_script_plan_result_text``）。
    """
    project_path = await ctx.aproject_path()
    project = await asyncio.to_thread(ctx.pm.load_project, ctx.project_name)
    try:
        revalidation = await revalidate_drama_script_plan_draft(
            project_path,
            project,
            episode,
            draft,
            config_resolver=ctx.config_resolver,
        )
    except VideoRequestFactsError as exc:
        raise DraftWorkflowError.video_facts(exc.failure) from exc
    except ValueError as exc:
        raise DraftWorkflowError("draft_invalid", f"❌ {exc}") from exc

    if revalidation.violations:
        # On schema violations write back the content as the Agent has it, without normalizing; when fields are
        # corrupted, normalizing would reshape its original, and when it goes back by the report it would not match
        # what it wrote. A draft that passed the schema is written back in normalized form.
        # schema 违约时写回 Agent 手里那份原样内容，不做收编——字段被改坏时收编会把它的原稿
        # 改形，它照着报告回去看反而对不上自己写的东西。过了 schema 的那份则回写收编后的内容。
        report = await run_sync_transaction(
            _rewrite_invalid_draft,
            project_path,
            episode,
            QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
            draft.content if revalidation.schema_failed else revalidation.content,
            revalidation.violations,
            draft.meta,
        )
        raise DraftWorkflowError("draft_invalid", report, draft_refreshed=True)

    content = revalidation.content
    script_plan_basis = revalidation.basis
    # The baseline fingerprint comes from base_fingerprint recorded in meta at checkout: if the final file was changed
    # by another writer (Web save, re-running normalize) after the draft was generated, promotion aborts and returns a
    # conflict report for the Agent to merge, rather than silently overwriting.
    # 基线指纹取自取回时记进 meta 的 base_fingerprint：正式文件在草稿产出后被其他写入方
    # （Web 端保存、重跑 normalize）改过时晋升中止、返回冲突报告让 Agent 合并，不静默覆盖。
    expected = draft.meta.get("base_fingerprint", script_review.UNCHECKED_FINGERPRINT)
    script_plan_path = episode_drafts_dir(project_path, episode) / SCRIPT_PLAN_FILENAMES["drama"]
    await reissue_plan_item_ids(
        ctx.pm, ctx.project_name, episode, content, keep=await asyncio.to_thread(plan_item_ids, script_plan_path)
    )
    try:
        await run_sync_transaction(
            commit_single_script_plan,
            project_path,
            episode,
            script_plan_path,
            QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
            content,
            expected,
            script_plan_basis,
        )
    except script_review.ScriptPlanWriteConflict as conflict:
        raise DraftWorkflowError(
            "formal_revision_conflict",
            _render_script_plan_conflict_report(
                episode,
                draft,
                conflict,
                to_draft_shape=_drama_script_plan_draft_shape,
                field_hint="content.scenes",
            ),
        ) from conflict
    raw_scenes = content.get("scenes")
    return drama_script_plan_result_text(
        script_plan_path.relative_to(project_path),
        raw_scenes if isinstance(raw_scenes, list) else [],
        action="晋升",
    )


async def _open_drama_script_plan_for_edit(
    ctx: DraftContext,
    episode: int,
    source: str | None,
) -> None:
    """Check out this episode's final drama script_plan as a draft (the final file stays unchanged) and return editing
    guidance for the Agent.

    Same flow as reference-to-video: checking whether a draft exists, reading the final file and writing the draft
    all happen inside the critical section of one per-path lock; if done separately outside the lock, two concurrent
    checkout requests for the same episode could both see "no draft" first and then each write, the later writer
    quietly overwriting the earlier one's content and meta.

    把本集正式 drama script_plan 取回为草稿（正式文件保持原样），返回给 Agent 的编辑指引。

    与参考生视频同一条流程：草稿有无的检查、正式文件的读取、草稿的写入整段在同一把 per-path
    锁的临界区内完成——拆开在锁外各做一次的话，同一集的两个并发取回请求会都先看到「无草稿」、
    再各自写入，后写者悄悄覆盖前者的内容与 meta。
    """
    project_path = await ctx.aproject_path()
    # source is validated before writing the draft: once the draft is saved it is recorded in meta.source as the basis
    # for promotion; if it is a missing / renamed / wrong path at this point, promotion would fail repeatedly, while
    # the existing draft blocks a re-checkout to fix source, leaving the Agent stuck in a corner it cannot get out of.
    # On validation failure nothing is saved, so invalid parameters leave no persistent side effects.
    # source 在写草稿前校验：草稿一旦落盘就把它记进 meta.source 供晋升取产物依据，若此刻是个
    # 缺失 / 改名 / 写错的路径，晋升会反复报错，而草稿已在场又挡住重新取回改正 source——Agent
    # 会卡在一个自己改不动的死角。校验失败时不落盘，无效参数不留持久副作用。
    if source is not None:
        try:
            await asyncio.to_thread(_validate_open_source, project_path, episode, source)
        except ValueError as exc:
            raise DraftWorkflowError("draft_open_failed", f"❌ {exc}") from exc

    script_plan_path = episode_drafts_dir(project_path, episode) / SCRIPT_PLAN_FILENAMES["drama"]
    await run_sync_transaction(
        _open_script_plan_draft,
        project_path,
        episode,
        script_plan_path,
        QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
        source,
        _drama_script_plan_draft_shape,
        f"❌ episode (id={episode}) has no editable final script_plan "
        f"({script_plan_path.relative_to(project_path).as_posix()} does not exist, is not valid JSON, "
        "or scenes is not a non-empty array); for the first generation call generate_script_plan",
    )


async def revalidate_narration_script_plan_draft(
    project_path: Path,
    project: dict[str, Any],
    episode: int,
    draft: QuarantinedDraft,
    *,
    config_resolver: ConfigResolver | None = None,
) -> SingleScriptPlanDraftRevalidation:
    """Fully re-validate a narration script_plan draft with the validators used at generation time; read-only, does not
    write to disk or clear the draft.

    Re-validation uses the same function as the split tool (``collect_narration_violations``), not a simplified
    copy: the promotion rules, the read-time recheck in content review and the generation rules must share one piece
    of code, otherwise forks like "passes here, rejected at the next generation" reappear. Capability duration
    options and source are both resolved again, since the user may have changed the model config or source during
    quarantine, and re-validation must check against current values.

    Like ``revalidate_reference_script_plan_draft`` this does not depend on ``DraftContext``: the read-time recheck
    in content review has no Agent tool ctx, only a ``ProjectManager``; both places share this function rather than
    each loading the project.

    Raises ``ValueError`` when ``meta.source`` is missing (the draft was corrupted and cannot be re-validated).

    按产出时那套校验器全量重判 narration script_plan 草稿，只读、不写盘、不清草稿。

    重判走的是拆分工具用的同一个函数（``collect_narration_violations``），不是它的简化副本：
    晋升口径、内容确认的读时重算与产出口径必须同一份代码，否则「这里放行、下次生成时被拒」
    这类分叉会重新出现。能力档位与源文都重新解析——隔离期间用户可能改过模型配置或源文，重判要
    对着现值判。

    与 ``revalidate_reference_script_plan_draft`` 同样不依赖 ``DraftContext``：内容确认的读时重算
    没有 Agent 工具的 ctx，只有 ``ProjectManager``，两处共用本函数而不各自加载 project。

    ``meta.source`` 缺失（草稿被改坏、无从重判）时抛 ``ValueError``。
    """
    # meta.source records the source range at generation time. A missing key means meta was corrupted: we must not
    # silently re-resolve with the default source; if a different source file was specified at generation time, the
    # coverage check against another range would not be the same question as at generation time.
    # meta.source 记的是产出时的源文范围。缺键说明 meta 被改坏了：不能默默按缺省源文重解析
    # ——产出时若指定过别的源文件，换一份范围重判的覆盖判定与产出时不是同一道题。
    if "source" not in draft.meta:
        raise ValueError(
            f"draft {draft.path} is missing meta.source (the source range recorded at generation time); "
            "restore that field (the relative source path when a source was given, null when generated from this "
            "episode's derived source) and retry"
        )
    # The source can be several hundred KB; reading it synchronously inside this async function body would occupy the
    # event loop. The promotion tool runs on a separate session thread and is not sensitive, but the read-time recheck
    # in content review (same code) runs in the request coroutine, so offload to a thread to avoid slowing other
    # concurrent requests.
    # 源文可能达数百 KB，同步读盘直接放在这个 async 函数体里会占用事件
    # 循环——晋升工具走的是独立会话线程不敏感，但内容确认的读时重算（同一份代码）在请求
    # 协程里跑，卸到线程避免拖慢并发的其它请求。
    novel_text, _prompt_inputs, script_plan_basis = await asyncio.to_thread(
        load_script_plan_source_with_basis,
        project_path,
        draft.meta["source"],
        project,
        episode,
        "narration",
    )
    _default_duration, supported_durations = await fetch_storyboard_durations(project, config_resolver=config_resolver)

    # A modified draft first goes through the schema used at generation time: on the split side, response_schema and
    # _parse_script_plan_json enforce fields and types; if the promotion side skipped this layer, changing
    # duration_seconds to a string or deleting novel_text entirely could be promoted all the way into the final file,
    # which is exactly the "final file pollution" this mechanism prevents. The outer shape (segments missing / not an
    # array / empty array) and per-shot field violations take the same report path: both are mistakes the Agent makes
    # when editing a draft.
    # 修改过的草稿先过产出时那份 schema：拆分侧由 response_schema 与 _parse_script_plan_json 卡住字段与
    # 类型，晋升侧漏掉这一层的话，把 duration_seconds 改成字符串、或整个删掉 novel_text 都能一路
    # 晋升进正式文件——正是本机制要防的「正式文件被污染」。外层形状（segments 缺失 / 不是数组 /
    # 空数组）与逐分镜的字段违约走同一条报告路径：两者都是 Agent 编辑草稿时会犯的错。
    raw_segments = draft.content.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        logger.debug("草稿 content.segments 形状非法: %s", type(raw_segments).__name__)
        violation = DraftViolation(
            "draft content.segments must be a non-empty array of segment objects", code="schema_invalid"
        )
        return SingleScriptPlanDraftRevalidation([violation], {}, schema_failed=True, basis=script_plan_basis)
    try:
        content = NarrationScriptPlanDraft.model_validate(draft.content).model_dump()
    except ValidationError as exc:
        return SingleScriptPlanDraftRevalidation(
            schema_violations(exc, draft.content, "segments", "segment_id"),
            {},
            schema_failed=True,
            basis=script_plan_basis,
        )
    content = with_new_assets(content, content[NEW_ASSETS_FIELD])

    violations = collect_narration_violations(
        content["segments"],
        episode=episode,
        supported_durations=supported_durations,
        project=project,
        new_assets=content.get(NEW_ASSETS_FIELD),
        novel_text=novel_text,
        # The source range used for re-validation comes from the draft's own meta.source: a draft checked out without
        # specifying source records null (the episode's derived source); if this episode's final script_plan was
        # originally generated from a different source file, an unchanged checked-out draft would be judged as
        # incomplete coverage here. Only by putting the range and how to fix it into the message can the Agent find
        # its way out; while the draft exists it cannot be checked out again, and the source range must be updated via
        # patch_draft.
        # 重判用的源文范围来自草稿自己的 meta.source：取回时未指定 source
        # 的草稿记的是 null（本集派生源文），若本集正式 script_plan 当初是按别的源文件产出的，这里会把
        # 一份原样取回、一字未改的草稿判成覆盖不全。把范围与改法一并写进消息，Agent 才走得出去
        # ——草稿在场时不能重新取回，须由 patch_draft 更新源文范围。
        source_scope=(
            f"{coverage_source_scope(cast(str | None, draft.meta['source']), episode=episode)}"
            ", taken from the draft's source range; if this range differs from the one used to produce this "
            "episode's final script_plan, call patch_draft with source set to that original source file's relative "
            "path and retry"
        ),
    )
    return SingleScriptPlanDraftRevalidation(violations, content, schema_failed=False, basis=script_plan_basis)


def _narration_script_plan_draft_shape(content: dict[str, Any]) -> dict[str, Any] | None:
    """Final narration script_plan content -> shot structure held by the draft; returns None when it is not a valid
    script_plan.

    This variant has no machine-derived fields to strip (``segment_id`` is an alignment anchor written by the model
    itself, not derived from the index), so the draft layer and the saved layer have the same shape, keeping only
    ``segments`` and this episode's new items. Shot items are carried over as-is, including non-dict items: skipping
    would shorten the array, and if the remaining shots happened to pass validation, promotion would quietly
    overwrite the final file and lose that segment without anyone knowing.

    正式 narration script_plan 内容 → 草稿装的分镜结构；不是合法 script_plan 时返回 None。

    该变体没有机器派生字段可剥（``segment_id`` 是模型自己写的对齐锚、不由序号派生），草稿层与
    落盘层同形，只保留 ``segments`` 与本集新增项。分镜项原样带过、包括非 dict 的项：跳过会让
    数组变短，若剩余分镜恰好都能过校验，晋升会悄悄覆盖正式文件、丢掉这一段而无人知晓。
    """
    segments = content.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    return {"segments": list(segments), **_new_assets_of(content)}


async def _promote_narration_script_plan(
    ctx: DraftContext,
    episode: int,
    draft: QuarantinedDraft,
) -> str:
    """Fully re-validate a narration script_plan draft with the validators used at generation time; on success, promote
    it to the final script_plan and clear the draft.

    Returns the promotion receipt text: the same exit as the split receipt (``narration_script_plan_result_text``).

    按产出时那套校验器全量重判 narration script_plan 草稿，通过则晋升为正式 script_plan 并清除草稿。

    返回晋升回执文本：与拆分回执同一出口（``narration_script_plan_result_text``）。
    """
    project_path = await ctx.aproject_path()
    project = await asyncio.to_thread(ctx.pm.load_project, ctx.project_name)
    try:
        revalidation = await revalidate_narration_script_plan_draft(
            project_path,
            project,
            episode,
            draft,
            config_resolver=ctx.config_resolver,
        )
    except VideoRequestFactsError as exc:
        raise DraftWorkflowError.video_facts(exc.failure) from exc
    except ValueError as exc:
        raise DraftWorkflowError("draft_invalid", f"❌ {exc}") from exc

    # On schema violations write back the content as the Agent has it, without normalizing; when fields are corrupted,
    # normalizing would reshape its original, and when it goes back by the report it would not match what it wrote. A
    # draft that passed the schema is written back in normalized form.
    # schema 违约时写回 Agent 手里那份原样内容，不做收编——字段被改坏时收编会把它的原稿改形，
    # 它照着报告回去看反而对不上自己写的东西。过了 schema 的那份则回写收编后的内容。
    if revalidation.violations:
        content = draft.content if revalidation.schema_failed else revalidation.content
        report = await run_sync_transaction(
            _rewrite_invalid_draft,
            project_path,
            episode,
            QUARANTINE_KIND_NARRATION_SCRIPT_PLAN,
            content,
            revalidation.violations,
            draft.meta,
        )
        raise DraftWorkflowError("draft_invalid", report, draft_refreshed=True)

    # The baseline fingerprint comes from base_fingerprint recorded in meta at checkout / draft generation: if the
    # final file was changed by another writer (Web save, re-running the split) after the draft was generated,
    # promotion aborts and returns a conflict report for the Agent to merge, rather than silently overwriting the
    # other side's changes.
    # Drafts without base_fingerprint are promoted without a baseline.
    # 基线指纹取自取回 / 草稿产出时记进 meta 的 base_fingerprint：正式文件在草稿产出后被其他写入方
    # （Web 端保存、重跑拆分）改过时晋升中止、返回冲突报告让 Agent 合并，不静默覆盖对方的修改。
    # 缺少 base_fingerprint 的草稿按无基线晋升。
    expected = draft.meta.get("base_fingerprint", script_review.UNCHECKED_FINGERPRINT)
    script_plan_path = narration_script_plan_path(project_path, episode)
    await reissue_plan_item_ids(
        ctx.pm,
        ctx.project_name,
        episode,
        revalidation.content,
        keep=await asyncio.to_thread(plan_item_ids, script_plan_path),
    )
    try:
        await run_sync_transaction(
            commit_single_script_plan,
            project_path,
            episode,
            script_plan_path,
            QUARANTINE_KIND_NARRATION_SCRIPT_PLAN,
            revalidation.content,
            expected,
            revalidation.basis,
        )
    except script_review.ScriptPlanWriteConflict as conflict:
        conflict_report = _render_script_plan_conflict_report(
            episode,
            draft,
            conflict,
            to_draft_shape=_narration_script_plan_draft_shape,
            field_hint="content.segments",
        )
        raise DraftWorkflowError("formal_revision_conflict", conflict_report) from conflict
    raw_segments = revalidation.content.get("segments")
    return narration_script_plan_result_text(
        script_plan_path.relative_to(project_path),
        raw_segments if isinstance(raw_segments, list) else [],
        action="晋升",
    )


async def _open_narration_script_plan_for_edit(
    ctx: DraftContext,
    episode: int,
    source: str | None,
) -> None:
    """Check out this episode's final narration script_plan as a draft (the final file stays unchanged) and return
    editing guidance for the Agent.

    Same flow as the other two routes: checking whether a draft exists, reading the final file and writing the draft
    all happen inside the critical section of one per-path lock; if done separately outside the lock, two concurrent
    checkout requests for the same episode could both see "no draft" first and then each write, the later writer
    quietly overwriting the earlier one's content and meta.

    把本集正式 narration script_plan 取回为草稿（正式文件保持原样），返回给 Agent 的编辑指引。

    与另两条路线同一条流程：草稿有无的检查、正式文件的读取、草稿的写入整段在同一把 per-path 锁
    的临界区内完成——拆开在锁外各做一次的话，同一集的两个并发取回请求会都先看到「无草稿」、再
    各自写入，后写者悄悄覆盖前者的内容与 meta。
    """
    project_path = await ctx.aproject_path()
    # source is validated before writing the draft: once the draft is saved it is recorded in meta.source for
    # promotion to re-check source coverage; if it is a missing / renamed / wrong path at this point, promotion would
    # fail repeatedly, while the existing draft blocks a re-checkout to fix source, leaving the agent stuck in a
    # corner it cannot get out of. On validation failure nothing is saved, so invalid parameters leave no persistent
    # side effects.
    # source 在写草稿前校验：草稿一旦落盘就把它记进 meta.source 供晋升重判原文覆盖，若此刻是个
    # 缺失 / 改名 / 写错的路径，晋升会反复报错，而草稿已在场又挡住重新取回改正 source——agent
    # 会卡在一个自己改不动的死角。校验失败时不落盘，无效参数不留持久副作用。
    if source is not None:
        try:
            await asyncio.to_thread(_validate_open_source, project_path, episode, source)
        except ValueError as exc:
            raise DraftWorkflowError("draft_open_failed", f"❌ {exc}") from exc

    script_plan_path = narration_script_plan_path(project_path, episode)
    await run_sync_transaction(
        _open_script_plan_draft,
        project_path,
        episode,
        script_plan_path,
        QUARANTINE_KIND_NARRATION_SCRIPT_PLAN,
        source,
        _narration_script_plan_draft_shape,
        f"❌ episode (id={episode}) has no editable final script_plan "
        f"({script_plan_path.relative_to(project_path).as_posix()} does not exist, is not valid JSON, "
        "or segments is not a non-empty array); for the first generation call generate_script_plan",
    )


class ScriptPlanDraftRevalidation(NamedTuple):
    """Re-validation result of a script_plan draft after dispatch by kind, normalized to the presentation layer's
    terms.

    ``content`` is the draft body to show the user: when the schema passed it is the normalized current shape
    (durations etc. re-checked against the current options); when it failed it is None, and the caller then presents
    ``draft.content``, the text the Agent edited, as-is.
    The shape differs by variant (reference-to-video units, drama title+scenes, narration segments); the
    presentation layer renders it with the card of its own route.

    ``soft_violations`` are per-entry degradation notes, produced only by reference-to-video; empty when the schema
    fails.

    按 kind 分派后的 script_plan 草稿重判结果，归一到呈现层口径。

    ``content`` 是要展示给用户的那一份草稿正文：过了 schema 时是收编后的现值形状（时长等已按
    当前档位重判），没过时为 None——调用方据此改用 ``draft.content`` 原样呈现 Agent 手改的文本。
    形状随变体不同（参考生视频 units、drama title+scenes、narration segments），呈现层按自己那条
    路线的卡片渲染。

    ``soft_violations`` 是逐条目定位的降级提示，只有参考生视频产出；schema 没过时为空。
    """

    violations: list[DraftViolation]
    content: dict[str, Any] | None
    soft_violations: tuple[SoftViolation, ...] = ()


class _SingleScriptPlanRevalidator(Protocol):
    """Call shape of the re-validators in the table: draft-locating parameters have the same shape, and the capability
    resolver is injected by keyword.

    表内重判器的调用形状：草稿定位参数同形，能力解析器按关键字注入。"""

    def __call__(
        self,
        project_path: Path,
        project: dict[str, Any],
        episode: int,
        draft: QuarantinedDraft,
        *,
        config_resolver: ConfigResolver | None = None,
    ) -> Awaitable[SingleScriptPlanDraftRevalidation]: ...


_SINGLE_SCRIPT_PLAN_REVALIDATORS: dict[str, _SingleScriptPlanRevalidator] = {
    QUARANTINE_KIND_DRAMA_SCRIPT_PLAN: revalidate_drama_script_plan_draft,
    QUARANTINE_KIND_NARRATION_SCRIPT_PLAN: revalidate_narration_script_plan_draft,
}


async def revalidate_script_plan_draft(
    project_path: Path,
    project: dict[str, Any],
    episode: int,
    draft: QuarantinedDraft,
    *,
    config_resolver: ConfigResolver | None = None,
) -> ScriptPlanDraftRevalidation:
    """Hand a script_plan draft to the re-validator of its route and return a route-neutral re-validation result.

    The read-time recheck in content review goes through this single entry by kind: the three routes' re-validators
    have the same signature and isomorphic results, so content review need not know any route's internal shape, and
    no dispatch site is missed when a variant is added. The promotion side still calls its own re-validator
    directly, since it needs bits like basis and schema_failed that saving requires and the presentation layer does
    not care about.

    Reference-to-video's soft violations are returned with the result, so places that show entries can present
    degradation notes per entry.

    Raises ``ValueError`` when ``draft.kind`` is not one of the three script_plan sources (e.g. a prompt_authoring
    draft passed by mistake).

    把一份 script_plan 草稿交给它那条路线的重判器，返回路线中立的重判结果。

    内容确认的读时重算按 kind 走这一个入口：三条路线的重判器签名同形、结果同构，内容确认
    因此不必认得任一条路线的内部形状，也不会在新增变体时漏掉一处分派。晋升侧仍各自直接调用
    自己那个重判器——它们要用到 basis 与 schema_failed 这些落盘所需、呈现层不关心的位。

    参考生视频的软违约随结果一并返回，供展示条目的地方逐条目呈现降级提示。

    ``draft.kind`` 不是 script_plan 的三个来源之一（如误传 prompt_authoring 草稿）时抛 ``ValueError``。
    """
    if draft.kind == QUARANTINE_KIND_SCRIPT_PLAN:
        reference = await revalidate_reference_script_plan_draft(
            project_path,
            project,
            episode,
            draft,
            config_resolver=config_resolver,
        )
        content = None if reference.schema_failed else reference.flat_content()
        return ScriptPlanDraftRevalidation(reference.violations, content, tuple(reference.soft_violations))
    revalidator = _SINGLE_SCRIPT_PLAN_REVALIDATORS.get(draft.kind)
    if revalidator is None:
        raise ValueError(f"not a script_plan draft origin, cannot re-validate: {draft.kind}")
    single = await revalidator(project_path, project, episode, draft, config_resolver=config_resolver)
    return ScriptPlanDraftRevalidation(single.violations, None if single.schema_failed else single.content)


async def _open_reference_script_plan_for_edit(
    ctx: DraftContext,
    episode: int,
    source: str | None,
) -> None:
    """Check out this episode's final reference-to-video script_plan as a draft (the final file stays unchanged) and
    return editing guidance for the Agent.

    把本集正式参考生视频 script_plan 取回为草稿（正式文件保持原样），返回给 Agent 的编辑指引。"""
    project_path = await ctx.aproject_path()
    # source is validated before writing the draft: once the draft is saved it is recorded in meta.source for
    # promotion's re-validation; if it is a missing/renamed/wrong path at this point, promotion would fail repeatedly
    # in load_novel_source, while the existing draft blocks a re-checkout to fix source, leaving the Agent stuck in a
    # corner it cannot get out of. On validation failure nothing is saved, so invalid parameters leave no persistent
    # side effects.
    # source 在写草稿前校验：草稿一旦落盘就把它记进 meta.source 供晋升重判用，若此刻
    # 是个缺失/改名/写错的路径，晋升会在 load_novel_source 上反复报错，而草稿已在场
    # 又挡住重新取回改正 source——Agent 会卡在一个自己改不动的死角。校验失败时不落盘，
    # 无效参数不留持久副作用。
    if source is not None:
        try:
            await asyncio.to_thread(_validate_open_source, project_path, episode, source)
        except ValueError as exc:
            raise DraftWorkflowError("draft_open_failed", f"❌ {exc}") from exc

    # Checking whether a draft exists, reading the final file and writing the draft must happen inside the critical
    # section of one lock: if done separately outside the lock, two concurrent checkout requests for the same episode
    # could both see "no draft" first and then both write a draft, the later writer quietly overwriting the earlier
    # one's content and meta.source. The write critical section uses the same lock as Web saves and migrations; reads
    # also hold the lock to avoid checking out a half-written script_plan.
    # 草稿有无的检查、正式文件的读取、草稿的写入须在同一把锁的临界区内完成：拆开在锁外
    # 各做一次的话，同一集的两个并发取回请求可能都先看到「无草稿」，再都各自写入草稿，
    # 后写者悄悄覆盖前者的 content 与 meta.source。写临界区与 Web 端保存、迁移同一把锁，
    # 读也持锁避免取回一份写到一半的 script_plan。
    script_plan_path = script_review.official_reference_script_plan_path(project_path, episode)
    await run_sync_transaction(
        _open_script_plan_draft,
        project_path,
        episode,
        script_plan_path,
        QUARANTINE_KIND_SCRIPT_PLAN,
        source,
        _reference_script_plan_draft_shape,
        f"❌ episode (id={episode}) has no editable final script_plan "
        f"({script_plan_path.relative_to(project_path).as_posix()} does not exist, is not valid JSON, "
        "or units is not a non-empty array); for the first generation call generate_script_plan",
    )


_SCRIPT_PLAN_EDIT_OPENERS: dict[
    str,
    Callable[[DraftContext, int, str | None], Awaitable[None]],
] = {
    QUARANTINE_KIND_SCRIPT_PLAN: _open_reference_script_plan_for_edit,
    QUARANTINE_KIND_DRAMA_SCRIPT_PLAN: _open_drama_script_plan_for_edit,
    QUARANTINE_KIND_NARRATION_SCRIPT_PLAN: _open_narration_script_plan_for_edit,
}

_SINGLE_SCRIPT_PLAN_PROMOTERS: dict[
    str,
    Callable[[DraftContext, int, QuarantinedDraft], Awaitable[str]],
] = {
    QUARANTINE_KIND_DRAMA_SCRIPT_PLAN: _promote_drama_script_plan,
    QUARANTINE_KIND_NARRATION_SCRIPT_PLAN: _promote_narration_script_plan,
}


class DraftWorkflow:
    """Open, patch, promote, and discard drafts without host envelopes."""

    def __init__(self, ctx: DraftContext):
        self.ctx = ctx

    async def _kind(self, episode: int, doc_type: str, *, allow_stale_discard: bool = False) -> str:
        if isinstance(episode, bool) or episode < 1:
            raise DraftWorkflowError("invalid_request", "episode must be a positive integer")
        kind = DOC_TYPE_TO_QUARANTINE_KIND.get(doc_type)
        if kind is None:
            raise DraftWorkflowError("invalid_request", f"unsupported doc_type: {doc_type}")
        if allow_stale_discard:
            return kind
        project = await asyncio.to_thread(self.ctx.pm.load_project, self.ctx.project_name)
        active_script_plan = script_review.script_plan_quarantine_kind(project)
        compatible = kind == active_script_plan or (
            kind == QUARANTINE_KIND_PROMPT_AUTHORING and uses_reference_video_units(project)
        )
        if not compatible:
            raise DraftWorkflowError(
                "doc_type_not_applicable", f"doc_type {doc_type} does not match the project workflow"
            )
        return kind

    async def resolve_kind(self, episode: int, doc_type: str) -> str:
        """Resolve ``doc_type`` to a draft source and confirm it applies to the current project's workflow.

        把 ``doc_type`` 解析为草稿来源，并确认它适用于当前项目的工作流。"""
        return await self._kind(episode, doc_type)

    def _reject_confirmed_script_plan_edit(self, episode: int, kind: str, draft: QuarantinedDraft | None) -> None:
        """A confirmed script plan is read-only: refuse to check out an editing copy of it, and refuse to modify or
        promote an existing editing copy.

        ``draft`` being None means a new editing copy is about to be checked out; drafts left by re-running the
        script plan carry no edit marker and are not restricted.

        已确认的脚本规划只读：拒绝为它取回编辑副本，以及修改、晋升已有的编辑副本。

        ``draft`` 为 None 表示正要取回新的编辑副本；重跑脚本规划留下的草稿不带编辑标记，不受限制。
        """
        if kind == QUARANTINE_KIND_PROMPT_AUTHORING:
            return
        if draft is not None and draft.meta.get(FORMAL_EDIT_META_KEY) is not True:
            return
        project = self.ctx.pm.load_project(self.ctx.project_name)
        if script_review.formal_script_plan_confirmed(self.ctx.project_path, project, episode):
            raise DraftWorkflowError("script_plan_confirmed", _script_plan_confirmed_detail(episode))

    def _read_if_present(
        self,
        episode: int,
        kind: str,
    ) -> dict[str, Any] | None:
        draft = read_quarantine(self.ctx.project_path, episode, kind)
        if draft is not None:
            payload = draft_payload(draft)
            payload["formal_revision"] = script_review.published_revision(
                script_review.content_fingerprint(self._formal_path(episode, kind))
            )
            return payload
        if quarantine_exists(self.ctx.project_path, episode, kind):
            detail = f"episode (id={episode}): {doc_type_for_kind(kind)} draft is not a valid JSON envelope"
            raise DraftWorkflowError("draft_not_found", detail)
        return None

    def _read(
        self,
        episode: int,
        kind: str,
    ) -> dict[str, Any]:
        payload = self._read_if_present(episode, kind)
        if payload is not None:
            return payload
        detail = f"episode (id={episode}) has no {doc_type_for_kind(kind)} draft"
        raise DraftWorkflowError("draft_not_found", detail)

    def _draft_snapshot(
        self,
        episode: int,
        kind: str,
    ) -> tuple[QuarantinedDraft | None, str | None]:
        draft = read_quarantine(self.ctx.project_path, episode, kind)
        return draft, draft_revision(draft) if draft is not None else None

    def _quarantine_lock_target(self, episode: int, kind: str) -> tuple[Path, Path, ProjectManager]:
        """Project directory, draft path and the ProjectManager used for locking: locating the directory and
        constructing it both touch the disk, so offload them to a thread in one go.

        项目目录、草稿路径与持锁用的 ProjectManager：定位目录与构造都会触盘，供一次卸到线程。"""
        project_path = self.ctx.project_path
        return project_path, quarantine_path(project_path, episode, kind), ProjectManager(self.ctx.data_root)

    def _formal_path(self, episode: int, kind: str) -> Path:
        if kind == QUARANTINE_KIND_PROMPT_AUTHORING:
            return self.ctx.project_path / "scripts" / episode_script_filename(episode)
        project = self.ctx.pm.load_project(self.ctx.project_name)
        path = script_review.script_plan_path(self.ctx.project_path, project, episode)
        if path is None:
            raise ValueError("project has no formal script_plan document")
        return path

    def _open_reference_prompt_authoring(
        self,
        episode: int,
        resolved: str,
    ) -> None:
        script = self.ctx.pm.load_script_readonly(
            self.ctx.project_name,
            episode_script_filename(episode),
        )
        units = script.get("video_units")
        if not isinstance(units, list) or not units:
            raise DraftWorkflowError("draft_source_missing", "formal reference prompt_authoring has no video_units")
        write_quarantine(
            self.ctx.project_path,
            episode,
            resolved,
            content={
                "title": script.get("title") or episode_title(self.ctx.pm.load_project(self.ctx.project_name), episode),
                "units": [{"text": unit.get("text", "")} for unit in units if isinstance(unit, dict)],
            },
            violations=[],
            meta={"base_fingerprint": script_review.content_fingerprint_of_data(script), FORMAL_EDIT_META_KEY: True},
        )

    async def open(
        self,
        episode: int,
        doc_type: str,
        source: str | None = None,
    ) -> dict[str, Any]:
        resolved = await self._kind(episode, doc_type)
        _, path, locks = await asyncio.to_thread(self._quarantine_lock_target, episode, resolved)

        try:
            async with locks.async_file_lock(path):
                existing = await asyncio.to_thread(self._read_if_present, episode, resolved)
                if existing is not None:
                    return existing
                await asyncio.to_thread(self._reject_confirmed_script_plan_edit, episode, resolved, None)
                if resolved == QUARANTINE_KIND_PROMPT_AUTHORING:
                    await run_sync_transaction(
                        self._open_reference_prompt_authoring,
                        episode,
                        resolved,
                    )
                else:
                    await _SCRIPT_PLAN_EDIT_OPENERS[resolved](self.ctx, episode, source)
                return await asyncio.to_thread(self._read, episode, resolved)
        except DraftWorkflowError:
            raise
        except Exception as exc:
            raise DraftWorkflowError("draft_open_failed", str(exc)) from exc

    def _patch_locked(
        self,
        episode: int,
        resolved: str,
        content: dict[str, Any],
        base_revision: str,
        accept_formal_revision: str | None,
        accepts_formal_revision: bool,
        source: str | None,
        updates_source: bool,
        before_commit: Callable[[], None] | None,
    ) -> dict[str, Any]:
        path = quarantine_path(self.ctx.project_path, episode, resolved)
        draft = read_quarantine(self.ctx.project_path, episode, resolved)
        if draft is None:
            return self._read(episode, resolved)
        actual_revision = draft_revision(draft)
        if script_review.fingerprint_of_revision(base_revision) != script_review.fingerprint_of_revision(
            actual_revision
        ):
            raise DraftWorkflowError(
                "revision_conflict",
                f"draft revision changed: expected {base_revision}, actual {actual_revision}",
            )
        self._reject_confirmed_script_plan_edit(episode, resolved, draft)
        meta = draft.meta
        if updates_source:
            if resolved == QUARANTINE_KIND_PROMPT_AUTHORING:
                raise DraftWorkflowError("invalid_request", "source is only valid for script_plan drafts")
            load_novel_source(self.ctx.project_path, source, episode=episode)
            meta = {**meta, "source": source}
        if accepts_formal_revision:
            actual_formal_revision = script_review.content_fingerprint(self._formal_path(episode, resolved))
            if script_review.fingerprint_of_revision(accept_formal_revision) != actual_formal_revision:
                raise DraftWorkflowError(
                    "formal_revision_conflict",
                    "formal document changed again: "
                    f"expected {accept_formal_revision}, "
                    f"actual {script_review.published_revision(actual_formal_revision)}",
                )
            meta = {**meta, "base_fingerprint": actual_formal_revision}
        if before_commit is not None:
            before_commit()
        atomic_write_json(
            path,
            quarantine_envelope(
                draft.kind,
                draft.episode,
                content=content,
                violations=draft.violations,
                meta=meta,
            ),
        )
        announce_draft_change(self.ctx.project_path, path)
        record_project_activity(self.ctx.project_path)
        receipt = self._read(episode, resolved)
        # Violations in the draft are judgments from the previous generation / promotion on the old body and no longer
        # correspond to current values after an edit: the receipt omits them so the caller does not mistake the old
        # report for the result of this edit. Violations of current values are determined by the full re-validation at
        # promotion.
        # 草稿里的违约是上一轮产出 / 晋升针对旧正文的判定，修改后不再对应现值：回执不带它，
        # 免得调用方把旧报告当成这次修改的结果。现值的违约以晋升时的全量重判为准。
        receipt.pop("violations", None)
        return receipt

    async def patch(
        self,
        episode: int,
        doc_type: str,
        content: dict[str, Any],
        base_revision: str,
        accept_formal_revision: str | None = None,
        accepts_formal_revision: bool = False,
        source: str | None = None,
        updates_source: bool = False,
        *,
        before_commit: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        resolved = await self._kind(episode, doc_type)
        _, path, locks = await asyncio.to_thread(self._quarantine_lock_target, episode, resolved)
        async with locks.async_file_lock(path):
            return await run_sync_transaction(
                self._patch_locked,
                episode,
                resolved,
                content,
                base_revision,
                accept_formal_revision,
                accepts_formal_revision,
                source,
                updates_source,
                before_commit,
            )

    async def promote(
        self,
        episode: int,
        doc_type: str,
        base_revision: str,
        *,
        before_commit: Callable[[], None] | None = None,
        before_lock: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        resolved = await self._kind(episode, doc_type)
        project_path, path, locks = await asyncio.to_thread(self._quarantine_lock_target, episode, resolved)
        result_path: Path | None = None
        # Promotion receipt: each of the three script_plan variants is produced by its own promoter, and the stats
        # section uses the same exit as the generation-side receipt (reference-to-video additionally appends the
        # soft-violation section); this function does not reassemble it. Prompt authoring has no matching
        # generation-side receipt (it saves the final script, and the generation side sends no receipt); only the path
        # can be reported, so it is written directly in the branch below.
        # 晋升回执：script_plan 三个变体各由自己的晋升器产出，统计段与产出侧回执同一出口
        # （参考生视频那条另接软违约段），本函数不重拼。提示词编写没有对位的产出侧回执——它落的
        # 是正式剧本、产出侧不发回执——只有路径一项可报，就在下面那条分支里直接成文。
        message: str
        if before_lock is not None:
            before_lock()
        async with locks.async_file_lock(path):
            draft, actual_revision = await asyncio.to_thread(
                self._draft_snapshot,
                episode,
                resolved,
            )
            if draft is None:
                return await asyncio.to_thread(self._read, episode, resolved)
            if script_review.fingerprint_of_revision(base_revision) != script_review.fingerprint_of_revision(
                actual_revision
            ):
                raise DraftWorkflowError(
                    "revision_conflict",
                    f"draft revision changed: expected {base_revision}, actual {actual_revision}",
                )
            await asyncio.to_thread(self._reject_confirmed_script_plan_edit, episode, resolved, draft)
            try:
                if resolved in _SINGLE_SCRIPT_PLAN_PROMOTERS:
                    message = await _SINGLE_SCRIPT_PLAN_PROMOTERS[resolved](self.ctx, episode, draft)
                elif resolved == QUARANTINE_KIND_SCRIPT_PLAN:
                    message = await _promote_reference_script_plan(
                        self.ctx,
                        episode,
                        draft,
                        before_commit=before_commit,
                    )
                else:
                    # Prompt authoring only reads the final script: an unconfirmed script plan after a re-run does not
                    # block promotion, same rule as the authoring entry.
                    # 提示词编写只读正式剧本：脚本规划重跑后尚未确认不阻塞晋升，与编写入口同口径。
                    generator = await ScriptGenerator.create(
                        project_path,
                        config_resolver=self.ctx.config_resolver,
                    )
                    promote_kwargs = (
                        {"expected_fingerprint": draft.meta["base_fingerprint"]}
                        if "base_fingerprint" in draft.meta
                        else {}
                    )
                    result_path = await generator.promote_reference_prompt_authoring_draft(
                        episode,
                        _prompt_authoring_lock_held=True,
                        **promote_kwargs,
                    )
                    message = f"✅ 提示词编写晋升（正式剧本）已保存: {_project_relative(result_path, project_path)}"
            except (DraftWorkflowError, ModelNotInCatalogError):
                # ModelNotInCatalogError is re-raised as-is: the caller uses it to give "choose another model"
                # guidance
                # ModelNotInCatalogError 原样上抛：调用方据此给出「重新选择模型」指引
                raise
            except ScriptWriteConflict as exc:
                raise DraftWorkflowError(
                    "formal_revision_conflict", _render_prompt_authoring_conflict_report(episode, draft, exc)
                ) from exc
            except DraftViolation as exc:
                # When prompt authoring promotion is blocked by violations, the report has been written back to the
                # draft (code="quarantined"); other violations come from the final script, and those with a text key
                # (a final script unit that failed the pre-check) explain the cause using the UI's name for units.
                # 提示词编写晋升被违约挡下时，报告已写回草稿（code="quarantined"）；其余违约来自正式剧本，
                # 带文案 key 的（正式剧本单元没过预判）按界面单元称呼说明原因。
                refreshed = exc.code == "quarantined"
                notice = exc.params is not None and not refreshed
                raise DraftWorkflowError(
                    "draft_invalid",
                    str(exc),
                    draft_refreshed=refreshed,
                    notice_key=exc.code if notice else None,
                    notice_params=dict(exc.params or {}) if notice else None,
                ) from exc
            except Exception as exc:
                raise DraftWorkflowError("draft_promote_failed", str(exc)) from exc
        value: dict[str, Any] = {
            "episode": episode,
            "doc_type": doc_type,
            "promoted": True,
            "message": message,
        }
        if result_path is not None:
            value["path"] = _project_relative(result_path, project_path)
        return value

    async def save(
        self,
        episode: int,
        doc_type: str,
        content: dict[str, Any],
        base_revision: str,
    ) -> dict[str, Any]:
        """A creator hand-fixes an invalid draft: after writing the body, fully re-validate with the promotion rules,
        and adopt it as final content once violations are cleared.

        Returns ``adopted=True`` when adopted; when violations remain, the report has been written back to the draft
        based on current values, returning ``adopted=False`` and the refreshed draft. Editable drafts belong to the
        Agent, so they are refused. Failures other than re-validation (concurrency conflict, video capabilities that
        cannot be resolved) are raised as usual; the body has already been written to the draft by then and is not
        lost.

        创作者手修待修复草稿：写入正文后按晋升口径全量重判，违约清零即采用为正式内容。

        采用时返回 ``adopted=True``；仍有违约时报告已按现值写回草稿，返回 ``adopted=False`` 与刷新后
        的草稿。可编辑草稿归 Agent 处置，拒绝。重判之外的失败（并发冲突、视频能力解析不出）照常
        抛出，此时正文已写入草稿，不会丢失。
        """
        resolved = await self._kind(episode, doc_type)
        current = await asyncio.to_thread(lambda: read_quarantine(self.ctx.project_path, episode, resolved))
        if current is not None and draft_owner(current) == DRAFT_OWNER_AGENT:
            raise DraftWorkflowError(
                "draft_agent_owned", f"episode (id={episode}): {doc_type} draft is being edited by the agent"
            )
        patched = await self.patch(episode, doc_type, content, base_revision)
        try:
            promoted = await self.promote(episode, doc_type, patched["revision"])
        except DraftWorkflowError as exc:
            if not exc.draft_refreshed:
                raise
            refreshed = await asyncio.to_thread(self._read, episode, resolved)
            return {"episode": episode, "doc_type": doc_type, "adopted": False, "draft": refreshed}
        return {"episode": episode, "doc_type": doc_type, "adopted": True, "message": promoted["message"]}

    async def discard(
        self,
        episode: int,
        doc_type: str,
        base_revision: str,
    ) -> dict[str, Any]:
        resolved = await self._kind(episode, doc_type, allow_stale_discard=True)
        project_path, path, locks = await asyncio.to_thread(self._quarantine_lock_target, episode, resolved)
        async with locks.async_file_lock(path):
            draft, actual_revision = await asyncio.to_thread(
                self._draft_snapshot,
                episode,
                resolved,
            )
            if draft is not None and script_review.fingerprint_of_revision(
                base_revision
            ) != script_review.fingerprint_of_revision(actual_revision):
                raise DraftWorkflowError(
                    "revision_conflict",
                    f"draft revision changed: expected {base_revision}, actual {actual_revision}",
                )
            discarded = await run_sync_transaction(discard_quarantine, project_path, episode, resolved)
        return {"episode": episode, "doc_type": doc_type, "discarded": discarded}


def doc_type_for_kind(kind: str) -> str:
    return next(doc_type for doc_type, mapped in DOC_TYPE_TO_QUARANTINE_KIND.items() if mapped == kind)
