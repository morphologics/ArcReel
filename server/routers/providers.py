"""
Provider configuration management API.

Provides endpoints for listing providers, reading/writing a single provider's configuration, and connectivity
checks.

供应商配置管理 API。

提供供应商列表查询、单个供应商配置读写和连通性检查端点。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from arcreel_market_core.video_backend_contract import VideoAudioMode
from lib.backends.backend_assembly.specs import builtin_video_capabilities_for_model
from lib.backends.gemini_shared import VERTEX_SCOPES
from lib.backends.http_status_errors import raise_for_status_redacted
from lib.billing.multipliers import (
    PROVIDER_MULTIPLIER_KEY,
    get_default_provider_multiplier,
    get_provider_multiplier,
    set_provider_multiplier,
)
from lib.catalog import query as catalog
from lib.catalog.errors import ModelNotInCatalogError
from lib.config.registry import PROVIDER_REGISTRY
from lib.config.repository import mask_secret
from lib.config.resolver import (
    ConfigResolver,
    VideoBucketCapabilityError,
    VoiceConsistency,
    builtin_video_audio_track,
    derive_voice_consistency,
)
from lib.config.service import ConfigService, ProviderConfigValueError
from lib.config.url_utils import normalize_base_url
from lib.custom_provider import is_custom_provider, parse_provider_id
from lib.db import async_session_factory, get_async_session
from lib.db.base import dt_to_iso
from lib.db.repositories.credential_repository import CredentialRepository
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.generation.video_request_facts import ResolutionOverride, VideoRequestFactsError
from lib.i18n import render_message, translate_or
from lib.infra.api_errors import BadRequestError, UnprocessableError
from lib.infra.async_thread import run_sync_transaction
from lib.infra.data_root_layout import DataRootLayout
from server.dependencies import get_config_service
from server.i18n import Locale, Translator, load_display_names_or_builtin
from server.routers._validators import split_video_backend_query
from server.services.tasks.video_caps import capability_request_facts, duration_constraints_payload

if TYPE_CHECKING:
    from lib.db.models.credential import ProviderCredential

logger = logging.getLogger(__name__)

MAX_VERTEX_CREDENTIALS_BYTES = 1024 * 1024  # 1 MiB

router = APIRouter(prefix="/providers", tags=["Providers"])

_CREDENTIAL_KEYS = frozenset({"api_key", "credentials_path", "base_url", "access_key", "secret_key"})

# ---------------------------------------------------------------------------
# Field metadata mapping (key -> label/type/placeholder)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 字段元数据映射（key → label/type/placeholder）
# ---------------------------------------------------------------------------

_FIELD_META: dict[str, dict[str, str]] = {
    "api_key": {"label": "API Key", "type": "secret"},
    "access_key": {"label": "Access Key", "type": "secret"},
    "secret_key": {"label": "Secret Key", "type": "secret"},
    "base_url": {"label": "Base URL", "type": "url", "placeholder": "Default"},
    "wan3_base_url": {"label": "Wan 3.0 Base URL", "type": "url", "placeholder": "Default"},
    "credentials_path": {"label": "Vertex Credentials Path", "type": "text"},
    "gcs_bucket": {"label": "GCS Bucket", "type": "text"},
    "image_rpm": {"label": "Image RPM", "type": "number"},
    "video_rpm": {"label": "Video RPM", "type": "number"},
    "request_gap": {"label": "Request Gap (sec)", "type": "number"},
    "image_max_workers": {"label": "Image Max Workers", "type": "number"},
    "video_max_workers": {"label": "Video Max Workers", "type": "number"},
    "audio_max_workers": {"label": "Audio Max Workers", "type": "number"},
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------


class ModelInfoResponse(BaseModel):
    display_name: str
    media_type: str
    capabilities: list[str]
    default: bool
    # Linked duration constraints (resolution<->duration, reference images<->duration) are not exposed in the catalog:
    # the narrowed result is computed per context by /providers/video-capabilities and
    # /projects/{name}/video-capabilities and returned to the frontend; the rules live only in lib.config.resolver.
    # 时长联动约束（分辨率↔时长、参考图↔时长）不在目录里透出：收窄结果由
    # /providers/video-capabilities 与 /projects/{name}/video-capabilities 按上下文算好回给
    # 前端，规则只在 lib.config.resolver 一处。
    supported_durations: list[int] = []
    resolutions: list[str] = []
    # Final-cut audio track shape (``VideoAudioMode``: controllable / always with audio / always silent), one per
    # execution path -- the same model can have different request shapes on the image-to-video and reference-to-video
    # sub-paths (Kling v3-omni's multi-image subject sub-path has no audio track switch). The settings page takes the
    # field of the currently effective bucket to decide whether the switch is interactive, and capability line
    # rendering treats "not always silent" as having an audio track; the frontend does not interpret capabilities
    # tokens or synthesize the three states itself. The source of truth is the backend's VideoCapabilities
    # (lib.config.resolver.builtin_video_audio_track); non-video models are always always_off.
    # 成片音轨形态（``VideoAudioMode``：可控 / 恒有声 / 恒无声），按执行路径各给一份——同一
    # model 在图生与参考生两条子路径上的请求形态可以不同（可灵 v3-omni 的多图主体子路径不带
    # 音轨开关）。设置页按当前生效的桶取对应字段决定开关是否可交互，能力线渲染按「非恒无声即
    # 有音轨」判定；前端不解读 capabilities token，也不自行合成三态。
    # 真相源是 backend 的 VideoCapabilities（lib.config.resolver.builtin_video_audio_track）；
    # 非视频 model 恒 always_off。
    audio_track: str = VideoAudioMode.ALWAYS_OFF.value
    reference_route_audio_track: str = VideoAudioMode.ALWAYS_OFF.value
    # Voice consistency level without project context (global settings "Model selection"): generation_mode is unknown,
    # so it is derived along the non-reference-to-video path, and native is always downgraded. Derivation goes through
    # the single formula lib.config.resolver.derive_voice_consistency, and the frontend only reads the enum. With
    # project context /projects/{name}/video-capabilities is used instead (same function, with the real
    # generation_mode), and this field does not take part.
    # 无项目上下文（全局设置「模型选择」）下的声音一致性档位：generation_mode 未知，故按非参考
    # 生视频路径派生，native 恒降格。派生走 lib.config.resolver.derive_voice_consistency 这唯一
    # 一处公式，前端只读枚举。有项目上下文时改用 /projects/{name}/video-capabilities（同一函数、
    # 带真实 generation_mode），本字段不参与。
    voice_consistency: VoiceConsistency = "none"


class ProviderSummary(BaseModel):
    id: str
    display_name: str
    description: str
    status: str
    media_types: list[str]
    capabilities: list[str]
    # Number of credentials (called "keys" in the UI); 0 when there are none. / 凭证（界面称「密钥」）条数；没有凭证时为 0。
    credential_count: int
    models: dict[str, ModelInfoResponse]


class ProvidersListResponse(BaseModel):
    providers: list[ProviderSummary]


class FieldInfo(BaseModel):
    key: str
    label: str
    type: str
    required: bool
    is_set: bool
    value: str | None = None
    value_masked: str | None = None
    placeholder: str | None = None


class CredentialSecretField(BaseModel):
    """凭证表单需渲染的 secret 输入字段（按 provider 的 required ∩ secret ∩ 凭证键派生）。

    驱动设置页凭证表单渲染：单 secret provider 给 ``[api_key]``（见 ADR 0037），可灵给
    ``[api_key, access_key, secret_key]``（二选一分组见 ``secret_field_groups``）。
    key 名全程同名，前端据此读写各字段。
    """

    key: str
    label: str


class ProviderConfigResponse(BaseModel):
    id: str
    display_name: str
    description: str
    status: str
    media_types: list[str]
    fields: list[FieldInfo]
    # Whether this provider's credentials accept a custom base_url (source of truth: optional_keys contains base_url).
    # base_url goes with the credential and not into fields; the frontend uses this to decide whether to render a URL
    # input in the key form.
    # 该供应商凭证是否接受自定义 base_url（真相源：optional_keys 含 base_url）。
    # base_url 随凭证走、不进 fields，前端据此决定是否在密钥表单渲染 URL 输入。
    supports_base_url: bool
    # Secret fields the credential form should render (ordered; source of truth: registry required_keys ∩ secret_keys
    # ∩ credential keys).
    # 凭证表单应渲染的 secret 字段（有序，真相源：registry required_keys ∩ secret_keys ∩ 凭证键）。
    secret_fields: list[CredentialSecretField]
    # Credential "either-or" groups: the frontend uses them to validate "all fields of at least one group are filled"
    # rather than "all fields are filled" (source of truth: registry credential_groups; with an empty declaration the
    # router falls back to [all secret_fields], equivalent to the pre-migration "all required" semantics).
    # 凭证「二选一」分组：前端据此校验「至少一组的字段全部填写」而非「全部字段都填写」
    # （真相源：registry credential_groups；空声明时 router 回退为 [全部 secret_fields]，
    # 与迁移前「全部必填」语义等价）。
    secret_field_groups: list[list[str]]


class ConnectivityCheckResponse(BaseModel):
    success: bool
    available_models: list[str]
    message: str


class CredentialResponse(BaseModel):
    id: int
    provider: str
    name: str
    api_key_masked: str | None = None
    credentials_filename: str | None = None
    base_url: str | None = None
    # Each field is redacted independently (the two parts are not treated as one secret); always None except for Kling
    # (see ADR 0037).
    # 逐字段独立脱敏（不把两段当一个 secret）；除可灵外恒为 None（见 ADR 0037）。
    access_key_masked: str | None = None
    secret_key_masked: str | None = None
    is_active: bool
    created_at: str


class CredentialListResponse(BaseModel):
    credentials: list[CredentialResponse]


def _stripped(v: str | None) -> str | None:
    """Trim surrounding whitespace from credential string inputs.

    Pasted keys often carry stray leading/trailing whitespace or newlines that
    silently break auth; normalizing at the API boundary covers the frontend and
    any direct/third-party caller. Unset fields keep their None default (the
    validator runs only on provided values), so PATCH preserve-semantics — an
    omitted secret leaves the stored value untouched — are unaffected.
    """
    return v.strip() if isinstance(v, str) else v


_StrippedStr = Annotated[str, AfterValidator(_stripped)]
_StrippedOptStr = Annotated[str | None, AfterValidator(_stripped)]


class ProviderMultiplierResponse(BaseModel):
    """供应商倍率：``multiplier`` 是生效值，``default`` 是没有设置时的值。"""

    multiplier: float
    default: float


class ProviderMultiplierUpdate(BaseModel):
    """设置供应商倍率；``null`` 表示恢复默认值。"""

    multiplier: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None


class CreateCredentialRequest(BaseModel):
    name: _StrippedStr
    api_key: _StrippedOptStr = None
    base_url: _StrippedOptStr = None
    access_key: _StrippedOptStr = None
    secret_key: _StrippedOptStr = None


class UpdateCredentialRequest(BaseModel):
    name: _StrippedOptStr = None
    api_key: _StrippedOptStr = None
    base_url: _StrippedOptStr = None
    access_key: _StrippedOptStr = None
    secret_key: _StrippedOptStr = None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _validate_provider(provider_id: str, _t: Callable[..., str]) -> None:
    """Verify the provider ID exists; raise 404 if not.

    验证供应商 ID 是否存在，不存在则抛 404。"""
    if provider_id not in PROVIDER_REGISTRY:
        raise HTTPException(status_code=404, detail=_t("unknown_provider", value=provider_id))


def _submitted_secret_values(
    provider_id: str, body: CreateCredentialRequest | UpdateCredentialRequest
) -> dict[str, str | None]:
    """Take from the request body the key fields used to decide a credential group switch; shared by create/update so
    the field set stays consistent.

    Filtered dynamically by the `secret_keys` the provider declares in `PROVIDER_REGISTRY` rather than a hard-coded
    exclusion list, from the same declaration source as `credential_groups`. Note: when adding a credential field,
    besides registering it in `secret_keys` in the registry, the matching field must still be added to
    `CreateCredentialRequest`/`UpdateCredentialRequest` -- keys without a field are silently ignored by the
    `hasattr` filter.

    从请求体取出参与凭证组切换判定的密钥字段，供 create/update 共用以保持字段集一致。

    按 provider 在 `PROVIDER_REGISTRY` 中声明的 `secret_keys` 动态过滤，而非硬编码排除列表，
    与 `credential_groups` 同源、同步声明驱动。注：新增凭证字段时，除在注册表登记 `secret_keys`
    外，仍需同步给 `CreateCredentialRequest`/`UpdateCredentialRequest` 加对应字段——未加字段的
    key 会被 `hasattr` 过滤静默忽略。
    """
    meta = PROVIDER_REGISTRY[provider_id]
    return {k: getattr(body, k, None) for k in meta.secret_keys if hasattr(body, k)}


def _resolve_credential_group_switch(
    provider_id: str,
    submitted: Mapping[str, str | None],
    current: Mapping[str, str | None] | None,
    _t: Callable[..., str],
) -> set[str]:
    """Decide whether a single submission triggers a credential group switch, and return the set of fields of other
    groups to clear.

    The switch is driven by the ``ProviderMeta.credential_groups`` declaration:
    - when the non-empty fields of one submission span several mutually exclusive groups, the intent cannot be
      determined, so it is rejected with an error (to avoid silently dropping values the user entered);
    - when it fully covers a group that was not already complete in the stored values, it counts as "switching into"
      that group, and all fields of the other declared groups are returned for the caller to clear -- removing
      leftovers of the old group that would silently keep the old mode;
    - when the fully covered group was already complete in the stored values (routine rotation of that group on an
      existing row where both coexist), it is not a switch and the other group is not cleared, to avoid wiping a
      dormant credential the user did not touch;
    - when no group is fully covered (including the vast majority of providers that declare no credential_groups),
      an empty set is returned, keeping the retain semantics of partial updates.

    ``current`` passes the stored values for the "switching into" decision above; the create endpoint has no stored
    values and passes None (a new row has nothing to clear and only reuses this function's spanning-multiple-groups
    rejection check).

    判定单次提交是否触发凭证切组，返回需清空的其它组字段集合。

    切组由 ``ProviderMeta.credential_groups`` 声明驱动：
    - 单次提交的非空字段横跨多个互斥组时无法判断意图，报错拒绝（避免静默丢弃用户填入的值）；
    - 完整覆盖某一组、且该组在库内原值中原本未完整存在时，视为「切入」该组，返回其它已声明组
      的全部字段供调用方清空——消除旧组残留导致的静默沿用旧模式；
    - 完整覆盖的组在库内原本就已完整存在（存量共存行上例行轮换该组），则不算切组、不清空另一组，
      避免清掉用户未触碰的休眠凭证；
    - 未完整覆盖任何组（含未声明 credential_groups 的绝大多数 provider）时返回空集合，维持部分
      更新的保留语义。

    ``current`` 传入库内原值用于上述「切入」判定；创建端点无原值传 None（新行无可清空项，仅复用
    本函数的横跨多组拒绝校验）。
    """
    meta = PROVIDER_REGISTRY[provider_id]
    groups = meta.credential_groups
    if not groups:
        return set()
    touched = [g for g in groups if any(submitted.get(k) for k in g)]
    if len(touched) > 1:
        raise HTTPException(status_code=422, detail=_t("credential_group_ambiguous"))
    covered = meta.fully_covered_credential_groups(submitted)
    if not covered:
        return set()
    target = covered[0]
    if current is not None:
        pre_covered = {frozenset(g) for g in meta.fully_covered_credential_groups(current)}
        if frozenset(target) in pre_covered:
            return set()
    target_keys = set(target)
    return {k for group in groups for k in group if k not in target_keys}


async def _get_credential_or_404(
    repo: CredentialRepository,
    provider_id: str,
    cred_id: int,
    _t: Callable[..., str],
) -> ProviderCredential:
    """Fetch the credential and check ownership; raise 404 if it does not exist.

    获取凭证并校验归属，不存在则抛 404。"""
    cred = await repo.get_by_id(cred_id)
    if not cred or cred.provider != provider_id:
        raise HTTPException(status_code=404, detail=_t("credentials_not_found"))
    return cred


def _cred_to_response(cred: ProviderCredential) -> CredentialResponse:
    return CredentialResponse(
        id=cred.id,
        provider=cred.provider,
        name=cred.name,
        api_key_masked=mask_secret(cred.api_key) if cred.api_key else None,
        credentials_filename=Path(credentials_file).name if (credentials_file := cred.credentials_file()) else None,
        base_url=cred.base_url,
        access_key_masked=mask_secret(cred.access_key) if cred.access_key else None,
        secret_key_masked=mask_secret(cred.secret_key) if cred.secret_key else None,
        is_active=cred.is_active,
        created_at=dt_to_iso(cred.created_at) or "",
    )


async def _invalidate_caches(request: Request) -> None:
    from server.services.tasks.generation_context import invalidate_backend_cache

    invalidate_backend_cache()
    worker = getattr(request.app.state, "generation_worker", None)
    if worker:
        await worker.reload_limits()


def _build_field(
    key: str,
    required: bool,
    db_entry: dict[str, Any] | None,
) -> FieldInfo:
    """Build a FieldInfo from the key, whether it is required, and the entry fetched from the DB.

    根据 key、是否必填 and DB 取出的条目，构建 FieldInfo。"""
    meta = _FIELD_META.get(key, {"label": key, "type": "text"})
    is_set = db_entry is not None and db_entry.get("is_set", False)

    field: dict[str, Any] = {
        "key": key,
        "label": meta["label"],
        "type": meta["type"],
        "required": required,
        "is_set": is_set,
    }

    if "placeholder" in meta:
        field["placeholder"] = meta["placeholder"]

    if db_entry is not None and is_set:
        if meta["type"] == "secret":
            field["value_masked"] = db_entry.get("masked", "••••")
        else:
            field["value"] = db_entry.get("value", "")
    else:
        if meta["type"] == "secret":
            field["value_masked"] = None
        else:
            field["value"] = ""

    return FieldInfo(**field)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def _video_reference_audio_mode(provider_id: str, model_id: str) -> str:
    """Reference audio transport shape of this video model; non-video providers / undeclared are always "none".

    `VideoCapabilities` is the backend-side declaration (the single source of truth), and the catalog's model info
    does not keep a second copy -- consistent with `lib/config/resolver.py::_resolve_video_caps_for_model`. The
    router layer (not the lib package) is not bound by the lib.config layering contract and may read the backend
    declaration directly.

    该视频 model 的参考音频运输形态；非视频 provider / 未声明一律 "none"。

    `VideoCapabilities` 是 backend 侧声明（唯一真相源），目录的模型信息不复制第二份——
    与 `lib/config/resolver.py::_resolve_video_caps_for_model` 的做法一致。router 层（非
    lib 包）不受 lib.config 分层契约约束，可直接读 backend 声明。
    """
    try:
        caps = builtin_video_capabilities_for_model(provider_id, model_id)
    except ValueError:
        return "none"
    return caps.reference_audio_mode.value


@router.get("", response_model=ProvidersListResponse)
async def list_providers(
    _t: Translator,
    locale: Locale,
    svc: Annotated[ConfigService, Depends(get_config_service)],
) -> ProvidersListResponse:
    """返回所有供应商及其状态。"""
    statuses = await svc.get_all_providers_status()
    providers = []
    for s in statuses:
        models: dict[str, ModelInfoResponse] = {}
        for mid, minfo in (s.models or {}).items():
            silent = VideoAudioMode.ALWAYS_OFF.value
            audio_track = builtin_video_audio_track(s.name, mid, generation_type="i2v") or silent
            reference_route_audio_track = builtin_video_audio_track(s.name, mid, generation_type="r2v") or silent
            reference_audio_mode = (
                _video_reference_audio_mode(s.name, mid) if minfo.get("media_type") == "video" else "none"
            )
            models[mid] = ModelInfoResponse(
                **minfo,
                audio_track=audio_track,
                reference_route_audio_track=reference_route_audio_track,
                # generation_mode / character_voice_binding are both None: the catalog endpoint has no project
                # context, so it derives along the non-reference-to-video path, and the audio track bit also comes
                # from the i2v path -- with project context, /projects/{name}/video-capabilities is used instead.
                # generation_mode / character_voice_binding 均为 None：目录端点无项目上下文，按非
                # 参考生视频路径派生，音轨位同样取 i2v 路径——有项目上下文时改用
                # /projects/{name}/video-capabilities。
                voice_consistency=derive_voice_consistency(
                    reference_audio_mode=reference_audio_mode,
                    generation_mode=None,
                    has_audio=audio_track != silent,
                    character_voice_binding=None,
                ),
            )
        providers.append(
            ProviderSummary(
                id=s.name,
                # display_name from the registry / DB is the data source's original name; names with a translation
                # table are rendered in the request language, others (pure brand names, names custom providers typed
                # in by users) are returned as is.
                # registry / DB 的 display_name 是数据源原名；有译名表的按请求语言成文，
                # 没有的（纯品牌名、自定义供应商用户自填的名字）原样返回。
                display_name=translate_or(f"provider_name_{s.name}", s.display_name, locale),
                description=_t(f"provider_desc_{s.name}"),
                status=s.status,
                media_types=s.media_types,
                capabilities=s.capabilities,
                credential_count=s.credential_count,
                models=models,
            )
        )
    return ProvidersListResponse(providers=providers)


@router.get("/video-capabilities")
async def get_model_video_capabilities(
    _t: Translator,
    video_backend: Annotated[str, Query()],
    resolution: Annotated[str | None, Query()] = None,
    uses_reference_images: Annotated[bool, Query()] = False,
):
    """无项目上下文的视频模型能力：创建向导里项目尚不存在，按候选模型直接解析。

    与 `/projects/{name}/video-capabilities` 同一条桶能力闸和模型能力解析链路，
    只是没有项目可读：`default_duration` / `generation_mode` 等项目偏好为 None。
    `duration_constraints` 由候选模型所在桶（`uses_reference_images` 为真即 r2v，否则 i2v）的视频请求
    事实给出，`resolution` 作为「覆盖分辨率」参与求值（缺省即「自动」：自定义供应商取模型默认档，其余不带分辨率）。
    裸 provider 的补全与格式校验同项目端点。
    """
    provider_id, model_id = split_video_backend_query(video_backend)
    resolver = ConfigResolver(async_session_factory)
    generation_type = "r2v" if uses_reference_images else "i2v"
    candidate_project = {f"video_provider_{generation_type}": f"{provider_id}/{model_id}"}
    try:
        await resolver.resolve_video_backend(candidate_project, None, generation_type=generation_type)
        caps = await resolver.video_capabilities_for_model(provider_id, model_id, None, generation_type=generation_type)
        if (caps["provider_id"], caps["model"]) != (provider_id, model_id):
            raise BadRequestError("video_capability_reference_unavailable", provider=provider_id, model=model_id)
        caps["duration_constraints"] = duration_constraints_payload(
            await capability_request_facts(
                candidate_project,
                generation_type=generation_type,
                config_resolver=resolver,
                resolution_override=ResolutionOverride(resolution or None),
            )
        )
        return caps
    except VideoBucketCapabilityError as exc:
        raise BadRequestError(exc.code, **exc.params) from exc
    except VideoRequestFactsError as exc:
        # The issue code of video request facts is the errors catalog key (a ValueError subclass, must be caught
        # before it)
        # 视频请求事实的问题码即 errors 目录 key（ValueError 子类，须先于其捕获）
        raise UnprocessableError(exc.code, **exc.params) from exc
    except ModelNotInCatalogError:
        # The referenced model is not in the provider's list: let the app-level handler return a 400 asking to
        # reselect the model (a ValueError subclass, must be caught before it)
        # 引用的模型不在供应商清单中：交 app 级 handler 返回要求重新选择模型的 400（ValueError 子类，须先于其捕获）
        raise
    except ValueError as exc:
        # The raw exception text only goes to the log: str(exc) mixes in English technical details, and inserting it
        # into translated text would mix untranslated text into the en/vi UI
        # 异常原文只进日志：str(exc) 混英文技术细节，直接插进翻译文案会让 en/vi 界面混入未译原文
        logger.warning("视频模型 '%s' 能力解析失败: %s", video_backend, exc)
        raise HTTPException(
            status_code=422, detail=_t("video_model_capabilities_unresolved", value=video_backend)
        ) from exc


@router.get("/{provider_id}/config", response_model=ProviderConfigResponse)
async def get_provider_config(
    provider_id: str,
    _t: Translator,
    locale: Locale,
    session: AsyncSession = Depends(get_async_session),
) -> ProviderConfigResponse:
    """返回单个供应商的配置字段（registry 元数据与 DB 值合并）。"""
    _validate_provider(provider_id, _t)

    meta = PROVIDER_REGISTRY[provider_id]
    svc = ConfigService(session)
    db_values = await svc.get_provider_config_masked(provider_id)

    # Compute status: based on whether the credential table has an active credential / 计算状态：基于凭证表是否有生效凭证
    cred_repo = CredentialRepository(session)
    has_active = await cred_repo.has_active_credential(provider_id)
    status = "ready" if has_active else "unconfigured"

    # Build the field list: required first, then optional, skipping credential fields / 构建字段列表：先必填，再可选，跳过凭证字段
    fields: list[FieldInfo] = []
    fields.extend(
        _build_field(key, required=True, db_entry=db_values.get(key))
        for key in meta.required_keys
        if key not in _CREDENTIAL_KEYS
    )
    fields.extend(
        _build_field(key, required=False, db_entry=db_values.get(key))
        for key in meta.optional_keys
        if key not in _CREDENTIAL_KEYS
    )

    # Secret input fields of the credential form: required ∩ secret ∩ credential keys, keeping the required_keys
    # order. Single-secret provider -> [api_key] (see ADR 0037); Kling -> [api_key, access_key, secret_key], with
    # either-or grouping in secret_field_groups below.
    # 凭证表单的 secret 输入字段：required ∩ secret ∩ 凭证键，保留 required_keys 顺序。
    # 单 secret provider → [api_key]（见 ADR 0037）；可灵 → [api_key, access_key, secret_key]，
    # 二选一分组见下方 secret_field_groups。
    secret_keys = set(meta.secret_keys)
    secret_fields = [
        CredentialSecretField(key=key, label=_FIELD_META.get(key, {"label": key})["label"])
        for key in meta.required_keys
        if key in _CREDENTIAL_KEYS and key in secret_keys
    ]
    # An empty declaration (the vast majority of single-secret / dual-secret-AND providers) falls back to one required
    # group = all secret_fields, equivalent to the pre-migration "all required" semantics; only Kling declares
    # non-empty credential_groups (api_key single key / access_key+secret_key key pair, either-or). When secret_fields
    # is empty (currently only gemini-vertex, which takes the file upload branch without group validation), [[]] is
    # not synthesized -- consistent with the fail-fast semantics for empty groups in registry.py, so that a future
    # provider with "no secret fields but the regular credential form" does not get an always-true group that silently
    # bypasses validation.
    # 空声明（绝大多数单 secret / 双 secret-AND provider）回退为单一必填组 = 全部 secret_fields，
    # 与迁移前「全部必填」语义等价；仅可灵声明了非空 credential_groups（api_key 单键 /
    # access_key+secret_key 双键二选一）。secret_fields 为空时（当前仅 gemini-vertex，走文件
    # 上传分支不经过分组校验）不合成 [[]]——与 registry.py 的空分组 fail-fast 语义保持一致，
    # 避免未来新增"无 secret 字段但走普通凭证表单"的 provider 时该分组恒真、静默绕过校验。
    secret_field_groups = meta.credential_groups or ([[f.key for f in secret_fields]] if secret_fields else [])

    return ProviderConfigResponse(
        id=provider_id,
        display_name=translate_or(f"provider_name_{provider_id}", meta.display_name, locale),
        description=_t(f"provider_desc_{provider_id}"),
        status=status,
        media_types=catalog.provider_media_types(provider_id),
        fields=fields,
        supports_base_url="base_url" in meta.optional_keys,
        secret_fields=secret_fields,
        secret_field_groups=secret_field_groups,
    )


@router.patch("/{provider_id}/config", status_code=204)
async def patch_provider_config(
    provider_id: str,
    body: dict[str, str | None],
    request: Request,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """更新供应商配置。值为 null 表示删除该键。供应商倍率只经 ``PUT /{provider_id}/multiplier`` 修改。"""
    _validate_provider(provider_id, _t)
    if PROVIDER_MULTIPLIER_KEY in body:
        raise HTTPException(status_code=422, detail=_t("provider_multiplier_not_in_config"))

    svc = ConfigService(session)
    for key, value in body.items():
        if value is None:
            await svc.delete_provider_config(provider_id, key, flush=False)
        else:
            try:
                await svc.set_provider_config(provider_id, key, value, flush=False)
            except ProviderConfigValueError as exc:
                # Replace field names in error messages with the same labels as the UI (e.g. Image Max Workers),
                # matching the form display
                # 错误文案中的字段名换成 UI 同款 label（如 Image Max Workers），与表单展示一致
                params = dict(exc.params)
                meta = _FIELD_META.get(exc.key)
                if meta is not None:
                    params["field"] = meta["label"]
                raise HTTPException(status_code=422, detail=_t(exc.code, **params)) from exc

    await session.commit()

    # Refresh the cache and concurrency pool after a config change / 配置变更后刷新缓存和并发池
    await _invalidate_caches(request)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Provider multipliers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 供应商倍率
# ---------------------------------------------------------------------------


async def _validate_priced_provider(provider_id: str, session: AsyncSession, _t: Callable[..., str]) -> None:
    """Multipliers are available for built-in providers and existing custom providers; others raise 404.

    倍率对内置供应商与已存在的自定义供应商开放，其余抛 404。"""
    if is_custom_provider(provider_id):
        try:
            db_id = parse_provider_id(provider_id)
        except ValueError:
            db_id = None
        if db_id is None or await CustomProviderRepository(session).get_provider(db_id) is None:
            raise HTTPException(status_code=404, detail=_t("provider_not_found"))
        return
    _validate_provider(provider_id, _t)


@router.get("/{provider_id}/multiplier", response_model=ProviderMultiplierResponse)
async def get_provider_multiplier_setting(
    provider_id: str,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
) -> ProviderMultiplierResponse:
    """供应商倍率：乘在这个供应商全部调用的参考费用上。"""
    await _validate_priced_provider(provider_id, session, _t)
    return ProviderMultiplierResponse(
        multiplier=await get_provider_multiplier(session, provider_id),
        default=await get_default_provider_multiplier(session, provider_id),
    )


@router.put("/{provider_id}/multiplier", response_model=ProviderMultiplierResponse)
async def put_provider_multiplier_setting(
    provider_id: str,
    body: ProviderMultiplierUpdate,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
) -> ProviderMultiplierResponse:
    """设置供应商倍率，只影响之后结算的调用；已记下的费用不重算。"""
    await _validate_priced_provider(provider_id, session, _t)
    await set_provider_multiplier(session, provider_id, body.multiplier)
    await session.commit()
    return ProviderMultiplierResponse(
        multiplier=await get_provider_multiplier(session, provider_id),
        default=await get_default_provider_multiplier(session, provider_id),
    )


# ---------------------------------------------------------------------------
# Credential CRUD endpoints
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 凭证 CRUD 端点
# ---------------------------------------------------------------------------


@router.get("/{provider_id}/credentials", response_model=CredentialListResponse)
async def list_credentials(
    provider_id: str,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
) -> CredentialListResponse:
    _validate_provider(provider_id, _t)
    repo = CredentialRepository(session)
    creds = await repo.list_by_provider(provider_id)
    return CredentialListResponse(credentials=[_cred_to_response(c) for c in creds])


@router.post("/{provider_id}/credentials", status_code=201, response_model=CredentialResponse)
async def create_credential(
    provider_id: str,
    body: CreateCredentialRequest,
    request: Request,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
) -> CredentialResponse:
    _validate_provider(provider_id, _t)
    # Create is a brand-new row with nothing to clear -- it only reuses the spanning-multiple-groups ambiguity check
    # to reject contradictory submissions (the new row is not persisted).
    # 创建是全新行，无可清空——仅复用横跨多组的歧义校验拒绝矛盾提交（新行不落盘）。
    _resolve_credential_group_switch(provider_id, _submitted_secret_values(provider_id, body), None, _t)
    repo = CredentialRepository(session)
    cred = await repo.create(
        provider=provider_id,
        name=body.name,
        api_key=body.api_key,
        base_url=body.base_url,
        access_key=body.access_key,
        secret_key=body.secret_key,
    )
    await session.commit()
    await _invalidate_caches(request)
    return _cred_to_response(cred)


@router.patch("/{provider_id}/credentials/{cred_id}", status_code=204)
async def update_credential(
    provider_id: str,
    cred_id: int,
    body: UpdateCredentialRequest,
    request: Request,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    _validate_provider(provider_id, _t)
    repo = CredentialRepository(session)
    # First confirm the credential exists (404 takes priority over the later 422 for group-switch ambiguity), then use
    # the stored values for the "switching into" decision.
    # 先确认凭证存在（404 优先于后续切组歧义的 422），再用库内原值参与「切入」判定。
    cred = await _get_credential_or_404(repo, provider_id, cred_id, _t)
    submitted = _submitted_secret_values(provider_id, body)
    clear_keys = _resolve_credential_group_switch(
        provider_id,
        submitted,
        {k: getattr(cred, k, None) for k in submitted},
        _t,
    )
    kwargs: dict = {}
    if body.name is not None:
        kwargs["name"] = body.name
    if "api_key" in body.model_fields_set:
        kwargs["api_key"] = body.api_key
    if "base_url" in body.model_fields_set:
        kwargs["base_url"] = body.base_url
    if "access_key" in body.model_fields_set:
        kwargs["access_key"] = body.access_key
    if "secret_key" in body.model_fields_set:
        kwargs["secret_key"] = body.secret_key
    # Switching into a group: clear the fields of the other declared groups, removing "silently keeping the old mode".
    # The switch decision already excludes groups touched by the same submission, so values the user entered in that
    # submission are never overwritten.
    # 切入某组：清空其它已声明组的字段，消除"静默沿用旧模式"。切入判定已排除同一次提交触及的组，
    # 故不会覆盖用户在该次提交中填入的值。
    for key in clear_keys:
        kwargs[key] = None
    if kwargs:
        await repo.update(cred_id, **kwargs)
        await session.commit()
        if cred.is_active:
            await _invalidate_caches(request)
    return Response(status_code=204)


@router.delete("/{provider_id}/credentials/{cred_id}", status_code=204)
async def delete_credential(
    provider_id: str,
    cred_id: int,
    request: Request,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    _validate_provider(provider_id, _t)
    repo = CredentialRepository(session)
    cred = await _get_credential_or_404(repo, provider_id, cred_id, _t)
    # Saved before delete, so it is still accessible after the ORM object expires
    cred_path = cred.credentials_file()  # 在 delete 前保存，避免 ORM 对象过期后无法访问
    await repo.delete(cred_id)
    await session.commit()
    await _invalidate_caches(request)
    # Delete the associated credential file (such as the JSON under vertex_keys/), done after commit to keep data
    # consistent
    # 删除关联的凭证文件（如 vertex_keys/ 下的 JSON），放在 commit 之后确保数据一致性
    if cred_path:
        cred_file = Path(cred_path)
        # Credential file existence check, local metadata
        if cred_file.is_file():  # noqa: ASYNC240 -- 凭证文件存在性检查，本地元数据
            try:
                # hard-delete: out-of-scope -- provider credential, not part of a project / 供应商凭证，不属于项目
                cred_file.unlink()  # noqa: ASYNC240 -- Delete a single credential file, local metadata / 删除单个凭证文件，本地元数据
                logger.info("已删除凭证文件: %s", cred_file)
            except OSError:
                logger.warning("删除凭证文件失败: %s", cred_file, exc_info=True)
    return Response(status_code=204)


@router.post("/{provider_id}/credentials/{cred_id}/activate", status_code=204)
async def activate_credential(
    provider_id: str,
    cred_id: int,
    request: Request,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    _validate_provider(provider_id, _t)
    repo = CredentialRepository(session)
    await _get_credential_or_404(repo, provider_id, cred_id, _t)
    await repo.activate(cred_id, provider_id)
    await session.commit()
    await _invalidate_caches(request)
    return Response(status_code=204)


def _write_vertex_credential_file(dest: Path, contents: bytes) -> None:
    """Write the credential atomically via a temp file, and on POSIX restrict it to owner read/write only.

    经临时文件原子落盘凭证，并在 POSIX 上收紧为仅属主可读写。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(".tmp")
    tmp_path.write_bytes(contents)
    # chmod 0o600 only controls the read-only bit on Windows and cannot restrict access by other users; on Windows
    # credential protection is left to filesystem ACLs (user-level %LOCALAPPDATA%).
    # chmod 0o600 在 Windows 上只控制只读位，无法限制其他用户访问；
    # Windows 上凭证保护交给文件系统 ACL（用户级 %LOCALAPPDATA%）。
    if os.name == "posix":
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            logger.warning("无法设置临时凭证文件权限: %s", tmp_path, exc_info=True)
    os.replace(tmp_path, dest)
    if os.name == "posix":
        try:
            os.chmod(dest, 0o600)
        except OSError:
            logger.warning("无法设置凭证文件权限: %s", dest, exc_info=True)


