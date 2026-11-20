"""Service layer for the web content review between script_plan and prompt_authoring: reading review state, editing the
structured intermediate state, and the confirm action.

The pure gate logic (applicability / fingerprint / state derivation) lives in ``lib.script.script_review``; this
layer adds ProjectManager persistence (the confirm fingerprint goes into project.json
``episodes[i].script_plan_review``) and Pydantic validation and saving of the structured content.

Confirming mechanically converts the whole script plan into a final script (all entries pending authoring), written
in the same write as the confirm record; if the episode already has a final script, confirming overwrites it and
must be explicitly approved by the caller. Prompt authoring (script visual generation) is done by the Agent's
``generate_episode_script`` tool, which only reads the final script and does not pass through the content review
gate.

script_plan→prompt_authoring web 内容确认的服务层：确认状态读取、结构化中间态编辑、确认动作。

纯 gate 逻辑（适用性 / 指纹 / 状态派生）在 ``lib.script.script_review``；本层叠加 ProjectManager
持久化（确认指纹落 project.json ``episodes[i].script_plan_review``）与结构化内容的 Pydantic 校验、落盘。

确认即把脚本规划整份机械转为正式脚本（全部条目待编写），并与确认记录同一次写入落盘；该集已有
正式脚本时确认就是覆盖，须调用方显式认可。提示词编写（剧本视觉生成）由 Agent 的
``generate_episode_script`` 工具执行，只读正式脚本，不经内容确认门禁。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from lib.config.resolver import ConfigResolver
from lib.episode.episode_target_duration import project_episode_target_duration
from lib.generation.video_request_facts import (
    VideoRequestFactsError,
    VideoRequestFactsFailure,
    reference_migration_durations,
)
from lib.infra.async_thread import run_sync_transaction
from lib.infra.json_io import load_json_or_none
from lib.project.project_manager import ProjectManager, ScriptWriteConflict
from lib.script import script_review
from lib.script.draft_quarantine import (
    DRAFT_OWNER_AGENT,
    DRAFT_OWNER_USER,
    QUARANTINE_KIND_PROMPT_AUTHORING,
    QUARANTINE_KIND_TO_DOC_TYPE,
    draft_owner,
    draft_revision,
    quarantine_path,
    read_quarantine,
    violation_entries,
)
from lib.script.plan_new_assets import NEW_ASSETS_FIELD, NewAssetsError, UnregisteredReferencesError, resolve_new_assets
from lib.script.script_generator import ScriptGenerator, UnitDurationOffTier
from lib.script.script_models import DramaNormalizedScript, NarrationScriptPlanDraft, ReferenceScriptPlanDraft
from lib.speech.speech_composition import SpeechAdmission, SpeechAdmissionError, admit_script_unit
from server.services.tasks.video_caps import (
    reference_request_facts_lookup,
    reference_unit_capabilities,
)
from server.text_generation import fetch_reference_split_caps, reference_soft_violations, soft_violation_entries

logger = logging.getLogger(__name__)

#: Validation models for the structured script_plan intermediate state (by script_plan variant
#: ``script_review.script_plan_kind``). Saving an edit validates the structure against these: drama uses the
#: content-layer DramaNormalizedScript (utterances / source_text / scene_description), narration uses
#: NarrationScriptPlanDraft (structured novel_text shots), reference_video uses ReferenceScriptPlanDraft (units -> body
#: + planned duration).
#: 结构化 script_plan 中间态的校验模型（按 script_plan 变体 ``script_review.script_plan_kind``）。编辑保存按此做结构校验：
#: drama 为内容层 DramaNormalizedScript（utterances / source_text / scene_description），
#: narration 为 NarrationScriptPlanDraft（结构化 novel_text 分镜），reference_video 为 ReferenceScriptPlanDraft
#: （units → 正文 + 编排时长）。
_SCRIPT_PLAN_CONTENT_MODEL: dict[str, type[BaseModel]] = {
    "drama": DramaNormalizedScript,
    "narration": NarrationScriptPlanDraft,
    "reference_video": ReferenceScriptPlanDraft,
}


class ScriptReviewError(Exception):
    """Domain error for gate operations. ``code`` lets the router map the HTTP status and i18n key; ``message`` holds
    technical details.

    gate 操作的领域错误。``code`` 供 router 映射 HTTP 状态与 i18n key；``message`` 为技术细节。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        admission: SpeechAdmission | None = None,
        overwrite: dict[str, Any] | None = None,
        script_filename: str | None = None,
        problem: VideoRequestFactsFailure | None = None,
        params: Mapping[str, object] | None = None,
    ):
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.admission = admission
        #: ``video_request_facts`` carries the failure object of the video request facts; the router returns it to the
        #: user by its problem code and parameters.
        #: ``video_request_facts`` 携带视频请求事实的失败对象，router 按其问题码与参数回给用户。
        self.problem = problem
        #: ``overwrite_required`` carries the final script that would be overwritten
        #: (``FormalScriptOverwrite.to_dict()``).
        #: ``overwrite_required`` 携带将被覆盖的正式脚本（``FormalScriptOverwrite.to_dict()``）。
        self.overwrite = overwrite
        #: ``foreign_formal_script`` carries the file name occupying this episode's canonical path.
        #: ``foreign_formal_script`` 携带占着本集规范路径的那份文件名。
        self.script_filename = script_filename
        #: ``duration_off_tier`` carries the UI position, current duration and selectable options of the off-tier unit;
        #: the router renders the message from these.
        #: ``duration_off_tier`` 携带越档单元的界面位置、现时长与可选档位，router 按此渲染文案。
        self.params: dict[str, object] = dict(params or {})


