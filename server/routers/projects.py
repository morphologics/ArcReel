"""
Project management routes

Handles project CRUD operations, reusing lib/project/project_manager.py

Most handlers in this module fall back to 500 with ``except Exception``. Domain exceptions (``ApiError`` and its
subclasses) can be raised inside the disk-writing closures covered by that fallback (such as backend field
validation and script structure validation), so each handler's pass-through clause is written as ``except
(HTTPException, ApiError)`` -- listing only ``HTTPException`` would silently downgrade these 4xx to 500.

项目管理路由

处理项目的 CRUD 操作，复用 lib/project/project_manager.py

本模块多数处理器以 ``except Exception`` 兜底为 500。领域异常（``ApiError`` 及其子类）
可以在被兜底覆盖的写盘闭包内抛出（如 backend 字段校验、脚本结构校验），因此各处理器的
透传子句写成 ``except (HTTPException, ApiError)``——只列 ``HTTPException`` 会把这些
4xx 静默降级成 500。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

# fastapi only re-exports the plural BackgroundTasks; the singular version has to come from starlette.
# fastapi 只 re-export 复数的 BackgroundTasks，单数版本只能取 starlette。
from starlette.background import BackgroundTask

logger = logging.getLogger(__name__)

from lib.agent.agent_session_store import default_sdk_config_dir
from lib.agent.profile_manifest import ContentMode
from lib.backends.text_backends.base import TextOutputTruncatedError
from lib.catalog.errors import ModelNotInCatalogError
from lib.config.resolver import (
    ConfigResolver,
    VideoBucketCapabilityError,
    caps_generation_mode,
    video_bucket_for_generation_mode,
)
from lib.db import async_session_factory
from lib.episode.episode_ledger import is_derived_episode_name
from lib.episode.episode_source_commands import (
    EpisodeSourceError,
    register_whole_source_file,
    set_episode_source_text,
)
from lib.episode.episode_target_duration import (
    EPISODE_TARGET_DURATION_FIELD,
    MAX_EPISODE_TARGET_DURATION,
    MIN_EPISODE_TARGET_DURATION,
    is_valid_episode_target_duration,
)
from lib.episode.source_kinds import SourceKind
from lib.generation.task_failure import FAILURE_CODE_KEYS
from lib.generation.video_request_facts import ResolutionOverride, VideoRequestFactsError, planning_durations
from lib.i18n import render_generation_input_error, render_message
from lib.infra.api_errors import ApiError, BadRequestError, ConflictError, NotFoundError, UnprocessableError
from lib.infra.async_thread import (
    EventLoopBridge,
    run_noninterruptible_async,
    run_noninterruptible_sync,
    run_sync_transaction,
)
from lib.infra.data_root_layout import DataRootLayout
from lib.infra.json_io import domain_error_on_value_error
from lib.project import deleted_projects
from lib.project.asset_fingerprints import compute_asset_fingerprints
from lib.project.asset_types import asset_name_comparison_key
from lib.project.project_change_hints import project_change_source
from lib.project.project_manager import EmptySourceError, EpisodeScriptReboundError, get_project_manager
from lib.prompts.style_templates import is_known_template, resolve_template_prompt
from lib.script.blank_script import BlankScriptError, start_blank_script
from lib.script.script_batch_edit import ScriptBatchEditCommand, ScriptBatchEditor, blank_item_after, script_revision
from lib.script.script_editor import resolve_items
from lib.script.script_references import annotate_derivative_references
from lib.speech.character_voice import PROJECT_FIELD as CHARACTER_VOICE_BINDING_FIELD
from lib.speech.character_voice import VALID_CHARACTER_VOICE_BINDINGS
from lib.speech.narration_config import (
    NARRATION_DELIVERY_FIELD,
    POST_PRODUCTION,
    TTS_BACKEND_FIELD,
    TTS_SPEED_FIELD,
    TTS_VOICE_FIELD,
    NarrationConfigError,
    NarrationDelivery,
    validate_project_narration_config,
)
from lib.speech.speech_rate import MAX_SPEECH_RATE_UPS, MIN_SPEECH_RATE_UPS, SPEECH_RATE_FIELD, is_valid_speech_rate
from lib.workflow.workflow_plan import WorkflowPlan, WorkflowPlanRequest
from lib.workflow.workflow_state import (
    EpisodeNextStep,
    ProjectSummary,
    WorkflowRequestError,
    WorkflowStateService,
    WorkflowStatus,
)
from server.auth import CurrentUser, create_download_token, reject_embedded_agent, verify_download_token
from server.dependencies import require_project_migration_ok
from server.error_handlers import code_or_generic, machine_params
from server.i18n import DisplayNamesCatalog, Locale, Translator
from server.routers._coalesce import RequestCoalescer
from server.routers._episode_source_errors import episode_source_http_error
from server.routers._script_edits import (
    execute_current_script_edit,
    require_script_edit_result,
    script_batch_status,
)
from server.routers._validators import split_video_backend_query, validate_backend_value
from server.services.admission.prompt_preview import ScriptItemNotFound, preview_item_prompts
from server.services.project import workflow_planner as workflow_plan_service
from server.services.project.episode_display import present_episode_diagnostics
from server.services.project.episode_id_records import recorded_episode_ids_on
from server.services.project.narration_settings import (
    NarrationSettingsInput,
    new_project_narration_fields,
    validate_tts_backend,
    validate_tts_speed,
)
from server.services.project.project_activity import project_last_activity_at
from server.services.project.project_archive import (
    ProjectArchiveService,
    ProjectArchiveValidationError,
)
from server.services.project.project_cover import resolve_project_cover
from server.services.project.project_retirement import restore_retired_project, retire_project_on, retiring_project
from server.services.tasks.video_caps import (
    capability_request_facts,
    duration_constraints_payload,
)
from server.tool_runtime import truncation_problem

router = APIRouter()

# Self-authenticating endpoint: native browser download navigation cannot carry an Authorization header, so the
# endpoint checks a short-lived download token via verify_download_token, and no Bearer dependency is attached at
# registration.
# 自带认证端点：浏览器原生下载导航带不了 Authorization header，
# 端点内 verify_download_token 校验短时效下载 token，注册时不挂 Bearer 依赖。
self_auth_router = APIRouter()

_project_detail_coalescer = RequestCoalescer()


def get_project_detail_coalescer() -> RequestCoalescer:
    return _project_detail_coalescer


def get_workflow_state_service() -> WorkflowStateService:
    return WorkflowStateService(get_project_manager())


WorkflowStateServiceDep = Annotated[WorkflowStateService, Depends(get_workflow_state_service)]


def _project_status_payload(
    summary: ProjectSummary, project: dict[str, Any], translate: Callable[..., str]
) -> dict[str, Any]:
    """Project-level status payload: the project summary minus per-episode details.

    Both list and detail ``status`` give only project granularity -- repair flags, asset counts, episode totals. The
    per-episode details in the summary stay in the service layer, so a list of N projects does not carry N×episodes
    objects; episode-level consumers fetch them through the episode endpoints.

    项目级状态负载：项目摘要去掉每集明细。

    列表与详情的 ``status`` 都只给项目粒度——修复标记、资产计数、分集汇总。摘要里的
    每集明细留在服务层，不让 N 个项目的列表驮上 N×集数 的对象；剧集粒度的消费方另经
    剧集接口取。
    """

    return present_episode_diagnostics(summary.model_dump(mode="json", exclude={"episodes"}), project, translate)


def _merge_episode_summaries(project: dict[str, Any], summary: ProjectSummary) -> dict[str, Any]:
    """Merge the project summary's per-episode details into ``project["episodes"]`` (computed on read, not written to
    disk).

    Each episode's script progress, artifact counts and duration have only one source, the project summary, whose
    convention is the artifact manifest: usable = current ∪ stale, with stale counted separately. So the numbers
    read by episode cards, episode headers and the canvas share their source with the workbench. Fields on the
    project.json side (title / script_file / hook / outline...) are kept as is.

    把项目摘要的每集明细并进 ``project["episodes"]``（读时计算，不写盘）。

    每集的脚本进度、产物计数与时长只有项目摘要一个来源，口径是产物清单：可用 = current ∪
    stale，stale 另计。剧集卡、剧集头与画布读到的数字因此与工作台同源。project.json 侧的
    字段（title / script_file / hook / outline……）原样保留。
    """

    per_episode = {item.episode: item for item in summary.episodes}
    episodes = []
    for entry in project.get("episodes", []):
        if not isinstance(entry, dict):
            continue
        merged = dict(entry)
        number = entry.get("episode")
        item = per_episode.get(number) if isinstance(number, int) else None
        if item is not None:
            merged.update(item.model_dump(mode="json", exclude={"episode"}))
        episodes.append(merged)
    project["episodes"] = episodes
    return project


async def get_archive_service() -> ProjectArchiveService:
    # Constructed on the event loop: the overwrite import runs in a worker thread and closes out the existing
    # project's records via the captured event loop.
    # 在事件循环上构造：覆盖导入跑在工作线程里，经捕获的事件循环收尾现有项目的记录。
    return ProjectArchiveService(
        get_project_manager(),
        retire_project=retire_project_on(EventLoopBridge.capture(), async_session_factory),
    )


ArchiveServiceDep = Annotated[ProjectArchiveService, Depends(get_archive_service)]


def get_script_batch_editor(manager: Any | None = None) -> ScriptBatchEditor:
    return ScriptBatchEditor(manager or get_project_manager())


def get_script_batch_editor_factory() -> Callable[[Any], ScriptBatchEditor]:
    """Route dependency of the batch editor; the editor must bind the manager resolved inside the handler, so a factory
    is injected rather than an instance.

    批量编辑器的路由依赖；编辑器要绑处理器内解析出的 manager，故注入工厂而非实例。"""
    return get_script_batch_editor


ScriptBatchEditorFactoryDep = Annotated[Callable[[Any], ScriptBatchEditor], Depends(get_script_batch_editor_factory)]


# Project-level model fields: validated one by one and written to project.json at creation; PATCH additionally has
# audio_backend. Values look like provider/model or a bare provider; empty = clear that layer and fall back to the
# next.
# 项目级模型字段：创建时逐一校验并写入 project.json，PATCH 时另加 audio_backend。
# 值形如 provider/model 或裸 provider，空值 = 清除该层、回退下一层。
_PROJECT_BACKEND_FIELDS = (
    "video_backend",
    "video_provider_i2v",
    "video_provider_r2v",
    "image_provider_t2i",
    "image_provider_i2i",
    "default_image_backend",
    "text_backend_simple",
    "text_backend_complex",
    "default_text_backend",
)


def _reject_bool_speech_rate(value: object) -> object:
    """A boolean is not a speech rate: Pydantic non-strict mode folds JSON ``true`` into 1.0 and ``false`` into 0.0.

    Both the source of truth and the data validator treat bool as a dirty value; if the write side let it through, a
    stored 1.0 could no longer be recognized as originally boolean, while 0.0 would be skipped as "not filled" --
    two outcomes for the same kind of input. Reject directly before the range check.

    布尔不是语速：Pydantic 非严格模式会把 JSON ``true`` 折成 1.0、``false`` 折成 0.0。

    真相源与数据校验器都把 bool 判为脏值，写入侧若放行，落库后的 1.0 已无从辨认原本是布尔，
    而 0.0 又会被当成「未填」跳过写入——同一类输入两种结局。在进入区间校验前直接拒。
    """
    if isinstance(value, bool):
        raise ValueError("speech rate must be a number, not a boolean")
    return value


#: Field type of the speech rate estimate on create / PATCH requests; both models share the same boolean guard.
#: 创建 / PATCH 请求上的口播语速估算字段类型，两个模型共用同一把布尔守卫。
SpeechRateOverride = Annotated[float | None, BeforeValidator(_reject_bool_speech_rate)]


def _validated_episode_target_duration(value: int, _t: Translator) -> int:
    """Bring the per-episode target duration passed in on create / PATCH into the hard range; out of range gives 422.

    The range uses the same yardstick (``is_valid_episode_target_duration``) as the read-time guard of
    ``lib.episode.episode_target_duration``, the coercion in ``patch_project`` and frontend input validation; no
    separate boundary numbers are written here.

    把创建 / PATCH 传入的单集目标时长收进硬区间，越界即 422。

    区间与 ``lib.episode.episode_target_duration`` 的读时守卫、``patch_project`` 的强制转换、
    前端输入校验同一把尺（``is_valid_episode_target_duration``），不在这里另写边界数字。
    """
    if not is_valid_episode_target_duration(value):
        raise HTTPException(
            status_code=422,
            detail=_t(
                "episode_target_duration_out_of_range",
                min=MIN_EPISODE_TARGET_DURATION,
                max=MAX_EPISODE_TARGET_DURATION,
            ),
        )
    return value


def _validated_speech_rate(value: float, _t: Translator) -> float:
    """Bring the speech rate estimate passed in on create / PATCH into the hard range; out of range gives 422.

    The range uses the same yardstick (``is_valid_speech_rate``) as the read-time guard of
    ``lib.speech.speech_rate`` and frontend input validation; no separate boundary numbers are written here.

    把创建 / PATCH 传入的口播语速估算收进硬区间，越界即 422。

    区间与 ``lib.speech.speech_rate`` 的读时守卫、前端输入校验同一把尺（``is_valid_speech_rate``），
    不在这里另写边界数字。
    """
    rate = float(value)
    if not is_valid_speech_rate(rate):
        raise HTTPException(
            status_code=422, detail=_t("speech_rate_out_of_range", min=MIN_SPEECH_RATE_UPS, max=MAX_SPEECH_RATE_UPS)
        )
    return rate


class CreateProjectRequest(BaseModel):
    name: str | None = None
    title: str | None = None
    style: str | None = ""  # Kept but no longer a user entry point / 保留但不再是用户入口
    content_mode: ContentMode | None = "narration"
    aspect_ratio: str | None = "9:16"
    default_duration: int | None = None
    # Per-episode target duration (seconds): optional soft preference for non-ad projects; range check in
    # _validated_episode_target_duration.
    # 单集目标时长（秒）：可选软偏好，非 ad 项目适用；区间校验在 _validated_episode_target_duration。
    episode_target_duration: int | None = None
    # content_mode=ad only: target total duration (seconds). The UI offers four options (15/30/60/90, default 60); the
    # data layer does not hard-enumerate, any positive integer is valid.
    # 仅 content_mode=ad：目标总时长（秒）。UI 给四档（15/30/60/90，默认 60），
    # 数据层不硬枚举，任意正整数合法。
    target_duration: int | None = Field(default=None, gt=0)
    # content_mode=ad only: short brief text (may be empty, does not go through source_loader)
    # 仅 content_mode=ad：创作诉求短文本（可空，不走 source_loader）
    brief: str | None = None
    # Generation mode: storyboard or reference_video must be chosen explicitly at creation; missing or old grid values
    # get 422 from Pydantic validation. Cannot be changed after creation (the PATCH model has no such field
    # structurally).
    # 生成模式：创建时必须显式选择 storyboard 或 reference_video；缺失或旧 grid 值由
    # Pydantic 校验返回 422。创建后不可更改（PATCH 模型结构上无此字段）。
    generation_mode: Literal["storyboard", "reference_video"]
    # Grid storyboard switch: only changes how storyboard images are produced, not a separate generation mode; only
    # meaningful in storyboard generation mode, and can be toggled at any time after creation via project PATCH. Ad
    # projects refuse to enable it.
    # 宫格分镜开关：只改变分镜图的生产方式，不是独立生成模式；仅 storyboard 生成模式有意义，
    # 创建后可经项目 PATCH 随时切换。ad 项目拒绝开启。
    grid_storyboard: bool = False
    # Project-level override of the speech rate estimate (reading units / second): empty = fall back to the language
    # default of lib.speech.speech_rate. Unrelated to TTS narration_speed (provider voiceover multiplier); the two are
    # not linked.
    # 口播语速估算（阅读单位 / 秒）项目级覆盖：空 = 回退 lib.speech.speech_rate 的语言默认。
    # 与 TTS 的 narration_speed（供应商配音倍率）无关，两者不联动。
    speech_rate_units_per_second: SpeechRateOverride = None
    style_template_id: str | None = None
    video_backend: str | None = None
    # Project-level override of video task type buckets (docs/adr/0054): i2v = image-to-video / grid, r2v =
    # reference-to-video; empty = fall back to the project default (video_backend) and the global layer
    # 视频任务类型桶（docs/adr/0054）项目级覆盖：i2v = 图生视频 / 宫格，r2v = 参考生视频；
    # 空值 = 回退项目默认（video_backend）与全局层
    video_provider_i2v: str | None = None
    video_provider_r2v: str | None = None
    # Project-level override of image task type buckets (docs/adr/0054) + project default model: t2i = text-to-image,
    # i2i = image-to-image; empty bucket = fall back to the project default (default_image_backend) and the global
    # layer
    # 图片任务类型桶（docs/adr/0054）项目级覆盖 + 项目默认模型：t2i = 文生图，i2i = 图生图；
    # 桶为空 = 回退项目默认（default_image_backend）与全局层
    image_provider_t2i: str | None = None
    image_provider_i2i: str | None = None
    default_image_backend: str | None = None
    # Project-level override of text task tiers (docs/adr/0051) + project default model; empty = inherit global
    # 文本任务档位（docs/adr/0051）项目级覆盖 + 项目默认模型；空值 = 继承全局
    text_backend_simple: str | None = None
    text_backend_complex: str | None = None
    default_text_backend: str | None = None
    model_settings: dict[str, dict[str, str | None]] | None = None
    # Narration delivery (docs/adr/0089): required project setting, defaults to post-production voiceover. When
    # choosing TTS voiceover, an omitted model, voice and voiceover speed are prefilled with the global defaults; once
    # written they become the project snapshot and no longer inherit the global defaults. An explicit null speed = do
    # not send a speed.
    # 旁白交付方式（docs/adr/0089）：必填的项目配置，缺省后期配音。选 TTS 配音时省略的模型、音色、
    # 配音语速以全局默认预填；写入后成为项目快照，不再继承全局默认。显式 null 语速 = 不传语速。
    narration_delivery: NarrationDelivery = POST_PRODUCTION
    audio_backend: str | None = None
    narration_voice: str | None = None
    narration_speed: float | None = None


class EpisodePatch(BaseModel):
    """单集更新请求体。仅包含可写字段；未声明字段会被忽略。"""

    model_config = ConfigDict(extra="ignore")
    episode: int
    script_file: str | None = None


class UpdateProjectRequest(BaseModel):
    title: str | None = None
    style: str | None = None
    aspect_ratio: str | None = None
    default_duration: int | None = None
    # Per-episode target duration (seconds): explicit null clears the preference; ad projects reject the field's mere
    # presence
    # 单集目标时长（秒）：显式 null 清除该偏好；ad 项目对字段出现本身即拒绝
    episode_target_duration: int | None = None
    # Ad projects only: target total duration (seconds), any positive integer is valid, cannot be cleared / 仅 ad 项目：目标总时长（秒），任意正整数合法，不可清空
    target_duration: int | None = Field(default=None, gt=0)
    # Ad projects only: short brief text; explicit null clears it to an empty string / 仅 ad 项目：创作诉求短文本；显式 null 清为空字符串
    brief: str | None = None
    # The generation mode is fixed at creation and immutable; PATCH has no generation_mode field structurally; the
    # grid switch can be toggled any time
    # 生成模式创建即定、不可变，PATCH 结构上无 generation_mode 字段；宫格开关随时可切
    grid_storyboard: bool | None = None
    video_backend: str | None = None
    video_provider_i2v: str | None = None
    video_provider_r2v: str | None = None
    image_provider_t2i: str | None = None
    image_provider_i2i: str | None = None
    default_image_backend: str | None = None
    video_generate_audio: bool | None = None
    # How character voices are bound: prompt (default, voice_style prompt soft constraint) / reference_audio (attach
    # character reference audio)
    # 角色声音绑定方式：prompt（默认，voice_style 提示词软约束）/ reference_audio（挂角色参考音频）
    character_voice_binding: str | None = None
    # Narration delivery (docs/adr/0089): can be changed any time without making any existing artifact stale; cannot
    # be cleared
    # 旁白交付方式（docs/adr/0089）：随时可改，不让任何已有产物过期；不可清空
    narration_delivery: NarrationDelivery | None = None
    # Narration audio (TTS) snapshot: model (provider/model) / voice / voiceover speed, not inheriting the global
    # defaults. The model and voice of a TTS voiceover project cannot be cleared; speed null = do not send a speed to
    # the provider
    # 旁白配音（TTS）快照：模型（provider/model）/ 音色 / 配音语速，不继承全局默认。
    # TTS 配音项目的模型与音色不可清空；语速 null = 不向供应商传语速
    audio_backend: str | None = None
    narration_voice: str | None = None
    narration_speed: float | None = None
    # Project-level override of the speech rate estimate (reading units / second); null = clear and fall back to the
    # language default
    # 口播语速估算（阅读单位 / 秒）项目级覆盖；null = 清除、回退语言默认
    speech_rate_units_per_second: SpeechRateOverride = None
    # Project-level override of text task tiers (docs/adr/0051) + project default model; empty = clear and inherit
    # global
    # 文本任务档位（docs/adr/0051）项目级覆盖 + 项目默认模型；空值 = 清除、继承全局
    text_backend_simple: str | None = None
    text_backend_complex: str | None = None
    default_text_backend: str | None = None
    style_template_id: str | None = None
    clear_style_image: bool | None = None
    episodes: list[EpisodePatch] | None = None
    model_settings: dict[str, dict[str, str | None]] | None = None


def _apply_narration_patch(project: dict, req: UpdateProjectRequest) -> None:
    """Write the narration delivery and TTS snapshot; after the change a TTS voiceover project must still carry a
    complete snapshot, otherwise the whole PATCH gives 422.

    The delivery method is not part of the artifacts' generation basis: switching it does not make existing
    artifacts stale, and the TTS snapshot is kept when changing to post-production voiceover.

    写入旁白交付方式与 TTS 快照；改完后 TTS 配音项目仍须带完整快照，否则整次 PATCH 422。

    交付方式不进产物的生成依据：切换交付方式不让已有产物过期，改为后期配音时 TTS 快照保留。
    """

    fields = req.model_fields_set
    try:
        if "narration_delivery" in fields:
            if req.narration_delivery is None:
                raise NarrationConfigError("narration_delivery_required")
            project[NARRATION_DELIVERY_FIELD] = req.narration_delivery
        if TTS_BACKEND_FIELD in fields:
            if req.audio_backend:
                validate_backend_value(req.audio_backend, TTS_BACKEND_FIELD)
                project[TTS_BACKEND_FIELD] = validate_tts_backend(req.audio_backend)
            else:
                project.pop(TTS_BACKEND_FIELD, None)
        # The voice is a string id filled in per the provider docs; empty string = clear / 音色是照供应商文档填的字符串 id；空串 = 清除
        if TTS_VOICE_FIELD in fields:
            voice = (req.narration_voice or "").strip()
            if voice:
                project[TTS_VOICE_FIELD] = voice
            else:
                project.pop(TTS_VOICE_FIELD, None)
        # Voiceover speed only gets a positive-finite sanity check; the value range is constrained by each provider
        # 配音语速只做正有限数卫生校验，取值范围由各供应商约束
        if TTS_SPEED_FIELD in fields:
            if req.narration_speed is None:
                project.pop(TTS_SPEED_FIELD, None)
            else:
                project[TTS_SPEED_FIELD] = validate_tts_speed(req.narration_speed)
        validate_project_narration_config(project)
    except NarrationConfigError as exc:
        raise UnprocessableError(exc.code) from exc


def _cleanup_temp_file(path: str) -> None:
    try:
        os.unlink(path)  # hard-delete: temp -- temp file of archive import/export / 归档导入导出的临时文件
    except FileNotFoundError:
        return


@router.post("/projects/import")
async def import_project_archive(
    _t: Translator,
    archive_service: ArchiveServiceDep,
    file: UploadFile = File(...),
    conflict_policy: str = Form("prompt"),
    confirm_report: bool = Form(False),
):
    """从 ZIP 导入项目。

    导入包升级时有调整或没能带入的内容时，未带 ``confirm_report`` 的请求以 409 返回
    ``confirmation_required`` 与 ``migration_report``，不留下任何项目数据；用户确认后带上它重新提交。
    """
    # Register the path as soon as the temp file is created, so finally can still clean up on cancel or failure
    # 临时文件一建好就登记路径，取消或失败时 finally 仍能清理
    upload_paths: list[str] = []
    try:
        # Use the underlying SpooledTemporaryFile's synchronous handle, offloading temp file creation and the whole
        # write loop to a thread, to avoid blocking the event loop with mixed async reads + sync writes
        # 使用底层 SpooledTemporaryFile 的同步句柄，建临时文件与整个写循环 offload 到线程，
        # 避免 async 读取 + 同步写入的混合模式阻塞事件循环
        raw_file = file.file

        def _write_upload() -> str:
            fd, path = tempfile.mkstemp(prefix="arcreel-upload-", suffix=".zip")
            upload_paths.append(path)
            with os.fdopen(fd, "wb") as target:
                while True:
                    chunk = raw_file.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
            return path

        upload_path = await run_sync_transaction(_write_upload)

        def _sync():
            return archive_service.import_project_archive(
                Path(upload_path),
                uploaded_filename=file.filename,
                conflict_policy=conflict_policy,
                confirm_report=confirm_report,
                translate=_t,
            )

        result = await run_sync_transaction(_sync)
        return {
            "success": True,
            "project_name": result.project_name,
            "project": result.project,
            "warnings": [warning.render(_t) for warning in result.warnings],
            "conflict_resolution": result.conflict_resolution,
            "diagnostics": result.diagnostics,
        }
    except ProjectArchiveValidationError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.detail.render(_t),
                # YouReel (G4): machine-readable code + params, as every error body
                # YouReel（G4）：与所有错误体一致的机器可读 code + params
                "code": code_or_generic(exc.detail.key, exc.status_code),
                "params": machine_params(None, exc.detail.params),
                "errors": exc.render_errors(_t),
                "warnings": exc.render_warnings(_t),
                "diagnostics": exc.diagnostics_payload(_t),
                **exc.extra,
            },
        )
    except Exception:
        logger.exception("请求处理失败")
        return JSONResponse(
            status_code=500,
            content={
                "detail": _t("internal_server_error"),
                "code": "internal_server_error",
                "params": {},
                "errors": [],
                "warnings": [],
            },
        )
    finally:
        await file.close()
        for path in upload_paths:
            await run_noninterruptible_sync(_cleanup_temp_file, path)


@router.post("/projects/{name}/export/token")
async def create_export_token(
    name: str,
    current_user: CurrentUser,
    _t: Translator,
    archive_service: ArchiveServiceDep,
    scope: str = Query("full"),
):
    """签发短时效下载 token，用于浏览器原生下载认证。"""
    try:
        if scope not in ("full", "current"):
            raise HTTPException(status_code=422, detail=_t("scope_invalid"))

        def _sync():
            if not get_project_manager().project_exists(name):
                raise HTTPException(status_code=404, detail=_t("project_not_found", name=name))
            return archive_service.get_export_diagnostics(name, scope=scope, translate=_t)

        diagnostics = await asyncio.to_thread(_sync)
        username = current_user.sub
        download_token = create_download_token(username, name)
        return {
            "download_token": download_token,
            "expires_in": 300,
            "diagnostics": diagnostics,
        }
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@self_auth_router.get("/projects/{name}/export")
async def export_project_archive(
    name: str,
    _t: Translator,
    archive_service: ArchiveServiceDep,
    download_token: str = Query(...),
    scope: str = Query("full"),
):
    """将项目导出为 ZIP。需要 download_token 认证（通过 POST /export/token 获取）。"""
    if scope not in ("full", "current"):
        raise HTTPException(status_code=422, detail=_t("scope_invalid"))

    # Verify download_token / 验证 download_token
    import jwt as pyjwt

    try:
        verify_download_token(download_token, name)
    except pyjwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail=_t("download_expired")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=_t("download_token_mismatch")) from exc
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=_t("download_token_invalid")) from exc

    # Register the zip as soon as it hits disk: if cancelled mid-packing the return value is lost, and finally
    # reclaims it from the registration; once handed to FileResponse, the response's background task reclaims it.
    # zip 一落盘就登记：打包进行中被取消时返回值随之丢弃，finally 按登记回收；
    # 交给 FileResponse 之后由响应的后台任务回收。
    archive_paths: list[Path] = []

    def _export() -> tuple[Path, str]:
        archive_path, download_name = archive_service.export_project(name, scope=scope)
        archive_paths.append(archive_path)
        return archive_path, download_name

    handed_off = False
    try:
        archive_path, download_name = await run_sync_transaction(_export)
        response = FileResponse(
            archive_path,
            media_type="application/zip",
            filename=download_name,
            background=BackgroundTask(_cleanup_temp_file, str(archive_path)),
        )
        handed_off = True
        return response
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc
    finally:
        if not handed_off:
            for path in archive_paths:
                await run_noninterruptible_sync(_cleanup_temp_file, str(path))


_NO_ACTIVITY = datetime.min.replace(tzinfo=UTC)


@router.get("/projects")
async def list_projects(summaries: WorkflowStateServiceDep, _t: Translator):
    """列出所有项目，最近有活动的在前。"""

    def _sync():
        manager = get_project_manager()
        projects = []
        last_activity: dict[str, datetime] = {}
        for name in manager.list_projects():
            try:
                # Projects deleted after listing are no longer listed / 列举之后被删除的项目不再列出
                if manager.project_exists(name):
                    project = manager.load_project(name)
                    # Preload each episode's script once and feed it to both downstreams, cover + status, removing
                    # duplicate JSON I/O. The key is the raw episode['script_file'] value (matching what
                    # resolve_project_cover / the project summary projection expect as key). A load failure in any
                    # episode does not affect the list: it is just skipped from the map, and downstream consumers
                    # naturally fall back via the "missing" path.
                    # 一次性预加载每集剧本，喂给 cover + status 两路下游，去除重复 JSON I/O。
                    # key 为 episode['script_file'] 原值（match resolve_project_cover /
                    # 项目摘要投影对 key 的期望）。任何一集加载失败都不影响列表：
                    # 仅跳过入 map，下游消费者自然按"缺失"路径兜底。
                    preloaded_scripts: dict[str, dict] = {}
                    for ep in project.get("episodes") or []:
                        script_file = ep.get("script_file")
                        if not script_file:
                            continue
                        try:
                            preloaded_scripts[script_file] = manager.load_script(name, script_file)
                        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as load_err:
                            # Aligned with resolve_project_cover / the project summary projection: I/O missing +
                            # JSON/schema parse failure → skip this episode and keep preloading the others; unexpected
                            # exceptions (RuntimeError/MemoryError etc.) bubble up to the outer try and take the basic
                            # info fallback row.
                            # 与 resolve_project_cover / 项目摘要投影对齐：I/O 缺失 +
                            # JSON/schema 解析失败 → 跳过此集，继续预加载其他集；
                            # 非预期异常（RuntimeError/MemoryError 等）让其冒泡到外层 try，走 basic info 兜底行。
                            logger.debug(
                                "list_projects 预加载剧本失败 project=%s script=%s err=%s",
                                name,
                                script_file,
                                load_err,
                            )

                    # The cover follows the resolve_project_cover fallback chain: video_thumbnail → storyboard_image →
                    # scene_sheet → character_sheet -- covering both storyboard-to-video (including grid assembly) and
                    # reference-to-video.
                    # 封面走 resolve_project_cover fallback 链：
                    # video_thumbnail → storyboard_image → scene_sheet → character_sheet
                    # —— 同时覆盖分镜图生视频（含宫格装配）与参考生视频。
                    thumbnail = resolve_project_cover(manager, name, project, preloaded_scripts=preloaded_scripts)

                    # Stage and artifact counts all come from the project summary projection (computed on read,
                    # artifact convention from the artifact manifest). The list only looks at manifest registration
                    # and file presence: comparing each item to the canonical state would hash every storyboard image
                    # and reference image of every project, a cost a list of N projects cannot afford.
                    # 阶段与产物计数一律来自项目摘要投影（读时计算，产物口径取产物清单）。
                    # 列表只看清单登记与文件在场：逐件比对规范状态要哈希每个项目的
                    # 全部分镜图与引用图，N 个项目的列表付不起这个代价。
                    status = _project_status_payload(
                        summaries.get_project_summary(
                            name,
                            preloaded_scripts=preloaded_scripts,
                            currency="registered",
                        ),
                        project,
                        _t,
                    )

                    activity = project_last_activity_at(
                        manager.get_project_path(name), project, preloaded_scripts.values()
                    )
                    if activity is not None:
                        last_activity[name] = activity

                    raw_title = project.get("title")
                    projects.append(
                        {
                            "name": name,
                            # When title is missing/None/of an unexpected type it is normalized to an empty string,
                            # and the frontend i18n falls back to showing "Untitled project", so the API contract
                            # always returns str.
                            # title 缺失/为 None/类型异常时统一归一为空串,前端 i18n
                            # 兜底显示「未命名项目」,确保接口契约始终返回 str。
                            "title": raw_title if isinstance(raw_title, str) else "",
                            "style": project.get("style", ""),
                            "style_template_id": project.get("style_template_id"),
                            "style_image": project.get("style_image"),
                            "thumbnail": thumbnail,
                            "status": status,
                            "last_activity_at": activity.isoformat() if activity is not None else None,
                        }
                    )
            except Exception as e:
                # Return basic info on error / 出错时返回基本信息
                logger.warning("加载项目 '%s' 元数据失败: %s", name, e)
                projects.append(
                    {"name": name, "title": "", "style": "", "thumbnail": None, "status": {}, "last_activity_at": None}
                )

        # Projects without activity time go last, keeping their listing order by name among themselves. / 没有活动时间的项目排在最后，彼此保持按名字的列举顺序。
        projects.sort(key=lambda p: last_activity.get(p["name"], _NO_ACTIVITY), reverse=True)
        return {"projects": projects}

    return await run_sync_transaction(_sync)


@router.post("/projects")
async def create_project(
    req: CreateProjectRequest,
    _t: Translator,
    locale: Locale,
):
    """创建新项目"""
    try:
        try:
            narration_fields = await new_project_narration_fields(
                NarrationSettingsInput(
                    delivery=req.narration_delivery,
                    audio_backend=req.audio_backend,
                    narration_voice=req.narration_voice,
                    narration_speed=req.narration_speed,
                    provided=frozenset(req.model_fields_set & {TTS_BACKEND_FIELD, TTS_VOICE_FIELD, TTS_SPEED_FIELD}),
                ),
                resolver=ConfigResolver(async_session_factory),
            )
        except NarrationConfigError as exc:
            raise UnprocessableError(exc.code) from exc

        def _sync():
            manager = get_project_manager()
            title = (req.title or "").strip()
            manual_name = (req.name or "").strip()
            if not title and not manual_name:
                raise HTTPException(status_code=400, detail=_t("title_required"))
            project_name = manual_name or manager.generate_project_name(title)

            style_prompt = req.style or ""
            if req.style_template_id:
                if not is_known_template(req.style_template_id):
                    raise HTTPException(
                        status_code=400,
                        detail=_t("unknown_style_template", template_id=req.style_template_id),
                    )
                style_prompt = resolve_template_prompt(req.style_template_id)

            # Mode-specific fields are mutually exclusive: target_duration/brief are ad only; ad does not expose
            # default_duration and does not allow grid storyboard
            # 模式专属字段互斥：target_duration/brief 仅 ad 可用；
            # ad 不暴露 default_duration、不开放宫格分镜
            content_mode = req.content_mode or "narration"
            if content_mode == "ad":
                if req.default_duration is not None:
                    raise HTTPException(status_code=400, detail=_t("ad_no_default_duration"))
                if req.episode_target_duration is not None:
                    raise HTTPException(status_code=400, detail=_t("ad_no_episode_target_duration"))
                if req.grid_storyboard:
                    raise HTTPException(status_code=400, detail=_t("ad_grid_not_supported"))
            else:
                if req.target_duration is not None:
                    raise HTTPException(status_code=400, detail=_t("ad_only_field", field="target_duration"))
                if req.brief is not None:
                    raise HTTPException(status_code=400, detail=_t("ad_only_field", field="brief"))

            # Symmetric with the update path: validate all backend fields / 与 update 路径对称：校验所有 backend 字段
            for field_name in _PROJECT_BACKEND_FIELDS:
                value = getattr(req, field_name)
                if value:
                    validate_backend_value(value, field_name)

            # Speech rate estimate: optional, not written to disk if not filled (absent means falling back to the
            # language default of lib.speech.speech_rate). Checked before create_project, so an out-of-range request
            # leaves no half-built project directory.
            # 口播语速估算：可选，未填则不落盘（缺省即回退 lib.speech.speech_rate 的语言默认）。
            # 在 create_project 之前判，越界请求不留下半成品项目目录。
            speech_rate = (
                None
                if req.speech_rate_units_per_second is None
                else _validated_speech_rate(req.speech_rate_units_per_second, _t)
            )
            # Per-episode target duration: likewise checked before create_project, so an out-of-range request leaves
            # no half-built project directory.
            # 单集目标时长：同理在 create_project 之前判，越界请求不留下半成品项目目录。
            episode_target_duration = (
                None
                if req.episode_target_duration is None
                else _validated_episode_target_duration(req.episode_target_duration, _t)
            )

            try:
                manager.create_project(project_name, content_mode=req.content_mode or "narration")
            except FileExistsError as exc:
                raise HTTPException(status_code=400, detail=_t("project_exists", name=project_name)) from exc
            extras = {field: value for field in _PROJECT_BACKEND_FIELDS if (value := getattr(req, field))}
            if req.model_settings is not None:
                extras["model_settings"] = req.model_settings
            # Generation mode and grid switch are merged into extras and written in one go, avoiding an extra RMW of
            # load-save after create; both fields always write explicit values (grid_storyboard default false is
            # written too), so a new project is in the complete v5 form
            # 生成模式与宫格开关并入 extras 一次性写入，避免 create 后再 load-save 的额外 RMW；
            # 两字段恒写显式值（grid_storyboard 默认 false 也落盘），新项目即 v5 完整形态
            extras["generation_mode"] = req.generation_mode
            extras["grid_storyboard"] = req.grid_storyboard
            if speech_rate is not None:
                extras[SPEECH_RATE_FIELD] = speech_rate
            # YouReel: a new project starts in its creator's interface language. Without it the overview and the
            # scripts fell back to Chinese ("中文") until a source file was analysed or the member asked for another
            # language (owner 2026-11-17). Generating the overview still replaces it with the source's own language.
            extras["source_language"] = locale
            with project_change_source("webui"):
                project = manager.create_project_metadata(
                    project_name,
                    title or manual_name,
                    style_prompt,
                    req.content_mode,
                    aspect_ratio=req.aspect_ratio,
                    default_duration=req.default_duration,
                    episode_target_duration=episode_target_duration,
                    style_template_id=req.style_template_id,
                    extras=extras or None,
                    target_duration=req.target_duration,
                    brief=req.brief,
                    narration=narration_fields,
                )
            return {"success": True, "name": project_name, "project": project}

        return await run_sync_transaction(_sync)
    except ValueError as e:
        # Config validation failures for project name / duration / brief etc.; str(e) only goes to the log
        # 项目名 / duration / brief 等配置校验失败，str(e) 只进日志
        logger.warning("创建项目参数错误: name=%s (%s)", req.name or req.title, e)
        raise BadRequestError("project_config_invalid") from e
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.get("/projects/{name}/video-capabilities")
async def get_video_capabilities(
    name: str,
    _t: Translator,
    video_backend: Annotated[str | None, Query()] = None,
    resolution: Annotated[str | None, Query()] = None,
    uses_reference_images: Annotated[bool | None, Query()] = None,
):
    """解析当前项目视频模型能力 + 用户项目偏好。

    三级模型选择（项目 > 系统设置 > 系统默认）后，读 model 的 `supported_durations`
    并派生 `max_duration`；同时带回 `project.json.default_duration`（用户偏好）。
    两条生成模式（storyboard/reference_video）都可复用。

    `video_backend`（"provider/model"）用于设置表单里尚未保存的候选模型：不带该参数时按已
    落盘配置解析，带上则按候选模型 × 本项目的生成模式解析，使 voice_consistency 等二维派生值
    对应用户当前选中的模型而非上一次保存的模型；候选身份先过所属桶能力闸。裸 provider（无 "/"）按其 registry
    默认视频 model 补全，与 project.json 存量裸 provider 覆盖同口径（见 `_parse_project_provider`）。

    `resolution` / `uses_reference_images` 是表单里编辑中的未保存值：`uses_reference_images` 决定
    按哪个任务类型桶解析（缺省按项目生成模式），`resolution` 作为「覆盖分辨率」交给该桶的视频请求
    事实求值，响应里的 `duration_constraints` 即这次求值的收窄结果与成因。缺省按项目已保存档位求值
    （工作台）；`resolution` 传空串表示表单里选了「自动」：不回退到已保存档位，按项目未存档位解析
    （自定义供应商仍取模型默认档）。

    能力按项目生成模式定轴、全项目同一口径，故无需集号：生成模式创建即定、之后不可更改。
    """
    resolver = ConfigResolver(async_session_factory)
    resolution_override = None if resolution is None else ResolutionOverride(resolution or None)
    try:
        project = await asyncio.to_thread(get_project_manager().load_project, name)
        generation_type = (
            ("r2v" if uses_reference_images else "i2v")
            if uses_reference_images is not None
            else video_bucket_for_generation_mode(caps_generation_mode(project))
        )
        if video_backend:
            provider_id, model_id = split_video_backend_query(video_backend)
            project = {**project, f"video_provider_{generation_type}": f"{provider_id}/{model_id}"}
            await resolver.resolve_video_backend(project, None, generation_type=generation_type)
            caps = await resolver.video_capabilities_for_model(
                provider_id, model_id, project, generation_type=generation_type
            )
            if (caps["provider_id"], caps["model"]) != (provider_id, model_id):
                raise BadRequestError("video_capability_reference_unavailable", provider=provider_id, model=model_id)
        else:
            caps = await resolver.video_capabilities_for_project(project, generation_type=generation_type)
        request_facts = await capability_request_facts(
            project,
            generation_type=generation_type,
            config_resolver=resolver,
            resolution_override=resolution_override,
        )
        caps["duration_constraints"] = duration_constraints_payload(request_facts)
        # Durations on the content review page are chosen and judged by the script planning options: confirm
        # conversion uses the same planning_durations.
        # 内容确认页的时长按剧本规划档位选与判：确认转换用的是同一份 planning_durations。
        caps["duration_constraints"]["planning"] = planning_durations(request_facts)
        return caps
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except VideoBucketCapabilityError as exc:
        # Errors from the task type bucket resolution gate carry their own errors catalog key and render parameters;
        # turn them into a structured 400 so the user sees the fix guidance, not swallowed by the generic 422 text
        # below (ValueError subclass, must be caught before it)
        # 任务类型桶解析闸的报错自带 errors 目录 key 与渲染参数，转成结构化 400 让用户看到修复指引，
        # 不被下面的通用 422 文案吞掉（ValueError 子类，须先于其捕获）
        raise BadRequestError(exc.code, **exc.params) from exc
    except VideoRequestFactsError as exc:
        # The problem code of the video request facts is the errors catalog key (ValueError subclass, must be caught
        # before it)
        # 视频请求事实的问题码即 errors 目录 key（ValueError 子类，须先于其捕获）
        raise UnprocessableError(exc.code, **exc.params) from exc
    except ModelNotInCatalogError:
        # The referenced model is not in the provider's list: hand to the app-level handler to return a 400 asking to
        # choose the model again (ValueError subclass, must be caught before it)
        # 引用的模型不在供应商清单中：交 app 级 handler 返回要求重新选择模型的 400（ValueError 子类，须先于其捕获）
        raise
    except ValueError as exc:
        # The raw exception text only goes to the log: str(exc) mixes in English technical details, and inserting it
        # into translated text would leak untranslated raw text into en/vi UIs
        # 异常原文只进日志：str(exc) 混英文技术细节，直接插进翻译文案会让 en/vi 界面混入未译原文
        logger.warning("项目 '%s' 视频模型能力解析失败: %s", name, exc)
        raise HTTPException(
            status_code=422,
            detail=_t("video_capabilities_unresolved", name=name),
        ) from exc


@router.get("/projects/{name}/workflow-status", response_model=WorkflowStatus)
async def get_workflow_status(
    name: str,
    _t: Translator,
    episode: Annotated[int | None, Query(ge=1)] = None,
):
    """Return the authenticated, server-authoritative project workflow status."""

    try:
        manager = get_project_manager()
        status = await asyncio.to_thread(WorkflowStateService(manager).get_status, name, episode)
        project = await asyncio.to_thread(manager.load_project, name)
        return WorkflowStatus.model_validate(present_episode_diagnostics(status.model_dump(mode="json"), project, _t))
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except WorkflowRequestError as exc:
        raise BadRequestError("request_invalid") from exc


class EpisodeNextSteps(BaseModel):
    episodes: list[EpisodeNextStep]


@router.get("/projects/{name}/workflow-status/episodes", response_model=EpisodeNextSteps)
async def get_episode_next_steps(name: str, _t: Translator):
    """账本顺序中每一集建议的下一步，供项目层的逐集清单使用。"""

    try:
        manager = get_project_manager()
        steps = await asyncio.to_thread(WorkflowStateService(manager).get_episode_next_steps, name)
        if not steps:
            # When the whole project is unusable there are no per-episode next steps, and the project is not read
            # again.
            # 项目整体不可用时没有逐集下一步，也不再读一次项目。
            return EpisodeNextSteps(episodes=[])
        project = await asyncio.to_thread(manager.load_project, name)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    return EpisodeNextSteps.model_validate(
        present_episode_diagnostics(EpisodeNextSteps(episodes=steps).model_dump(mode="json"), project, _t)
    )


@router.post("/projects/{name}/workflow-plan", response_model=WorkflowPlan)
async def get_workflow_plan(name: str, request: WorkflowPlanRequest, current_user: CurrentUser, _t: Translator):
    """Return the side-effect-free plan for one transient workflow request."""

    try:
        manager = get_project_manager()
        plan = await workflow_plan_service.get_workflow_planner(manager).get_plan(
            name,
            request,
            user_id=current_user.id,
        )
        project = await asyncio.to_thread(manager.load_project, name)
        return WorkflowPlan.model_validate(present_episode_diagnostics(plan.model_dump(mode="json"), project, _t))
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except WorkflowRequestError as exc:
        raise BadRequestError("request_invalid") from exc


@router.get("/projects/{name}")
async def get_project(
    name: str,
    _t: Translator,
    locale: Locale,
    summaries: WorkflowStateServiceDep,
    coalescer: Annotated[RequestCoalescer, Depends(get_project_detail_coalescer)],
):
    """获取项目详情（含实时计算字段）。同一项目、同一语言的并发请求合流计算。"""
    try:

        def _sync():
            manager = get_project_manager()
            if not manager.project_exists(name):
                raise HTTPException(status_code=404, detail=_t("project_not_found", name=name))

            project = manager.load_project(name)

            # Stage, artifact counts and per-episode details all come from the project summary projection (computed on
            # read, not written to JSON)
            # 阶段、产物计数与每集明细一律来自项目摘要投影（读时计算，不写入 JSON）
            summary = summaries.get_project_summary(name)
            project = _merge_episode_summaries(project, summary)
            project["status"] = _project_status_payload(summary, project, _t)

            scripts = {}
            for ep in project.get("episodes", []):
                script_file = ep.get("script_file", "")
                if script_file:
                    try:
                        script = manager.load_script(name, script_file)
                        key = (
                            script_file.replace("scripts/", "", 1)
                            if script_file.startswith("scripts/")
                            else script_file
                        )
                        scripts[key] = script
                    except FileNotFoundError:
                        logger.debug("剧本文件不存在，跳过: %s/%s", name, script_file)

            # A derivative's "referenced" status is returned on read and not written to disk (see docs/adr/0072): the
            # scripts were already all read above, so the decision is just one more pass over the same data.
            # 衍生的「被引用」状态随读取返回、不落盘（见 docs/adr/0072）：脚本已在上面读齐，
            # 判定只是在同一份数据上多扫一遍。
            project = annotate_derivative_references(project, scripts.values())

            # Compute media file fingerprints (for frontend content-addressed caching) / 计算媒体文件指纹（用于前端内容寻址缓存）
            project_path = manager.get_project_path(name)
            fingerprints = compute_asset_fingerprints(project_path)

            return {
                "project": project,
                "scripts": scripts,
                "asset_fingerprints": fingerprints,
            }

        return await coalescer.run((name, locale), lambda: run_sync_transaction(_sync))
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.get("/projects/{name}/agent-profile")
async def get_agent_profile_status(name: str, _t: Translator):
    """Return project-local Agent Profile customizations."""

    def _sync():
        manager = get_project_manager()
        try:
            project_dir = manager.get_project_path(name)
        except ValueError as exc:
            raise BadRequestError("invalid_project_name", name=name) from exc
        return manager.get_agent_profile_status(project_dir)

    try:
        return await asyncio.to_thread(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except ApiError:
        raise
    except Exception as exc:
        logger.exception("读取项目 Agent profile 状态失败: project=%s", name)
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.post("/projects/{name}/agent-profile/reset")
async def reset_agent_profile(name: str, _t: Translator):
    """Destructively restore the project Agent Profile to current built-ins."""

    def _sync():
        manager = get_project_manager()
        try:
            project_dir = manager.get_project_path(name)
        except ValueError as exc:
            raise BadRequestError("invalid_project_name", name=name) from exc
        stats = manager.force_resync_profile(project_dir)
        if stats.get("errors"):
            raise RuntimeError(f"profile reset completed with {stats['errors']} file errors")
        return {"customized": False, "customized_files": []}

    try:
        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except ApiError:
        raise
    except Exception as exc:
        logger.exception("重置项目 Agent profile 失败: project=%s", name)
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.patch("/projects/{name}")
async def update_project(name: str, req: UpdateProjectRequest, _t: Translator):
    """更新项目元数据"""
    try:

        def _sync():
            manager = get_project_manager()

            def _mutate(project: dict) -> None:
                # The whole read-modify-write happens inside a single _project_lock, avoiding lost updates from
                # concurrent PATCH / task write-backs
                # 整段 read-modify-write 在单一 _project_lock 内完成，避免并发 PATCH / 任务回写丢更新
                is_ad = project.get("content_mode") == "ad"
                if req.title is not None:
                    project["title"] = req.title
                if req.style is not None:
                    project["style"] = req.style
                for field in _PROJECT_BACKEND_FIELDS:
                    if field in req.model_fields_set:
                        value = getattr(req, field)
                        if value:
                            validate_backend_value(value, field)
                            project[field] = value
                        else:
                            project.pop(field, None)

                if "video_generate_audio" in req.model_fields_set:
                    if req.video_generate_audio is None:
                        project.pop("video_generate_audio", None)
                    else:
                        project["video_generate_audio"] = req.video_generate_audio
                # How character voices are bound: enum; null / empty string = clear and fall back to the default
                # (prompt soft constraint)
                # 角色声音绑定方式：枚举，null / 空串 = 清除、回落默认（提示词软约束）
                if "character_voice_binding" in req.model_fields_set:
                    binding = (req.character_voice_binding or "").strip()
                    if not binding:
                        project.pop(CHARACTER_VOICE_BINDING_FIELD, None)
                    elif binding in VALID_CHARACTER_VOICE_BINDINGS:
                        project[CHARACTER_VOICE_BINDING_FIELD] = binding
                    else:
                        raise HTTPException(status_code=422, detail=_t("character_voice_binding_invalid"))
                _apply_narration_patch(project, req)
                # Speech rate estimate (reading units / second): loose hard range; null = clear and fall back to the
                # language default
                # 口播语速估算（阅读单位 / 秒）：宽松硬区间，null = 清除、回退语言默认
                if "speech_rate_units_per_second" in req.model_fields_set:
                    if req.speech_rate_units_per_second is None:
                        project.pop(SPEECH_RATE_FIELD, None)
                    else:
                        project[SPEECH_RATE_FIELD] = _validated_speech_rate(req.speech_rate_units_per_second, _t)
                if "aspect_ratio" in req.model_fields_set and req.aspect_ratio is not None:
                    project["aspect_ratio"] = req.aspect_ratio
                if "grid_storyboard" in req.model_fields_set:
                    if is_ad and req.grid_storyboard:
                        raise HTTPException(status_code=400, detail=_t("ad_grid_not_supported"))
                    # null means the same as false: the grid-off state is written to disk as an explicit false, the
                    # same form as the create path
                    # null 与 false 同义：宫格关闭态落盘为显式 false，与创建路径同形态
                    project["grid_storyboard"] = bool(req.grid_storyboard)
                if "default_duration" in req.model_fields_set:
                    # Ad projects reject the field's mere presence (including null): consistent with the create path's
                    # "forbidden field" contract, so null does not take the delete branch and silently return 200
                    # ad 项目对字段出现本身即拒绝（含 null）：与创建路径"禁写字段"契约一致，
                    # 避免 null 走删除分支静默返回 200
                    if is_ad:
                        raise HTTPException(status_code=400, detail=_t("ad_no_default_duration"))
                    if req.default_duration is None:
                        project.pop("default_duration", None)
                    else:
                        project["default_duration"] = req.default_duration
                if "episode_target_duration" in req.model_fields_set:
                    # Same convention as default_duration: ad projects reject the field's mere presence (including
                    # null)
                    # 与 default_duration 同口径：ad 项目对字段出现本身即拒绝（含 null）
                    if is_ad:
                        raise HTTPException(status_code=400, detail=_t("ad_no_episode_target_duration"))
                    if req.episode_target_duration is None:
                        project.pop(EPISODE_TARGET_DURATION_FIELD, None)
                    else:
                        project[EPISODE_TARGET_DURATION_FIELD] = _validated_episode_target_duration(
                            req.episode_target_duration, _t
                        )
                if "target_duration" in req.model_fields_set:
                    if not is_ad:
                        raise HTTPException(status_code=400, detail=_t("ad_only_field", field="target_duration"))
                    if req.target_duration is None:
                        raise HTTPException(status_code=400, detail=_t("ad_target_duration_required"))
                    project["target_duration"] = req.target_duration
                if "brief" in req.model_fields_set:
                    if not is_ad:
                        raise HTTPException(status_code=400, detail=_t("ad_only_field", field="brief"))
                    project["brief"] = req.brief if req.brief is not None else ""

                if "style_template_id" in req.model_fields_set:
                    if req.style_template_id is None:
                        # Deselect the template: also clear the expanded style prompt, so no orphan text is left
                        # 取消模版选择：同时清掉展开的 style prompt，避免遗留孤儿文本
                        project.pop("style_template_id", None)
                        project["style"] = ""
                    else:
                        if not is_known_template(req.style_template_id):
                            raise HTTPException(
                                status_code=400,
                                detail=_t("unknown_style_template", template_id=req.style_template_id),
                            )
                        project["style_template_id"] = req.style_template_id
                        project["style"] = resolve_template_prompt(req.style_template_id)
                        # Strictly mutually exclusive: template or reference image, one or the other / 强互斥:模版与参考图二选一
                        project.pop("style_image", None)
                        project.pop("style_description", None)

                if req.clear_style_image:
                    # Explicitly clear the custom reference image, used by the "cancel style" flow / 显式清除自定义参考图，用于"取消风格"流程
                    project.pop("style_image", None)
                    project.pop("style_description", None)

                if "model_settings" in req.model_fields_set:
                    if req.model_settings is None:
                        project.pop("model_settings", None)
                    else:
                        project["model_settings"] = req.model_settings

                if "episodes" in req.model_fields_set and req.episodes is not None:
                    # Merge episodes: keep the full data of existing episodes and only update fields explicitly
                    # provided in the request. Use model_fields_set (rather than exclude_none) to decide whether a
                    # field appeared explicitly, so passing null can clear the field. Writable fields are defined by
                    # EpisodePatch itself (extra="ignore"): per-episode statistics fields computed on read are not on
                    # the model and cannot get in even if the request carries them. title is not writable either: its
                    # single source of truth is the script's top-level title, synced one way into episodes[].title by
                    # _apply_episode_sync, and written through the dedicated endpoint PATCH /episodes/{episode}.
                    # 合并 episodes：保留现有 episode 的完整数据，仅更新请求中显式提供的字段。
                    # 使用 model_fields_set（而非 exclude_none）判断字段是否显式出现，使得
                    # 传 null 可用于清空对应字段。可写字段由 EpisodePatch 自身界定（extra="ignore"）：
                    # 读时计算的每集统计字段不在模型上，请求里带了也进不来。title 同样不可写：
                    # 它以剧本顶层 title 为唯一真相源，经 _apply_episode_sync 单向同步进
                    # episodes[].title，专用端点 PATCH /episodes/{episode} 写入。
                    existing_list = project.get("episodes", [])
                    patch_map: dict[int, EpisodePatch] = {}
                    for ep in req.episodes:
                        patch_map[ep.episode] = ep  # Duplicate number: the latter overrides the former / 重复编号：后者覆盖前者

                    new_episodes: list[dict] = []
                    for existing_ep in existing_list:
                        ep_num = existing_ep.get("episode")
                        patch = patch_map.pop(ep_num, None)
                        if patch is None:
                            new_episodes.append(existing_ep)
                            continue
                        updated = dict(existing_ep)
                        for field_name in patch.model_fields_set - {"episode"}:
                            value = getattr(patch, field_name)
                            if value is None:
                                updated.pop(field_name, None)
                            else:
                                updated[field_name] = value
                        new_episodes.append(updated)

                    for unknown_ep in patch_map:
                        logger.warning("Skipping patch for unknown episode %s", unknown_ep)

                    project["episodes"] = new_episodes

            with project_change_source("webui"):
                # Field update and the cleanup of Manifest claims affected by episode binding are done inside a single
                # project lock; returns the upgraded fields, no second load_project needed.
                # 单一 project 锁内完成字段更新与 episode 绑定所影响的 Manifest claim 清理；
                # 返回升级后字段，无需二次 load_project。
                project = manager.update_project_reconciling_episode_bindings(name, _mutate)
                return {"success": True, "project": project}

        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.delete("/projects/{name}")
async def delete_project(name: str, _t: Translator):
    """删除项目：整体移入「已删除项目」，保留期内可以恢复"""
    try:
        manager = get_project_manager()
        project_dir = await asyncio.to_thread(manager.get_project_path, name)
        # The closing record and moving the directory away happen in the same transaction: if the move or commit
        # fails, the record is rolled back and the directory moved back. Queued tasks will not start on the moved
        # directory; running tasks finish as usual but give up writing to disk.
        # 收尾记录与移走目录在同一个事务里：移走或提交失败时记录回滚，目录移回原处。
        # 排队任务不会在移走的目录上开跑，执行中的任务照常跑完但放弃落盘。
        tombstone: str | None = None
        try:
            async with retiring_project(async_session_factory, project_dir.name) as tombstone:
                await run_sync_transaction(deleted_projects.move_in, manager.layout, project_dir, tombstone)
        except BaseException:
            if tombstone is not None:
                await run_sync_transaction(deleted_projects.move_back, manager.layout, project_dir, tombstone)
            raise
        return {"success": True, "message": _t("project_deleted", name=name)}
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.get("/deleted-projects")
async def list_deleted_projects():
    """已删除项目，最近删除的在前"""
    entries = await asyncio.to_thread(deleted_projects.list_entries, get_project_manager().layout)
    return {
        "projects": [entry.to_dict() for entry in entries],
        "retention_days": deleted_projects.RETENTION.days,
    }


@router.post("/deleted-projects/{deleted_id}/restore")
async def restore_deleted_project(deleted_id: str):
    """整体恢复已删除项目：原名被占用时加后缀，记录改回恢复后的名字，再走启动时的迁移链"""
    # The whole section is uninterruptible: if cancelled mid-move the destination is lost with the return value, the
    # directory is back in place while the record still hangs under the tombstone name.
    # 整段不可中断：移出途中被取消时落点随返回值丢弃，目录已回原位而记录仍挂在墓碑名下。
    project_dir = await run_noninterruptible_async(_restore_deleted_project(get_project_manager().layout, deleted_id))
    return {"name": project_dir.name}


async def _restore_deleted_project(layout: DataRootLayout, deleted_id: str) -> Path:
    project_dir = await run_sync_transaction(deleted_projects.move_out, layout, deleted_id)
    try:
        await restore_retired_project(
            async_session_factory, deleted_id, project_dir, sdk_config_dir=default_sdk_config_dir()
        )
    except BaseException:
        await run_sync_transaction(project_dir.rename, layout.deleted_projects_dir / deleted_id)
        raise
    # Revert the record first: the migration chain looks up historical episode IDs by project name. / 记录先改回：迁移链按项目名查历史集 ID。
    await run_sync_transaction(
        deleted_projects.migrate_restored,
        project_dir,
        recorded_episode_ids=recorded_episode_ids_on(asyncio.get_running_loop()),
    )
    return project_dir


@router.delete("/deleted-projects/{deleted_id}", dependencies=[Depends(reject_embedded_agent)])
async def purge_deleted_project(deleted_id: str):
    """彻底删除已删除项目（只开放给创作者，不给 Agent 工具）"""
    await run_sync_transaction(deleted_projects.purge, get_project_manager().layout, deleted_id)
    return {"success": True}


@router.get("/projects/{name}/scripts/{script_file}")
async def get_script(name: str, script_file: str, _t: Translator):
    """获取剧本内容"""
    try:
        script = await run_sync_transaction(get_project_manager().load_script, name, script_file)
        return {"script": script, "revision": script_revision(script)}
    except FileNotFoundError as exc:
        raise NotFoundError("script_not_found", name=script_file) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.post(
    "/projects/{name}/script-edits",
    response_model=None,
    dependencies=[Depends(require_project_migration_ok)],
)
async def edit_script_batch(
    name: str,
    command: ScriptBatchEditCommand,
    _t: Translator,
    make_script_batch_editor: ScriptBatchEditorFactoryDep,
) -> JSONResponse:
    """Execute the same revisioned script-edit command exposed to the in-process Agent."""

    manager = None
    try:
        manager = get_project_manager()
        try:
            await asyncio.to_thread(manager.get_project_path, name)
        except ValueError as exc:
            raise BadRequestError("invalid_project_name", name=name) from exc
        except FileNotFoundError as exc:
            raise NotFoundError("project_not_found", name=name) from exc

        with project_change_source("webui"):
            result = await run_sync_transaction(make_script_batch_editor(manager).execute, name, command)
        return JSONResponse(status_code=script_batch_status(result), content=result.model_dump(mode="json"))
    except FileNotFoundError as exc:
        if manager is None or not await asyncio.to_thread(manager.project_exists, name):
            raise NotFoundError("project_not_found", name=name) from exc
        target = command.script or str(command.episode)
        raise NotFoundError("script_not_found", name=target) from exc
    except ApiError:
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.get(
    "/projects/{name}/script-items/{item_id}/prompt-preview",
    dependencies=[Depends(require_project_migration_ok)],
)
async def preview_script_item_prompts(
    name: str,
    item_id: str,
    _t: Translator,
    names: DisplayNamesCatalog,
    script_file: str = Query(..., description="Script file name"),
):
    """渲染该条目当前会送进图像 / 视频模型的最终提示词文本。

    与执行路径共用同一渲染出口，结果逐字等于本次生成实际发出的提示词。只读：不向供应商
    发请求、不产生费用、不写产物清单。某一侧缺提示词或形状不合规时，该侧返回不可用原因、
    另一侧照常渲染，半成品条目仍能看到已写好的那一半。

    挂迁移闸门与生成入口同判据：商品参考的装配读产物清单，且被阻断的项目本就无法生成，
    此时报「先修复」比渲染一份不会被发出的文本诚实。
    """
    try:
        preview = await preview_item_prompts(name, script_file, item_id)
    except ScriptItemNotFound as exc:
        raise NotFoundError("script_item_not_found", id=item_id) from exc
    except FileNotFoundError as exc:
        raise NotFoundError("script_not_found", name=script_file) from exc
    except ValueError as exc:
        raise UnprocessableError("script_validation_failed").with_diagnostic(str(exc)) from exc

    def _side(rendered):
        return {
            "text": rendered.text,
            "unavailable": render_generation_input_error(rendered.unavailable, rendered.unavailable_params, _t, names)
            if rendered.unavailable
            else None,
            "is_text_form": rendered.is_text_form,
            # Notes produced during rendering (such as trimming over-limit reference images) share their source with
            # the task result's warnings and are likewise rendered into final text in the request language
            # 渲染时产生的提示（如参考图超限裁剪）与任务结果的 warnings 同源，同样按请求语言渲染成成品文案
            "warnings": [render_message(warning["key"], warning["params"], _t, names) for warning in rendered.warnings],
        }

    return {
        "item_id": preview.item_id,
        "content_mode": preview.content_mode,
        "storyboard_image": _side(preview.storyboard_image),
        "video": _side(preview.video),
    }


#: Shot entry shapes that can be manually added / removed on the timeline; reference-to-video units go through the
#: video unit routes.
#: 可在时间线手动新增 / 移除的分镜条目形态；参考生视频单元走视频单元路由。
_STORYBOARD_ITEM_KINDS = frozenset({"segments", "scenes", "shots"})


def _require_storyboard_items(script: dict, item_id: str | None) -> tuple[list, str, str]:
    """Return the entry array of a storyboard-to-video script; raise the corresponding API error when the shape is
    unsupported or the given id is not found.

    返回分镜图生视频剧本的条目数组；形态不支持或给定 id 未命中时抛对应 API 错误。"""
    items, id_field, kind = resolve_items(script)
    if kind not in _STORYBOARD_ITEM_KINDS:
        raise BadRequestError("storyboard_script_required")
    if item_id is not None and not any(isinstance(item, dict) and item.get(id_field) == item_id for item in items):
        raise NotFoundError("script_item_not_found", id=item_id)
    return items, id_field, kind


class InsertScriptItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_file: str
    #: The new shot is inserted after this shot; appended at the end when omitted, i.e. the first one in an empty
    #: script.
    #: 新分镜插在这条分镜之后；缺省时追加到末尾，空脚本里即第一条。
    after_id: str | None = Field(default=None, min_length=1)
    #: The body of a narration / commentary shot is the voiceover content and is required when adding; other shapes
    #: insert an empty entry and ignore this field.
    #: 旁白 / 解说分镜的正文即配音内容，新增时必填；其余形态插入空条目、忽略此字段。
    novel_text: str | None = None


@router.post(
    "/projects/{name}/script-items",
    dependencies=[Depends(require_project_migration_ok)],
)
async def insert_script_item(
    name: str,
    req: InsertScriptItemRequest,
    _t: Translator,
    make_script_batch_editor: ScriptBatchEditorFactoryDep,
):
    """新增一条待编写分镜，按当前剧本 revision 执行 ``insert_after``；新分镜的编号由服务端另发，取本集从没用过的号。"""
    try:

        def _sync():
            manager = get_project_manager()
            current = manager.load_script(name, req.script_file)
            items, id_field, kind = _require_storyboard_items(current, req.after_id)
            last_id = next(
                (item.get(id_field) for item in reversed(items) if isinstance(item, dict)),
                None,
            )
            after_id = req.after_id if req.after_id is not None else last_id
            item = blank_item_after(current, after_id)
            if kind == "segments":
                if req.novel_text is None or not req.novel_text.strip():
                    raise UnprocessableError("narration_segment_text_required")
                item["novel_text"] = req.novel_text
            with project_change_source("webui"):
                result = execute_current_script_edit(
                    manager,
                    name,
                    req.script_file,
                    [{"op": "insert_after", "after_id": after_id, "item": item}],
                    editor=make_script_batch_editor(manager),
                    current_script=current,
                )
            require_script_edit_result(result, operation_not_found=True)
            saved_items, _id_field, _kind = resolve_items(manager.load_script(name, req.script_file))
            # Between commit and read-back the entry may have been removed concurrently, in which case item is null.
            # On batch edit commit the server issues the number; the one given by blank_item_after is only a
            # placeholder.
            # 提交后到回读之间条目可能已被并发移除，此时 item 为 null。
            # 批量编辑提交时由服务端发编号，blank_item_after 给的只是占位。
            inserted_id = result.affected_ids[0] if result.affected_ids else None
            inserted = next(
                (entry for entry in saved_items if isinstance(entry, dict) and entry.get(id_field) == inserted_id),
                None,
            )
            return {"success": True, "item": inserted, "edit_result": result.model_dump(mode="json")}

        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("script_not_found", name=req.script_file) from exc
    except ValueError as exc:
        raise UnprocessableError("script_validation_failed").with_diagnostic(str(exc)) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.post(
    "/projects/{name}/episodes/{episode}/blank-script",
    dependencies=[Depends(require_project_migration_ok)],
)
async def start_episode_blank_script(name: str, episode: int):
    """从空白开始：本集没有正式脚本时建出空的正式脚本，弃置未确认的脚本规划。"""

    def _sync():
        with project_change_source("webui"):
            return start_blank_script(get_project_manager(), name, episode)

    try:
        script_file = await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except BlankScriptError as exc:
        if exc.code == "episode_not_found":
            raise NotFoundError("episode_not_found", episode=episode) from exc
        if exc.code == "draft_agent_owned":
            raise ConflictError("draft_agent_owned") from exc
        raise ConflictError("blank_script_formal_exists").with_diagnostic(str(exc)) from exc
    return {"success": True, "script_file": script_file}


@router.delete(
    "/projects/{name}/script-items/{item_id}",
    dependencies=[Depends(require_project_migration_ok)],
)
async def remove_script_item(
    name: str,
    item_id: str,
    _t: Translator,
    make_script_batch_editor: ScriptBatchEditorFactoryDep,
    script_file: str = Query(..., description="Script file name"),
):
    """移除分镜 ``item_id``，按当前剧本 revision 执行 ``remove``；其产物随分镜一并移除。"""
    try:

        def _sync():
            manager = get_project_manager()
            current = manager.load_script(name, script_file)
            _require_storyboard_items(current, item_id)
            with project_change_source("webui"):
                result = execute_current_script_edit(
                    manager,
                    name,
                    script_file,
                    [{"op": "remove", "id": item_id}],
                    editor=make_script_batch_editor(manager),
                    current_script=current,
                )
            require_script_edit_result(result, operation_not_found=True)
            return {"success": True, "edit_result": result.model_dump(mode="json")}

        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("script_not_found", name=script_file) from exc
    except ValueError as exc:
        raise UnprocessableError("script_validation_failed").with_diagnostic(str(exc)) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


class MoveScriptItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_file: str
    #: Move after this shot; move to the front when null. / 移到这条分镜之后；为 null 时移到最前。
    after_id: str | None = Field(min_length=1)


@router.post(
    "/projects/{name}/script-items/{item_id}/move",
    dependencies=[Depends(require_project_migration_ok)],
)
async def move_script_item(
    name: str,
    item_id: str,
    req: MoveScriptItemRequest,
    _t: Translator,
    make_script_batch_editor: ScriptBatchEditorFactoryDep,
):
    """把分镜 ``item_id`` 移到 ``after_id`` 之后，按当前剧本 revision 执行 ``move_after``；分镜连同产物一起移动。"""
    try:

        def _sync():
            manager = get_project_manager()
            current = manager.load_script(name, req.script_file)
            _require_storyboard_items(current, item_id)
            _require_storyboard_items(current, req.after_id)
            with project_change_source("webui"):
                result = execute_current_script_edit(
                    manager,
                    name,
                    req.script_file,
                    [{"op": "move_after", "id": item_id, "after_id": req.after_id}],
                    editor=make_script_batch_editor(manager),
                    current_script=current,
                )
            require_script_edit_result(result)
            return {"success": True, "edit_result": result.model_dump(mode="json")}

        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("script_not_found", name=req.script_file) from exc
    except ValueError as exc:
        raise UnprocessableError("script_validation_failed").with_diagnostic(str(exc)) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


class UpdateSceneRequest(BaseModel):
    script_file: str
    updates: dict


@router.patch("/projects/{name}/script-scenes/{scene_id}", dependencies=[Depends(require_project_migration_ok)])
async def update_scene(
    name: str,
    scene_id: str,
    req: UpdateSceneRequest,
    _t: Translator,
    make_script_batch_editor: ScriptBatchEditorFactoryDep,
):
    """更新剧情演绎剧本中的单个分镜（按 scene_id 定位）。

    路径与项目场景资产 CRUD（``/projects/{name}/scenes/{entry_name}``）做明确区分，
    避免 FastAPI 按注册顺序优先匹配本端点导致 SceneCard 保存请求被截获、Pydantic
    必填字段校验返回双 "Field required"。
    """
    try:

        def _sync():
            manager = get_project_manager()
            current = manager.load_script(name, req.script_file)
            scenes = current.get("scenes")
            if not isinstance(scenes, list) or not any(
                isinstance(scene, dict) and scene.get("scene_id") == scene_id for scene in scenes
            ):
                raise HTTPException(status_code=404, detail=_t("scene_not_found", id=scene_id))
            allowed = {
                "duration_seconds",
                "image_prompt",
                "video_prompt",
                "characters_in_scene",
                "scenes",
                "props",
                "segment_break",
                "utterances",
                "note",
            }
            fields: dict[str, Any] = {}
            for key, raw_value in req.updates.items():
                if key not in allowed or (raw_value is None and key != "note"):
                    continue
                value = raw_value
                if key in {"characters_in_scene", "scenes", "props"} and isinstance(value, list):
                    value = [asset_name_comparison_key(entry) if isinstance(entry, str) else entry for entry in value]
                fields[key] = value
            if not fields:
                matched = next(scene for scene in scenes if scene.get("scene_id") == scene_id)
                return {"success": True, "scene": matched}
            with project_change_source("webui"):
                result = execute_current_script_edit(
                    manager,
                    name,
                    req.script_file,
                    [{"op": "update", "id": scene_id, "fields": fields}],
                    editor=make_script_batch_editor(manager),
                )
            require_script_edit_result(
                result,
                operation_not_found=True,
            )
            saved = manager.load_script(name, req.script_file)
            matched = next(scene for scene in saved["scenes"] if scene.get("scene_id") == scene_id)
            return {"success": True, "scene": matched, "edit_result": result.model_dump(mode="json")}

        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("script_not_found", name=req.script_file) from exc
    except ValueError as exc:
        # Structure validation failures, episode number mismatch and invalid file names all raise ValueError
        # (ScriptStructureValidationError is a subclass): uniformly turned into a 422 client error, avoiding the 500
        # fallback below.
        # 结构校验失败、集号错配、非法文件名都抛 ValueError（ScriptStructureValidationError
        # 即其子类）：统一转 422 客户端错误，避免落到下面的 500 兜底。
        raise UnprocessableError("script_validation_failed").with_diagnostic(str(exc)) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


class UpdateShotRequest(BaseModel):
    script_file: str
    updates: dict


# Ad shot PATCH allowlist: shot_id (locator key) and generated_assets (runtime state) cannot be rewritten.
# ad 分镜 PATCH 白名单：shot_id（定位键）与 generated_assets（运行时状态）不可改写。
_SHOT_UPDATABLE_FIELDS = (
    "section",
    "voiceover_text",
    "duration_seconds",
    "image_prompt",
    "video_prompt",
    "characters_in_shot",
    "scenes",
    "props",
    "products_in_shot",
    "note",
)


def _require_ad_script(script: dict, _t: Translator) -> list[dict]:
    """Assert the script is ad-shaped (content_mode=ad and has a shots key) and return the shots list.

    Same pattern as update_segment's narration guard: scripts of other modes are refused even with a leftover shots
    key, so they are not rewritten as ad.

    断言剧本是 ad 形状（content_mode=ad 且含 shots 键），返回 shots 列表。

    与 update_segment 的 narration 守卫同模式：其他模式的脚本即使残留 shots 键也拒绝，
    避免被当 ad 改写。
    """
    if script.get("content_mode") != "ad" or "shots" not in script:
        raise HTTPException(status_code=400, detail=_t("ad_mode_required"))
    shots = script.get("shots")
    # Invalid shape fails loud; the route turns ValueError into 422 uniformly. / 非法形状 fail loud，ValueError 由路由统一转 422。
    if not isinstance(shots, list):
        raise ValueError("ad script field 'shots' must be a list")
    if not all(isinstance(s, dict) for s in shots):
        raise ValueError("ad script field 'shots' contains non-object elements")
    # Missing/dirty-typed shot_id is caught too: otherwise PATCH locating by id would misreport 404.
    # shot_id 缺失/脏类型同样拦下：否则 PATCH 按 id 定位会误报 404。
    if not all(isinstance(s.get("shot_id"), str) and s["shot_id"] for s in shots):
        raise ValueError("ad script field 'shots' contains elements missing valid 'shot_id'")
    # shot_id is the identity key of a single shot: duplicate values would make PATCH silently update the first match
    # shot_id 是单个分镜的身份键：重复值会让 PATCH 静默更新首个命中项
    shot_ids = [s["shot_id"] for s in shots]
    if len(set(shot_ids)) != len(shot_ids):
        raise ValueError("ad script field 'shots' contains duplicate 'shot_id' values")
    return shots


@router.patch("/projects/{name}/script-shots/{shot_id}", dependencies=[Depends(require_project_migration_ok)])
async def update_shot(
    name: str,
    shot_id: str,
    req: UpdateShotRequest,
    _t: Translator,
    make_script_batch_editor: ScriptBatchEditorFactoryDep,
):
    """更新广告/短片剧本中的单个分镜（按 shot_id 定位）。

    路径风格与 ``script-scenes`` 对齐；口播文案 / section / 时长 / 引用列表等
    白名单字段可改，结构合法性由写盘统一入口的「不更坏」校验兜底。
    """
    try:

        def _sync():
            manager = get_project_manager()
            current = manager.load_script(name, req.script_file)
            shots = _require_ad_script(current, _t)
            matched = next((shot for shot in shots if shot.get("shot_id") == shot_id), None)
            if matched is None:
                raise HTTPException(status_code=404, detail=_t("shot_not_found", id=shot_id))
            fields = {
                key: value
                for key, value in req.updates.items()
                if key in _SHOT_UPDATABLE_FIELDS and (value is not None or key == "note")
            }
            if not fields:
                return {"success": True, "shot": matched}
            with project_change_source("webui"):
                result = execute_current_script_edit(
                    manager,
                    name,
                    req.script_file,
                    [{"op": "update", "id": shot_id, "fields": fields}],
                    editor=make_script_batch_editor(manager),
                )
            require_script_edit_result(
                result,
                operation_not_found=True,
            )
            saved = manager.load_script(name, req.script_file)
            matched = next(shot for shot in saved["shots"] if shot.get("shot_id") == shot_id)
            return {"success": True, "shot": matched, "edit_result": result.model_dump(mode="json")}

        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("script_not_found", name=req.script_file) from exc
    except ValueError as exc:
        # Structure validation failures, episode number mismatch and invalid file names all raise ValueError
        # (ScriptStructureValidationError is a subclass): uniformly turned into a 422 client error, avoiding the 500
        # fallback below.
        # 结构校验失败、集号错配、非法文件名都抛 ValueError（ScriptStructureValidationError
        # 即其子类）：统一转 422 客户端错误，避免落到下面的 500 兜底。
        raise UnprocessableError("script_validation_failed").with_diagnostic(str(exc)) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


class UpdateSegmentRequest(BaseModel):
    script_file: str
    duration_seconds: int | None = None
    segment_break: bool | None = None
    novel_text: str | None = None
    image_prompt: dict | str | None = None
    video_prompt: dict | str | None = None
    note: str | None = None
    characters_in_segment: list[str] | None = None
    scenes: list[str] | None = None
    props: list[str] | None = None


class UpdateOverviewRequest(BaseModel):
    synopsis: str | None = None
    genre: str | None = None
    theme: str | None = None
    world_setting: str | None = None


class UpdateEpisodeRequest(BaseModel):
    title: str


class UpdateEpisodeSourceRequest(BaseModel):
    text: str
    #: Source file type of this episode's source text in a drama project; when omitted the existing type is kept, and
    #: with no record it is recorded as novel. Other content modes ignore it.
    #: 剧情演绎项目这一集原文的源文件类型；缺省时保留已有类型，没有记录时记为小说。其他创作类型忽略。
    source_kind: SourceKind | None = None
    #: A confirmed type change makes this episode's existing script plan stale. / 已确认改类型会让本集已有的脚本规划判 stale。
    confirm: bool = False


@router.patch("/projects/{name}/segments/{segment_id}", dependencies=[Depends(require_project_migration_ok)])
async def update_segment(
    name: str,
    segment_id: str,
    req: UpdateSegmentRequest,
    _t: Translator,
    make_script_batch_editor: ScriptBatchEditorFactoryDep,
):
    """更新旁白/解说分镜"""
    try:

        def _sync():
            manager = get_project_manager()
            current = manager.load_script(name, req.script_file)
            if current.get("content_mode") != "narration" or "segments" not in current:
                raise HTTPException(status_code=400, detail=_t("narration_mode_required"))
            segments = current.get("segments")
            if not isinstance(segments, list):
                raise ValueError("narration script field 'segments' must be a list")
            matched = next(
                (
                    segment
                    for segment in segments
                    if isinstance(segment, dict) and segment.get("segment_id") == segment_id
                ),
                None,
            )
            if matched is None:
                raise HTTPException(status_code=404, detail=_t("segment_not_found", id=segment_id))
            fields: dict[str, Any] = {}
            for field in (
                "duration_seconds",
                "segment_break",
                "novel_text",
                "image_prompt",
                "video_prompt",
            ):
                value = getattr(req, field)
                if value is not None:
                    fields[field] = value
            if "note" in req.model_fields_set:
                fields["note"] = req.note
            for field in ("characters_in_segment", "scenes", "props"):
                if field in req.model_fields_set:
                    fields[field] = [asset_name_comparison_key(value) for value in (getattr(req, field) or [])]
            if not fields:
                return {"success": True, "segment": matched}
            with project_change_source("webui"):
                result = execute_current_script_edit(
                    manager,
                    name,
                    req.script_file,
                    [{"op": "update", "id": segment_id, "fields": fields}],
                    editor=make_script_batch_editor(manager),
                )
            require_script_edit_result(
                result,
                operation_not_found=True,
            )
            saved = manager.load_script(name, req.script_file)
            matched = next(segment for segment in saved["segments"] if segment.get("segment_id") == segment_id)
            return {"success": True, "segment": matched, "edit_result": result.model_dump(mode="json")}

        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("script_not_found", name=req.script_file) from exc
    except ValueError as exc:
        # Structure validation failures, episode number mismatch and invalid file names all raise ValueError
        # (ScriptStructureValidationError is a subclass): uniformly turned into a 422 client error, avoiding the 500
        # fallback below.
        # 结构校验失败、集号错配、非法文件名都抛 ValueError（ScriptStructureValidationError
        # 即其子类）：统一转 422 客户端错误，避免落到下面的 500 兜底。
        raise UnprocessableError("script_validation_failed").with_diagnostic(str(exc)) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.patch("/projects/{name}/episodes/{episode}", dependencies=[Depends(require_project_migration_ok)])
async def update_episode(name: str, episode: int, req: UpdateEpisodeRequest, _t: Translator):
    """更新分集顶层元数据（当前仅标题）。

    有剧本时以剧本 scripts/*.json 顶层 title 为唯一真相源：走 locked_episode_script 在
    「脚本锁 → 项目锁」临界区内改剧本 title，并内联 _apply_episode_sync 把镜像同步回
    project.json 的 episodes[].title，原子且无 TOCTOU。镜像由 PATCH /projects 改写的入口
    已移除（title 不在 EpisodePatch 上），杜绝第二真相源。

    还没有剧本的集（新建的空集、尚未规划脚本的集）标题只记在账本条目上；之后建出的剧本以它为初值。
    标题可以清空，空标题的集名由呈现层按播出位置派生。
    """
    title = req.title.strip()

    try:

        def _sync():
            manager = get_project_manager()

            def _resolve(project: dict) -> str:
                episodes = project.get("episodes") or []
                meta = next((e for e in episodes if e.get("episode") == episode), None)
                if meta is None or not meta.get("script_file"):
                    raise HTTPException(status_code=404, detail=_t("episode_not_found", episode=episode))
                return meta["script_file"]

            def _retitle_ledger_entry(project: dict) -> None:
                script_file = _resolve(project)
                script_path = (
                    manager.get_project_path(name) / "scripts" / manager.normalize_script_filename(script_file)
                )
                if script_path.is_file():
                    # The script was created between the two reads: the title is now carried by the script, ask the
                    # caller to retry
                    # 剧本在两次读取之间被建出：标题改由剧本承载，请调用方重试
                    raise HTTPException(status_code=409, detail=_t("ref_script_rebound"))
                meta = next(e for e in project["episodes"] if e.get("episode") == episode)
                meta["title"] = title

            with project_change_source("webui"):
                try:
                    with manager.locked_episode_script(name, _resolve) as script:
                        script["title"] = title
                except FileNotFoundError as exc:
                    if not manager.project_exists(name):
                        raise NotFoundError("project_not_found", name=name) from exc
                    # This episode has no script yet: the title is recorded on the ledger entry / 这一集还没有剧本：标题记在账本条目上
                    manager.update_project(name, _retitle_ledger_entry)
                except EpisodeScriptReboundError as exc:
                    logger.info("episode script rebound during title update: %s", exc)
                    raise HTTPException(status_code=409, detail=_t("ref_script_rebound")) from exc
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=_t("script_validation_failed", details=str(exc))
                    ) from exc

            # Return the value just written (the frontend refreshes the whole project after saving with refreshProject
            # and does not depend on this return). No second load_project after the lock: saves a disk read and keeps
            # a read outside the lock from being polluted by concurrent writers.
            # 返回刚写入的值（前端保存后整体 refreshProject，不强依赖此返回）。
            # 不再锁后二次 load_project：省一次读盘，且避免锁外读取被并发写者污染返回值。
            return {"success": True, "episode": {"episode": episode, "title": title}}

        return await run_sync_transaction(_sync)
    except (HTTPException, ApiError):
        # ApiError alongside HTTPException: the NotFoundError raised inside _sync is not an HTTPException subclass,
        # and if not included here it would be swallowed into a 500 by the except Exception below
        # ApiError 与 HTTPException 并列：_sync 内部抛出的 NotFoundError 不是
        # HTTPException 子类，不并入这里会被下面的 except Exception 吞成 500
        raise
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.put("/projects/{name}/episodes/{episode}/source", dependencies=[Depends(require_project_migration_ok)])
async def update_episode_source(name: str, episode: int, req: UpdateEpisodeSourceRequest, _t: Translator):
    """集页填写或改写本集原文：无原文的集填上后转为自带原文的集。切出集的原文由分集规划派生，这里拒绝。

    改类型会让本集已有的脚本规划判 stale 而 ``confirm`` 为 false 时，原文与类型都不写入，返回
    ``needs_confirmation`` 与受影响的集 ID（``affected_episodes``）。
    """

    def _sync() -> dict[str, Any]:
        manager = get_project_manager()
        if not manager.project_exists(name):
            raise NotFoundError("project_not_found", name=name)
        with project_change_source("webui"):
            written = set_episode_source_text(
                manager, name, episode, req.text, source_kind=req.source_kind, confirm=req.confirm
            )
        return {
            "success": True,
            "episode": episode,
            "source_origin": written.origin.value,
            "applied": written.applied,
            "needs_confirmation": not written.applied,
            "affected_episodes": written.affected_episodes,
        }

    try:
        return await run_sync_transaction(_sync)
    except EpisodeSourceError as exc:
        raise episode_source_http_error(exc, _t, episode=episode) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


# ==================== Source file management ==================== / ==================== 源文件管理 ====================


@router.post("/projects/{name}/source", dependencies=[Depends(require_project_migration_ok)])
async def set_project_source(
    name: Annotated[str, FastAPIPath(pattern=r"^[a-zA-Z0-9_-]+$")],
    _t: Translator,
    names: DisplayNamesCatalog,
    generate_overview: Annotated[bool, Form()] = True,
    content: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
):
    """上传小说源文件或直接提交文本内容，可选触发 AI 概述生成。

    两种输入方式（互斥，均使用 multipart/form-data）：
    - file：上传 .txt/.md 文件，文件名取自上传文件
    - content：直接提交文本内容，自动命名为 novel.txt

    最大 200000 字符（约 10 万汉字）。
    """
    MAX_CHARS = 200_000
    ALLOWED_SUFFIXES = {".txt", ".md"}

    if not content and not file:
        raise HTTPException(status_code=400, detail=_t("content_or_file_required"))
    if content and file:
        raise HTTPException(status_code=400, detail=_t("one_of_content_or_file"))

    try:
        manager = get_project_manager()

        # Read the uploaded file asynchronously / 异步读取上传文件
        raw: bytes | None = None
        original_name: str = "novel.txt"
        if file:
            original_name = file.filename or "novel.txt"
            suffix = Path(original_name).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise HTTPException(status_code=400, detail=_t("unsupported_file_type", name=suffix))
            if file.size is not None and file.size > MAX_CHARS * 4:
                raise HTTPException(status_code=400, detail=_t("file_too_large", max_chars=MAX_CHARS))
            raw = await file.read()
        text_content: str = content or ""

        # Synchronous file I/O runs in a thread / 同步文件 I/O 在线程中执行
        def _sync_write():
            if not manager.project_exists(name):
                raise HTTPException(status_code=404, detail=_t("project_not_found", name=name))
            with manager.locked_source_registration(name) as (source_dir, project, _undo):
                if raw is not None:
                    safe_filename = Path(original_name).name
                    if is_derived_episode_name(safe_filename):
                        raise HTTPException(status_code=400, detail=_t("source_name_reserved"))
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise HTTPException(status_code=400, detail=_t("invalid_encoding")) from exc
                    if len(text) > MAX_CHARS:
                        raise HTTPException(status_code=400, detail=_t("file_too_large", max_chars=MAX_CHARS))
                    (source_dir / safe_filename).write_text(text, encoding="utf-8")
                    register_whole_source_file(project, f"source/{safe_filename}")
                    return safe_filename, len(text)
                if len(text_content) > MAX_CHARS:
                    raise HTTPException(status_code=400, detail=_t("file_too_large", max_chars=MAX_CHARS))
                safe_filename = "novel.txt"
                (source_dir / safe_filename).write_text(text_content, encoding="utf-8")
                register_whole_source_file(project, f"source/{safe_filename}")
                return safe_filename, len(text_content)

        safe_filename, chars = await run_sync_transaction(_sync_write)

        result: dict = {"success": True, "filename": safe_filename, "chars": chars}

        if generate_overview:
            try:
                with project_change_source("webui"):
                    overview = await manager.generate_overview(name)
                result["overview"] = overview
            except ModelNotInCatalogError as ov_err:
                # The text model is no longer in the provider's list: return "choose the model again" guidance rather
                # than a generic generation failure
                # 文本模型已不在供应商清单中：回传「重新选择模型」指引，而非笼统的生成失败
                logger.warning("上传后概览生成失败：模型不在清单中 (%s)", ov_err)
                result["overview"] = None
                result["overview_error"] = render_message(FAILURE_CODE_KEYS[ov_err.code], ov_err.params, _t, names)
            except Exception as ov_err:
                # Overview generation is an optional follow-up step of upload; failure only degrades to returning a
                # note and does not affect upload success. A raw str(ov_err) may carry internal details such as server
                # paths, so return translated generic text.
                # 概览生成是上传的可选后续步骤，失败仅降级回传提示、不影响上传成功。
                # 裸 str(ov_err) 可能携带服务器路径等内部细节，回传翻译后的通用文案。
                logger.exception("上传后概览生成失败")
                result["overview"] = None
                result["overview_error"] = (
                    _t("overview_ai_response_invalid")
                    if isinstance(ov_err, PydanticValidationError)
                    else _t("overview_generation_failed")
                )

        return result
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc
    finally:
        if file:
            await file.close()


# ==================== Project overview management ==================== / ==================== 项目概述管理 ====================


@router.post("/projects/{name}/generate-overview", dependencies=[Depends(require_project_migration_ok)])
async def generate_overview(name: str, _t: Translator):
    """使用 AI 生成项目概述"""
    try:
        await asyncio.to_thread(get_project_manager().get_project_path, name)
    except ValueError as e:
        # An invalid project name (path traversal etc.) is caught before the generation flow, so it does not fall into
        # the except ValueError of the provider resolution chain inside generate_overview() below and get misjudged as
        # "no provider configured"
        # 非法项目名（路径穿越等）先于生成流程拦截，避免落入下面 generate_overview()
        # 内部供应商解析链路的 except ValueError，被误判为「未配置供应商」
        raise BadRequestError("invalid_project_name", name=name) from e
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc

    def _provider_not_configured(exc: ValueError) -> BadRequestError:
        # Invalid project names were already caught by the pre-check above, so everything here comes from the provider
        # resolution chain (not configured / no usable provider); str(exc) only goes to the log
        # 非法项目名已由上方预校验拦截，此处均来自供应商解析链路（未配置/无可用供应商）；str(exc) 只进日志
        logger.warning("生成概述配置错误: name=%s (%s)", name, exc)
        return BadRequestError("text_provider_not_configured")

    try:
        # EmptySourceError / PydanticValidationError / ModelNotInCatalogError are all ValueError subclasses and must
        # be let through to their own handlers below, not misjudged as "no provider configured" by
        # domain_error_on_value_error's generic ValueError handling
        # EmptySourceError / PydanticValidationError / ModelNotInCatalogError 都是 ValueError 子类，须放行给
        # 下方各自的处理，不能被 domain_error_on_value_error 的通用 ValueError 处理误判为「未配置供应商」
        with (
            project_change_source("webui"),
            domain_error_on_value_error(
                _provider_not_configured,
                extra_passthrough=(EmptySourceError, PydanticValidationError, ModelNotInCatalogError),
            ),
        ):
            overview = await get_project_manager().generate_overview(name)
        return {"success": True, "overview": overview}
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except PydanticValidationError as exc:
        # The model output failed schema validation (the last line of defense when backend degradation still fails);
        # the raw pydantic error string contains fragments of the model's raw output and is not passed through to the
        # user
        # 模型输出未通过 schema 校验（后端降级仍失守时的最后防线），
        # 裸 pydantic 错误串含模型原始输出片段，不透传给用户
        logger.exception("概述生成响应解析失败")
        raise HTTPException(status_code=400, detail=_t("overview_ai_response_invalid")) from exc
    except TextOutputTruncatedError as exc:
        # Output truncated by the max output length: same problem ticket shape as the other text tasks, so the
        # frontend offers the way out of registering an output length or switching models
        # 输出被最大输出长度截断：与各文本任务同一个问题票形状，前端据此给出登记输出长度或换模型的出路
        logger.warning("概述生成输出被截断: name=%s (%s)", name, exc)
        raise UnprocessableError(
            "text_output_truncated", provider_id=exc.provider_id or exc.provider, model=exc.model
        ).with_diagnostic(truncation_problem(exc).model_dump()) from exc
    except EmptySourceError as e:
        logger.warning("生成概述参数错误: name=%s (%s)", name, e)
        raise BadRequestError("overview_source_empty") from e
    except json.JSONDecodeError as exc:
        # The provider resolution chain reloads the project internally, so a corrupt project.json must not be
        # misjudged as "no provider configured"
        # 供应商解析链路内部会重新 load_project，project.json 损坏时不能误判为「未配置供应商」
        logger.exception("生成概述失败：项目数据损坏 name=%s", name)
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc
    except (HTTPException, ApiError, ModelNotInCatalogError):
        # ModelNotInCatalogError: the text model referenced by the project is no longer in the provider's list; hand
        #                         to the app-level handler to return a 400 asking to choose the model again
        # ModelNotInCatalogError：项目引用的文本模型已不在供应商清单中，交 app 级 handler 返回要求重新选择模型的 400
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.patch("/projects/{name}/overview")
async def update_overview(name: str, req: UpdateOverviewRequest, _t: Translator):
    """更新项目概述（手动编辑）"""
    try:

        def _sync():
            manager = get_project_manager()
            captured: dict[str, Any] = {}

            def _mutate(project: dict) -> None:
                # The whole RMW happens inside a single _project_lock, so it and concurrent overview write-backs from
                # generation do not overwrite each other
                # 整段 RMW 在单一 _project_lock 内完成，避免与并发生成的 overview 回写互相覆盖
                if "overview" not in project:
                    project["overview"] = {}
                if req.synopsis is not None:
                    project["overview"]["synopsis"] = req.synopsis
                if req.genre is not None:
                    project["overview"]["genre"] = req.genre
                if req.theme is not None:
                    project["overview"]["theme"] = req.theme
                if req.world_setting is not None:
                    project["overview"]["world_setting"] = req.world_setting
                captured["overview"] = project["overview"]

            with project_change_source("webui"):
                manager.update_project(name, _mutate)
            return {"success": True, "overview": captured["overview"]}

        return await run_sync_transaction(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=name) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc
