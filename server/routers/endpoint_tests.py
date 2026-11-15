"""Endpoint test API: preview request, validate response, test connection.

Three separate endpoints, no ``mode`` field: the three differ in inputs, cost and response body, and merging them
into one endpoint would only turn "which fields matter this time" into a paragraph of explanation. Definitions are
always inline (test connection may also take a ``model_ref``); the server keeps no drafts.

The request body takes JSON; with media it uses ``multipart/form-data`` instead, with the same JSON in the
``payload`` field and media in file fields named after the ``source`` of ``inputs`` (``start_image`` / ``end_image``
/ ``reference_images`` / ``reference_audio_files``; the last two may repeat). Media is discarded after use; no
temporary upload endpoint, no references to project assets.

Invalid definitions and render-time errors are uniformly 422 + ``diagnostic.errors[{path, code, message}]``, judged
by the same shared validator as the save API: a definition accepted here is guaranteed to be accepted by the save
API too.

端点测试 API：预览请求、验证响应、测试连接。

三个独立端点，不用 ``mode`` 字段——三者的入参、代价与返回体各不相同，合成一个端点只会让「哪些
字段这次有意义」变成一段说明文字。定义一律内联（测试连接另可给 ``model_ref``），服务端不留草稿。

请求体收 JSON；带素材时改用 ``multipart/form-data``，同一份 JSON 放在 ``payload`` 字段，素材按
``inputs`` 的 ``source`` 名走文件字段（``start_image`` / ``end_image`` / ``reference_images`` /
``reference_audio_files``，后两者可重复）。素材用完即丢，不建临时上传端点、不引用项目资产。

定义不合法与渲染期错误统一 422 + ``diagnostic.errors[{path, code, message}]``，判定来自保存接口
那一个共享校验器：这里放行的定义，保存接口必然也放行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

# UploadFile is the starlette base class, not the fastapi subclass: this code does isinstance checks on the result of
# Request.form(), and the parser produces starlette.datastructures.UploadFile, so a check against the subclass would
# always miss.
# UploadFile 取 starlette 基类而非 fastapi 子类：这里对 Request.form() 解析结果做 isinstance
# 判定，解析器产出的是 starlette.datastructures.UploadFile，用子类判定会全部落空。
from starlette.datastructures import FormData, UploadFile

from arcreel_market_core.endpoint_definition import AssetData, DefinitionDiagnostics, validate_definition
from lib.backends.backend_assembly.specs import builtin_declarative_video_diagnostics
from lib.catalog import query as catalog
from lib.config.registry import PROVIDER_REGISTRY, ProviderMeta
from lib.config.resolver import ConfigResolver
from lib.custom_provider import is_custom_provider, parse_provider_id
from lib.custom_provider.comfyui.failures import ComfyuiError
from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
from lib.custom_provider.endpoint_test import (
    ASSET_SOURCES,
    EndpointTestAssets,
    EndpointTestCredentials,
    EndpointTestDefinitionError,
    EndpointTestMode,
    EndpointTestParameters,
    KindTestSupport,
    TrialRun,
    TrialRunBusyError,
    TrialRunManager,
    TrialRunTarget,
    artifact_media_type,
    check_response,
    model_ref_target,
    parse_response_body,
    stage_report_payload,
    support_for_kind,
    supports_test_mode,
    trial_run_manager,
)
from lib.custom_provider.path_overrides import NO_PATH_OVERRIDES, PathOverrides, effective_path_overrides
from lib.db import async_session_factory, get_async_session
from lib.db.models.custom_provider import CustomProvider
from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.generation.generation_result import problem_from_task_failure
from lib.generation.task_failure import encode_failure, parse_failure, render_failure
from lib.i18n.display_names import DisplayNames
from lib.infra.api_errors import BadRequestError, ConflictError, NotFoundError, UnprocessableError
from lib.infra.async_thread import run_sync_transaction
from server.i18n import DisplayNamesCatalog, Translator

logger = logging.getLogger(__name__)

#: Limit for a single media file. First frames / reference images for tests are the same magnitude as real generation,
#: so it uses the same tier as the reference audio limit.
#: 单个素材文件的上限。测试用的首帧 / 参考图与真实生成同量级，与参考音频上限取同一档。
MAX_ASSET_BYTES = 15 * 1024 * 1024

#: Limit on the total number of media files per request: still has headroom over the heaviest legitimate combination
#: (H3's 9 reference images + start/end frames + 3 audio clips).
#: 单次请求的素材文件总数上限：覆盖最重的合法组合（H3 的 9 参考图 + 首尾帧 + 3 段音频）仍有余量。
MAX_ASSET_FILES = 16

#: Endpoint media types that can run a test connection. The model row entry point uses this to reject text / audio
#: rows: dispatching those rows to a video or image endpoint would only send a paid call to a different model.
#: 能跑测试连接的端点媒体类型。模型行这条入口据此拒掉文本 / 音频档的行——那些行被派发到
#: 视频或图像端点上只会付费打给另一个模型。
_TESTABLE_MEDIA_TYPES = frozenset({"video", "image"})

router = APIRouter(tags=["Custom Endpoints"])


def get_trial_run_manager() -> TrialRunManager:
    """Injection point of the test connection registry. A per-process singleton; tests swap in an isolated one via
    dependency override.

    测试连接登记处的注入口。进程内单例，测试按依赖覆盖换成隔离的一台。"""
    return trial_run_manager()


def get_config_resolver() -> ConfigResolver:
    """Config resolver used by ``model_ref`` to assemble the backend; it opens its own session and does not share a
    transaction with the request.

    ``model_ref`` 装配 backend 用的配置解析器——它自开 session，与请求那条不共享事务。"""
    return ConfigResolver(async_session_factory)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class TestParametersInput(BaseModel):
    """调用参数，与 ``VideoGenerationRequest`` 同名。刻意没有 seed（产品界面无处设置）。"""

    model: str
    prompt: str = ""
    duration_seconds: int = 5
    aspect_ratio: str = "9:16"
    resolution: str | None = None
    generate_audio: bool = True

    def to_parameters(self) -> EndpointTestParameters:
        return EndpointTestParameters(
            model=self.model,
            prompt=self.prompt,
            duration_seconds=self.duration_seconds,
            aspect_ratio=self.aspect_ratio,
            resolution=self.resolution,
            generate_audio=self.generate_audio,
        )


class TestCredentialsInput(BaseModel):
    """凭证两版：``provider_id`` 读库，或内联 ``base_url`` + ``api_key``。"""

    provider_id: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class ModelRefInput(BaseModel):
    """模型行引用：``provider_id`` 取规范形（内置如 ``kling``，自定义如 ``custom-3``）。"""

    provider_id: str
    model_id: str


class PreviewRequestInput(BaseModel):
    definition: Any
    parameters: TestParametersInput
    credentials: TestCredentialsInput | None = None


class CheckResponseInput(BaseModel):
    definition: Any
    stage: Literal["submit", "poll", "result"]
    #: The provider response body as is. Strings are tried as JSON; if parsing fails they are validated as the raw
    #: string.
    #: 供应商响应体原样。字符串按 JSON 试解析，解析不了就按原始字符串验证。
    response_body: Any = None


class TrialRunInput(BaseModel):
    definition: Any = None
    model_ref: ModelRefInput | None = None
    parameters: TestParametersInput
    credentials: TestCredentialsInput | None = None


class PreviewedRequestResponse(BaseModel):
    method: str
    url: str
    headers: dict[str, str]
    body: Any = None


class PreviewResponse(BaseModel):
    """渲染后的各节请求，外加「这份请求是怎么算出来的」。

    ``conversions`` 的形状由定义的 ``kind`` 决定：声明式端点的请求全部来自模板直填、没有可说明
    的换算，为 null；ComfyUI 端点给出尺寸 / 帧数 / 种子的换算与这次改图删掉的节点。
    """

    submit: PreviewedRequestResponse
    poll: PreviewedRequestResponse
    result: PreviewedRequestResponse | None = None
    conversions: dict[str, Any] | None = None


class TrialRunResponse(BaseModel):
    id: str
    status: str
    provider: str
    model: str
    #: Whether this run produced video or an image; the reading side uses it to show the artifact in a player or as an
    #: image.
    #: 这一笔产的是视频还是图像；读侧据此决定产物按播放器还是按图展示。
    media_type: str = "video"
    created_at: float
    finished_at: float | None = None
    api_call_id: int | None = None
    provider_job_id: str | None = None
    stages: dict[str, str] = {}
    request: dict[str, Any] | None = None
    submit_response: Any = None
    poll_responses: list[Any] = []
    result_response: Any = None
    extractions: dict[str, Any] = {}
    video_url: str | None = None
    duration_seconds: int | None = None
    error: str | None = None
    #: The stable failure code behind ``error``. The reading side branches on it: a failure code has a lookup table and
    #: troubleshooting direction to offer, raw exception text does not.
    #: ``error`` 背后那个稳定失败码。读侧据它分流：失败码有对照表与排查方向可给，裸异常文本没有。
    error_code: str | None = None
    #: What this failure code should make the user do (``GenerationAction``), the same decision a generation failure
    #: gives on the project page.
    #: 这条失败码该让用户去做什么（``GenerationAction``），与生成失败在项目页给出的是同一份判定。
    error_action: str | None = None
    has_artifact: bool = False


# ---------------------------------------------------------------------------
# Shared: request parsing and the definition gate
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 共用：请求解析与定义闸门
# ---------------------------------------------------------------------------


async def _parse_body[T: BaseModel](request: Request, model: type[T]) -> tuple[T, EndpointTestAssets]:
    """Accept JSON or multipart. For multipart, the ``payload`` field is the same JSON and the other fields are media.

    收 JSON 或 multipart。multipart 时 ``payload`` 字段是同一份 JSON，其余字段是素材。"""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return _validated(model, await _json_body(request)), EndpointTestAssets()
    form = await request.form()
    raw = form.get("payload")
    if not isinstance(raw, str):
        raise BadRequestError("endpoint_test_payload_required")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise BadRequestError("endpoint_test_payload_invalid") from exc
    assets = await _read_assets(form)
    return _validated(model, payload), assets


async def _json_body(request: Request) -> object:
    try:
        return await request.json()
    except ValueError as exc:
        raise BadRequestError("endpoint_test_payload_invalid") from exc


def _validated[T: BaseModel](model: type[T], payload: object) -> T:
    try:
        return model.model_validate(payload)
    except ValueError as exc:
        raise BadRequestError("endpoint_test_payload_invalid").with_diagnostic(str(exc)) from exc


async def _read_assets(form: FormData) -> EndpointTestAssets:
    # The count limit is judged on all files in the form, not just the registered fields: a file stuffed in under
    # another field name has also been parsed, buffered or spilled to temp disk, and counting only known fields would
    # give a limit that is bypassed by changing the key.
    # 件数上限按表单里的全部文件数判，不只数已登记的字段：换个字段名塞进来的文件同样已被
    # 解析、缓冲或落进临时盘，只数认得的字段等于给出一个换个 key 就绕开的上限。
    if sum(1 for _name, item in form.multi_items() if isinstance(item, UploadFile)) > MAX_ASSET_FILES:
        raise BadRequestError("endpoint_test_too_many_assets", limit=MAX_ASSET_FILES)
    by_source: dict[str, AssetData | Sequence[AssetData] | None] = {}
    for source, is_list in ASSET_SOURCES.items():
        uploads = [item for item in form.getlist(source) if isinstance(item, UploadFile)]
        loaded = [await _read_upload(upload) for upload in uploads]
        by_source[source] = loaded if is_list else (loaded[0] if loaded else None)
    return EndpointTestAssets(by_source=by_source)


async def _read_upload(upload: UploadFile) -> AssetData:
    # Read only up to the limit + 1 byte: an oversized file can be detected without loading it whole into memory.
    # 只读到上限 +1 字节：超限件不整块载入内存就能判超。
    content = await upload.read(MAX_ASSET_BYTES + 1)
    if len(content) > MAX_ASSET_BYTES:
        raise BadRequestError(
            "endpoint_test_asset_too_large",
            name=upload.filename or "asset",
            limit_mb=MAX_ASSET_BYTES // (1024 * 1024),
        )
    return AssetData(upload.content_type or "application/octet-stream", content)


def _accepted_definition(definition: object, _t: Translator, *, mode: EndpointTestMode) -> dict[str, Any]:
    """Run the shared validator, then check by the definition's ``kind`` whether this mode is supported.

    Validation uses the same decision and the same error codes as the save API; endpoint tests add no extra
    threshold. "This kind has no such mode" is not a definition error, so it is not mixed into the definition
    diagnostics but rejected separately as a 400.

    过共享校验器，再按定义的 ``kind`` 判这种模式支不支持。

    校验与保存接口同一份判定、同一套错误码，端点测试不另设门槛。「这种 kind 没有这种模式」不是
    定义有错，故不混进定义诊断，按一条 400 单独拒绝。
    """
    diagnostics = validate_definition(definition)
    if diagnostics.errors or not isinstance(definition, dict):
        raise _invalid(diagnostics, _t)
    kind = str(definition["kind"])
    if not supports_test_mode(kind, mode):
        raise BadRequestError("endpoint_test_mode_unsupported_for_kind", kind=kind)
    return definition


def _support(definition: Mapping[str, Any]) -> KindTestSupport:
    """Endpoint test implementation for this definition's kind. The definition has already passed the mode gate of
    :func:`_accepted_definition`.

    这份定义所属 kind 的端点测试实现。定义已过 :func:`_accepted_definition` 的模式闸。"""
    return support_for_kind(str(definition["kind"]))


def _invalid(diagnostics: DefinitionDiagnostics, _t: Translator) -> UnprocessableError:
    return UnprocessableError("custom_endpoint_definition_invalid").with_diagnostic(diagnostics.to_payload(_t))


async def _resolve_credentials(
    body_credentials: TestCredentialsInput | None,
    session: AsyncSession,
    *,
    require_base_url: bool = True,
    require_api_key: bool = True,
) -> EndpointTestCredentials | None:
    """Normalize the two versions of credentials. ``provider_id`` only accepts custom providers: built-in providers'
    credentials are not called by endpoint definition.

    When ``require_base_url`` is False (all three URL sections of the definition are hard-coded absolute addresses),
    inline credentials need not carry a base URL; when ``require_api_key`` is False (``auth`` is empty) no api_key
    is needed: such a definition uses that credential nowhere, and forcing the caller to make up a fake value is
    pointless.

    凭证两版归一。``provider_id`` 只接受自定义供应商——内置供应商的凭证不按端点定义调用。

    ``require_base_url`` 为 False 时（定义三节 URL 都写死绝对地址）内联凭证不必带接口地址，
    ``require_api_key`` 为 False 时（``auth`` 为空）不必带 api_key：那种定义没有一处会用到
    对应凭证，逼调用方编一个假值填进来没有意义。
    """
    if body_credentials is None:
        return None
    if body_credentials.provider_id:
        if body_credentials.base_url or body_credentials.api_key:
            # Silently picking one when two sources are mixed would send the paid request to a billing target the
            # caller did not explicitly choose.
            # 混合两种来源时静默取其一，付费请求会打到调用方没明确选择的计费目标上。
            raise BadRequestError("endpoint_test_credentials_ambiguous")
        provider = await _custom_provider(body_credentials.provider_id, session)
        # A row in the DB storing an empty value is judged the same as an inline missing field: a missing credential
        # should surface on the request thread, not fail only when a background task renders a request with empty
        # auth.
        # 库里的行存了空值时与内联缺字段同判：缺的凭证要在请求线程上露头，而不是等后台任务
        # 渲染出一个空鉴权请求才失败。
        if (require_base_url and not provider.base_url) or (require_api_key and not provider.api_key):
            return None
        return EndpointTestCredentials(base_url=provider.base_url or "", api_key=provider.api_key or "")
    if (body_credentials.base_url or not require_base_url) and (
        body_credentials.api_key is not None or not require_api_key
    ):
        return EndpointTestCredentials(base_url=body_credentials.base_url or "", api_key=body_credentials.api_key or "")
    return None


async def _required_credentials(
    body_credentials: TestCredentialsInput | None,
    session: AsyncSession,
    *,
    require_base_url: bool = True,
    require_api_key: bool = True,
) -> EndpointTestCredentials:
    """A test connection must have the credentials the definition uses: without them the request cannot be sent at all,
    and queueing it into a background task to fail later is pointless. When the definition uses neither credential
    (``auth`` is empty and all three URL sections are hard-coded absolute addresses), empty credentials are let
    through.

    测试连接必须有定义用得上的凭证：没有它们的请求本就发不出去，让它排到后台任务里再失败
    没有意义。定义两种凭证都用不上（``auth`` 为空且三节 URL 全写死绝对地址）时按空凭证放行。"""
    credentials = await _resolve_credentials(
        body_credentials, session, require_base_url=require_base_url, require_api_key=require_api_key
    )
    if credentials is None:
        if not require_base_url and not require_api_key:
            return EndpointTestCredentials(base_url="", api_key="")
        raise BadRequestError("endpoint_test_credentials_required")
    return credentials


async def _custom_provider(provider_id: str, session: AsyncSession) -> CustomProvider:
    if not is_custom_provider(provider_id):
        raise BadRequestError("endpoint_test_provider_not_custom", provider_id=provider_id)
    try:
        row_id = parse_provider_id(provider_id)
    except ValueError as exc:
        raise BadRequestError("endpoint_test_provider_not_custom", provider_id=provider_id) from exc
    provider = await CustomProviderRepository(session).get_provider(row_id)
    if provider is None:
        raise NotFoundError("provider_not_found")
    return provider


# ---------------------------------------------------------------------------
# Preview request / validate response
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 预览请求 / 验证响应
# ---------------------------------------------------------------------------


@router.post("/preview-request")
async def preview_endpoint_request(
    request: Request,
    _t: Translator,
    names: DisplayNamesCatalog,
    session: AsyncSession = Depends(get_async_session),
) -> PreviewResponse:
    """渲染将要发出的请求，不外发。凭证打码、素材换成体积摘要、提交后才有的 id 保持占位符。"""
    body, assets = await _parse_body(request, PreviewRequestInput)
    definition = _accepted_definition(body.definition, _t, mode=EndpointTestMode.PREVIEW_REQUEST)
    support = _support(definition)
    require_base_url, require_api_key = support.credential_needs(definition)
    credentials = await _resolve_credentials(
        body.credentials, session, require_base_url=require_base_url, require_api_key=require_api_key
    )
    try:
        preview = support.preview(definition, body.parameters.to_parameters(), credentials=credentials, assets=assets)
    except EndpointTestDefinitionError as exc:
        raise _invalid(exc.diagnostics, _t) from exc
    except ComfyuiError as exc:
        # ComfyUI failures at build time (e.g. fewer reference images than grid cells, an image-edit cascade reaching
        # the output node) are not definition errors; this definition cannot render a request with this set of
        # parameters. Explain with the failure code's own three-language text instead of writing separate wording.
        # 构造期的 ComfyUI 失败（如参考图少于格子数、改图的级联触到产物节点）不是定义有错，而是
        # 这份定义配上这组参数渲染不出请求。用失败码本身的三语文案说明，不另写一套措辞。
        raise BadRequestError(
            "endpoint_test_preview_failed", detail=render_failure(encode_failure(exc.code, **exc.params), _t, names)
        ) from exc
    return PreviewResponse(
        submit=_previewed(preview.submit),
        poll=_previewed(preview.poll),
        result=_previewed(preview.result) if preview.result else None,
        conversions=dict(preview.conversions) if preview.conversions is not None else None,
    )


@router.post("/check-response")
async def check_endpoint_response(
    body: CheckResponseInput,
    _t: Translator,
) -> dict[str, Any]:
    """用一份供应商真实响应验证取值路径与状态映射，不外发。零费用。"""
    definition = _accepted_definition(body.definition, _t, mode=EndpointTestMode.CHECK_RESPONSE)
    try:
        report = check_response(definition, body.stage, parse_response_body(body.response_body))
    except EndpointTestDefinitionError as exc:
        raise _invalid(exc.diagnostics, _t) from exc
    return stage_report_payload(report)


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 测试连接
# ---------------------------------------------------------------------------


@router.post("/trial-runs", status_code=201)
async def start_trial_run(
    request: Request,
    _t: Translator,
    names: DisplayNamesCatalog,
    session: AsyncSession = Depends(get_async_session),
    manager: TrialRunManager = Depends(get_trial_run_manager),
    resolver: ConfigResolver = Depends(get_config_resolver),
) -> TrialRunResponse:
    """真实提交一次生成并轮询到终态，**会产生费用**。接口不设确认参数——明示计费归调用方。

    定义内联或 ``model_ref`` 二选一：后者按模型行解析端点与凭证，内置与自定义走同一资源、同一
    结果体、同一记账。每用户同时只允许一个。
    """
    body, assets = await _parse_body(request, TrialRunInput)
    parameters = body.parameters.to_parameters()
    if body.model_ref is not None and body.definition is not None:
        # When both target forms are present, silently picking one would send the paid request to the target the
        # caller did not choose.
        # 两种目标形态并存时静默取其一会让付费请求打到调用方没选中的那个目标上。
        raise BadRequestError("endpoint_test_definition_and_model_ref_exclusive")
    if body.model_ref is not None:
        target, credentials, definition, path_overrides = await _model_ref_target(body.model_ref, session, resolver, _t)
        # The model recorded in the result body must be the same one actually executed and billed: the model_ref path
        # follows the model row and overrides the model carried separately in parameters.
        # 结果体记录的模型与实际执行、记账的必须是同一个：model_ref 路径以模型行为准，
        # 覆盖 parameters 里独立携带的 model。
        parameters = replace(parameters, model=body.model_ref.model_id)
    elif body.definition is not None:
        path_overrides = NO_PATH_OVERRIDES
        definition = _accepted_definition(body.definition, _t, mode=EndpointTestMode.TRIAL_RUN)
        support = _support(definition)
        require_base_url, require_api_key = support.credential_needs(definition)
        credentials = await _required_credentials(
            body.credentials, session, require_base_url=require_base_url, require_api_key=require_api_key
        )
        target = support.trial_target(definition, credentials, parameters)
    else:
        raise BadRequestError("endpoint_test_definition_or_model_ref_required")

    preview_payload = _preview_payload(definition, parameters, credentials, assets, _t, path_overrides)
    # The temp directory is registered here as soon as it is created in the thread: if cancellation lands during the
    # media-writing step the return value is lost, and this is what lets it be reclaimed.
    # 临时目录在线程里建好就登记进来：取消落在落素材那一程时返回值会丢，靠它回收。
    staged: list[Path] = []
    try:
        persisted = await run_sync_transaction(_stage_assets, assets, staged)
        run = await manager.start(
            target,
            parameters,
            assets=persisted,
            staging=staged[0],
            request_preview=preview_payload,
        )
    except TrialRunBusyError as exc:
        await run_sync_transaction(_remove_staging, staged)
        raise ConflictError("trial_run_already_running") from exc
    except BaseException:
        # If it fails before the manager takes over (writing media, creating the result directory), the temp directory
        # has no owner yet and nobody would come back to delete it from disk.
        # manager 接手前失败（落素材、建结果目录）时临时目录还没有主人，留在盘上没人会回来删。
        await run_sync_transaction(_remove_staging, staged)
        raise
    return _run_response(run, _t, names)


@router.get("/trial-runs/{run_id}")
async def get_trial_run(
    run_id: str,
    _t: Translator,
    names: DisplayNamesCatalog,
    manager: TrialRunManager = Depends(get_trial_run_manager),
) -> TrialRunResponse:
    """读一次测试连接。运行中读内存、终态读盘；取消或被重启打断的 run 不留结果。"""
    run = await asyncio.to_thread(manager.get, run_id)
    if run is None:
        raise NotFoundError("trial_run_not_found")
    return _run_response(run, _t, names)


@router.post("/trial-runs/{run_id}/cancel", status_code=204)
async def cancel_trial_run(
    run_id: str,
    manager: TrialRunManager = Depends(get_trial_run_manager),
) -> Response:
    """停本地轮询，记账按失败结算。远端由 backend 的取消路径自己叫停，停不下来时照跑、钱照花。"""
    if not await manager.cancel(run_id):
        raise NotFoundError("trial_run_not_found")
    return Response(status_code=204)


@router.get("/trial-runs/{run_id}/artifact")
async def get_trial_run_artifact(
    run_id: str,
    manager: TrialRunManager = Depends(get_trial_run_manager),
) -> FileResponse:
    """播放该测试连接生成的产物。

    MIME 按落盘字节的文件头给，不按文件名：产物名是本层起的（视频 ``artifact.mp4``、图像
    ``artifact.png``），而 ComfyUI 那一侧导出的容器可以是同族里的另一个，按名字声明会让浏览器
    按一个错误的类型解。
    """
    located = await asyncio.to_thread(_locate_artifact, manager, run_id)
    if located is None:
        raise NotFoundError("trial_run_artifact_not_found")
    path, media_type = located
    return FileResponse(path, media_type=media_type)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _previewed(section: Any) -> PreviewedRequestResponse:
    return PreviewedRequestResponse(method=section.method, url=section.url, headers=section.headers, body=section.body)


def _run_response(run: TrialRun, translate: Translator, names: DisplayNames) -> TrialRunResponse:
    """Localize the result body: the failure reason is rendered in the current language, and the failure code and its
    follow-up action are given separately in original form.

    Code and action are given together instead of letting the reading side infer them from the text: the text varies
    by language, while "which kind of failure this is and what to do" branches on the code. The action comes from
    :func:`problem_from_task_failure`: the same failure code should give the same follow-up action on the project
    page and here, and writing it separately in two places would inevitably diverge (three of ComfyUI's eight codes
    point to retry rather than configuring the provider). Raw exception text that cannot be mapped to a code has
    neither.

    结果体本地化：失败原因渲染成当前语言，失败码与它的后续动作另给一份原文。

    码与动作一并给出，而不是让读侧从文案里认：文案随语言变，而「这是哪一类失败、该去做什么」
    按码分流。动作取自 :func:`problem_from_task_failure`——同一条失败码在项目页与这里该给出
    同一个后续动作，两处各写一份必然分叉（ComfyUI 的八条码里就有三条指向重试而非配置供应商）。
    编不出码的裸异常文本两者皆无。
    """
    payload = run.to_payload()
    parsed = parse_failure(run.error)
    payload["error"] = render_failure(run.error, translate, names)
    payload["error_code"] = parsed[0] if parsed else None
    payload["error_action"] = problem_from_task_failure(run.error).action.value if parsed else None
    return TrialRunResponse(**payload)


def _has_usable_credentials(meta: ProviderMeta, config: Mapping[str, str]) -> bool:
    """Whether the credentials are enough for assembly. Grouping semantics match the credential form: if
    credential_groups are declared, one of them is required; otherwise everything in required ∩ secret is mandatory.
    When neither yields a non-empty group (e.g. vertex, which uses a file upload), no gate is set here.

    凭证够不够装配用。分组语义与凭证表单一致：声明了 credential_groups 按二选一，否则
    required ∩ secret 全部必填；两者都给不出非空分组（如走文件上传的 vertex）时不在此设闸。"""
    groups = [
        group
        for group in (meta.credential_groups or [[key for key in meta.required_keys if key in meta.secret_keys]])
        if group
    ]
    return not groups or any(all(config.get(key) for key in group) for group in groups)


async def _model_ref_target(
    model_ref: ModelRefInput,
    session: AsyncSession,
    resolver: ConfigResolver,
    _t: Translator,
) -> tuple[TrialRunTarget, EndpointTestCredentials | None, dict[str, Any] | None, PathOverrides]:
    """Resolve the target from the model row. When it resolves to a declarative definition, the credentials are fetched
    along with it so the result body has both the request and extraction sections.

    A custom provider's model row also carries its effective path overrides (``docs/adr/0101``), so the rendered
    request section matches what is actually sent.

    按模型行解析目标。解析出声明式定义时连凭证一起取出，好让结果体有请求与提取两段。

    自定义供应商的模型行还带出它生效的路径覆盖（``docs/adr/0101``），渲染请求段与真发一致。
    """
    if not is_custom_provider(model_ref.provider_id):
        # Built-in providers must also check the model row first: an unregistered model would make the assembly layer
        # fall back to that provider's default video model, and a registered non-video model (e.g. a text tier) would
        # be dispatched to the video endpoint; both send a paid call to a different model.
        # 内置供应商同样要先核模型行：未登记的 model 会让装配层落到该供应商的默认视频模型，
        # 已登记的非视频 model（如文本档）则会被派发到视频端点——两种都会付费打给另一个模型。
        info = catalog.get_model(model_ref.provider_id, model_ref.model_id)
        if info is None:
            raise NotFoundError("model_not_found")
        if info.media_type != "video":
            raise BadRequestError("endpoint_test_model_unavailable")
        # Credentials are also checked on the request thread: when credentials are missing the assembly layer raises
        # Chinese sentences written by each backend, and since it runs in a background task detached from the request,
        # they would end up verbatim in the error field of the result body.
        # 凭证也在请求线程上核：装配层缺凭证时抛的是各 backend 自己写的中文句子，而它跑在
        # 脱离请求的后台任务里，会原样进结果体的 error 字段。
        config = await resolver.provider_config(model_ref.provider_id)
        if not _has_usable_credentials(PROVIDER_REGISTRY[model_ref.provider_id], config):
            raise BadRequestError("missing_credentials")
        # When a built-in model row uses declarative assembly, the two diagnostic sections (rendered request,
        # per-stage extraction) and the pre-submit render gate are provided as well: the definition and credentials
        # fall back to the same ones as assembly; for rows implemented by a Python backend both sections are left
        # empty.
        # 内置模型行走声明式装配时，诊断两段（渲染请求、逐阶段提取）与提交前渲染闸照给：
        # 定义与凭证回落取装配同一份，Python backend 实现的行两段留空。
        definition = None
        credentials = None
        diagnostics = builtin_declarative_video_diagnostics(model_ref.provider_id, model_ref.model_id, config)
        if diagnostics is not None:
            builtin_definition, base_url, api_key = diagnostics
            definition = _accepted_definition(dict(builtin_definition), _t, mode=EndpointTestMode.TRIAL_RUN)
            credentials = EndpointTestCredentials(base_url=base_url, api_key=api_key)
        target = model_ref_target(model_ref.provider_id, model_ref.model_id, resolver=resolver, definition=definition)
        return target, credentials, definition, NO_PATH_OVERRIDES
    provider = await _custom_provider(model_ref.provider_id, session)
    model = await CustomProviderRepository(session).get_model_by_ids(provider.id, model_ref.model_id)
    if model is None:
        raise NotFoundError("model_not_found")
    # Checking existence is not enough: a disabled row or one for a non-video endpoint would make the assembly layer
    # fall back to the provider's default video model; the paid test would hit and bill a different model while the
    # result body still shows the requested row.
    # 只查存在不够：已禁用或非视频端点的行会让装配层回退到该供应商的默认视频模型——
    # 付费测试打到并计费给另一个模型，而结果体仍标着请求的那一行。
    try:
        endpoint_spec = await resolve_endpoint_spec(model.endpoint, CustomEndpointRepository(session).get)
    except ValueError:
        endpoint_spec = None
    if not model.is_enabled or endpoint_spec is None or endpoint_spec.media_type not in _TESTABLE_MEDIA_TYPES:
        raise BadRequestError("endpoint_test_model_unavailable")
    # Credential configuration errors can be judged on the request thread and are rejected with a translated 400: with
    # base_url missing the assembly layer would only raise an untranslatable Chinese sentence, inside a background
    # task detached from the request, ending up verbatim in the error field of the result body; with api_key missing
    # the request would go out with an empty auth header, paying for a call that is bound to be rejected.
    # 凭证配置错误在请求线程上就能判，按翻译过的 400 拒掉：缺 base_url 时装配层只会抛一句
    # 不可翻译的中文，且落在脱离请求的后台任务里、原样进结果体的 error 字段；缺 api_key 时
    # 请求会带着空鉴权头发出去，付费打一个注定被拒的调用。
    if endpoint_spec.definition is not None:
        require_base_url, require_api_key = _support(endpoint_spec.definition).credential_needs(
            endpoint_spec.definition
        )
        if require_base_url and not provider.base_url:
            raise BadRequestError("endpoint_test_provider_base_url_required")
        if require_api_key and not provider.api_key:
            raise BadRequestError("missing_credentials")
    definition = None
    if endpoint_spec.definition is not None:
        # Built-in declarative endpoints (newapi-video / v2 / minimax family) have definitions just like ce-*: the
        # rendered request, per-stage extraction and pre-submit render gate of the result body are given along the
        # same path, so no diagnostic section goes missing just because the definition ships with the release.
        # 内置声明式端点（newapi-video / v2 / minimax 系）与 ce-* 一样有定义：结果体的渲染请求、
        # 逐阶段提取与提交前渲染闸按同一条路给出，不因定义随版发布而缺一段诊断。
        definition = _accepted_definition(dict(endpoint_spec.definition), _t, mode=EndpointTestMode.TRIAL_RUN)
    credentials = EndpointTestCredentials(base_url=provider.base_url, api_key=provider.api_key)
    target = model_ref_target(
        model_ref.provider_id,
        model_ref.model_id,
        resolver=resolver,
        definition=definition,
        media_type=endpoint_spec.media_type,
    )
    return target, credentials, definition, effective_path_overrides(model.path_overrides, endpoint_spec.requests)


def _preview_payload(
    definition: Mapping[str, Any] | None,
    parameters: EndpointTestParameters,
    credentials: EndpointTestCredentials | None,
    assets: EndpointTestAssets,
    _t: Translator,
    path_overrides: PathOverrides = NO_PATH_OVERRIDES,
) -> dict[str, Any] | None:
    """The "rendered request" section of the result body.

    Also serves as the pre-submit render gate: the template is rendered once before actually sending, so errors like
    a missing placeholder value return 422 right away instead of failing only once the background task runs, when
    the user is already waiting on a run that is bound to fail. Left empty when there is no definition to render
    (endpoints implemented in Python) or when this kind does not provide this section (ComfyUI, see
    ``KindTestSupport``).

    结果体里的「渲染请求」段。

    顺带充当提交前的渲染闸：模板在真发之前就渲一次，占位符缺值这类错误当场回 422，而不是等
    后台任务跑起来才失败——那时用户已经在等一个注定失败的 run。没有定义可渲（Python 实现的
    端点）、或这种 kind 不给这一段（ComfyUI，见 ``KindTestSupport``）时留空。
    """
    if definition is None or credentials is None:
        return None
    support = _support(definition)
    if not support.trial_run_request_preview:
        return None
    try:
        preview = support.preview(
            definition,
            parameters,
            credentials=credentials,
            assets=assets,
            placeholder_missing_assets=False,
            path_overrides=path_overrides,
        )
    except EndpointTestDefinitionError as exc:
        raise _invalid(exc.diagnostics, _t) from exc
    return {
        "method": preview.submit.method,
        "url": preview.submit.url,
        "headers": preview.submit.headers,
        "body": preview.submit.body,
    }


def _locate_artifact(manager: TrialRunManager, run_id: str) -> tuple[Path, str] | None:
    path = manager.artifact_path(run_id)
    return None if path is None else (path, artifact_media_type(path))


def _stage_assets(assets: EndpointTestAssets, staged: list[Path]) -> dict[str, Path | list[Path] | None]:
    staging = Path(tempfile.mkdtemp(prefix="arcreel-trial-run-"))
    staged.append(staging)
    return _persist_assets(assets, staging)


def _remove_staging(staged: list[Path]) -> None:
    for staging in staged:
        shutil.rmtree(staging, ignore_errors=True)  # hard-delete: temp -- mkdtemp staging of the test run / 试跑的 mkdtemp 暂存


def _persist_assets(assets: EndpointTestAssets, staging: Path) -> dict[str, Path | list[Path] | None]:
    """Write media into a temp directory: the backend takes file paths, while the uploaded content is only in memory
    right now. The directory is cleaned up when the run reaches a final state.

    素材落到临时目录：backend 收的是文件路径，而上传内容此刻只在内存里。目录随 run 终态清掉。"""
    persisted: dict[str, Path | list[Path] | None] = {}
    for source, is_list in ASSET_SOURCES.items():
        if is_list:
            persisted[source] = [
                _write_asset(staging, f"{source}_{index}", item) for index, item in enumerate(assets.items(source))
            ]
            continue
        single = assets.single(source)
        persisted[source] = _write_asset(staging, source, single) if single is not None else None
    return persisted


def _write_asset(staging: Path, name: str, asset: AssetData) -> Path:
    suffix = mimetypes.guess_extension(asset.mime_type) or ".bin"
    path = staging / f"{uuid.uuid4().hex}_{name}{suffix}"
    path.write_bytes(asset.content)
    return path