def _require_changed_speech_admitted(kind: str, previous: object, candidate: object) -> None:
    """Reject only new or edited speech content, leaving legacy mixed metadata editable."""

    if kind == "drama":
        root, skeleton, id_field, speech_fields = "scenes", "scenes", "scene_id", ("utterances",)
    elif kind == "reference_video":
        root, skeleton, id_field, speech_fields = "units", "video_units", "unit_id", ("text",)
    else:
        return
    if not isinstance(candidate, dict) or not isinstance(candidate.get(root), list):
        return
    previous_units = previous.get(root) if isinstance(previous, dict) else None
    previous_by_id = {
        unit.get(id_field): unit
        for unit in previous_units or []
        if isinstance(unit, dict) and isinstance(unit.get(id_field), str)
    }
    for unit in candidate[root]:
        if not isinstance(unit, dict) or not isinstance(unit.get(id_field), str):
            continue
        old = previous_by_id.get(unit[id_field])
        speech_changed = old is None or any(old.get(field) != unit.get(field) for field in speech_fields)
        if not speech_changed:
            if isinstance(old, dict) and old.get("needs_replan") is True:
                unit["needs_replan"] = True
            continue
        admission = admit_script_unit(skeleton, unit, ignore_marker=True)
        if not admission.allowed:
            raise ScriptReviewError("speech_admission", admission=admission)
        unit.pop("needs_replan", None)