@router.post("/gemini-vertex/credentials/upload", status_code=201, response_model=CredentialResponse)
async def upload_vertex_credential(
    request: Request,
    _t: Translator,
    name: str = "Vertex Credentials",
    session: AsyncSession = Depends(get_async_session),
    file: UploadFile = File(...),
) -> CredentialResponse:
    """上传 Vertex AI 服务账号 JSON 凭证文件，同时创建凭证记录。"""
    try:
        contents = await file.read(MAX_VERTEX_CREDENTIALS_BYTES + 1)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_t("vertex_json_read_failed")) from exc

    if len(contents) > MAX_VERTEX_CREDENTIALS_BYTES:
        raise HTTPException(status_code=413, detail=_t("vertex_json_too_large"))

    try:
        payload = json.loads(contents.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_t("vertex_json_invalid")) from exc

    if not isinstance(payload, dict) or not payload.get("project_id"):
        raise HTTPException(status_code=400, detail=_t("vertex_json_missing_project_id"))

    repo = CredentialRepository(session)
    cred = await repo.create(provider="gemini-vertex", name=name)

    dest = DataRootLayout.current().vertex_credential_path(cred.id)
    await run_sync_transaction(_write_vertex_credential_file, dest, contents)

    await session.commit()
    await _invalidate_caches(request)

    await session.refresh(cred)
    # Serialization probes the credential file location (stat), so run it in a thread / 序列化要探测凭证文件位置（stat），放进线程
    return await asyncio.to_thread(_cred_to_response, cred)