class ScriptReviewService:
    """Wraps reads and writes of the script_plan -> prompt_authoring content review. The router and tests operate the
    gate through this and do not touch files / project.json directly.

    封装 script_plan→prompt_authoring 内容确认的读写。router 与测试经此操作 gate，不直接碰文件 / project.json。"""

    def __init__(self, pm: ProjectManager, *, config_resolver: ConfigResolver | None = None):
        self.pm = pm
        self.config_resolver = config_resolver

    def _resolve_script_plan_model(self, project: dict[str, Any], episode: int) -> tuple[str, type[BaseModel]]:
        """The episode's script_plan variant + structure validation model; raises not_applicable when the gate does not
        apply (no structured script_plan).

        The single source of truth for the variant is ``script_review.script_plan_kind`` (reference_video goes by
        the project generation mode first, across content_mode); this layer picks the Pydantic model from it.
        Returns the variant name so callers can branch by variant.

        该集 script_plan 变体 + 结构校验模型；不适用 gate（无结构化 script_plan）时抛 not_applicable。

        变体判定单一真相源在 ``script_review.script_plan_kind``（reference_video 按项目生成模式优先，
        跨 content_mode）；本层据此选 Pydantic 模型。返回变体名供调用方按变体分流。
        """
        kind = script_review.script_plan_kind(project)
        if kind is None:
            raise ScriptReviewError("not_applicable")
        return kind, _SCRIPT_PLAN_CONTENT_MODEL[kind]

    def _require_episode(self, project_name: str, project: dict[str, Any], episode: int) -> dict[str, Any]:
        """When the gate applies, check that the episode is registered in project.json ``episodes[]`` and return the
        project.

        Matches the write precondition of ``confirm``: keeps ``get_state`` from misreporting an unregistered episode
        as no_script_plan, and keeps ``save_content`` from writing an orphan script_plan file for an unregistered
        episode that could never be linked to project.json. Episodes enter the ledger only through explicit
        registration; an episode file with the same name in ``source/`` does not count as registered.

        gate 适用时校验该集已在 project.json ``episodes[]`` 登记，返回 project。

        与 ``confirm`` 的写入前置一致：避免 ``get_state`` 把未登记分集误报成 no_script_plan、
        ``save_content`` 给未登记分集写出永远无法与 project.json 关联的孤儿 script_plan 文件。
        集只经显式登记进入账本，``source/`` 里有同名集文件不算登记。
        """
        if script_review.find_episode(project, episode) is None:
            raise ScriptReviewError("episode_not_found")
        return project

    async def _resolve_supported_durations(self, project: dict[str, Any]) -> list[int] | None:
        """The full duration set used for structural normalization: the reference route takes the union of the declared
        full sets of the r2v and i2v buckets of the video request facts.

        None when either bucket cannot be resolved: the read-time migration then only clamps to the structural range
        and does not rewrite the user's seconds by the other bucket's options, and the gate panel offers no
        selectable options; tier drift is blocked by the per-unit fact check that follows.

        Callers must await this method **before** taking ``self.pm.file_lock``: that lock is a blocking file lock,
        and holding it across an await would also lock out other coroutines on the event loop.

        结构收编用的时长全集：参考路线取 r2v 与 i2v 两桶视频请求事实声明全集的并集。

        任一桶解析不出时为 None——读时迁移只做结构区间 clamp、不按另一桶的档位改写用户秒数，
        gate 面板也不提供可选档位；档位偏移由随后的逐单元事实校验阻断。

        调用方须在取 ``self.pm.file_lock`` **之前** await 本方法：那把锁是阻塞式文件锁，跨
        await 持有会连带把事件循环上的其它协程挡在锁外。
        """
        if script_review.script_plan_kind(project) != "reference_video":
            return None
        request_facts = reference_request_facts_lookup(project, self.config_resolver)
        return reference_migration_durations(await request_facts("r2v"), await request_facts("i2v"))

    def _read_script_plan_migrated(
        self,
        project_name: str,
        project: dict[str, Any],
        episode: int,
        path: Path,
        project_path: Path,
        supported_durations: list[int] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Read the structured script_plan and run a one-time duration normalization migration on reference-to-video
        drafts; returns ``(content, latest project)``.

        The draft is the only content source for the gate's three entry points (read state / save / confirm), and
        the normalized ``ReferenceScriptPlanUnit`` requires unit-level ``duration_seconds`` and accepts no extra
        fields: if existing drafts are not migrated at read time, both save and confirm hit structure validation,
        and the user can neither edit nor confirm in the gate. The migration for episode scripts is in
        ``ProjectManager.load_script``, the one for drafts is here; both use the same migrator.

        ``supported_durations`` is resolved by the caller via ``_resolve_supported_durations`` and passed in (this
        function runs inside the lock and cannot await itself), from the same source as the prompt_authoring
        generation side: the migration is idempotent and one-time, and whoever runs first sets the final result. If
        the two sides disagree, a content review that runs first would fix on disk seconds that are inside the
        structural range but not tier members; prompt_authoring's enum schema then hard-rejects them, and the user
        can neither see the problem nor change it in the gate. When None (no video model configured for the
        project), fall back to the structural range clamp: missing configuration should not block loading the draft,
        and tier drift is still caught when the tier is taken at execution time.

        The caller must already hold ``self.pm.file_lock(path)``; reference_video must also hold the
        prompt_authoring draft lock first, with the uniform lock order "downstream draft -> final script_plan". This
        function only reads, modifies and writes and takes no lock itself, avoiding a same-thread re-acquire
        deadlock on a lock the caller (e.g. ``confirm``) already holds.

        读结构化 script_plan，并对参考生视频草稿做一次性时长收编迁移；返回 ``(内容, 最新 project)``。

        草稿是 gate 的三个入口（读状态 / 保存 / 确认）唯一的内容来源，而收编后的
        ``ReferenceScriptPlanUnit`` 要求 unit 级 ``duration_seconds`` 且不接受多余字段：存量草稿若不在
        读时迁移，保存与确认都会撞结构校验，用户在 gate 里既改不了也确认不了。
        剧集脚本的迁移在 ``ProjectManager.load_script``，草稿的在这里，两处共用同一迁移器。

        ``supported_durations`` 由调用方经 ``_resolve_supported_durations`` 解析后传入（本函数
        跑在锁内，不能自行 await），与 prompt_authoring 生成侧同源：迁移幂等一次性、谁先跑谁定终局，两侧
        口径不一致时先跑的内容确认会把落在结构区间内但非档位成员的秒数固化到盘上，prompt_authoring 的枚举
        schema 随后硬拒，用户在 gate 里既看不出问题也改不动。为 None（项目未配置视频型号）时
        退回结构区间 clamp——缺配置不该阻断草稿加载，档位偏移仍由执行时取档兜底。

        调用方须已持有 ``self.pm.file_lock(path)``；reference_video 还须先持 prompt_authoring 草稿锁，
        统一锁序为「下游草稿 → 正式 script_plan」。本函数只做读改写，不自行加锁，避免与调用方
        （如 ``confirm``）已持有的同一把锁发生同线程二次获取的死锁。
        """
        content = _read_json(path)
        if content is None or script_review.script_plan_kind(project) != "reference_video":
            return content, project

        updated, _warnings = script_review.migrate_script_plan_draft_in_place(
            project_path,
            content,
            episode=episode,
            update_project=lambda mutate: self.pm.update_project(project_name, mutate),
            supported_durations=supported_durations,
        )
        return content, updated or project

    async def get_state(self, project_name: str, episode: int) -> dict[str, Any]:
        """Return the episode's content review state + structured intermediate content (for web rendering).

        ``content`` is the parsed structured script_plan (drama: {title, scenes[]}; narration: {segments[]};
        reference_video: {units[]}); None when the gate does not apply or the script_plan is missing / corrupt.

        ``supported_durations`` is the full structural tier set for the reference route's read-time migration; None
        when i2v is unknown, so r2v tiers are not borrowed to rewrite seconds. The selectable options in the UI are
        provided per bucket by ``get_reference_duration_tiers``.

        Tier resolution finishes before the disk reads and writes; the rest of the synchronous I/O is offloaded to a
        thread as a whole: ``file_lock`` is a blocking file lock and cannot be held across await, while tier
        resolution itself has to await the video capability query.

        返回该集内容确认状态 + 结构化中间态内容（供 web 渲染）。

        ``content`` 为解析后的结构化 script_plan（drama: {title, scenes[]}；narration: {segments[]}；
        reference_video: {units[]}）；不适用 gate 或 script_plan 缺失 / 损坏时为 None。

        ``supported_durations`` 是参考路线读时迁移的结构档位全集；i2v 未知时为 None，
        不借用 r2v 档位改写秒数。界面可选档位由 ``get_reference_duration_tiers`` 按桶提供。

        档位解析先于落盘读写完成，其余同步 I/O 整段卸到线程：``file_lock`` 是阻塞式文件锁，
        不能跨 await 持有，而档位解析本身要 await 视频能力查询。
        """
        project = await asyncio.to_thread(self.pm.load_project, project_name)
        supported_durations = await self._resolve_supported_durations(project)
        state = await run_sync_transaction(self._get_state_sync, project_name, project, episode, supported_durations)
        state["soft_violations"] = []
        content = state["content"]
        if script_review.script_plan_kind(project) == "reference_video" and isinstance(content, dict):
            try:
                caps = await fetch_reference_split_caps(project, config_resolver=self.config_resolver)
            except VideoRequestFactsError:
                return state
            units = content.get("units") or []
            texts = [str(unit.get("text") or "") if isinstance(unit, dict) else "" for unit in units]
            unit_ids = [
                unit_id for unit in units if isinstance(unit, dict) and isinstance(unit_id := unit.get("unit_id"), str)
            ]
            state["soft_violations"] = soft_violation_entries(
                reference_soft_violations(
                    texts,
                    project,
                    episode=episode,
                    voice=caps.voice,
                    # Units of the final script_plan carry the numbers issued when written to disk; the hint locates
                    # by them
                    # 正式 script_plan 的单元带着落盘时发出的编号，提示按它定位
                    unit_ids=unit_ids if len(unit_ids) == len(units) else None,
                )
            )
        return state

    def _get_state_sync(
        self,
        project_name: str,
        project: dict[str, Any],
        episode: int,
        supported_durations: list[int] | None,
    ) -> dict[str, Any]:
        """Synchronous body of ``get_state``: episode self-heal, read-time migration, state and fingerprint derivation,
        all done in one thread.

        ``project`` and ``supported_durations`` are fetched in advance by ``get_state`` and passed in: the migration
        does not change the video model configuration, and the tier table describes the same thing before and after
        the migration, so there is no need to recompute it inside the lock.

        ``get_state`` 的同步主体：分集自愈、读时迁移、状态与指纹派生，全部在一个线程内完成。

        ``project`` 与 ``supported_durations`` 由 ``get_state`` 预先取好传入——迁移不改动视频
        型号配置，档位表在迁移前后描述的是同一件事，无需在锁内重算。
        """
        project_path = self.pm.get_project_path(project_name)
        path = script_review.script_plan_path(project_path, project, episode)
        if path is not None:
            # Only an applicable gate (drama / narration, not reference_video) requires the episode to be registered;
            # not_applicable (ad / reference_video) has nothing to do with episode existence and is returned as is.
            # 适用 gate（drama / narration 非 reference_video）才要求分集已登记；
            # not_applicable（ad / reference_video）与分集存在性无关，保持原样返回。
            project = self._require_episode(project_name, project, episode)
        content: dict[str, Any] | None = None
        fingerprint: str | None = None
        if path is not None:
            # The migration may write back the script_plan and the confirm record; fingerprint and state are taken
            # inside the same lock after the migration, so all three are derived from the same on-disk content. Taking
            # them outside the lock would let a concurrent save_content make the fingerprint describe different
            # content.
            # 迁移可能回写 script_plan 与确认记录；指纹与状态在同一把锁内、迁移之后取，三者才据
            # 同一份落盘内容派生——锁外取则并发 save_content 会让指纹描述另一份内容。
            prompt_authoring_lock = (
                self.pm.file_lock(quarantine_path(project_path, episode, QUARANTINE_KIND_PROMPT_AUTHORING))
                if script_review.script_plan_kind(project) == "reference_video"
                else nullcontext()
            )
            with prompt_authoring_lock, self.pm.file_lock(path):
                content, project = self._read_script_plan_migrated(
                    project_name, project, episode, path, project_path, supported_durations
                )
                fingerprint = script_review.content_fingerprint(path)
                status = script_review.review_status(project_path, project, episode)
        else:
            status = script_review.review_status(project_path, project, episode)
        return {
            "episode": episode,
            "content_mode": project.get("content_mode"),
            "status": status,
            "fingerprint": fingerprint,
            "confirmed_at": script_review.stored_review(project, episode).get("confirmed_at"),
            "content": content,
            "supported_durations": supported_durations,
            # Project-level "target duration per episode" preference (seconds), None if unset. The review panel uses
            # it to render the "episode total / target" comparison; it is returned with the state rather than having
            # the frontend send another project request, so the total and target the panel gets come from the same
            # read.
            # 项目级「单集目标时长」偏好（秒），未设时 None。审核面板据它渲染「本集合计 / 目标」
            # 对比；随 state 一起回传而非让前端另发一次项目请求，面板拿到的合计与目标出自同一次读取。
            "episode_target_duration": project_episode_target_duration(project),
            # The final script that confirming would overwrite (None if none): the content review page uses it to
            # render the confirm as danger and list the consequences.
            # 确认将覆盖的正式脚本（无则 None）：内容确认页据此把确认渲染为 danger 并列出后果。
            "script_overwrite": _overwrite_dict(project_path, project, episode) if path is not None else None,
        }

    async def get_quarantine_info(self, project_name: str, episode: int) -> dict[str, Any] | None:
        """Presentation view of this episode's script_plan draft (for display and manual fixes on the content review
        page); None when not applicable / no draft.

        View fields: ``doc_type``, ``revision`` (concurrency token for saving and discarding manual fixes; None when
        the envelope is corrupt), ``editable_by`` (``user`` for an invalid draft, ``agent`` for the Agent's editable
        draft), ``content`` (the draft body as is, for the creator to edit in place; not shown for the Agent's
        editable draft, None), ``violations``, ``soft_violations`` (degradation notes located per entry) and
        ``formal_exists`` (whether there is a final script plan to fall back to after discarding).

        Violations are fully recomputed at read time with the same validators used at production
        (``revalidate_script_plan_draft`` dispatches by kind to that variant's re-judge, the same code as the
        promotion tool), without trusting the previous round's ``violations`` snapshot in the draft: the source or
        model configuration may have changed while the draft exists, and the report must answer for current values.
        When meta is broken so badly that it cannot be recomputed, "cannot recompute" itself is returned as a
        violation, rather than falling back to the previous snapshot in the draft. Editable drafts belong to the
        Agent and are not recomputed.

        This method and ``get_state`` each read from disk independently; the router merges their returns within the
        same request. Synchronous project / draft file reads are offloaded to a thread via ``asyncio.to_thread`` so
        a large source does not hog the event loop.

        本集 script_plan 草稿的呈现视图（供内容确认页展示与手修），不适用 / 无草稿时 None。

        视图字段：``doc_type``、``revision``（手修保存与丢弃的并发令牌；信封损坏时为 None）、
        ``editable_by``（``user`` 为待修复草稿，``agent`` 为 Agent 的可编辑草稿）、``content``（草稿正文
        原样，供创作者就地修改；Agent 的可编辑草稿不展示内容，为 None）、``violations``、
        ``soft_violations``（逐条目定位的降级提示）与 ``formal_exists``（丢弃后是否有正式脚本规划可回）。

        违约读时按产出时那套校验器全量重算（``revalidate_script_plan_draft`` 按 kind 分派到该变体的
        重判器，晋升工具同一份代码），不信任草稿里 ``violations`` 的上一轮快照——草稿在场期间源文或
        模型配置可能已变，报告要对现值负责。meta 被改坏以致无从重算时，把「无法重算」本身作为一条
        违约返回，而不是退回草稿里那份上一轮快照。可编辑草稿归 Agent 处置，不重算。

        本方法与 ``get_state`` 各自读盘、互不依赖，由 router 在同一次请求内合并两者的返回。项目 /
        草稿的同步文件读取经 ``asyncio.to_thread`` 卸到线程，避免源文较大时占用事件循环。
        """
        project = await asyncio.to_thread(self.pm.load_project, project_name)
        quarantine_kind = script_review.script_plan_quarantine_kind(project)
        if quarantine_kind is None:
            return None

        def _locate() -> tuple[Path, Path | None, bool]:
            project_path = self.pm.get_project_path(project_name)
            quarantine_path = script_review.script_plan_quarantine_path(project_path, project, episode)
            if quarantine_path is None or not quarantine_path.exists():
                return project_path, None, False
            formal_path = script_review.script_plan_path(project_path, project, episode)
            return project_path, quarantine_path, formal_path is not None and formal_path.exists()

        project_path, quarantine_path, formal_exists = await asyncio.to_thread(_locate)
        if quarantine_path is None:
            return None
        view: dict[str, Any] = {
            "doc_type": QUARANTINE_KIND_TO_DOC_TYPE[quarantine_kind],
            "revision": None,
            "editable_by": DRAFT_OWNER_USER,
            "content": None,
            "violations": [],
            "soft_violations": [],
            "formal_exists": formal_exists,
        }
        draft = await asyncio.to_thread(read_quarantine, project_path, episode, quarantine_kind)
        if draft is None:
            if not await asyncio.to_thread(quarantine_path.exists):
                # In the window between the existence check and the read, the Agent's promotion/re-split tool cleared
                # the file (final content written, pending draft removed): this is not envelope corruption but this
                # read spanning the moment of "clearing"; treat it as "no draft" and do not misreport corruption.
                # 存在性检查与读取之间的窗口内，Agent 的晋升/重拆分工具把文件清掉了（正式内容
                # 已写入、待处置草稿已清除）：不是信封损坏，是这次读跨越了「清除」那一刻，按「无
                # 草稿」处理，不误报损坏。
                return None
            # File exists but the envelope shape is broken (invalid JSON / top level not an object / content not an
            # object): `read_quarantine` returning None as "no draft" is its own read convention (so confirm's
            # existence check still blocks as usual), but the presentation layer must not take it at face value: that
            # would make the panel look "clean" while the draft is still blocking confirmation.
            # 文件存在但信封形状坏（非法 JSON / 顶层非对象 / content 非对象）：`read_quarantine`
            # 按「无草稿」返回 None 是它自己的读取口径（供 confirm 的存在性判断照常阻塞），
            # 但呈现层不能照单全收——那会让面板看起来「干净」，实际草稿仍在阻塞确认。
            view["violations"] = [_unreadable_violation("The draft file is corrupted or malformed; cannot parse it")]
            return view
        view["revision"] = draft_revision(draft)
        view["editable_by"] = draft_owner(draft)
        if view["editable_by"] == DRAFT_OWNER_AGENT:
            return view
        view["content"] = draft.content
        # Deferred import avoids a module-level circular dependency: draft_workflow reuses this service's locked write
        # capability.
        # 延迟导入避免模块级循环依赖：draft_workflow 复用本服务的持锁写入能力。
        from server.draft_workflow import revalidate_script_plan_draft

        try:
            revalidation = await revalidate_script_plan_draft(
                project_path,
                project,
                episode,
                draft,
                config_resolver=self.config_resolver,
            )
        except VideoRequestFactsError as exc:
            # Video request facts that cannot be resolved are a configuration problem, not draft corruption: report
            # one violation with no entry attribution using the fact's problem code and parameters; the router builds
            # the message from the problem code's text key and parameters.
            # 视频请求事实解析不出是配置问题而非草稿损坏：按事实的问题码与参数报成一条无条目归属的
            # 违约，message 由 router 按问题码的文案 key 与参数成文。
            view["violations"] = [
                {
                    "code": exc.code,
                    "label": "",
                    "message": "",
                    "line": None,
                    "params": exc.params,
                }
            ]
            return view
        except ValueError as exc:
            # Cases where the draft was broken, such as missing meta.source: report the recompute failure itself as
            # one episode-level violation. The exception text contains draft.path, so it is only logged and not
            # returned to the caller; user-facing messages must not carry internal paths.
            # meta.source 缺失等草稿被改坏的情形：把重算失败本身报成一条整集层面的违约。异常文本含
            # draft.path，只记日志、不回传给调用方——面向用户的 message 不能带内部路径。
            logger.warning("草稿重算失败 project=%s episode=%s：%s", project_name, episode, exc)
            view["violations"] = [
                _unreadable_violation("The draft's generation context is missing or corrupted, so it cannot be revalidated")
            ]
            return view
        view["violations"] = violation_entries(revalidation.violations)
        view["soft_violations"] = soft_violation_entries(list(revalidation.soft_violations))
        return view

    async def get_reference_duration_tiers(
        self, project_name: str, episode: int, units: Sequence[dict[str, Any]] = ()
    ) -> dict | None:
        """Return the effective duration options of the reference route with reference images, and the per-unit bucket
        verdicts.

        ``units`` are the units of this episode's script plan: the ``units`` key gives, by ``unit_id``, the bucket
        each unit falls into based on its available reference images, that bucket's options, and how the declared
        references split; the panel picks options from these. Returns None for a non-reference route or when the
        video request facts of the r2v bucket cannot be resolved; the presentation layer then stays read-only.
        Project file reads are offloaded to a thread to avoid blocking the request loop.

        返回参考路线带参考图的生效档位与逐单元定桶结论。

        ``units`` 是本集脚本规划的单元：``units`` 键按 ``unit_id`` 给出每个单元按可用参考图
        所落的桶、该桶档位与声明引用的分裂情况，面板据此取档。
        非参考路线或 r2v 桶的视频请求事实解析不出时返回 None，呈现层保持只读。项目文件读取卸到线程，避免阻塞请求循环。
        """
        project = await asyncio.to_thread(self.pm.load_project, project_name)
        if script_review.script_plan_kind(project) != "reference_video":
            return None
        request_facts = reference_request_facts_lookup(project, self.config_resolver)
        with_ref_facts = await request_facts("r2v")
        if isinstance(with_ref_facts, VideoRequestFactsFailure):
            return None
        project_path = await asyncio.to_thread(self.pm.get_project_path, project_name)
        unit_capabilities = await reference_unit_capabilities(
            project,
            project_path,
            units,
            request_facts=request_facts,
        )
        return {
            "with_references": list(with_ref_facts.allowed_durations),
            "units": unit_capabilities,
        }

    async def save_content(
        self, project_name: str, episode: int, content: object, base_fingerprint: str | None = None
    ) -> dict[str, Any]:
        """Validate and save the edited structured intermediate state (written back after manual or Agent edits);
        returns the latest state (awaiting confirmation again).

        A content change makes the fingerprint drift, and ``get_state`` then automatically goes back to
        pending_review: saving means confirmation is needed again. A confirmed script plan is read-only; saving
        raises ``script_plan_confirmed`` and writes nothing; it becomes saveable again after re-running the script
        plan writes new content. While the Agent's editable draft exists, the final plan is also read-only and
        saving raises ``draft_agent_owned``.

        ``base_fingerprint`` is the fingerprint the editor got when reading the content (``get_state``): when given,
        it is compared inside the lock against the current value on disk; on mismatch (another writer changed the
        script_plan during editing) ``conflict`` is raised and nothing is written. The later writer gets a 409
        conflict prompt to refresh and merge, and the earlier writer's content is not silently overwritten. ``None``
        skips the comparison (direct calls without a baseline).

        The save itself is fully synchronous (no duration normalization, no tier table needed) and is offloaded to a
        thread as a whole; the following ``get_state`` resolves the tier table itself, so the save response and the
        first GET load come from the same source.

        校验并落盘编辑后的结构化中间态（手动或 Agent 编辑后回写），返回最新状态（重新等待确认）。

        内容变更使指纹漂移，``get_state`` 据此自动回到 pending_review——保存即重新需要确认。
        已确认的脚本规划只读，保存抛 ``script_plan_confirmed``、不落盘；重跑脚本规划写出新内容后恢复可保存。
        Agent 的可编辑草稿在场时，正式规划也只读，保存抛 ``draft_agent_owned``。

        ``base_fingerprint`` 是编辑方读取内容（``get_state``）时拿到的指纹：给定时在锁内与盘上
        现值比对，不一致（编辑期间另一写入方已改过 script_plan）抛 ``conflict``、不落盘——后写方拿
        409 冲突提示去刷新合并，先写方的内容不被静默覆盖。``None`` 不比对（无基线的直连调用）。

        落盘本身全同步（不做时长收编，无需档位表），整段卸到线程；随后的 ``get_state`` 自行
        解析档位表，保存的响应与 GET 首次加载因此同源。
        """
        await run_sync_transaction(self._save_content_sync, project_name, episode, content, base_fingerprint)
        return await self.get_state(project_name, episode)

    def _save_content_sync(
        self, project_name: str, episode: int, content: object, base_fingerprint: str | None
    ) -> None:
        """Synchronous body of ``save_content``: structure validation, baseline comparison, saving.

        ``save_content`` 的同步主体：结构校验、基线比对、落盘。"""
        project = self.pm.load_project(project_name)
        project_path = self.pm.get_project_path(project_name)
        path = script_review.script_plan_path(project_path, project, episode)
        if path is None:
            raise ScriptReviewError("not_applicable")
        project = self._require_episode(project_name, project, episode)
        kind, model = self._resolve_script_plan_model(project, episode)
        try:
            validated = model.model_validate(content).model_dump()
        except ValidationError as exc:
            _require_changed_speech_admitted(kind, _read_json(path), content)
            raise ScriptReviewError("invalid_content", str(exc)) from exc
        if kind == "drama":
            for scene in validated["scenes"]:
                if scene.get("needs_replan") is not True:
                    scene.pop("needs_replan", None)
        if not validated.get(NEW_ASSETS_FIELD):
            validated.pop(NEW_ASSETS_FIELD, None)
        # None in the input means "caller has no baseline, do not compare"; None in the comparison semantics means
        # something else (the file did not exist when the baseline was taken). The two are converted here once, and
        # all three variants share the same expected.
        # 入参的 None 表示「调用方无基线、不比对」；比对语义里的 None 另有含义（取基线时文件不存在），
        # 两者在此一次性转换，三个变体共用同一个 expected。
        expected = base_fingerprint if base_fingerprint is not None else script_review.UNCHECKED_FINGERPRINT
        try:
            if kind == "reference_video":
                # Disk writes go through a single exit: the lock, baseline comparison and prompt_authoring draft
                # cleanup all live in write_script_plan_locked.
                # 写盘经单一出口：锁、基线比对、prompt_authoring 草稿清理都在 write_script_plan_locked 一处。
                prompt_authoring_path = quarantine_path(project_path, episode, QUARANTINE_KIND_PROMPT_AUTHORING)
                with (
                    self.pm.file_lock(prompt_authoring_path),
                    script_review.script_plan_write_lock(project_path, episode),
                ):
                    self._reject_readonly_script_plan(project_name, project_path, episode)
                    _require_changed_speech_admitted(kind, _read_json(path), validated)
                    script_review.write_script_plan_locked(
                        project_path, episode, validated, expected_fingerprint=expected
                    )
            else:
                # Shares the same per-path lock with _read_script_plan_migrated: the read-modify-write of saving and
                # migration are mutually exclusive. The baseline comparison uses assert_base_fingerprint just like rv,
                # so the two conflict criteria do not fork; the comparison comes before the dialogue line admission
                # check, so a save with a stale baseline gets conflict rather than an admission complaint it cannot
                # fix. Since the comparison is done here, the write exit does not compare again (default UNCHECKED).
                # 与 _read_script_plan_migrated 共享同一把 per-path 锁：保存与迁移的读改写相互互斥。
                # 基线比对同 rv 走 assert_base_fingerprint，两者的冲突判据不分叉；比对先于
                # 台词准入判定，让基线过期的保存拿到 conflict 而不是一条它改不动的准入意见。
                # 比对既已在此做过，落盘出口不再重复比对（默认 UNCHECKED）。
                with script_review.formal_script_plan_lock(project_path, episode, path):
                    self._reject_readonly_script_plan(project_name, project_path, episode)
                    script_review.assert_base_fingerprint(path, expected)
                    _require_changed_speech_admitted(kind, _read_json(path), validated)
                    script_review.write_formal_script_plan_locked(project_path, episode, path, validated)
        except script_review.ScriptPlanWriteConflict as exc:
            raise ScriptReviewError("conflict", str(exc)) from exc

    def _reject_readonly_script_plan(self, project_name: str, project_path: Path, episode: int) -> None:
        """Holding the final plan lock, reject saves of a read-only plan based on the latest confirm record and the
        Agent draft ownership.

        持正式规划锁按最新确认记录与 Agent 草稿归属拒绝只读规划的保存。"""
        project = self.pm.load_project(project_name)
        if script_review.formal_script_plan_agent_owned(project_path, project, episode):
            raise ScriptReviewError("draft_agent_owned")
        if script_review.formal_script_plan_confirmed(project_path, project, episode):
            raise ScriptReviewError("script_plan_confirmed")

    async def confirm(
        self, project_name: str, episode: int, *, overwrite_revision: str | None = None
    ) -> dict[str, Any]:
        """Confirm the episode's script_plan: convert it wholesale into a final script and record the confirm
        fingerprint in the same write, unlocking prompt_authoring.

        Raises ScriptReviewError, mapped to 4xx by the router, when there is no script_plan / not applicable / the
        episode entry is missing / the script_plan content structure is invalid / a draft is pending / conversion
        admission is not met. If the episode already has a final script, ``overwrite_revision`` must equal the
        ``revision`` of its overwrite manifest (the caller approves overwriting exactly this one); if missing or
        mismatched, ``overwrite_required`` is raised carrying the current overwrite manifest, and neither the final
        script nor the confirm record is written.

        The tier table is resolved before taking the lock (same as ``get_state``): the confirm path also does a
        read-time normalization of existing drafts, and the tier table it uses must come from the same source as the
        one shown in the gate panel; otherwise confirming would fix on disk seconds that cannot be selected in the
        panel.

        确认该集 script_plan：整份转为正式脚本，并在同一次写入里记录确认指纹，放行 prompt_authoring。

        无 script_plan / 不适用 / 集条目缺失 / script_plan 内容结构非法 / 有草稿待处置 / 转换准入
        不满足时抛 ScriptReviewError，由 router 映射 4xx。该集已有正式脚本时，``overwrite_revision``
        须等于其覆盖清单的 ``revision``（调用方认可覆盖的正是这一份）；缺失或不符时抛
        ``overwrite_required``，携带当前的覆盖清单，不写正式脚本与确认记录。

        档位表先于加锁解析（同 ``get_state``）：确认路径同样要对存量草稿做一次读时收编，收编
        用的档位表须与 gate 面板呈现的那份同源，否则确认会把面板上选不到的秒数固化到盘上。
        """
        project = await asyncio.to_thread(self.pm.load_project, project_name)
        supported_durations = await self._resolve_supported_durations(project)

        def _locate_overwrite() -> tuple[Path, script_review.FormalScriptOverwrite | None]:
            project_path = self.pm.get_project_path(project_name)
            return project_path, script_review.formal_script_overwrite(project_path, project, episode)

        project_path, overwrite = await asyncio.to_thread(_locate_overwrite)
        fingerprint = await run_sync_transaction(
            self._confirm_sync, project_name, project, episode, supported_durations
        )
        if overwrite is not None and script_review.fingerprint_of_revision(overwrite_revision) != overwrite.fingerprint:
            raise ScriptReviewError("overwrite_required", overwrite=overwrite.to_dict())
        await self._materialize(
            project_path,
            episode,
            plan_fingerprint=fingerprint,
            script_fingerprint=overwrite.fingerprint if overwrite is not None else None,
        )
        return await self.get_state(project_name, episode)

    async def _materialize(
        self, project_path: Path, episode: int, *, plan_fingerprint: str, script_fingerprint: str | None
    ) -> None:
        """Save the whole-episode conversion together with the confirm record; if the script plan or final script was
        changed during conversion, reject as a conflict.

        整集转换与确认记录一起落盘；转换期间脚本规划或正式脚本被改过即按冲突拒绝。"""
        confirmed_at = datetime.now(UTC).isoformat()

        def _record_confirmation(project: dict[str, Any]) -> None:
            if not script_review.apply_confirmation(project, episode, plan_fingerprint, confirmed_at):
                raise ScriptReviewError("episode_not_found")

        generator = await asyncio.to_thread(ScriptGenerator, project_path, config_resolver=self.config_resolver)
        try:
            await generator.materialize_script_plan(
                episode,
                expected_plan_revision=plan_fingerprint,
                expected_script_fingerprint=script_fingerprint,
                project_update=_record_confirmation,
            )
        except SpeechAdmissionError as exc:
            raise ScriptReviewError("speech_admission", admission=exc.admission) from exc
        except (ScriptWriteConflict, script_review.ScriptPlanWriteConflict) as exc:
            raise ScriptReviewError("conversion_conflict", str(exc)) from exc
        except VideoRequestFactsError as exc:
            # Video request facts cannot be resolved (no model configured, options missing / invalid, narrowed to an
            # empty set): return them to the user by the fact's problem code and parameters, same codes as precheck
            # and execution.
            # 视频请求事实解析不出（未配置模型、档位缺失 / 无效、收成空集）：按事实的问题码与参数
            # 回给用户，与预检、执行同码。
            raise ScriptReviewError("video_request_facts", exc.code, problem=exc.failure) from exc
        except script_review.ForeignFormalScriptError as exc:
            # Before the ValueError branch below: when the binding is lost and the canonical path holds another
            # episode's script, the conversion is rejected before writing, neither rebuilding that episode's script
            # nor changing this episode's binding, and the hint points to the place that can be acted on.
            # 先于下面的 ValueError 分支：绑定失联而规范路径上是别集剧本时，转换在写盘前被拒，
            # 既不重建那一集的剧本也不改本集的绑定，提示要指向可操作的那一处。
            raise ScriptReviewError("foreign_formal_script", str(exc), script_filename=exc.filename) from exc
        except NewAssetsError as exc:
            raise ScriptReviewError("invalid_new_assets", str(exc)) from exc
        except UnregisteredReferencesError as exc:
            raise ScriptReviewError("unregistered_references", str(exc)) from exc
        except UnitDurationOffTier as exc:
            # Before the ValueError branch below: a confirmed duration was narrowed out; give the current duration and
            # selectable options by the unit's position in the script plan.
            # 先于下面的 ValueError 分支：已确认时长被收窄出局，按脚本规划的单元位置给出现时长与可选档位。
            params = {
                "position": exc.position,
                "duration_seconds": exc.duration_seconds,
                "tiers": " / ".join(str(tier) for tier in exc.tiers),
            }
            raise ScriptReviewError("duration_off_tier", str(exc), params=params) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise ScriptReviewError("conversion_refused", str(exc)) from exc

    def _confirm_sync(
        self,
        project_name: str,
        project: dict[str, Any],
        episode: int,
        supported_durations: list[int] | None,
    ) -> str:
        """Synchronous first half of ``confirm``: pending draft check, read-time normalization, structure validation;
        returns the fingerprint of the content to confirm.

        ``confirm`` 的同步前半：待处置草稿校验、读时收编、结构校验，返回待确认内容的指纹。"""
        project_path = self.pm.get_project_path(project_name)
        path = script_review.script_plan_path(project_path, project, episode)
        if path is None:
            raise ScriptReviewError("not_applicable")
        project = self._require_episode(project_name, project, episode)
        # Reject confirmation while a draft exists: the final script_plan is still the previous version (or does not
        # exist), so confirming it would approve on the user's behalf content they have not seen, while the
        # just-produced violating body is still in the draft waiting for the Agent to handle it. The validation uses
        # the same ruler as the generation side: the promotion tool uses exactly the validators from production; here
        # we only check whether a pending draft exists.
        # 草稿在场时拒绝确认：正式 script_plan 此刻仍是上一版（或不存在），确认它等于替用户
        # 认可一份他没看过的内容，而刚产出的那份违约正文还在草稿里等 Agent 处置。校验
        # 口径与生成侧同一把尺——晋升工具用的正是产出时那套校验器，这里只判待处置草稿是否在场。
        quarantine = script_review.script_plan_quarantine_path(project_path, project, episode)
        if quarantine is not None and quarantine.exists():
            raise ScriptReviewError("quarantined", f"script_plan has a pending draft: {quarantine}")
        # Existing drafts get duration normalization before validation: the Agent / direct calls may confirm without
        # going through get_state. The same per-path lock covers the whole read-modify-write (including the
        # reference_video branch's own write below), so writes from save_content or migration cannot cut in midway;
        # _read_script_plan_migrated requires the caller to hold the lock.
        # 存量草稿先做时长收编再校验：Agent / 直连调用可能不经 get_state 就确认。同一把
        # per-path 锁覆盖读改写全程（含下方 reference_video 分支自己的落盘），避免中途被
        # save_content 或迁移的写入插队——_read_script_plan_migrated 要求调用方已持锁。
        kind, model = self._resolve_script_plan_model(project, episode)
        prompt_authoring_lock = (
            self.pm.file_lock(quarantine_path(project_path, episode, QUARANTINE_KIND_PROMPT_AUTHORING))
            if kind == "reference_video"
            else nullcontext()
        )
        with prompt_authoring_lock, self.pm.file_lock(path):
            content, project = self._read_script_plan_migrated(
                project_name, project, episode, path, project_path, supported_durations
            )
            if content is None and not path.exists():
                raise ScriptReviewError("no_script_plan")
            # Before confirming, validate the script_plan structure with the script_plan variant model: content of
            # None (invalid JSON / not an object) is likewise rejected here by model_validate as invalid_content, and
            # is not let through just because "the file exists".
            # 确认前按 script_plan 变体模型校验 script_plan 结构：content 为 None（非法 JSON / 非对象）同样会
            # 在此被 model_validate 拒绝为 invalid_content，不会被仅凭「文件存在」放行。
            try:
                validated = model.model_validate(content)
            except ValidationError as exc:
                raise ScriptReviewError("invalid_content", str(exc)) from exc
            try:
                resolve_new_assets(project, validated.model_dump().get(NEW_ASSETS_FIELD))
            except NewAssetsError as exc:
                raise ScriptReviewError("invalid_new_assets", str(exc)) from exc

            marked_shape = {
                "drama": ("scenes", "scenes"),
                "reference_video": ("units", "video_units"),
            }.get(kind)
            if marked_shape is not None:
                root, skeleton = marked_shape
                for unit in validated.model_dump()[root]:
                    if unit.get("needs_replan") is not True:
                        continue
                    admission = admit_script_unit(skeleton, unit)
                    raise ScriptReviewError("speech_admission", admission=admission)

            if kind == "reference_video":
                # The fingerprint is computed directly from the object just written, so the confirm record matches the
                # content actually written.
                # 指纹按刚写盘的这份对象直接算，确认记录与实际写入内容一致。
                dumped = validated.model_dump()
                for unit in dumped["units"]:
                    admission = admit_script_unit("video_units", unit)
                    if not admission.allowed:
                        raise ScriptReviewError("speech_admission", admission=admission)
                # Single write exit (already holding the same per-path lock, so no script_plan_write_lock around it);
                # the read-modify-write within the same critical section has no concurrency window, so no baseline
                # comparison. When the content really changed, the base of the prompt_authoring draft becomes invalid,
                # and the exit cleans it up according to the change.
                # 单一写盘出口（已持同一把 per-path 锁，不再套 script_plan_write_lock）；同临界区
                # 读改写无并发窗口，不做基线比对。内容真的变了时，prompt_authoring 草稿的基底随之
                # 失效，由出口按变更清理。
                script_review.write_script_plan_locked(project_path, episode, dumped)
                fingerprint = script_review.content_fingerprint_of_data(dumped)
            else:
                # The fingerprint is derived from the validated content without a second disk read: the confirm record
                # must correspond to the content validated here.
                # 指纹从校验通过的 content 派生，不二次读盘：确认记录须对应这里校验过的内容。
                fingerprint = script_review.content_fingerprint_of_data(content)
        return fingerprint


def _overwrite_dict(project_path: Path, project: Mapping[str, Any], episode: int) -> dict[str, Any] | None:
    overwrite = script_review.formal_script_overwrite(project_path, project, episode)
    return overwrite.to_dict() if overwrite is not None else None


def _unreadable_violation(message: str) -> dict[str, Any]:
    """The episode-level violation for a draft that cannot be recomputed; the router localizes it with the
    ``quarantine_unreadable`` text key.

    草稿无从重算时的那条整集层面违约；router 按 ``quarantine_unreadable`` 的文案 key 本地化。"""
    return {"code": "quarantine_unreadable", "label": "", "message": message, "line": None}


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read and parse a structured script_plan file; returns None when missing / invalid JSON / not an object (state is
    otherwise covered by fingerprint derivation).

    Tolerant reading reuses ``lib.infra.json_io.load_json_or_none`` (OSError / JSON / encoding errors become None),
    the same convention as the project's other JSON reads; this function adds a "top level must be an object" guard,
    and non-objects likewise return None.

    读取并解析结构化 script_plan 文件；缺失 / 非法 JSON / 非对象时返回 None（状态另由指纹派生兜底）。

    容错读取复用 ``lib.infra.json_io.load_json_or_none``（OSError / JSON / 编码错误归 None），与项目
    其余 JSON 读取同口径；本函数再叠加「顶层须为对象」守卫，非对象同样返回 None。
    """
    data = load_json_or_none(path)
    return data if isinstance(data, dict) else None