# ---------------------------------------------------------------------------
# Connectivity checks: per-provider implementations
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 连通性检查：各供应商实现
# ---------------------------------------------------------------------------

_CONNECTIVITY_CHECK_TIMEOUT = 15  # seconds / 秒


def _check_gemini_aistudio(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Verify a Gemini AI Studio API Key via models.list().

    通过 models.list() 验证 Gemini AI Studio API Key。"""
    from google import genai

    api_key = config["api_key"]
    base_url = normalize_base_url(config.get("base_url"))
    http_options: genai.types.HttpOptionsDict | None = {"base_url": base_url} if base_url else None
    client = genai.Client(api_key=api_key, http_options=http_options)

    pager = client.models.list()
    available = _extract_gemini_models(pager)
    return ConnectivityCheckResponse(
        success=True,
        available_models=available,
        message=_t("connectivity_check_ok"),
    )


def _check_gemini_vertex(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Verify connectivity with Vertex AI credentials.

    通过 Vertex AI 凭证验证连通性。"""
    from google import genai
    from google.oauth2 import service_account

    credentials_path = config.get("credentials_path", "")
    if not credentials_path or not Path(credentials_path).is_file():
        return ConnectivityCheckResponse(
            success=False,
            available_models=[],
            message=_t("file_not_found", path=credentials_path),
        )

    with open(credentials_path, encoding="utf-8") as f:
        creds_data = json.load(f)

    project_id = creds_data.get("project_id")
    if not project_id:
        return ConnectivityCheckResponse(
            success=False,
            available_models=[],
            message=_t("vertex_json_missing_project_id"),
        )

    credentials = service_account.Credentials.from_service_account_file(
        credentials_path,
        scopes=VERTEX_SCOPES,
    )
    client = genai.Client(
        vertexai=True,
        project=project_id,
        location="global",
        credentials=credentials,
    )

    pager = client.models.list()
    available = _extract_gemini_models(pager)
    return ConnectivityCheckResponse(
        success=True,
        available_models=available,
        message=_t("connectivity_check_ok"),
    )


def _extract_gemini_models(pager) -> list[str]:
    """Extract video/image related models from the Gemini models.list() result, stripping the path prefix.

    从 Gemini models.list() 结果中提取视频/图像相关模型，去除路径前缀。"""
    keywords = ("veo", "imagen", "image")
    models: set[str] = set()
    for m in pager:
        name = m.name or ""
        if not any(k in name.lower() for k in keywords):
            continue
        # Strip the "models/" or "publishers/google/models/" prefix / 去掉 "models/" 或 "publishers/google/models/" 前缀
        short = name.rsplit("/", 1)[-1]
        models.add(short)
    return sorted(models)


def _check_ark(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Verify an Ark API Key via tasks.list.

    通过 tasks.list 验证 Ark API Key。"""
    from lib.backends.ark_shared import create_ark_client

    client = create_ark_client(api_key=config["api_key"], base_url=config.get("base_url"))
    # Lightweight call to verify connectivity, without creating any resources / 轻量级调用验证连通性，不创建任何资源
    client.content_generation.tasks.list(page_size=1)
    return ConnectivityCheckResponse(
        success=True,
        available_models=[],
        message=_t("connectivity_check_ok"),
    )


def _check_grok(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Verify an xAI API Key via models.list_language_models().

    通过 models.list_language_models() 验证 xAI API Key。"""
    import xai_sdk

    client = xai_sdk.Client(api_key=config["api_key"])
    models = client.models.list_language_models()
    available = sorted(m.name for m in models if m.name)
    return ConnectivityCheckResponse(
        success=True,
        available_models=available,
        message=_t("connectivity_check_ok"),
    )


_OPENAI_MODEL_KEYWORDS = ("gpt", "sora", "dall", "o1", "o3", "o4")


def _check_openai(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Verify an OpenAI API Key via models.list().

    通过 models.list() 验证 OpenAI API Key。"""
    from openai import OpenAI

    kwargs: dict = {"api_key": config["api_key"]}
    base_url = config.get("base_url")
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    models = client.models.list()
    available = sorted(m.id for m in models.data if any(k in m.id.lower() for k in _OPENAI_MODEL_KEYWORDS))
    return ConnectivityCheckResponse(
        success=True,
        available_models=available,
        message=_t("connectivity_check_ok"),
    )


def _check_vidu(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Vidu connectivity check -- HTTP details are encapsulated in lib.backends.vidu_shared.test_vidu_connection
    (fork-only).

    Vidu 连通性检查 — HTTP 细节封装在 lib.backends.vidu_shared.test_vidu_connection（fork-only）。"""
    from lib.backends.vidu_shared import test_vidu_connection

    test_vidu_connection(config)
    return ConnectivityCheckResponse(
        success=True,
        available_models=[],
        message=_t("connectivity_check_ok"),
    )


def _check_dashscope(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Verify a DashScope API Key via models.list() (compatible-mode, OpenAI protocol).

    Uses the same OpenAI-compatible mechanism as custom_provider model discovery; base_url is derived via
    dashscope_text_base_url into {host}/compatible-mode/v1, tolerating the user entering the host or either suffix.

    通过 models.list() 验证 DashScope API Key（compatible-mode，OpenAI 协议）。

    与 custom_provider 模型发现走同一 OpenAI 兼容机制；base_url 经 dashscope_text_base_url
    派生 {host}/compatible-mode/v1，容忍用户填 host 或带任一后缀。
    """
    from openai import OpenAI

    from lib.backends.dashscope_shared import dashscope_text_base_url

    client = OpenAI(
        api_key=config["api_key"],
        base_url=dashscope_text_base_url(config.get("base_url")),
    )
    models = client.models.list()
    available = sorted(m.id for m in models.data if "qwen" in m.id.lower() or "wan" in m.id.lower())
    return ConnectivityCheckResponse(
        success=True,
        available_models=available,
        message=_t("connectivity_check_ok"),
    )


def _check_minimax(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Verify a MiniMax API Key via models.list() (OpenAI-compatible protocol).

    Same structure as DashScope: reuse the OpenAI client to call models.list(); base_url is normalized via
    minimax_text_base_url to {host}/v1, tolerating the user entering the host or a /v1 suffix (defaults to the China
    site, can be pointed at the international site).

    通过 models.list() 验证 MiniMax API Key（OpenAI 兼容协议）。

    与 DashScope 同构：复用 OpenAI 客户端打 models.list()；base_url 经 minimax_text_base_url
    归一化为 {host}/v1，容忍用户填 host 或带 /v1 后缀（缺省国内站，可改指向国际站）。
    """
    from openai import OpenAI

    from lib.backends.minimax_shared import minimax_text_base_url

    client = OpenAI(
        api_key=config["api_key"],
        base_url=minimax_text_base_url(config.get("base_url")),
    )
    models = client.models.list()
    available = sorted(m.id for m in models.data if "minimax" in m.id.lower() or "abab" in m.id.lower())
    return ConnectivityCheckResponse(
        success=True,
        available_models=available,
        message=_t("connectivity_check_ok"),
    )


def _check_kling(config: dict[str, str], _t: Callable[..., str]) -> ConnectivityCheckResponse:
    """Verify Kling credentials by querying the account's resource package balance (``GET /account/costs``, officially
    marked free-to-call with no side effects).

    Two auth modes, either-or; the decision is centralized in kling_auth_mode (shared with
    backend_assembly._build_kling, so actual generation tasks and the connectivity check always dispatch in the same
    order); when missing, the ValueError raised by resolve_kling_api_key / resolve_kling_jwt_credentials is turned
    by the outer except into a clear connectivity_check_failed message. account/costs hangs off the domain root path
    (without the /v1 version prefix), so that suffix must be stripped from base_url.

    通过查询账户资源包余量验证可灵凭证（``GET /account/costs``，官方标注 free-to-call、无副作用）。

    双模式鉴权二选一，判定收口于 kling_auth_mode（与 backend_assembly._build_kling 共用，保证
    实际生成任务与连通性检查的分派顺序恒一致）；缺失时 resolve_kling_api_key /
    resolve_kling_jwt_credentials 抛出的 ValueError 经上层 except 转成明确的 connectivity_check_failed
    文案。account/costs 挂在域名根路径（不带 /v1 版本前缀），需从 base_url 剥离该后缀。
    """
    import time

    import httpx

    from lib.backends.kling_shared import (
        KLING_BASE_URL,
        KlingJWTManager,
        kling_auth_mode,
        kling_bearer_headers,
        kling_response_error,
        resolve_kling_api_key,
        resolve_kling_jwt_credentials,
    )

    if kling_auth_mode(config) == "bearer":
        headers = kling_bearer_headers(resolve_kling_api_key(config.get("api_key")))
    else:
        access_key, secret_key = resolve_kling_jwt_credentials(config.get("access_key"), config.get("secret_key"))
        headers = KlingJWTManager(access_key, secret_key).auth_headers()

    base_url = (config.get("base_url") or KLING_BASE_URL).rstrip("/")
    root = base_url.removesuffix("/v1")
    now_ms = int(time.time() * 1000)
    one_day_ms = 24 * 60 * 60 * 1000
    resp = httpx.get(
        f"{root}/account/costs",
        params={"start_time": now_ms - one_day_ms, "end_time": now_ms},
        headers=headers,
        timeout=_CONNECTIVITY_CHECK_TIMEOUT,
    )
    # Parse the JSON error body before raise_for_status: in cases such as auth failure Kling still returns a JSON
    # error body (business code + message, e.g. "access key does not exist"), and extracting it before
    # raise_for_status keeps that specific reason; otherwise raising directly on 4xx/5xx would discard the response
    # body, leaving only a generic HTTP status message. When content-type claims JSON but the body is actually
    # invalid/empty (e.g. a response truncated by a faulty gateway), parsing raises JSONDecodeError -- deciding
    # whether to try parsing by the declared content-type does not mean the body is valid; on parse failure give up
    # extracting the business error, skip it and fall through to raise_for_status to expose the HTTP status, instead
    # of letting the parse exception itself mask the more specific HTTP error.
    # JSON 错误体先于 raise_for_status 解析：鉴权失败等场景可灵仍带 JSON 错误体（业务 code +
    # message，如"access key 不存在"），先于 raise_for_status 提取能保留这份具体原因；否则
    # 4xx/5xx 直接 raise 会丢弃响应体，只剩泛泛的 HTTP 状态文案。content-type 声称 JSON 但
    # 实际非法/空体（如异常网关截断响应）时解析会抛 JSONDecodeError——按声明的 content-type
    # 判断是否尝试解析，不代表响应体一定合法，解析失败即放弃业务错误提取，跳过、直接走
    # raise_for_status 兜底暴露 HTTP 状态，而非让解析异常本身掩盖更具体的 HTTP 错误。
    if "application/json" in resp.headers.get("content-type", ""):
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if payload is not None:
            err = kling_response_error(payload)
            if err is not None:
                raise RuntimeError(err)
    raise_for_status_redacted(resp)
    return ConnectivityCheckResponse(
        success=True,
        available_models=[],
        message=_t("connectivity_check_ok"),
    )


_CONNECTIVITY_CHECK_DISPATCH: dict[str, Callable[[dict[str, str], Any], ConnectivityCheckResponse]] = {
    "gemini-aistudio": _check_gemini_aistudio,
    "gemini-vertex": _check_gemini_vertex,
    "ark": _check_ark,
    "ark-agent-plan": _check_ark,
    "grok": _check_grok,
    "openai": _check_openai,
    "vidu": _check_vidu,
    "dashscope": _check_dashscope,
    "minimax": _check_minimax,
    "kling": _check_kling,
}


@router.post("/{provider_id}/test", response_model=ConnectivityCheckResponse)
async def check_provider_connectivity(
    provider_id: str,
    _t: Translator,
    locale: Locale,
    credential_id: int | None = None,
    session: AsyncSession = Depends(get_async_session),
) -> ConnectivityCheckResponse:
    """调用供应商 API 验证连通性。可指定 credential_id 测试特定凭证。"""
    _validate_provider(provider_id, _t)

    repo = CredentialRepository(session)
    if credential_id is not None:
        cred = await _get_credential_or_404(repo, provider_id, credential_id, _t)
    else:
        cred = await repo.get_active(provider_id)

    if cred is None:
        return ConnectivityCheckResponse(
            success=False,
            available_models=[],
            message=_t("missing_credentials"),
        )

    svc = ConfigService(session)
    config = await svc.get_provider_config(provider_id)
    cred.overlay_config(config)

    # Symmetric with the base_url priority of simple-family backend construction: when the user has not explicitly
    # configured base_url, inject ProviderMeta.default_base_url so the connectivity check hits the right endpoint.
    # 与简单族 backend 构造的 base_url 优先级对称：用户未显式配 base_url
    # 时，注入 ProviderMeta.default_base_url，使连通性检查命中正确 endpoint。
    if not config.get("base_url"):
        meta = PROVIDER_REGISTRY.get(provider_id)
        if meta and meta.default_base_url:
            config["base_url"] = meta.default_base_url

    check_fn = _CONNECTIVITY_CHECK_DISPATCH.get(provider_id)
    if check_fn is None:
        names = await load_display_names_or_builtin(session, locale)
        return ConnectivityCheckResponse(
            success=False,
            available_models=[],
            message=render_message("connectivity_check_unsupported", {"provider_id": provider_id}, _t, names),
        )

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(check_fn, config, _t),
            timeout=_CONNECTIVITY_CHECK_TIMEOUT,
        )
    except TimeoutError:
        return ConnectivityCheckResponse(
            success=False,
            available_models=[],
            message=_t("connectivity_check_timeout"),
        )
    except Exception as exc:
        err_msg = str(exc)
        if len(err_msg) > 200:
            err_msg = err_msg[:200] + "..."
        logger.warning("连通性检查失败 [%s]: %s", provider_id, err_msg)
        return ConnectivityCheckResponse(
            success=False,
            available_models=[],
            message=_t("connectivity_check_failed", err_msg=err_msg),
        )
    return result
