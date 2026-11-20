"""Unified runtime configuration resolver.

Brings config reads and default value definitions scattered across many files into one place.
Each call reads from the DB, no caching (local SQLite overhead is negligible).

统一运行时配置解析器。

将散落在多个文件中的配置读取和默认值定义集中到一处。
每次调用从 DB 读取，不缓存（本地 SQLite 开销可忽略）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from lib.db.models.custom_provider import CustomProviderModel

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from arcreel_catalog_core import DurationConstraints
from lib.backends.backend_assembly.specs import (
    builtin_effective_generate_audio_for_model,
    builtin_video_capabilities_for_model,
)
from lib.backends.text_backends.base import TEXT_TASK_TIERS, VISION_REQUIRED_TASKS, TextTaskTier, TextTaskType
from lib.catalog import query as catalog
from lib.catalog.errors import ModelNotInCatalogError
from lib.config.registry import PROVIDER_REGISTRY
from lib.config.service import (
    DEFAULT_AUDIO_BACKEND,
    DEFAULT_IMAGE_BACKEND,
    DEFAULT_REFERENCE_SINGLE_MAX_BYTES,
    DEFAULT_REFERENCE_TOTAL_MAX_BYTES,
    DEFAULT_TEXT_BACKEND,
    DEFAULT_VIDEO_BACKEND,
    ConfigService,
)
from lib.custom_provider import is_custom_provider, parse_provider_id
from lib.db.repositories.credential_repository import CredentialRepository
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.episode.episode_target_duration import project_episode_target_duration
from lib.project.project_manager import get_project_manager
from lib.speech.character_voice import CharacterVoiceBinding, character_voice_binding

logger = logging.getLogger(__name__)

# Set of truthy values for parsing boolean strings / 布尔字符串解析的 truthy 值集合
_TRUTHY = frozenset({"true", "1", "yes"})


@dataclass(frozen=True)
class ProviderModel:
    """Result value object of provider resolution: a pair (canonical provider_id, model_id).

    See "ProviderModel" in GLOSSARY.md. This is "which provider and model were chosen", **not** a backend (no client is
    constructed); the name deliberately avoids ``*Backend`` to keep provider identity distinct from backend construction.
    ``provider_id`` is always the canonical id — the resolution chain assumes input is already canonical (guaranteed by
    project migration + the write boundary) and does no normalization.

    provider 解析的结果值对象：一对 (规范 provider_id, model_id)。

    见 GLOSSARY.md「ProviderModel」。这是"选了哪个 provider 及其 model"，**不是** backend
    （未构造任何客户端）；命名刻意避开 ``*Backend`` 以保持 provider 身份与 backend 构造的区分。
    ``provider_id`` 一律为规范 id——解析链假设输入即规范形态（由项目迁移 + 写边界保证），不做归一化。
    """

    provider_id: str
    model_id: str

    @property
    def pair_key(self) -> str:
        """Composite key form ``"<provider>/<model>"``, the inverse of ``_split_pair``.

        The provider fields of the config and payload layers and per-model storage keyed by execution model all use this
        form; writers always build it via this property, so the format is defined in this one place.

        复合键形态 ``"<provider>/<model>"``，与 ``_split_pair`` 互逆。

        配置层与 payload 层的 provider 字段、按执行模型取键的 per-model 存储都用这一形态；
        写入方一律经此属性成形，格式只此一处定义。
        """
        return f"{self.provider_id}/{self.model_id}"


def _parse_bool(raw: str) -> bool:
    """Parse a config string into a bool. / 将配置字符串解析为布尔值。"""
    return raw.strip().lower() in _TRUTHY


def _parse_int(raw: object, default: int) -> int:
    """Parse a config value into a positive int; empty string / non-numeric / non-positive all return default (tolerant, no raise).

    将配置值解析为正整数；空串 / 非数字 / 非正一律回 default（容错，不抛）。"""
    if isinstance(raw, bool):  # bool is a subclass of int; exclude it explicitly to avoid True→1 / bool 是 int 子类，显式排除避免 True→1
        return default
    if isinstance(raw, int):
        return raw if raw > 0 else default
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value if value > 0 else default
    return default


# Per-provider override keys for the reference upload copy limits (settable via the bare API PATCH /providers/{id}/config).
# 参考上传副本上限的 per-provider 覆盖 key（裸 API PATCH /providers/{id}/config 可设置）。
_REFERENCE_TOTAL_MAX_BYTES_KEY = "reference_total_max_bytes"
_REFERENCE_SINGLE_MAX_BYTES_KEY = "reference_single_max_bytes"


#: Payload key of a video model chosen for one request (``provider/model``); not an execution identity, so the queue
#: keeps it and every resolution of the task (claim, worker, quote) uses it.
VIDEO_MODEL_OVERRIDE_KEY = "video_model_override"


def _split_pair(raw: object) -> tuple[str, str] | None:
    """Parse ``"<provider>/<model>"`` → (provider, model); returns None when invalid.

    An empty / whitespace-only provider or model (e.g. ``"openai/"`` / ``"/m"``) counts as invalid and returns None,
    leaving the caller to fill in the bare provider's default model or fall back — so an empty model never reaches the execution layer.

    解析 ``"<provider>/<model>"`` → (provider, model)；不合法返回 None。

    provider 或 model 为空/纯空白（如 ``"openai/"`` / ``"/m"``）均视为不合法返回 None，
    交由调用方走裸供应商补默认 model 或回退——避免把空 model 带到执行层。"""
    if not isinstance(raw, str) or "/" not in raw:
        return None
    provider, model = raw.split("/", 1)
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        return None
    return provider, model


def _parse_project_provider(raw: object, media_type: str) -> tuple[str, str] | None:
    """Parse the provider field of project.json, accepting bare-provider overrides.

    - ``"provider/model"`` → (provider, model)
    - bare ``"provider"`` (exists in the registry and has a default model for this media_type) → (provider, default model)
    - anything else → None (left to global default resolution)

    A bare provider is guaranteed canonical by the write boundary (``validate_backend_value`` only lets registry keys
    through); here that provider is pinned and its default model filled in, to avoid silently falling back to a
    **different** provider of the global default. A bare built-in provider whose default model cannot be filled in does
    not fall back either; the resolution skeleton raises (``_reject_bare_builtin_without_default``).

    解析 project.json 的 provider 字段，兼容裸供应商覆盖。

    - ``"provider/model"`` → (provider, model)
    - 裸 ``"provider"``（registry 中存在且有该 media_type 默认 model）→ (provider, 默认 model)
    - 其余 → None（交由全局默认解析）

    裸供应商经写边界（``validate_backend_value`` 只放行 registry key）保证是规范 id，这里
    pin 住该供应商并补全其默认 model，避免静默回退到全局默认的**另一**供应商。补不出默认 model
    的裸内置供应商同样不回退，由解析骨架报错（``_reject_bare_builtin_without_default``）。"""
    pair = _split_pair(raw)
    if pair is not None:
        return pair
    if isinstance(raw, str):
        # Bare provider, or a dirty value with a trailing slash and no model (e.g. "openai/") → take that provider's default model
        # 裸供应商，或带尾斜杠缺 model 的脏值（如 "openai/"）→ 取该供应商默认 model
        provider = raw.strip().rstrip("/").strip()
        if provider:
            model = catalog.default_model(provider, media_type)
            if model is not None:
                return provider, model
    return None


def _reject_bare_builtin_without_default(raw: object, media_type: str) -> None:
    """Raise when a bare built-in provider at the project layer has no default model for this media type (e.g. openai has no video models any more).

    Only called after ``_parse_project_provider`` returned None: if the bare value is a built-in provider at that point,
    it has no models of this media type in the model catalog. Report "not in the model list" so the user reselects,
    instead of falling back to another provider to generate and bill.

    项目层的裸内置供应商在该媒体类型下没有默认模型（如 openai 已无视频模型）时报错。

    只在 ``_parse_project_provider`` 返回 None 之后调用：此时裸值若是内置供应商，说明它在模型目录里
    没有这一媒体类型的模型。报「不在模型清单中」让用户重新选择，不回退到另一供应商去生成和计费。
    """
    if not isinstance(raw, str):
        return
    provider = raw.strip().rstrip("/").strip()
    if provider in PROVIDER_REGISTRY:
        raise ModelNotInCatalogError(provider_id=provider, model_id="", media_type=media_type)


def _trusted_payload_provider(provider_id: object) -> str | None:
    """Return a trusted canonical provider_id (a known provider), else None.

    The payload is the only input source in the resolution chain that bypasses write-boundary validation (in-flight queue
    tasks were serialized when enqueued by old code). Guard accordingly: non-string / blank / unrecognized providers (e.g.
    legacy ``seedance``/``vertex``) are never trusted, returning None so resolution falls back to the migrated
    project/global — no legacy→canonical mapping, only rejection of untrusted input.

    返回可信任的规范 provider_id（已知供应商），否则 None。

    payload 是解析链唯一绕过写边界校验的输入来源（in-flight 队列任务在旧代码入队时即序列化）。
    据此守卫：非字符串 / 空白 / 不可识别的 provider（如 legacy ``seedance``/``vertex``）一律不予
    信任，返回 None 让解析回退到已迁移的 project/global——不做 legacy→规范映射，仅拒绝不可信输入。"""
    if not isinstance(provider_id, str):
        return None
    provider_id = provider_id.strip()
    if not provider_id:
        return None
    if provider_id in PROVIDER_REGISTRY or is_custom_provider(provider_id):
        return provider_id
    return None


def _payload_model_or_default(raw_model: object, provider_id: str, media_type: str) -> str | None:
    """An explicit payload model (non-empty string) wins; when missing, fill in the provider's default model from the model catalog.

    Avoids a "half payload" (provider only, no model) carrying an empty model to the execution layer. Returns None when
    no default model can be filled in, and the caller falls back to project/global.

    payload 显式 model（非空字符串）优先；缺失则补该 provider 在模型目录中的默认 model。

    避免「半截 payload」（只有 provider、缺 model）把空 model 带到执行层。补不出默认 model 时
    返回 None，由调用方回退 project/global。"""
    if isinstance(raw_model, str) and raw_model.strip():
        return raw_model.strip()
    return catalog.default_model(provider_id, media_type)


@dataclass(frozen=True)
class _LayeredBackendKeys:
    """Key declaration for the four-level "default + task type bucket" resolution skeleton, media type agnostic (see ``docs/adr/0054``).

    Each task type bucket of each media type declares one key set, consumed by ``ConfigResolver._resolve_layered_backend``
    in fixed order: project bucket > project default > global bucket > global default > auto inference. A None key means
    the layer does not exist and is skipped — onboarding a new media type / bucket only needs one more key declaration,
    the skeleton itself is unchanged.

    「默认 + 任务类型桶」四级解析骨架的键位声明，媒体类型无关（见 ``docs/adr/0054``）。

    每个媒体类型的每个任务类型桶声明一份键位，由 ``ConfigResolver._resolve_layered_backend``
    按固定顺序消费：项目桶 > 项目默认 > 全局桶 > 全局默认 > 自动推断。键为 None 表示该层
    不存在、直接跳过——新媒体 / 新桶接入只需补一份键位声明，不改骨架本身。
    """

    media_type: str
    # Fallback when a global layer value contains "/" but fails to parse, shaped like "provider/model" (the fallback of ConfigService._parse_backend)
    # 全局层值含 "/" 但解析异常时的兜底，形如 "provider/model"（ConfigService._parse_backend 的 fallback）
    parse_fallback: str
    project_bucket_key: str | None = None
    project_default_key: str | None = None
    global_bucket_key: str | None = None
    global_default_key: str | None = None
    # The task type bucket this key set belongs to, used by the auto inference layer to filter custom default models by
    # bucket. Empty for video and text / audio: the former's custom defaults are already at most one per media_type at save
    # time, the latter two have no buckets.
    # 这份键位所属的任务类型桶，供自动推断层按桶过滤自定义默认模型。视频与文本 / 音频留空：
    # 前者的自定义默认在保存期就是同一 media_type 至多一个，后两者无桶。
    generation_type: str | None = None


# Image bucket (t2i / i2i) keys. Buckets are optional overrides, an empty bucket falls back to the default layer: the
# project default layer uses project.json's default_image_backend field (same name and position as the text tier's
# default_text_backend), the global default layer uses the default_image_backend setting key.
# 图片桶（t2i / i2i）键位。桶为可选覆盖，空桶回退默认层：项目默认层用 project.json 的
# default_image_backend 字段（与文本档位的 default_text_backend 同名同位），全局默认层用
# default_image_backend 设置键。
_IMAGE_LAYERED_KEYS: dict[str, _LayeredBackendKeys] = {
    generation_type: _LayeredBackendKeys(
        media_type="image",
        parse_fallback=DEFAULT_IMAGE_BACKEND,
        project_bucket_key=f"image_provider_{generation_type}",
        project_default_key="default_image_backend",
        global_bucket_key=f"default_image_backend_{generation_type}",
        global_default_key="default_image_backend",
        generation_type=generation_type,
    )
    for generation_type in ("t2i", "i2i")
}


# Video bucket (i2v / r2v) keys. An empty bucket falls back to the default layer (docs/adr/0054 semantics). Zero
# migration for the default layer: the project default layer reuses the existing video_backend field, the global
# default layer reuses the default_video_backend key.
# 视频桶（i2v / r2v）键位。空桶回退默认层（docs/adr/0054 语义）。默认层零迁移：项目默认层
# 复用既有 video_backend 字段，全局默认层复用 default_video_backend 键。
_VIDEO_LAYERED_KEYS: dict[str, _LayeredBackendKeys] = {
    generation_type: _LayeredBackendKeys(
        media_type="video",
        parse_fallback=DEFAULT_VIDEO_BACKEND,
        project_bucket_key=f"video_provider_{generation_type}",
        project_default_key="video_backend",
        global_bucket_key=f"default_video_backend_{generation_type}",
        global_default_key="default_video_backend",
    )
    for generation_type in ("i2v", "r2v")
}


# Bucketless video keys: same source and layers as ``_VIDEO_LAYERED_KEYS``, just without the bucket layer (None is
# skipped by the skeleton). For callers that do not commit to a task type bucket (cost estimation, rate-limit routing
# fallback, config display); no bucket key override and no capability gate.
# 不定桶视频键位：与 ``_VIDEO_LAYERED_KEYS`` 同源同层，只去掉桶层（None 由骨架跳过）。供不承诺
# 任务类型桶的调用方（费用估算、限流路由兜底、配置展示）解析，不施加桶键覆盖也不过能力闸。
_VIDEO_DEFAULT_LAYERED_KEYS = _LayeredBackendKeys(
    media_type="video",
    parse_fallback=DEFAULT_VIDEO_BACKEND,
    project_default_key="video_backend",
    global_default_key="default_video_backend",
)


# Audio keys. Audio has no task type buckets; the bucket layer is left empty (None) and skipped by the skeleton: the
# project default layer uses project.json's audio_backend field, the global default layer uses the default_audio_backend setting key.
# 音频键位。音频无任务类型桶，桶层留空（None）由骨架直接跳过：项目默认层用 project.json 的
# audio_backend 字段，全局默认层用 default_audio_backend 设置键。
_AUDIO_LAYERED_KEYS = _LayeredBackendKeys(
    media_type="audio",
    parse_fallback=DEFAULT_AUDIO_BACKEND,
    project_default_key="audio_backend",
    global_default_key="default_audio_backend",
)

#: Video task type buckets: i2v (image-to-video / grid, driven by the first frame; also takes the degraded execution of
#: reference-to-video video units without reference images), r2v (video units with reference images in reference-to-video). No bucket for t2v (docs/adr/0054).
#: 视频任务类型桶：i2v（图生视频 / 宫格，由首帧驱动；另承接参考生视频无参考图视频单元的降级执行）、
#: r2v（参考生视频中有参考图的视频单元）。t2v 不设桶（docs/adr/0054）。
VideoGenerationType = Literal["i2v", "r2v"]

#: Video task type → task type bucket. The mapping of execution paths to buckets is fixed in code (docs/adr/0054):
#: image-to-video / grid video (task_type ``video``) → i2v; reference-to-video splits by whether the video unit carries
#: reference images (``lib.script.reference_video.units``), and this table registers its representative bucket r2v,
#: only as a fallback when the script / unit cannot be read. Task types outside the table have no video bucket and
#: callers treat them as "bucketless". Defined in this module (rather than lib.backends.generation_type_buckets) due to
#: a layering constraint: the enqueue and claim paths of the queue / worker lie within the dependency closure of
#: lib.backends.video_backends and must not indirectly pull in lib.custom_provider via the bucket decision module.
#: 视频任务类型 → 任务类型桶。执行路径与桶的映射固定在代码里（docs/adr/0054）：图生视频 /
#: 宫格生视频（task_type ``video``）→ i2v；参考生视频按视频单元是否携带参考图分流
#: （``lib.script.reference_video.units``），本表登记其代表桶 r2v，仅供剧本 / unit 读不到时回退。
#: 表外任务类型无视频桶，调用方按「不定桶」处理。定义在本模块（而非 lib.backends.generation_type_buckets）
#: 是分层约束：队列 / worker 的入队与认领路径处于 lib.backends.video_backends 的依赖闭包内，不得经
#: 桶判定模块间接引入 lib.custom_provider。
VIDEO_BUCKET_BY_TASK_TYPE: dict[str, VideoGenerationType] = {
    "video": "i2v",
    "reference_video": "r2v",
}

#: Generation mode → task type bucket. Two entry points describing the same mapping as ``VIDEO_BUCKET_BY_TASK_TYPE``:
#: the execution path picks the bucket by the task_type of an already formed task; the read side (capability queries /
#: duration constraint narrowing etc.) only has the project's generation_mode before a task is formed and picks the same
#: bucket by it, so both sides answer the same "model the current config will really execute".
#: Degradation of shots without reference images in reference-to-video projects (→ i2v) does not go through this table,
#: see ``lib.script.reference_video.units``.
#: 生成模式 → 任务类型桶。与 ``VIDEO_BUCKET_BY_TASK_TYPE`` 描述同一套映射的两个入口：执行路径按
#: 已成形任务的 task_type 定桶，读侧（能力查询 / 时长约束收窄等）在任务成形前只有项目的
#: generation_mode，按它定同一个桶，两侧因此回答同一个「当前配置真正会执行的模型」。
#: 参考生视频项目中无参考图分镜的降级（→ i2v）不经本表，见 ``lib.script.reference_video.units``。
VIDEO_BUCKET_BY_GENERATION_MODE: dict[str, VideoGenerationType] = {
    "storyboard": "i2v",
    "reference_video": "r2v",
}

#: Bucket for generation_mode values outside the table (no project context and dirty data). project.json is a plaintext
#: file and the generation mode field may be hand-edited into garbage; without project context (e.g. provider catalog
#: queries) there is no generation mode to go on either.
#: 表外 generation_mode（无项目上下文与脏数据）落的桶。project.json 是明文文件，生成模式字段
#: 可能被手工改坏；无项目上下文（如 provider 目录查询）同样没有生成模式可依。
_DEFAULT_VIDEO_BUCKET: VideoGenerationType = "i2v"


def video_bucket_for_generation_mode(generation_mode: str | None) -> VideoGenerationType:
    """Which video task type bucket a project's generation_mode maps to — the single entry for read-side bucket choice.

    project.json is a plaintext file and ``generation_mode`` may be written as a non-string, which also lands in the default bucket.

    项目的 generation_mode 归到哪个视频任务类型桶——读侧定桶的唯一入口。

    project.json 是明文文件，``generation_mode`` 可能被写成非字符串，一并落默认桶。
    """
    if not isinstance(generation_mode, str):
        return _DEFAULT_VIDEO_BUCKET
    return VIDEO_BUCKET_BY_GENERATION_MODE.get(generation_mode, _DEFAULT_VIDEO_BUCKET)


def _payload_video_pinned_pair(
    payload: dict, generation_type: VideoGenerationType | None
) -> tuple[VideoGenerationType, tuple[str, str]] | None:
    """Read the explicit video execution identity in the request payload (bucket key ``video_provider_<generation_type>``), returning it together with the matched bucket.

    The value is a composite in ``ProviderModel.pair_key`` form. Normal queue execution first removes such old keys from
    the enqueue payload and then evaluates per current config; the resume executor for already submitted videos builds
    exactly one bucket key from the immutable checkpoint and uses it to rebuild the original backend. When
    ``generation_type`` is not declared, scan both bucket keys in fixed bucket order — at most one hits, so the order
    causes no ambiguity.

    An untrusted provider (see ``_trusted_payload_provider``) is not dropped here: video-side identity availability is
    handled by ``_ensure_video_identity_resolvable``; when the provider has gone offline it should raise, since falling
    back would mean executing with a different provider.

    读请求 payload 里显式的视频执行身份（桶键 ``video_provider_<generation_type>``），连同命中的桶一并返回。

    值是 ``ProviderModel.pair_key`` 形态的复合值。正常队列执行会先移除 enqueue payload
    中的这类旧键，再按当前配置求值；已提交视频的 resume executor 则根据 immutable
    checkpoint 构造恰好一个桶键，用它重建原 backend。``generation_type`` 未声明时按固定桶序
    扫两个桶键——至多命中一个，桶序不产生歧义。

    provider 不可信（见 ``_trusted_payload_provider``）不在此丢弃：视频侧身份可用性由
    ``_ensure_video_identity_resolvable`` 收口，供应商已下线时该报错，回退等于换供应商执行。
    """
    candidates: tuple[VideoGenerationType, ...] = (
        (generation_type,) if generation_type is not None else get_args(VideoGenerationType)
    )
    for candidate in candidates:
        pair = _split_pair(payload.get(f"video_provider_{candidate}"))
        if pair is not None:
            return candidate, pair
    return None


def caps_generation_mode(project: dict | None) -> str | None:
    """generation_mode under capability query rules: read directly from the project field, None without project context.

    The generation mode is fixed at creation and the whole project generates along one path, so capability resolution
    needs no episode context: bucket choice, voice consistency, and the resolution and reference image constraints
    downstream evaluated from caps ``generation_mode`` all take the project's generation mode as their axis.

    Returns None instead of the default tier because ``generation_mode`` is an external caps field (returned to the
    frontend and Agent): without project context (provider catalog queries etc.) "undeclared" should not render as if the
    user had explicitly chosen a generation mode.

    能力查询口径的 generation_mode：直读项目字段，无项目上下文时为 None。

    生成模式创建即定、整个项目按同一条路径生成，能力解析因此不需要剧集上下文：定桶、声音
    一致性、以及下游按 caps ``generation_mode`` 求值的分辨率与参考图约束全部按项目生成模式定轴。

    返回 None 而非默认档，是因为 ``generation_mode`` 是 caps 的对外字段（回前端与 Agent）：
    无项目上下文（provider 目录查询等）时「未声明」不该渲染成用户显式选过某种生成模式。
    """
    if project is None:
        return None
    mode = project.get("generation_mode")
    return mode if isinstance(mode, str) and mode else None


def project_video_backend_ids(
    project: dict, *, generation_type: VideoGenerationType | None = None
) -> tuple[str, str] | None:
    """The video model identity project.json reports itself: the bucket key of the given bucket (by generation_mode when omitted), or the project default key if absent.

    Reads only project.json, no DB, for synchronous paths without a config DB session (archive import looks up registry
    tiers by the self-reported identity): the bucket key and default key live in the same plaintext file, and the bucket
    rule should not be degraded to the project default layer — otherwise a reference-to-video project configured with
    ``video_provider_r2v`` would get tiers from ``video_backend``. Value rules within the layer match the project layer of
    ``_resolve_layered_backend`` (including bare-provider overrides).

    project.json 自报的视频模型身份：取给定桶（缺省按 generation_mode 定桶）的桶键，缺则取项目默认键。

    纯读 project.json、不查 DB，供没有配置库会话的同步路径（归档导入按自报身份查 registry 档位）
    使用：桶键与默认键都在同一个明文文件里，不该把桶口径降成项目默认层——否则配了
    ``video_provider_r2v`` 的参考生视频项目会拿 ``video_backend`` 的档位取档。层内取值口径与
    ``_resolve_layered_backend`` 的项目层一致（含裸供应商覆盖）。
    """
    bucket = generation_type or video_bucket_for_generation_mode(project.get("generation_mode"))
    keys = _VIDEO_LAYERED_KEYS[bucket]
    for key in (keys.project_bucket_key, keys.project_default_key):
        if key is None:
            continue
        parsed = _parse_project_provider(project.get(key), "video")
        if parsed is not None:
            return parsed
    return None


def video_capability_satisfied(
    *,
    generation_type: VideoGenerationType,
    first_frame: bool,
    max_reference_images: int,
    text_to_video: bool = True,
    has_image: bool = True,
) -> bool:
    """Whether a set of video capability declarations satisfies a bucket — the single rule for bucket membership.

    The resolution gate (``_ensure_video_bucket_capability``) and the bucket candidate dropdown
    (``lib.backends.generation_type_buckets``) share this function instead of each writing its own boolean expression:
    combinations blocked by the dropdown are necessarily blocked by resolution, and vice versa. ``has_image`` tells pure
    text-to-video from first-frame requests within the i2v bucket. Scalar parameters instead of
    ``VideoCapabilities``: first, so lib.config does not import arcreel_market_core.video_backend_contract (layering
    contract); second, so both sources, built-in (backend declarations) and custom providers (endpoint ⊕ model-level
    override composition), can feed in directly.

    一组视频能力声明是否满足某个桶——桶归属判定的唯一口径。

    解析闸（``_ensure_video_bucket_capability``）与桶候选下拉（``lib.backends.generation_type_buckets``）共用本
    函数，不各写一份布尔式：下拉挡掉的组合解析层必然也挡，反之亦然。``has_image`` 区分
    i2v 桶内的纯文生与带首帧请求。取标量参数而非
    ``VideoCapabilities``，一是不在 lib.config 层导入 arcreel_market_core.video_backend_contract（分层契约），二是让
    内置（backend 声明）与自定义供应商（endpoint ⊕ 模型级覆盖的合成）两条来源都能直接喂进来。
    """
    if generation_type == "i2v":
        return first_frame if has_image else text_to_video
    return max_reference_images > 0


def builtin_video_audio_track(provider_id: str, model_id: str, *, generation_type: VideoGenerationType) -> str | None:
    """Audio track form of the output for a built-in video model on this task type bucket; None when it cannot be resolved (callers treat it as "no signal, no tightening").

    The source of truth is the backend's ``VideoCapabilities`` — the backend is the party that actually builds requests at
    execution, "whether the request body has an audio switch" is the literal meaning of this bit, and only it can express
    "forks by execution subpath within one model" (Kling v3-omni's request body has no ``sound`` on the multi-image
    subject subpath, so the output is necessarily silent). The display layer, enqueue precheck and voice consistency
    derivation all read this function instead of each interpreting its own declaration.

    The return value is a literal of ``arcreel_market_core.video_backend_contract.VideoAudioMode``. The enum is not
    imported here: the layering contract puts lib.config at the bottom, the same approach as ``derive_voice_consistency``
    comparing ``ReferenceAudioMode`` by literal (``StrEnum`` and literals can be compared directly with ``==``).

    All three causes of None count as "no signal": the provider is not in the registry (custom providers use
    ``synthesize_video_capabilities``, not this function), the model is not a video model, or the backend declares no capabilities.

    内置视频 model 在该任务类型桶上的成片音轨形态；无法解析时 None（调用方按「无信号不收紧」处理）。

    真相源是 backend 的 ``VideoCapabilities``——backend 是执行期真正构造请求的一方，「请求体里
    有没有音轨开关」就是这一位的字面含义，也只有它表达得了「同一 model 内按执行子路径分叉」
    （可灵 v3-omni 走多图主体子路径时请求体不含 ``sound``，成片必然无声）。展示层、入队预检与
    声音一致性派生共读本函数，不各自解读一份声明。

    返回值是 ``arcreel_market_core.video_backend_contract.VideoAudioMode`` 的字面量。此处不导入该枚举：分层契约以
    lib.config 为最底层，与 ``derive_voice_consistency`` 按字面量比较 ``ReferenceAudioMode`` 同一
    做法（``StrEnum`` 与字面量可直接 ``==``）。

    None 的三种成因都归到「无信号」：供应商不在注册表（自定义供应商走
    ``synthesize_video_capabilities``，不经本函数）、该 model 不是视频模型、backend 未声明能力。
    """
    if catalog.get_model(provider_id, model_id, media_type="video") is None:
        return None
    try:
        caps = builtin_video_capabilities_for_model(provider_id, model_id)
    except ValueError:
        return None
    return caps.audio_track_for_route(generation_type).value


# Tier → setting key. Global (system_settings) and project level (project.json) share names and structure.
# 档位 → 设置键。全局（system_settings）与项目级（project.json）同名同构。
_TEXT_TIER_SETTING_KEYS: dict[TextTaskTier, str] = {
    TextTaskTier.SIMPLE: "text_backend_simple",
    TextTaskTier.COMPLEX: "text_backend_complex",
}


# Text tier (docs/adr/0051) keys. A tier is a bucket and project level and global share names and structure, so the
# bucket layer has the same key on both sides; the default layer uses default_text_backend on both sides.
# 文本档位（docs/adr/0051）键位。档位即桶，项目级与全局同名同构，故桶层两侧同键；默认层
# 两侧同用 default_text_backend。
_TEXT_LAYERED_KEYS: dict[TextTaskTier, _LayeredBackendKeys] = {
    tier: _LayeredBackendKeys(
        media_type="text",
        parse_fallback=DEFAULT_TEXT_BACKEND,
        project_bucket_key=tier_key,
        project_default_key="default_text_backend",
        global_bucket_key=tier_key,
        global_default_key="default_text_backend",
    )
    for tier, tier_key in _TEXT_TIER_SETTING_KEYS.items()
}


#: Purposes for the auto model selection preview (``ConfigResolver.auto_resolved_backends``).
#: 自动选模预览的用途（``ConfigResolver.auto_resolved_backends``）。
AutoResolvePurpose = Literal["t2i", "i2i", "i2v", "r2v", "text"]

#: Purpose → the keys consumed when resolving that purpose at execution. The auto inference layer only reads the keys'
#: media_type and generation_type, so the preview taking the same keys follows the same rules as execution; these two
#: are the same for all text tiers, so they merge into one purpose.
#: 用途 → 执行期解析该用途时消费的键位。自动推断层只读键位的 media_type 与 generation_type，
#: 预览取同一份键位即与执行同口径；文本各档位这两项相同，合为一个用途。
_AUTO_RESOLVE_PURPOSE_KEYS: dict[AutoResolvePurpose, _LayeredBackendKeys] = {
    "t2i": _IMAGE_LAYERED_KEYS["t2i"],
    "i2i": _IMAGE_LAYERED_KEYS["i2i"],
    "i2v": _VIDEO_LAYERED_KEYS["i2v"],
    "r2v": _VIDEO_LAYERED_KEYS["r2v"],
    "text": _TEXT_LAYERED_KEYS[TextTaskTier.COMPLEX],
}


#: Video providers whose requests do not send the ``generate_audio`` switch and which always bill at the audio tier.
#: 请求里不下发 ``generate_audio`` 开关、供应商恒按含音档出账的 video provider。
_VIDEO_AUDIO_ALWAYS_BILLED_PROVIDERS = frozenset({"gemini-aistudio"})


def _video_audio_always_billed(provider_id: str) -> bool:
    """Whether this provider ignores the request value and always bills at the audio tier.

    AI Studio Veo requests do not send ``generate_audio`` (the switch only exists on the Vertex pricing page, the AI Studio
    pricing page has no audio-off tier), and ``GeminiVideoBackend`` forces True at settlement for
    ``backend_type != "vertex"``; this is a provider-level billing rule, and the registry name ``gemini`` maps to both
    aistudio / vertex, so it is judged by provider_id rather than the backend's static interface.

    该 provider 是否无视请求值恒按含音档出账。

    AI Studio 的 Veo 请求不下发 ``generate_audio``（该开关仅存在于 Vertex 定价页，AI Studio
    定价页无 audio-off 档），``GeminiVideoBackend`` 结算时对 ``backend_type != "vertex"`` 强制
    True；这是 provider 级出账规则，registry 名 ``gemini`` 同时映射 aistudio / vertex，故按
    provider_id 而非 backend 静态接口判定。
    """
    return provider_id in _VIDEO_AUDIO_ALWAYS_BILLED_PROVIDERS


#: Three-level voice consistency tiers. The frontend `VoiceConsistencyTier` maps one to one; adding or removing tiers must be synced on both sides.
#: 声音一致性三级标识。前端 `VoiceConsistencyTier` 与之一一对应，档位增减须两侧同步。
VoiceConsistency = Literal["native", "soft", "none"]


def derive_voice_consistency(
    *,
    reference_audio_mode: str,
    generation_mode: str | None,
    has_audio: bool,
    character_voice_binding: CharacterVoiceBinding | None,
) -> VoiceConsistency:
    """Derive the three-level voice consistency tier (native / soft / none): model capability × effective generation_mode × binding method.

    The only derivation point in the whole repo: in-project scenarios come here via `_resolve_video_caps_for_model`, and
    catalog scenarios without project context call the same function from `server/routers/providers.py` with
    ``generation_mode=None`` / ``character_voice_binding=None``; the frontend does not copy a second formula.

    ``reference_audio_mode`` is compared by literal (``ReferenceAudioMode`` is a ``StrEnum``, the two can be compared
    directly with ``==``), so lib.config does not import lib.backends.video_backends (layering contract, config is the bottom layer).

    native implies an audio track: when generation_mode is not reference-to-video, or the project chose the prompt soft
    constraint (``character_voice_binding == "prompt"``, the default), it is always demoted to soft, never to none. The
    demotion is the master switch for the whole chain: the reference-audio warnings of script preview, the ``@音频N``
    numbering in prompts and execution-time reference audio wiring all only recognize ``native``, so the binding method
    need not be passed down the render chain layer by layer. ``has_audio`` asks "does the output have an audio track",
    not "is the switch controllable", so always-audio models (switch not controllable) count as having an audio track
    too; callers get this bit from :func:`builtin_video_audio_track` instead of each interpreting its own declaration.

    三级声音一致性标识派生（native / soft / none）：模型能力 × 生效 generation_mode × 绑定方式。

    全仓库唯一派生点：项目内场景经 `_resolve_video_caps_for_model` 走这里，无项目上下文的
    目录场景由 `server/routers/providers.py` 以 ``generation_mode=None`` /
    ``character_voice_binding=None`` 调同一函数，前端不复制第二份公式。

    ``reference_audio_mode`` 按字面量比较（``ReferenceAudioMode`` 是 ``StrEnum``，两者可
    直接 ``==``），不在 lib.config 层导入 lib.backends.video_backends（分层契约，config 是最底层）。

    native 蕴含有音轨：generation_mode 非参考生视频、或项目选的是提示词软约束
    （``character_voice_binding == "prompt"``，默认档）时一律降格 soft，不降到 none。降格是全链路
    的总闸：脚本预览的参考音频类 warning、提示词里的 ``@音频N`` 编号与执行期的参考音频挂线都只认
    ``native``，因此绑定方式不必再沿渲染链逐层传递。``has_audio`` 问的是「成片有没有音轨」而非
    「开关可不可控」，故恒有声（开关不可控）的型号同样算有音轨；调用方由
    :func:`builtin_video_audio_track` 得出该位，不各自解读一份声明。
    """
    if (
        reference_audio_mode == "direct"
        and generation_mode == "reference_video"
        and character_voice_binding == "reference_audio"
    ):
        return "native"
    return "soft" if has_audio else "none"


def _safe_dict(value: object, *, field: str) -> dict:
    """Make sure value can keep being parsed as a dict in a chain; anything not a dict (including None) degrades to an empty dict.

    project.json is a plaintext file; after users hand-edit it or external scripts break it, nested fields may become
    string / list, and a direct ``.get()`` would raise ``AttributeError``. None is an explicit "not configured" and
    degrades silently; other non-dict types log a warning (field name + actual type) to help locate the source of dirty data.

    确保 value 可当作 dict 继续链式解析；非 dict（含 None）一律降级为空 dict。

    project.json 是明文文件，用户手编或外部脚本写坏后，嵌套字段可能变成 string / list，
    直接 ``.get()`` 会抛 ``AttributeError``。None 是显式「未配置」，静默降级；其余非 dict
    类型记录 warning（字段名 + 实际类型）便于定位脏数据来源。
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    logger.warning(
        "project.json field %r has non-dict type %s, falling back to empty dict",
        field,
        type(value).__name__,
    )
    return {}


def _resolution_from_project(project: dict, provider_id: str, model_id: str) -> str | None:
    """project.model_settings (``provider/model`` composite key) > legacy video_model_settings > None.

    Each layer uses ``_safe_dict`` to defend against non-dict intermediate layers (see the ``_safe_dict`` docstring); leaf
    values are likewise restricted to non-empty strings and stripped — downstream ``constrain_durations`` does
    ``.strip().lower()`` on it, and int / list / dict left by hand-edited project.json or historical dirty data would raise
    ``AttributeError`` there. Non-strings and blank strings all continue falling back as "not configured", consistent
    with the tier semantics on the write side.

    project.model_settings（``provider/model`` 复合 key）> legacy video_model_settings > None。

    逐层用 ``_safe_dict`` 防御非 dict 中间层（见 ``_safe_dict`` docstring）；叶子值同样按
    非空字符串收口并去空白——下游 ``constrain_durations`` 对它做 ``.strip().lower()``，手改
    project.json 或历史脏数据留下的 int / list / dict 会在那里抛 ``AttributeError``。非字符串
    与空白串一律按「未配置」继续回退，与写入侧的档位语义一致。
    """
    key = f"{provider_id}/{model_id}"
    model_settings = _safe_dict(project.get("model_settings"), field="model_settings")
    override = _safe_dict(model_settings.get(key), field=f"model_settings.{key}").get("resolution")
    if cleaned := _clean_resolution(override, field=f"model_settings.{key}.resolution"):
        return cleaned
    video_model_settings = _safe_dict(project.get("video_model_settings"), field="video_model_settings")
    legacy = _safe_dict(video_model_settings.get(model_id), field=f"video_model_settings.{model_id}").get("resolution")
    return _clean_resolution(legacy, field=f"video_model_settings.{model_id}.resolution")


def _clean_resolution(value: object, *, field: str) -> str | None:
    """Non-empty string → stripped value; anything else (including non-strings) → None. / 非空字符串 → 去空白后的值；其余（含非字符串）→ None。"""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        logger.warning("project %s is %s, expected str; treating resolution as unset", field, type(value).__name__)
        return None
    return value.strip() or None


# ---------------------------------------------------------------------------
# Duration coupling constraints
#
# Durations are not decided by supported_durations alone: some models (currently all of Veo and MiniMax Hailuo) narrow
# the duration to a single value at high resolution or on the reference image path, declared on the model catalog
# (single source of truth). This is the
# only backend consumer — the candidate set is narrowed here before being handed to the LLM, written into the dynamic
# schema, or having its first item taken as the default, otherwise the produced duration would necessarily be rejected
# by the backend at execution.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 时长联动约束
#
# 时长并非只由 supported_durations 决定：部分型号（当前是 Veo 全系与 MiniMax 海螺）在高分辨率
# 或参考图路径下把时长收窄到单一取值，声明在模型目录上（单一真相源）。这里是
# 后端唯一的消费入口——候选集合在交给 LLM、写进动态 schema、或作为默认值取首项之前都经此收窄，
# 否则产出的时长在执行期必然被 backend 拒。
# ---------------------------------------------------------------------------


def constrain_durations(
    provider_id: str | None,
    model_id: str | None,
    durations: list[int],
    *,
    resolution: str | None = None,
    uses_reference_images: bool = False,
) -> list[int]:
    """Narrow candidates by the "resolution↔duration" and "reference image↔duration" constraints declared on the catalog for built-in models.

    Returns the original candidates when the model is not in the catalog. Custom provider constraints are obtained via
    :meth:`ConfigResolver.video_duration_constraints` and handed to :func:`apply_duration_constraints`.

    按内置型号在目录上声明的「分辨率↔时长」「参考图↔时长」约束收窄候选。

    型号不在目录中时返回原候选。自定义供应商的约束经 :meth:`ConfigResolver.video_duration_constraints`
    取得后交给 :func:`apply_duration_constraints`。
    """
    return apply_duration_constraints(
        durations,
        builtin_duration_constraints(provider_id, model_id),
        resolution=resolution,
        uses_reference_images=uses_reference_images,
    )


def builtin_duration_constraints(provider_id: str | None, model_id: str | None) -> DurationConstraints | None:
    """Duration coupling constraints declared on the catalog for a built-in model; None when the model is not in the catalog.

    内置型号在目录上声明的时长联动约束；型号不在目录中时为 None。"""
    model_info = catalog.get_model(provider_id, model_id) if provider_id and model_id else None
    if model_info is None:
        return None
    return DurationConstraints(
        by_resolution={res: tuple(allowed) for res, allowed in model_info.duration_resolution_constraints.items()},
        with_reference_images=tuple(model_info.reference_image_durations),
    )


def apply_duration_constraints(
    durations: list[int],
    constraints: DurationConstraints | None,
    *,
    resolution: str | None = None,
    uses_reference_images: bool = False,
) -> list[int]:
    """Narrow candidates by duration coupling constraints.

    The two constraints trigger independently and can apply together, taking the intersection. Returns the original
    candidates when there are no constraints. Returns an empty set when the intersection is empty, for the consumer to
    handle per stage. Resolution keys are compared in lowercase.

    按时长联动约束收窄候选。

    两条约束各自独立触发、可同时生效，取交集。没有约束时返回原候选。交集为空时返回空集，由消费方按
    阶段处理。分辨率键按小写比较。
    """
    if not durations or constraints is None:
        return durations
    allowed = list(durations)
    if uses_reference_images and constraints.with_reference_images:
        allowed = [d for d in allowed if d in constraints.with_reference_images]
    by_resolution = constraints.by_resolution.get(resolution.strip().lower()) if resolution else None
    if by_resolution:
        allowed = [d for d in allowed if d in by_resolution]
    return allowed


#: Causes of a duration being removed by coupling constraints: ``resolution`` = unavailable at the current resolution, ``reference`` = unavailable on the reference image path.
#: "Not in the model's full set at all" is not among them — that is not narrowing; consumers judge it as not in ``supported_durations``.
#: 时长被联动约束剔除的成因：``resolution`` = 当前分辨率下不可用，``reference`` = 参考图路径下不可用。
#: 「型号全集就不含」不在此列——那不是收窄，消费方按不在 ``supported_durations`` 内判定。
DurationExclusionReason = Literal["resolution", "reference"]


#: Cause value of the endpoint-fixed flag: the only value of ``*_endpoint_fixed_reason`` in the capability payload; the cause is None when the flag is false.
#: 端点固定标志的成因值：能力载荷里 ``*_endpoint_fixed_reason`` 的唯一取值，标志为假时成因为 None。
DURATION_ENDPOINT_FIXED_REASON = "endpoint"


def duration_endpoint_fixed_reason(fixed: bool) -> str | None:
    """The cause for the endpoint-fixed flag: :data:`DURATION_ENDPOINT_FIXED_REASON` when the flag is true, otherwise None.

    端点固定标志对应的成因：标志为真给 :data:`DURATION_ENDPOINT_FIXED_REASON`，否则 None。"""
    return DURATION_ENDPOINT_FIXED_REASON if fixed else None


class VisionCapabilityError(ValueError):
    """The resolved text model does not support image input (vision) and cannot run tasks that need vision.

    Carries structured fields for callers (e.g. user-facing routers) to localize as needed; ``str(exc)`` is an English
    technical message for logs / non-user-visible paths.

    解析出的文本模型不支持图像输入（vision），无法执行需要 vision 的任务。

    携带结构化字段供调用方（如面向用户的 router）按需本地化；``str(exc)`` 是英文技术
    消息，供 log / 非用户可见路径直接使用。"""

    def __init__(self, *, task_type: TextTaskType, provider_id: str, model_id: str):
        self.task_type = task_type
        self.provider_id = provider_id
        self.model_id = model_id
        super().__init__(
            f"text model {provider_id}/{model_id} does not support vision, cannot perform task {task_type.value}"
        )


class VideoBucketCapabilityError(ValueError):
    """Video resolution gate error: the resolved model lacks a capability its task type bucket requires, or the config reference is no longer usable.

    ``code`` is an errors catalog key and ``params`` its render parameters: routers can localize directly with
    ``_t(exc.code, **exc.params)``, and the worker stores it structurally encoded via
    ``lib.generation.task_failure.encode_failure``. ``str(exc)`` is an English technical message for logs / non-user-visible paths.

    视频解析闸报错：解析出的模型缺所属任务类型桶要求的能力，或配置引用已不可用。

    ``code`` 是 errors 目录 key、``params`` 是其渲染参数：router 可直接
    ``_t(exc.code, **exc.params)`` 本地化，worker 落库经 ``lib.generation.task_failure.encode_failure``
    结构化编码。``str(exc)`` 是英文技术消息，供 log / 非用户可见路径直接使用。"""

    def __init__(
        self,
        *,
        code: str,
        generation_type: VideoGenerationType,
        provider_id: str,
        model_id: str,
        message: str,
    ):
        self.code = code
        self.generation_type = generation_type
        self.provider_id = provider_id
        self.model_id = model_id
        self.params: dict[str, str] = {"provider": provider_id, "model": model_id}
        super().__init__(message)


class VideoSupportedDurationsError(ValueError):
    """The duration options declaration of the model row is missing (``missing``) during capability composition.

    ``provider_id`` / ``model_id`` are the converged effective identity. Consumers map by ``kind`` to the problem codes of
    their own routes without parsing ``str(exc)``; the message text stays as is, for logs and callers still catching
    ``ValueError``.

    能力合成时模型行的时长档位声明缺失（``missing``）。

    ``provider_id`` / ``model_id`` 是收敛后的有效身份。消费方按 ``kind`` 映射到各自路线的问题码，
    不解析 ``str(exc)``；消息文本保持原样，供 log 与仍按 ``ValueError`` 捕获的调用方使用。
    """

    kind: Literal["missing"]

    def __init__(self, kind: Literal["missing"], *, provider_id: str, model_id: str, message: str):
        self.kind = kind
        self.provider_id = provider_id
        self.model_id = model_id
        super().__init__(message)


class ImageBucketCapabilityError(ValueError):
    """Image resolution gate error: the resolved model lacks a capability its task type bucket (t2i / i2i) requires.

    ``code`` is from the same batch as the execution layer's ``lib.backends.image_backends.base.ImageCapabilityError``
    (``image_capability_missing_<桶>``): the same thing reported at resolution time and execution time, so read-side
    rendering and failure encoding do not diverge. ``params`` are its render parameters; ``str(exc)`` is an English
    technical message for logs / non-user-visible paths.

    图片解析闸报错：解析出的模型缺所属任务类型桶（t2i / i2i）要求的能力。

    ``code`` 与执行层 ``lib.backends.image_backends.base.ImageCapabilityError`` 同一批
    （``image_capability_missing_<桶>``）：同一件事在解析期与执行期报出，读侧渲染与失败编码不分叉。
    ``params`` 是其渲染参数；``str(exc)`` 是英文技术消息，供 log / 非用户可见路径直接使用。
    """

    def __init__(self, *, generation_type: str, provider_id: str, model_id: str):
        self.code = f"image_capability_missing_{generation_type}"
        self.generation_type = generation_type
        self.provider_id = provider_id
        self.model_id = model_id
        self.params: dict[str, str] = {"provider": provider_id, "model": model_id}
        super().__init__(
            f"image model {provider_id}/{model_id} lacks the capability required by the {generation_type} bucket"
        )


def _video_bucket_capability_missing(
    generation_type: VideoGenerationType, provider_id: str, model_id: str
) -> VideoBucketCapabilityError:
    return VideoBucketCapabilityError(
        code=f"video_capability_missing_{generation_type}",
        generation_type=generation_type,
        provider_id=provider_id,
        model_id=model_id,
        message=f"video model {provider_id}/{model_id} lacks the capability required by the {generation_type} bucket",
    )


def _video_bucket_reference_unavailable(
    generation_type: VideoGenerationType, provider_id: str, model_id: str
) -> VideoBucketCapabilityError:
    return VideoBucketCapabilityError(
        code="video_capability_reference_unavailable",
        generation_type=generation_type,
        provider_id=provider_id,
        model_id=model_id,
        message=f"configured video model {provider_id}/{model_id} is no longer resolvable for the {generation_type} bucket",
    )


def _ensure_text_model_vision_capable(task_type: TextTaskType, provider_id: str, model_id: str) -> None:
    """Check that the resolved built-in model supports image input; raise directly if not, no silent model switch.

    Judged only by the model catalog; models not in the catalog pass. For custom provider models see ``ConfigResolver._ensure_text_model_vision_capable``.

    校验解析出的内置模型支持图像输入；不满足直接报错，不静默换模型。

    只按模型目录判定，不在目录中的模型放行；自定义供应商的模型见 ``ConfigResolver._ensure_text_model_vision_capable``。"""
    model_info = catalog.get_model(provider_id, model_id)
    if model_info is not None and "vision" not in model_info.capabilities:
        raise VisionCapabilityError(task_type=task_type, provider_id=provider_id, model_id=model_id)


async def _load_project_snapshot(project_name: str | None) -> dict | None:
    """Read the project snapshot; reading project.json is file I/O, so it runs in a worker thread, off the event loop thread.

    读取项目快照；读 project.json 是文件 I/O，放进工作线程，不占事件循环线程。"""

    if not project_name:
        return None
    return await asyncio.to_thread(lambda: get_project_manager().load_project(project_name))


class ConfigResolver:
    """Runtime config resolver.

    A thin layer on top of ConfigService, providing:
    - the single place where defaults are defined
    - typed output (bool / tuple / dict)
    - built-in priority resolution (global config → project-level override)

    运行时配置解析器。

    作为 ConfigService 的上层薄封装，提供：
    - 唯一的默认值定义点
    - 类型化输出（bool / tuple / dict）
    - 内置优先级解析（全局配置 → 项目级覆盖）
    """

    # ── The single place where defaults are defined ──
    # Consistent with Seedance / Grok being on by default and with storyboard user expectations.
    # server/routers/system_config.py and lib/generation/media_generator.py both read it by referencing this constant.
    # ── 唯一的默认值定义点 ──
    # 与 Seedance / Grok 默认开启、storyboard 用户期望一致。
    # server/routers/system_config.py 与 lib/generation/media_generator.py 均通过引用此常量读取。
    _DEFAULT_VIDEO_GENERATE_AUDIO = True

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        _bound_session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._bound_session = _bound_session

    # ── Session management ── / ── Session 管理 ──

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[ConfigResolver]:
        """Open a shared session and return a ConfigResolver bound to it. / 打开共享 session，返回绑定到该 session 的 ConfigResolver。"""
        if self._bound_session is not None:
            yield self
        else:
            async with self._session_factory() as sess:
                yield ConfigResolver(self._session_factory, _bound_session=sess)

    @asynccontextmanager
    async def _open_session(self) -> AsyncGenerator[tuple[AsyncSession, ConfigService]]:
        """Get (session, ConfigService), reusing the bound session first. / 获取 (session, ConfigService)，优先复用 bound session。"""
        if self._bound_session is not None:
            yield self._bound_session, ConfigService(self._bound_session)
        else:
            async with self._session_factory() as session:
                yield session, ConfigService(session)

    # ── Public API ── / ── 公开 API ──

    async def video_generate_audio(self, project_name: str | None = None) -> bool:
        """Resolve video_generate_audio.

        Priority: project-level override > global config > default (True).

        解析 video_generate_audio。

        优先级：项目级覆盖 > 全局配置 > 默认值(True)。
        """
        async with self._open_session() as (_session, svc):
            return await self._resolve_video_generate_audio(svc, project_name)

    async def video_generate_audio_for_project(self, project: dict | None) -> bool:
        """Same as :meth:`video_generate_audio`, but takes the project dict the caller already loaded, without re-reading from disk.

        For callers that need to judge the user's silent intent **independently** of video capabilities resolution: a failure
        in capability resolution (e.g. ``video_capabilities_for_model``) should not also lose this value — it does not depend
        on the capability interface anyway.

        同 :meth:`video_generate_audio`，但接收调用方已加载的 project 字典，不重复读盘。

        供需要与 video capabilities 解析**独立**判定用户无声意图的调用方使用：能力解析（如
        ``video_capabilities_for_model``）失败不应连带丢失这个值——它本就不依赖能力接口。
        """
        async with self._open_session() as (_session, svc):
            return await self._resolve_video_generate_audio_from_project(svc, project)

    async def default_video_backend(self) -> tuple[str, str]:
        """Return the system-level default (provider_id, model_id) (without project-level overrides).

        返回系统级默认 (provider_id, model_id)（不含项目级覆盖）。"""
        async with self._open_session() as (session, svc):
            return await self._resolve_default_video_backend(svc, session)

    async def video_backend(self, project_name: str | None = None) -> tuple[str, str]:
        """Resolve the video (provider_id, model_id) the current project should use.

        Priority: project-level `project.json.video_backend` > system setting `default_video_backend` >
        system default `DEFAULT_VIDEO_BACKEND` > auto-resolve (pick the first ready one in registry order).

        Returns the literal config result without custom provider identity convergence — for config display and "which one
        is currently chosen" checks; to get the identity actually executed at runtime use ``resolve_video_backend()``.

        解析当前项目应使用的视频 (provider_id, model_id)。

        优先级：项目级 `project.json.video_backend` > 系统设置 `default_video_backend` >
        系统默认 `DEFAULT_VIDEO_BACKEND` > auto-resolve（按 registry 顺序挑第一个 ready）。

        返回字面配置结果，不做自定义供应商的身份收敛——供配置展示与「当前选的是哪个」类
        判断使用；要拿运行时实际执行的身份请用 ``resolve_video_backend()``。
        """
        async with self._open_session() as (session, svc):
            return await self._resolve_video_backend(svc, session, project_name)

    async def resolve_image_backend(
        self,
        project: dict | None,
        payload: dict | None,
        *,
        generation_type: Literal["t2i", "i2i"],
    ) -> ProviderModel:
        """Resolve the ProviderModel an image task should use.

        Priority: payload > project bucket (``image_provider_<generation_type>``) > project default (``default_image_backend``)
        > global bucket (``default_image_backend_<generation_type>``) > global default (``default_image_backend``) > auto inference.
        Buckets are optional overrides; no value (including explicitly cleared) falls back to the default layer (``docs/adr/0054``).
        ``generation_type`` decides the t2i or i2i slot (see ``docs/adr/0001``). No provider normalization at all.

        Raises:
            ImageBucketCapabilityError: (ValueError subclass) the resolved custom model lacks a capability the bucket needs
                (``_ensure_image_bucket_capability``); no silent model switch.

        解析图片任务应使用的 ProviderModel。

        优先级：payload > 项目桶（``image_provider_<generation_type>``）> 项目默认（``default_image_backend``）
        > 全局桶（``default_image_backend_<generation_type>``）> 全局默认（``default_image_backend``）> 自动推断。
        桶是可选覆盖，无值（含显式清空）回退默认层（``docs/adr/0054``）。
        ``generation_type`` 决定走 t2i 还是 i2i 槽（见 ``docs/adr/0001``）。不做任何 provider 归一化。

        Raises:
            ImageBucketCapabilityError: （ValueError 子类）解析出的自定义模型缺该桶所需能力
                （``_ensure_image_bucket_capability``），不静默换模型。
        """
        async with self._open_session() as (session, svc):
            return await self._resolve_image_provider_model(svc, session, project, payload, generation_type)

    async def resolve_video_backend(
        self,
        project: dict | None,
        payload: dict | None,
        *,
        generation_type: VideoGenerationType | None = None,
    ) -> ProviderModel:
        """Resolve the ProviderModel a video task should use.

        The payload always has top priority: a materialized execution identity already written into the task type bucket key
        ``video_provider_<generation_type>`` wins. After that, two paths by ``generation_type`` (``docs/adr/0054``):

        - ``generation_type`` given (``"i2v"`` / ``"r2v"``): the four-level skeleton project bucket (``video_provider_<generation_type>``)
          > project default (``video_backend``) > global bucket (``default_video_backend_<generation_type>``) > global default
          (``default_video_backend``) > auto inference, empty buckets falling back to the default layer. The result passes the
          capability gate: when the model lacks a capability the bucket needs, or the config reference is no longer usable
          (model deleted / capability changed / provider deleted), raise ``VideoBucketCapabilityError``, no silent model
          switch; when a built-in model is not in the provider's video model list, raise ``ModelNotInCatalogError``. On a
          payload hit the capability gate is skipped — enqueued tasks run per payload as usual, without retroactive checks;
          but a materialized identity still passes the identity availability check, and a dangling one raises too.
        - ``generation_type`` None: the same skeleton without the bucket layer, project default (``video_backend``) > global
          default (``default_video_backend``) > auto inference, no capability gate; when a custom provider's model does not
          exist, is disabled, or the endpoint's media_type is not video, converge to the provider's default enabled video
          model (**the effective runtime identity**), raising ``ValueError`` if there is no usable default. For callers that
          do not commit to capabilities (cost estimation, rate-limit routing fallback).

        No provider id normalization. If you only need the literal config result (without convergence), use ``video_backend()`` instead.

        解析视频任务应使用的 ProviderModel。

        payload 恒为最高优先级：已写入任务类型桶键 ``video_provider_<generation_type>`` 的物化执行身份优先。
        其后按 ``generation_type`` 分两条路径（``docs/adr/0054``）：

        - ``generation_type`` 给定（``"i2v"`` / ``"r2v"``）：走四级骨架 项目桶（``video_provider_<generation_type>``）
          > 项目默认（``video_backend``）> 全局桶（``default_video_backend_<generation_type>``）> 全局默认
          （``default_video_backend``）> 自动推断，空桶回退默认层。解析结果过能力闸：模型缺该桶
          所需能力、或配置引用已不可用（模型被删 / 能力被改 / 供应商被删）时抛
          ``VideoBucketCapabilityError``，不静默换模型；内置模型不在供应商的视频模型清单中时抛
          ``ModelNotInCatalogError``。payload 命中时跳过能力闸——已入队任务按 payload 照常执行，
          不回头补校验；但已物化的身份仍过身份可用性校验，悬空同样报错。
        - ``generation_type`` 为 None：同一骨架去掉桶层，项目默认（``video_backend``）> 全局默认
          （``default_video_backend``）> 自动推断，无能力闸；自定义供应商的 model 不存在、
          已禁用或 endpoint 的 media_type 不是 video 时，收敛到该 provider 默认启用的 video
          model（**运行时有效身份**），无可用默认则抛 ``ValueError``。供不承诺能力的调用方
          （费用估算、限流路由兜底）使用。

        provider id 不做归一化。只要字面配置结果（不经收敛）请改用 ``video_backend()``。
        """
        async with self._open_session() as (session, svc):
            return await self._resolve_video_provider_model(svc, session, project, payload, generation_type)

    async def resolve_resolution(self, project: dict, provider_id: str, model_id: str) -> str | None:
        """By project.model_settings → legacy video_model_settings → custom provider default → None.

        None means "do not pass the SDK resolution parameter at call time" (see ``docs/adr/0019``). The first two levels read
        the project dict only, with no side effects; the custom provider default (``CustomProviderModel.resolution``) needs
        the DB, so the whole method is async and done within one session.

        按 project.model_settings → legacy video_model_settings → 自定义供应商默认 → None。

        None 代表"调用时不传 SDK resolution 参数"（见 ``docs/adr/0019``）。前两级纯读 project
        dict、无副作用；自定义供应商默认（``CustomProviderModel.resolution``）需 DB，故本方法
        整体为 async 并在同一 session 内完成。
        """
        from_project = _resolution_from_project(project, provider_id, model_id)
        if from_project:
            return from_project
        # Only custom providers have a DB-side default; preset providers return None right here, to avoid opening a session for
        # nothing on every generation task on the hot path (the original standalone module also checked is_custom_provider before touching the DB).
        # 仅自定义供应商才有 DB 侧默认；预置供应商在此直接 None，避免为热路径上的
        # 每次生成任务白开一个 session（原独立模块正是先判 is_custom_provider 再触 DB）。
        if not provider_id or not model_id or not is_custom_provider(provider_id):
            return None
        async with self._open_session() as (session, _svc):
            return await self._resolve_custom_resolution_default(session, provider_id, model_id)

    async def _resolve_custom_resolution_default(
        self,
        session: AsyncSession,
        provider_id: str,
        model_id: str,
    ) -> str | None:
        """The custom provider model's default resolution (``CustomProviderModel.resolution``); None for everything else.

        自定义供应商的模型默认 resolution（``CustomProviderModel.resolution``），其他一律 None。"""
        if not provider_id or not model_id or not is_custom_provider(provider_id):
            return None
        try:
            db_id = parse_provider_id(provider_id)
        except ValueError:
            return None
        repo = CustomProviderRepository(session)
        model = await repo.get_model_by_ids(db_id, model_id)
        return model.resolution if (model and model.resolution) else None

    async def default_audio_backend(self) -> tuple[str, str]:
        """Return the system-level default audio (provider_id, model_id) (without project-level overrides).

        返回系统级默认音频 (provider_id, model_id)（不含项目级覆盖）。"""
        async with self._open_session() as (session, svc):
            return await self._resolve_default_audio_backend(svc, session)

    async def default_narration_tts(self) -> tuple[ProviderModel, str, float | None]:
        """Prefilled values for a new TTS project: global default audio (provider, model), narration voice and speech rate.

        Raises ValueError when no audio provider is configured. After project creation these global values are no longer read (docs/adr/0089).

        新建 TTS 项目的预填值：全局默认音频 (provider, model)、旁白音色与配音语速。

        未配置任何音频供应商时抛 ValueError。项目创建后不再读取这些全局值（docs/adr/0089）。
        """
        async with self._open_session() as (session, svc):
            provider_id, model_id = await self._resolve_default_audio_backend(svc, session)
            return (
                ProviderModel(provider_id, model_id),
                await svc.get_narration_voice(),
                await svc.get_narration_speed(),
            )

    async def resolve_audio_backend(
        self,
        project: dict | None,
        payload: dict | None,
    ) -> ProviderModel:
        """Resolve the ProviderModel a speech synthesis task should use.

        Priority: payload (``audio_provider`` carried by historical tasks) > project (``audio_backend``) > global default.
        Speech tasks have no task type dimension. No provider normalization at all.

        解析语音合成任务应使用的 ProviderModel。

        优先级：payload（历史任务携带的 ``audio_provider``）> project（``audio_backend``）> 全局默认。
        语音任务无任务类型维度。不做任何 provider 归一化。
        """
        async with self._open_session() as (session, svc):
            return await self._resolve_audio_provider_model(svc, session, project, payload)

    async def video_capabilities_for_project(
        self,
        project: dict,
        *,
        generation_type: VideoGenerationType | None = None,
    ) -> dict:
        """Resolve the combined capabilities of the current video model of a project the caller already loaded + the user's project preferences.

        The model picks its bucket by the project's ``generation_mode`` (image-to-video / grid → i2v, reference-to-video →
        r2v) and then goes through the same resolution entry as execution, so it always answers "the model the current config
        will really execute" (``docs/adr/0054``). The generation mode is fixed at creation and the whole project generates in
        one mode, so resolution needs no episode context.

        When ``generation_type`` is not given, the bucket is picked by the project's generation_mode; when given, resolve by
        that bucket — for the read side that splits by video unit within reference-to-video (video units without reference
        images take tiers / prices from the i2v bucket).

        ``supported_durations`` is the model's full declared set; the options narrowed by request resolution and reference
        images come from video request facts (``lib.generation.video_request_facts``), not assembled in capability composition.

        Returns:
            {
              "provider_id": str,
              "model": str,
              "supported_durations": list[int],    # from the model (single source of truth); may be empty on ComfyUI endpoints
              "max_duration": int,                 # derived as max(supported_durations); 0 when empty
              "max_reference_images": int,         # backend declaration; custom: the composed effective value
              "first_frame": bool,                 # effective value (system verdict ⊕ user override), same source as the execution layer
              "last_frame": bool,                  # same as above
              "generate_audio": bool,              # pricing parameter after the backend's default execution tier applies
              "requested_generate_audio": bool,    # requested value of the user's silent intent in project.json/global settings, not the pricing rule
              "max_reference_audio_count": int,    # max reference audio segments per request (backend declaration)
              "reference_audio_per_image": bool,   # whether audio must attach per segment to specific reference items (backend declaration)
              "source": "registry" | "custom",
              "default_duration": int | None,      # the preference the user set in project.json
              "episode_target_duration": int | None,  # the user's per-episode target duration (seconds), a soft preference, not a model capability
              "content_mode": str | None,
              "generation_mode": str | None,       # project generation mode (None without project context)
              "voice_consistency": "native" | "soft" | "none",  # model capability × generation_mode × binding method
            }

        Raises:
            ValueError: when video_backend resolution fails / the model is not found / supported_durations is empty
                (except ComfyUI endpoints: on that protocol the empty set is the legitimate state "duration not driven by ArcReel").
            VideoBucketCapabilityError: (ValueError subclass) the resolved model lacks a capability the bucket needs, or the
                config reference is no longer usable.

        解析调用方已加载的项目当前视频 model 的综合能力 + 用户项目偏好。

        model 按项目 ``generation_mode`` 定桶（图生视频 / 宫格 → i2v，参考生视频 → r2v）后走与
        执行相同的解析入口，回答的始终是「当前配置真正会执行的那个模型」（``docs/adr/0054``）。
        生成模式创建即定、整个项目按同一种模式生成，解析因此不需要剧集上下文。

        ``generation_type`` 未给定时按项目 generation_mode 定桶；给定时按指定桶解析——供参考生视频
        内按视频单元分流的读侧（无参考图的视频单元按 i2v 桶取档 / 计价）使用。

        ``supported_durations`` 是型号声明全集；按请求分辨率与参考图收窄的档位由视频请求事实给出
        （``lib.generation.video_request_facts``），不在能力合成里组装。

        Returns:
            {
              "provider_id": str,
              "model": str,
              "supported_durations": list[int],    # 来自 model (单一真相源)；ComfyUI 端点上可为空集
              "max_duration": int,                 # max(supported_durations) 派生；空集时为 0
              "max_reference_images": int,         # backend 声明；custom: 合成后的生效值
              "first_frame": bool,                 # 生效值（系统判定 ⊕ 用户覆盖），与执行层同源
              "last_frame": bool,                  # 同上
              "generate_audio": bool,              # backend 默认执行档生效后的计价参数
              "requested_generate_audio": bool,    # 用户在 project.json/全局设置里的无声意图请求值，非计价口径
              "max_reference_audio_count": int,    # 每请求可携带的参考音频段数上限（backend 声明）
              "reference_audio_per_image": bool,   # 音频是否须逐段挂在具体参考素材项上（backend 声明）
              "source": "registry" | "custom",
              "default_duration": int | None,      # 用户在 project.json 里设置的偏好
              "episode_target_duration": int | None,  # 用户设置的单集目标时长（秒）软偏好，非模型能力
              "content_mode": str | None,
              "generation_mode": str | None,       # 项目生成模式（无项目上下文时 None）
              "voice_consistency": "native" | "soft" | "none",  # 模型能力 × generation_mode × 绑定方式
            }

        Raises:
            ValueError: 当 video_backend 解析失败 / model 找不到 / supported_durations 为空
                （ComfyUI 端点除外：该协议上空集是「时长不由 ArcReel 驱动」的合法态）。
            VideoBucketCapabilityError: （ValueError 子类）解析出的模型缺该桶所需能力，或配置
                引用已不可用。
        """
        async with self._open_session() as (session, svc):
            return await self._resolve_video_capabilities_from_project(
                svc, session, project, generation_type=generation_type
            )

    async def video_duration_constraints(self, provider_id: str, model_id: str) -> DurationConstraints | None:
        """The video model's duration coupling constraints (model facts); None when there are none.

        Built-in models take them from the catalog; custom provider models take capability override > matching entry, with
        no constraints when no entry matches. ComfyUI endpoint durations are derived only from node bindings and have no
        coupling constraints. None when the model row or endpoint cannot be resolved: broken references are reported by
        capability resolution, not repeated here.

        视频模型的时长联动约束（模型事实）；没有约束时为 None。

        内置模型取目录；自定义供应商的模型取能力覆盖 > 对应条目，对应不上条目时不加约束。ComfyUI 端点的
        时长只从节点绑定推导，没有联动约束。模型行或端点解析不出时为 None：引用失效由能力解析报错，
        这里不重复。
        """
        if not is_custom_provider(provider_id):
            return builtin_duration_constraints(provider_id, model_id)
        from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
        from lib.custom_provider.model_facts import video_model_facts
        from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository

        async with self._open_session() as (session, _svc):
            try:
                model = await CustomProviderRepository(session).get_model_by_ids(
                    parse_provider_id(provider_id), model_id
                )
                if model is None:
                    return None
                endpoint_spec = await resolve_endpoint_spec(model.endpoint, CustomEndpointRepository(session).get)
            except ValueError:
                return None
            if endpoint_spec.endpoint_durations is not None:
                return None
            return video_model_facts(
                model_id, model.capability_overrides, entry_binding=model.entry_binding
            ).duration_constraints

    async def video_audio_track(
        self, provider_id: str, model_id: str, *, generation_type: VideoGenerationType
    ) -> str | None:
        """The video model's output audio track form on this task type bucket (a ``VideoAudioMode`` literal); None when there is no signal.

        For built-in models see :func:`builtin_video_audio_track`. Custom provider models only recognize the user's
        capability override for the audio track form, the same rule as voice consistency derivation in
        :meth:`video_capabilities_for_model`; None when there is no override or the model row or endpoint cannot be resolved,
        callers treat it as "no signal, no tightening", and broken references are reported by capability resolution, not repeated here.

        视频模型在该任务类型桶上的成片音轨形态（``VideoAudioMode`` 的字面量）；没有信号时为 None。

        内置模型见 :func:`builtin_video_audio_track`。自定义供应商的模型只认用户对音轨形态的能力覆盖，与
        :meth:`video_capabilities_for_model` 派生声音一致性同一口径；没有覆盖、模型行或端点解析不出时为 None，
        调用方按「无信号不收紧」处理，引用失效由能力解析报错，这里不重复。
        """
        if not is_custom_provider(provider_id):
            return builtin_video_audio_track(provider_id, model_id, generation_type=generation_type)
        from lib.custom_provider.capabilities import (
            overridden_audio_track,
            synthesize_video_capabilities_with_overrides,
        )
        from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
        from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository

        async with self._open_session() as (session, _svc):
            try:
                model = await CustomProviderRepository(session).get_model_by_ids(
                    parse_provider_id(provider_id), model_id
                )
                if model is None:
                    return None
                endpoint_spec = await resolve_endpoint_spec(model.endpoint, CustomEndpointRepository(session).get)
                caps, applied_overrides = synthesize_video_capabilities_with_overrides(
                    endpoint=model.endpoint,
                    model_id=model_id,
                    overrides=model.capability_overrides,
                    endpoint_spec=endpoint_spec,
                )
            except ValueError:
                return None
        return overridden_audio_track(caps, applied_overrides, generation_type)

    async def video_capabilities_for_model(
        self,
        provider_id: str,
        model_id: str,
        project: dict | None = None,
        *,
        generation_type: VideoGenerationType | None = None,
    ) -> dict:
        """Read the video capabilities of the given provider/model without resolving the provider again.

        For the execution layer: the caller has already resolved the ProviderModel actually to be called via
        `resolve_video_backend(project, payload)` (including historical task payload overrides); getting capabilities with
        this variant ensures the supported_durations the duration guard relies on match the model actually called, avoiding
        the mismatch of "validating the payload-resolved model with the project default model's capabilities".

        The input identity is still converged once more (same rules as ``resolve_video_backend``), so passing the literal
        config directly also gets the effective identity's capabilities; raises ``ValueError`` when a custom provider has no
        usable default model.

        Two-dimensional values like voice consistency are derived from ``project``'s generation mode; when
        ``generation_type`` is given explicitly, per-path capability bits are derived for that task type bucket (the audio
        track form forks by execution subpath, see :func:`builtin_video_audio_track`) — the execution layer knows which bucket
        the task falls in, and only by passing it in gets a result shaped like the actual request.

        读取指定 provider/model 的视频能力，不再二次解析 provider。

        供执行层使用：调用方已通过 `resolve_video_backend(project, payload)` 解析出实际
        要调用的 ProviderModel（含历史任务 payload 覆盖），用此变体取能力可保证 duration
        守卫所依据的 supported_durations 与实际调用的 model 一致，避免「按项目默认 model
        的能力去校验 payload 解析出的 model」的错配。

        入参身份仍会再收敛一次（口径同 ``resolve_video_backend``），因此直接传字面配置也能
        拿到有效身份的能力；自定义供应商无可用默认 model 时抛 ``ValueError``。

        声音一致性等二维值按 ``project`` 的生成模式派生；``generation_type`` 显式给定时按该任务类型桶派生
        逐路径的能力位（音轨形态按执行子路径分叉，见 :func:`builtin_video_audio_track`）——
        执行层已知任务落在哪个桶，传进来才能拿到与实际请求同形的结果。
        """
        async with self._open_session() as (session, svc):
            return await self._resolve_video_caps_for_model(
                svc, session, provider_id, model_id, project, generation_type=generation_type
            )

    async def video_pricing_generate_audio(
        self,
        provider_id: str,
        model_id: str,
        project: dict | None = None,
    ) -> bool:
        """The effective ``generate_audio`` for cost estimation: read the capability interface, degrade when it cannot resolve, never raise.

        Capability resolution raises for model ids delisted from the catalog, and estimation should not be interrupted by
        that (price lookup records it as unpriced). Degradation has two layers: providers that always bill with audio take
        True by provider-level rule; other providers have no info on the default execution tier and can only fall back to the
        request value (the backend also sends and settles by the request value); always taking False would underestimate
        these historical models at the silent tier.

        费用预估用的有效 ``generate_audio``：读能力接口，解析不出时降级，绝不抛错。

        能力解析会对目录里已下线的 model id 抛错，估算不应因此中断（价目查询对它记为未定价）。降级口径分两层：
        恒含音出账的 provider 按 provider 级规则取 True；其余 provider 没有默认执行档的信息，
        只能回到请求值（backend 也正是照请求值下发并结算），若一律取 False，这些历史 model
        会被按静音档低估。
        """
        try:
            caps = await self.video_capabilities_for_model(provider_id, model_id, project)
        except Exception as exc:
            logger.info(
                "视频能力解析失败（%s/%s），计价 generate_audio 降级到 provider 级规则与请求值：%s",
                provider_id,
                model_id,
                exc,
            )
            if _video_audio_always_billed(provider_id):
                return True
            async with self._open_session() as (_session, svc):
                return await self._resolve_video_generate_audio_from_project(svc, project)
        return bool(caps["generate_audio"])

    async def default_image_backend_t2i(self) -> tuple[str, str]:
        """Return (provider_id, model_id), the T2I default. / 返回 (provider_id, model_id)，T2I 默认。"""
        async with self._open_session() as (session, svc):
            return await self._resolve_default_image_backend(svc, session, "t2i")

    async def default_image_backend_i2i(self) -> tuple[str, str]:
        """Return (provider_id, model_id), the I2I default. / 返回 (provider_id, model_id)，I2I 默认。"""
        async with self._open_session() as (session, svc):
            return await self._resolve_default_image_backend(svc, session, "i2i")

    async def default_image_backend(self) -> tuple[str, str]:
        """Compatibility shim: old callers can still call it; returns the T2I variant. / 兼容 shim：旧调用方仍可调；返回 T2I 变体。"""
        return await self.default_image_backend_t2i()

    async def auto_resolved_backends(self) -> dict[AutoResolvePurpose, tuple[str, str] | None]:
        """The (provider_id, model_id) auto inference picks for each purpose when all project and global layers are empty; None when nothing can be picked.

        Read-only preview, for the settings UI to show the actual "auto select" model and query capabilities by it.

        各用途在项目与全局各层都留空时自动推断选中的 (provider_id, model_id)，选不出即 None。

        只读预览，供设置界面显示「自动选择」的实际模型、并按它查询能力。
        """
        async with self._open_session() as (session, svc):
            return await self._resolve_auto_backends(svc, session)

    async def provider_config(self, provider_id: str) -> dict[str, str]:
        """Get a single provider config. / 获取单个供应商配置。"""
        async with self._open_session() as (session, svc):
            return await self._resolve_provider_config(svc, session, provider_id)

    async def all_provider_configs(self) -> dict[str, dict[str, str]]:
        """Get all provider configs in bulk. / 批量获取所有供应商配置。"""
        async with self._open_session() as (session, svc):
            return await self._resolve_all_provider_configs(svc, session)

    async def reference_payload_limits(self, provider_id: str | None) -> tuple[int, int]:
        """Resolve (total_max_bytes, single_max_bytes) for reference upload copies.

        Priority: per-provider config override > the service layer's conservative generic default. provider_id None (zero
        config / generic limit scenarios) returns the default tuple directly without touching the DB. Returns a plain int
        tuple and does not import reference_compression, to avoid dragging PIL indirectly into the widely depended-on resolver.

        解析参考上传副本的 (total_max_bytes, single_max_bytes)。

        优先级：per-provider 配置覆盖 > service 层保守通用默认。provider_id 为 None（零配置 /
        通用上限场景）直接返回默认元组、不触 DB。返回纯 int 元组、不 import reference_compression，
        避免把 PIL 间接拖进被广泛依赖的 resolver。
        """
        if provider_id is None:
            return DEFAULT_REFERENCE_TOTAL_MAX_BYTES, DEFAULT_REFERENCE_SINGLE_MAX_BYTES
        async with self._open_session() as (session, svc):
            return await self._resolve_reference_payload_limits(svc, session, provider_id)

    # ── Internal resolution methods (independently testable, take an already created svc) ── / ── 内部解析方法（可独立测试，接收已创建的 svc） ──

    async def _resolve_video_generate_audio(
        self,
        svc: ConfigService,
        project_name: str | None,
    ) -> bool:
        project = await _load_project_snapshot(project_name)
        return await self._resolve_video_generate_audio_from_project(svc, project)

    async def _resolve_video_generate_audio_from_project(
        self,
        svc: ConfigService,
        project: dict | None,
    ) -> bool:
        raw = await svc.get_setting("video_generate_audio", "")
        value = _parse_bool(raw) if raw else self._DEFAULT_VIDEO_GENERATE_AUDIO

        if project is not None:
            override = project.get("video_generate_audio")
            if override is not None:
                value = _parse_bool(override) if isinstance(override, str) else bool(override)

        return value

    async def _resolve_default_video_backend(self, svc: ConfigService, session: AsyncSession) -> tuple[str, str]:
        """Resolve the default video backend at the global layer only: global default key > auto inference.

        Uses the four-level skeleton without a project (project=None skips the project layer); the keys have no bucket layer (``_VIDEO_DEFAULT_LAYERED_KEYS``).

        仅全局层解析视频默认 backend：全局默认键 > 自动推断。

        走四级骨架但不带项目（project=None 跳过项目层），键位不含桶层（``_VIDEO_DEFAULT_LAYERED_KEYS``）。
        """
        return await self._resolve_layered_backend(svc, session, None, _VIDEO_DEFAULT_LAYERED_KEYS)

    async def _resolve_video_backend(
        self,
        svc: ConfigService,
        session: AsyncSession,
        project_name: str | None,
    ) -> tuple[str, str]:
        """Three-level resolution of the video backend the current project should use: project default > global default > auto inference.

        Uses the bucketless keys of the four-level skeleton (``_VIDEO_DEFAULT_LAYERED_KEYS``); the absent bucket layer is skipped by the skeleton.

        三级解析当前项目应使用的 video backend：项目默认 > 全局默认 > 自动推断。

        走四级骨架的不定桶键位（``_VIDEO_DEFAULT_LAYERED_KEYS``），桶层缺席由骨架跳过。
        """
        project = await _load_project_snapshot(project_name)
        return await self._resolve_layered_backend(svc, session, project, _VIDEO_DEFAULT_LAYERED_KEYS)

    async def _resolve_layered_backend(
        self,
        svc: ConfigService,
        session: AsyncSession,
        project: dict | None,
        keys: _LayeredBackendKeys,
    ) -> tuple[str, str]:
        """The four-level "default + task type bucket" resolution skeleton: project bucket > project default > global bucket > global default > auto inference.

        Media type agnostic; each layer's keys are declared by ``_LayeredBackendKeys`` (see ``docs/adr/0054``). Project layer
        fields accept bare-provider overrides (``_parse_project_provider``); the global layer requires the full
        ``provider/model`` form. The payload layer and runtime identity convergence (e.g. effective identity convergence for
        video custom providers) are not part of the skeleton and are handled by each media's caller outside it.

        「默认 + 任务类型桶」四级解析骨架：项目桶 > 项目默认 > 全局桶 > 全局默认 > 自动推断。

        媒体类型无关，各层键位由 ``_LayeredBackendKeys`` 声明（见 ``docs/adr/0054``）。项目层
        字段兼容裸供应商覆盖（``_parse_project_provider``）；全局层要求 ``provider/model``
        完整形态。payload 层与运行时身份收敛（如视频自定义供应商的有效身份收敛）不属于
        骨架，由各媒体的调用方在骨架外处理。
        """
        if project:
            for project_key in (keys.project_bucket_key, keys.project_default_key):
                if project_key is None:
                    continue
                raw = project.get(project_key)
                parsed = _parse_project_provider(raw, keys.media_type)
                if parsed is not None:
                    return parsed
                _reject_bare_builtin_without_default(raw, keys.media_type)
        settings = await svc.get_all_settings()
        if keys.global_bucket_key is not None:
            raw = settings.get(keys.global_bucket_key, "")
            if "/" in raw:
                return ConfigService._parse_backend(raw, keys.parse_fallback)
        if keys.global_default_key is not None:
            raw = settings.get(keys.global_default_key, "")
            if "/" in raw:
                return ConfigService._parse_backend(raw, keys.parse_fallback)
        return await self._auto_resolve_backend(svc, session, keys.media_type, keys.generation_type)

    async def _resolve_image_provider_model(
        self,
        svc: ConfigService,
        session: AsyncSession,
        project: dict | None,
        payload: dict | None,
        generation_type: Literal["t2i", "i2i"],
    ) -> ProviderModel:
        """Resolve the image ProviderModel payload first; without payload use the four-level skeleton.

        The payload layer keeps the canonical ``payload>project>global`` skeleton and accepts ``image_provider`` /
        ``image_model`` keys — tasks serialized in that format resolve by them. Image tasks do not lock the execution
        identity (short task lifetime, small window for config drift while queued), so the payload layer has no task type
        bucket key. The payload provider must be a known provider (see
        ``_trusted_payload_provider``), otherwise it is not trusted and falls back to the skeleton (``_resolve_layered_backend``,
        keys see ``_IMAGE_LAYERED_KEYS``).

        Identities resolved by both layers pass the capability gate (``_ensure_image_bucket_capability``): image-side buckets
        are only determined at execution (``docs/adr/0001``), and the payload layer has no materialized bucket key to exempt it.

        payload 优先解析图片 ProviderModel，无 payload 时走四级骨架。

        payload 层保留 ``payload>project>global`` 的规范骨架，接受 ``image_provider`` /
        ``image_model`` 键——按该格式序列化的任务据此解析。图片任务不锁定执行身份（任务周期
        短，排队期间配置漂移的窗口小），故 payload 层无任务类型桶键。payload 供应商须是已知
        供应商（见
        ``_trusted_payload_provider``），否则不予信任、回退骨架（``_resolve_layered_backend``，
        键位见 ``_IMAGE_LAYERED_KEYS``）。

        两层解析出的身份都过能力闸（``_ensure_image_bucket_capability``）：图片侧的桶只在执行时
        才确定（``docs/adr/0001``），payload 层没有已物化的桶键可豁免。
        """
        selected: ProviderModel | None = None
        if payload:
            provider_id = _trusted_payload_provider(payload.get("image_provider"))
            if provider_id is not None:
                model = _payload_model_or_default(payload.get("image_model"), provider_id, "image")
                if model is not None:
                    selected = ProviderModel(provider_id, model)
        if selected is None:
            provider_id, model_id = await self._resolve_layered_backend(
                svc, session, project, _IMAGE_LAYERED_KEYS[generation_type]
            )
            selected = ProviderModel(provider_id, model_id)
        await self._ensure_image_bucket_capability(session, selected, generation_type)
        return selected

    async def _ensure_image_bucket_capability(
        self,
        session: AsyncSession,
        selected: ProviderModel,
        generation_type: Literal["t2i", "i2i"],
    ) -> None:
        """Capability gate: raise directly when the resolved custom model lacks the capability the bucket needs, no silent model switch.

        The verdict shares one rule with the bucket candidate dropdown (``lib.backends.generation_type_buckets``), via
        ``lib.custom_provider.default_models.lacks_bucket``. Two differences from
        ``_ensure_video_bucket_capability``:

        - Only judges custom providers. Bucket capabilities of built-in image models are enforced by the execution layer's
          ``MediaGenerator`` by the same effective capabilities (``lib.backends.image_capabilities``)
          (``image_capability_missing_*``, the same batch of codes as this gate).
        - Broken references (model deleted / disabled / endpoint changed to another media type) are not reported here: the
          image side by design falls back to the bucket's default model (``lib.custom_provider.loader``), and the fallback
          itself is filtered by bucket too.

        能力闸：解析出的自定义模型不具备该桶所需能力时直接报错，不静默换模型。

        判定与桶候选下拉（``lib.backends.generation_type_buckets``）共用一份口径，经
        ``lib.custom_provider.default_models.lacks_bucket``。与
        ``_ensure_video_bucket_capability`` 两处不同：

        - 只判自定义供应商。内置图片模型的桶能力由执行层 ``MediaGenerator`` 按同一份生效能力
          （``lib.backends.image_capabilities``）把关（``image_capability_missing_*``，与本闸同一批 code）。
        - 引用失效（模型被删 / 禁用 / endpoint 改成别的媒体类型）不在此报错：图片侧按设计回退到
          该桶的默认模型（``lib.custom_provider.loader``），回退本身也按桶过滤。
        """
        if not is_custom_provider(selected.provider_id):
            return

        # Deferred import: the layering contract (pyproject.toml [tool.importlinter]) puts lib.config as a lower layer,
        # and the assembly layer holding this symbol in turn depends on lib.config, so a module-level import would form a cycle.
        # 延迟导入：分层契约（pyproject.toml [tool.importlinter]）以 lib.config 为下层，
        # 该符号所在的装配层反过来依赖 lib.config，模块级导入会成环。
        from lib.custom_provider.default_models import lacks_bucket

        try:
            db_pid = parse_provider_id(selected.provider_id)
        except ValueError:
            return
        model = await CustomProviderRepository(session).get_model_by_ids(db_pid, selected.model_id)
        if model is None or not model.is_enabled:
            return
        if await lacks_bucket(session, model, generation_type):
            raise ImageBucketCapabilityError(
                generation_type=generation_type,
                provider_id=selected.provider_id,
                model_id=selected.model_id,
            )

    async def _resolve_video_provider_model(
        self,
        svc: ConfigService,
        session: AsyncSession,
        project: dict | None,
        payload: dict | None,
        generation_type: VideoGenerationType | None = None,
    ) -> ProviderModel:
        """Resolve the video ProviderModel payload first; without payload, take the bucket skeleton or bucketless skeleton by ``generation_type``.

        The payload layer only recognizes materialized task type bucket keys (the ``video_provider_<generation_type>``
        composite value, see ``_payload_video_pinned_pair``): returned as is, without the capability gate, only the identity
        availability check (``_ensure_video_identity_resolvable``), raising if dangling. The locked form does not drop
        untrusted providers — when a provider has gone offline, falling back would mean executing with a different provider.
        For each layer's semantics see the ``resolve_video_backend`` docstring.

        payload 优先解析视频 ProviderModel；无 payload 时按 ``generation_type`` 走桶骨架或不定桶骨架。

        payload 层只认已物化的任务类型桶键（``video_provider_<generation_type>`` 复合值，见
        ``_payload_video_pinned_pair``）：原样返回、不过能力闸，只过身份可用性
        （``_ensure_video_identity_resolvable``），悬空即报错。锁定形态不丢弃不可信供应商——
        供应商已下线时回退等于换供应商执行。各层语义见 ``resolve_video_backend`` docstring。
        """
        if payload:
            pinned = _payload_video_pinned_pair(payload, generation_type)
            if pinned is not None:
                pinned_generation_type, pair = pinned
                selected = ProviderModel(*pair)
                await self._ensure_video_identity_resolvable(session, selected, pinned_generation_type)
                return selected
            # A model chosen for this one request ("Regenerate with …"): used instead of the configured layers,
            # through the same capability gate as a bucket setting (it must be able to run this task).
            override = _split_pair(payload.get(VIDEO_MODEL_OVERRIDE_KEY))
            if override is not None:
                selected = ProviderModel(*override)
                if generation_type is None:
                    return await self._resolve_effective_video_provider_model(session, selected)
                await self._ensure_video_bucket_capability(session, selected, generation_type)
                return selected
        if generation_type is None:
            provider_id, model_id = await self._resolve_layered_backend(
                svc, session, project, _VIDEO_DEFAULT_LAYERED_KEYS
            )
            return await self._resolve_effective_video_provider_model(session, ProviderModel(provider_id, model_id))
        provider_id, model_id = await self._resolve_layered_backend(
            svc, session, project, _VIDEO_LAYERED_KEYS[generation_type]
        )
        selected = ProviderModel(provider_id, model_id)
        await self._ensure_video_bucket_capability(session, selected, generation_type)
        return selected

    async def _ensure_video_identity_resolvable(
        self,
        session: AsyncSession,
        selected: ProviderModel,
        generation_type: VideoGenerationType,
    ) -> CustomProviderModel | None:
        """Check that the video identity still resolves now; raise if dangling.

        It judges identity existence, not capability declarations: built-in models check the model catalog, and when not in
        the provider's video model list (delisted, renamed, or the referenced model is not a video model) raise
        ``ModelNotInCatalogError``, the same "please reselect the model" as the assembly layer and image path; custom models
        check the DB (provider deleted, model deleted or disabled, endpoint changed to another media type), raising
        ``VideoBucketCapabilityError`` if dangling. When a custom model hits, the row is returned so the caller can compose
        capabilities in place without re-querying; built-in returns None.

        Backend caps functions do not all check model existence nor all check media_type (some providers return static
        capabilities for any model id, and image models of the same provider would get first_frame=True), so identity needs a separate check.

        校验视频身份现在仍解析得到，悬空即报错。

        判的是身份存在性而非能力声明：内置模型看模型目录，不在该供应商的视频模型清单里（已下架、
        已改名，或所引模型不是视频模型）时抛 ``ModelNotInCatalogError``，与装配层、图片路径同一个
        「请重新选择模型」；自定义模型看 DB（供应商被删、模型被删或禁用、endpoint 改成其它媒体类型），
        悬空抛 ``VideoBucketCapabilityError``。自定义模型命中时返回该行，供调用方就地合成能力、不重复
        查库；内置返回 None。

        backend caps 函数不都校验 model 存在性、也不都校验 media_type（部分 provider 对任意 model id
        返回静态能力，同一 provider 的图片模型也会拿到 first_frame=True），故身份需单独判。
        """
        provider_id, model_id = selected.provider_id, selected.model_id
        if not is_custom_provider(provider_id):
            if catalog.get_model(provider_id, model_id, media_type="video") is None:
                raise ModelNotInCatalogError(provider_id=provider_id, model_id=model_id, media_type="video")
            return None

        # Deferred import: the layering contract (pyproject.toml [tool.importlinter]) puts lib.config as a lower layer,
        # and the assembly layer holding this symbol in turn depends on lib.config, so a module-level import would form a cycle.
        # 延迟导入：分层契约（pyproject.toml [tool.importlinter]）以 lib.config 为下层，
        # 该符号所在的装配层反过来依赖 lib.config，模块级导入会成环。
        from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
        from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository

        try:
            db_pid = parse_provider_id(provider_id)
        except ValueError as exc:
            raise _video_bucket_reference_unavailable(generation_type, provider_id, model_id) from exc
        model = await CustomProviderRepository(session).get_model_by_ids(db_pid, model_id)
        if model is None or not model.is_enabled:
            raise _video_bucket_reference_unavailable(generation_type, provider_id, model_id)
        try:
            endpoint_spec = await resolve_endpoint_spec(model.endpoint, CustomEndpointRepository(session).get)
        except ValueError as exc:
            raise _video_bucket_reference_unavailable(generation_type, provider_id, model_id) from exc
        if endpoint_spec.media_type != "video":
            raise _video_bucket_reference_unavailable(generation_type, provider_id, model_id)
        return model

    async def _ensure_video_bucket_capability(
        self,
        session: AsyncSession,
        selected: ProviderModel,
        generation_type: VideoGenerationType,
    ) -> None:
        """Capability gate: check the resolved model has the capability the bucket needs; raise directly if not, no silent model switch.

        The verdict shares one rule with the bucket candidate dropdown (``lib.backends.generation_type_buckets``) via
        ``video_capability_satisfied``: built-in models take both dimensions from the backend's ``VideoCapabilities`` (same
        source as request construction, and the only place these two dimensions are declared; the catalog's model info does
        not declare video capability bits). The identity first passes
        ``_ensure_video_identity_resolvable``; dangling references (model deleted /
        capability changed afterwards / provider deleted / endpoint changed) are uniformly caught here; the write side
        neither blocks nor cascades cleanup (``docs/adr/0054``).

        能力闸：校验解析出的模型具备该桶所需能力，不满足直接报错、不静默换模型。

        判定经 ``video_capability_satisfied`` 与桶候选下拉（``lib.backends.generation_type_buckets``）共用一份
        口径：内置模型两维都取 backend ``VideoCapabilities``（与请求构造同源，也是这两维唯一的
        声明处；目录的模型信息不声明视频能力位）。身份先过
        ``_ensure_video_identity_resolvable``，悬空引用（模型被删 /
        能力被事后修改 / 供应商被删 / endpoint 变更）在此统一报错兜底，写入侧不拦截、不级联清理
        （``docs/adr/0054``）。
        """
        provider_id, model_id = selected.provider_id, selected.model_id
        model = await self._ensure_video_identity_resolvable(session, selected, generation_type)
        if model is not None:
            # Deferred import: the layering contract (pyproject.toml [tool.importlinter]) puts lib.config as a lower layer,
            # and the assembly layer holding this symbol in turn depends on lib.config, so a module-level import would form a cycle.
            # 延迟导入：分层契约（pyproject.toml [tool.importlinter]）以 lib.config 为下层，
            # 该符号所在的装配层反过来依赖 lib.config，模块级导入会成环。
            from lib.custom_provider.capabilities import synthesize_video_capabilities

            try:
                from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
                from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository

                endpoint_spec = await resolve_endpoint_spec(model.endpoint, CustomEndpointRepository(session).get)
                caps = synthesize_video_capabilities(
                    endpoint=model.endpoint,
                    model_id=model_id,
                    overrides=model.capability_overrides,
                    endpoint_spec=endpoint_spec,
                )
            except ValueError as exc:
                raise _video_bucket_reference_unavailable(generation_type, provider_id, model_id) from exc
        else:
            try:
                caps = builtin_video_capabilities_for_model(provider_id, model_id)
            except ValueError as exc:
                raise _video_bucket_reference_unavailable(generation_type, provider_id, model_id) from exc
        satisfied = video_capability_satisfied(
            generation_type=generation_type,
            first_frame=caps.first_frame,
            max_reference_images=caps.max_reference_images,
            text_to_video=caps.text_to_video,
        )
        if not satisfied:
            raise _video_bucket_capability_missing(generation_type, provider_id, model_id)

    async def _resolve_effective_video_provider_model(
        self,
        session: AsyncSession,
        selected: ProviderModel,
    ) -> ProviderModel:
        """Converge the selected identity to the video identity the backend will actually use when constructed.

        A built-in provider's registry identity is already the effective identity; custom providers must share one rule with
        the loader: when the model does not exist, is disabled, or the endpoint was changed to another media_type, fall back
        to the default enabled video model.

        Materialized identities do not take this convergence (see ``_resolve_video_provider_model``): executing with a
        different model would be a silent model switch.

        把选择身份收敛为 backend 构造时会实际使用的视频身份。

        内置供应商的 registry 身份已是有效身份；自定义供应商需与 loader 共用同一规则：
        model 不存在、禁用或 endpoint 已改成其它 media_type 时，回退到默认启用 video model。

        已物化的身份不走这条收敛（见 ``_resolve_video_provider_model``）：换 model 执行等于
        静默换模型。
        """
        if not is_custom_provider(selected.provider_id):
            return selected

        # Deferred import: the layering contract (pyproject.toml [tool.importlinter]) puts lib.config as a lower layer,
        # and the assembly layer holding this symbol in turn depends on lib.config, so a module-level import would form a
        # cycle; the built-in branch does not use it, so there is no need to pull in the whole assembly layer for it.
        # 延迟导入：分层契约（pyproject.toml [tool.importlinter]）以 lib.config 为下层，
        # 而该符号所在的装配层反过来依赖 lib.config，模块级导入会成环；内置分支不用它，
        # 也就不必为此拉起整个装配层。
        from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
        from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository

        try:
            db_pid = parse_provider_id(selected.provider_id)
        except ValueError as exc:
            raise ValueError(f"invalid custom provider_id: {selected.provider_id}") from exc
        repo = CustomProviderRepository(session)
        model = await repo.get_model_by_ids(db_pid, selected.model_id)
        if model is not None and model.is_enabled:
            try:
                endpoint_spec = await resolve_endpoint_spec(model.endpoint, CustomEndpointRepository(session).get)
            except ValueError:
                endpoint_spec = None
            if endpoint_spec is not None and endpoint_spec.media_type == "video":
                return selected

        logger.warning(
            "自定义模型 %s/%s 已不存在 / 已禁用 / 媒体类型不符（期望 video），身份解析回退到默认模型",
            selected.provider_id,
            selected.model_id,
        )
        from lib.custom_provider.default_models import resolve_default_model

        try:
            default_model = await resolve_default_model(
                session, provider_id=selected.provider_id, db_id=db_pid, media_type="video"
            )
        except ValueError as exc:
            raise ValueError(f"custom model not found: {selected.provider_id}/{selected.model_id}") from exc
        return ProviderModel(selected.provider_id, default_model.model_id)

    async def _resolve_default_audio_backend(self, svc: ConfigService, session: AsyncSession) -> tuple[str, str]:
        """Resolve the default audio backend at the global layer only: global default key > auto inference.

        Uses the four-level skeleton without a project (project=None skips the project layer).

        仅全局层解析音频默认 backend：全局默认键 > 自动推断。

        走四级骨架但不带项目（project=None 跳过项目层）。
        """
        return await self._resolve_layered_backend(svc, session, None, _AUDIO_LAYERED_KEYS)

    async def _resolve_audio_provider_model(
        self,
        svc: ConfigService,
        session: AsyncSession,
        project: dict | None,
        payload: dict | None,
    ) -> ProviderModel:
        """Three-level resolution of the audio ProviderModel: payload > project > global default.

        The payload layer serves draining historical tasks (carrying ``audio_provider`` + ``audio_model``). The payload
        provider must be a known provider (see ``_trusted_payload_provider``), otherwise fall back to project/global.

        payload > project > 全局默认 三级解析音频 ProviderModel。

        payload 层服务于历史任务（携带 ``audio_provider`` + ``audio_model``）的排空。payload
        供应商须是已知供应商（见 ``_trusted_payload_provider``），否则回退 project/global。
        """
        if payload:
            provider_id = _trusted_payload_provider(payload.get("audio_provider"))
            if provider_id is not None:
                model = _payload_model_or_default(payload.get("audio_model"), provider_id, "audio")
                if model is not None:
                    return ProviderModel(provider_id, model)
        provider_id, model_id = await self._resolve_layered_backend(svc, session, project, _AUDIO_LAYERED_KEYS)
        return ProviderModel(provider_id, model_id)

    async def _resolve_video_capabilities_from_project(
        self,
        svc: ConfigService,
        session: AsyncSession,
        project: dict | None,
        *,
        generation_type: VideoGenerationType | None = None,
    ) -> dict:
        """Resolve the model that will execute by task type bucket (picked by the project's generation_mode when not given explicitly), then read its capabilities.

        Shares ``_resolve_video_provider_model`` with the execution path (including the capability gate), so the read side
        has no second rule: after switching generation_mode, capability queries change with the bucket, and when the model
        lacks the bucket's required capability or the reference is broken it raises, no silent model switch
        (``docs/adr/0054``). payload is passed as None — capability queries answer for the current config, not for draining
        historical tasks.

        Only the selected identity is passed: effective identity convergence is done centrally by
        ``_resolve_video_caps_for_model``; doing it here first would make custom providers run one extra model query.

        按任务类型桶（未显式给定时按项目 generation_mode 定桶）解析出会执行的那个模型，再读它的能力。

        与执行路径共用 ``_resolve_video_provider_model``（含能力闸），读侧不留第二种口径：切换
        generation_mode 后能力查询随桶变化，模型缺该桶所需能力或引用已失效时报错、不静默换模型
        （``docs/adr/0054``）。payload 传 None——能力查询回答的是当前配置，不排空历史任务。

        只传选择身份：有效身份收敛由 ``_resolve_video_caps_for_model`` 统一做，在此先做一遍会让
        自定义供应商多跑一轮 model 查询。
        """
        if generation_type is None:
            generation_type = video_bucket_for_generation_mode(caps_generation_mode(project))
        selected = await self._resolve_video_provider_model(svc, session, project, None, generation_type)
        return await self._resolve_video_caps_for_model(
            svc, session, selected.provider_id, selected.model_id, project, generation_type=generation_type
        )

    async def _resolve_video_caps_for_model(
        self,
        svc: ConfigService,
        session: AsyncSession,
        provider_id: str,
        model_id: str,
        project: dict | None,
        *,
        generation_type: VideoGenerationType | None = None,
    ) -> dict:
        # The audio track form forks by execution subpath (Kling v3-omni's multi-image subject subpath has no audio switch), so
        # capability resolution needs to know which bucket it falls in; when not given explicitly, pick the bucket by the
        # project route, the same rule as `_resolve_video_capabilities_from_project`.
        # 音轨形态按执行子路径分叉（可灵 v3-omni 的多图主体子路径不带音轨开关），故能力解析需要
        # 知道落哪个桶；未显式给定时按项目路线定桶，与 `_resolve_video_capabilities_from_project`
        # 同一条规则。
        if generation_type is None:
            generation_type = video_bucket_for_generation_mode(caps_generation_mode(project))
        effective = await self._resolve_effective_video_provider_model(session, ProviderModel(provider_id, model_id))
        provider_id, model_id = effective.provider_id, effective.model_id
        if is_custom_provider(provider_id):
            # Deferred import: the layering contract (pyproject.toml [tool.importlinter]) puts lib.config as a lower layer,
            # and the assembly layer holding this symbol in turn depends on lib.config, so a module-level import would form a
            # cycle; the registry branch does not use it, so there is no need to pull in the whole assembly layer for it.
            # 延迟导入：分层契约（pyproject.toml [tool.importlinter]）以 lib.config 为下层，
            # 而该符号所在的装配层反过来依赖 lib.config，模块级导入会成环；注册表分支不用它，
            # 也就不必为此拉起整个装配层。
            from lib.custom_provider.capabilities import (
                overridden_audio_track,
                synthesize_video_capabilities_with_overrides,
            )
            from lib.custom_provider.model_facts import comfyui_duration_override, video_model_facts

            source = "custom"
            try:
                db_pid = parse_provider_id(provider_id)
            except ValueError as exc:
                raise ValueError(f"invalid custom provider_id: {provider_id}") from exc
            repo = CustomProviderRepository(session)
            model = await repo.get_model_by_ids(db_pid, model_id)
            if model is None:
                raise ValueError(f"custom model not found after identity resolution: {provider_id}/{model_id}")

            # Effective capabilities (system verdict ⊕ user override) have only this one composition point: what the factory
            # injects into the execution layer is also its return value, so the display and execution layers are strictly the same
            # source, with no merging of overrides or recomputing of the system verdict here. The pure function queries no
            # provider row and builds no SDK client, so per-video-unit resolution has no DB/network/client construction side effects
            # (and does not raise for a missing api_key).
            # 生效能力（系统判定 ⊕ 用户覆盖）只此一个合成点：工厂给执行层注入的也是它的返回值，
            # 展示层与执行层因此严格同源，不在此处自行合并覆盖或重算系统判定。纯函数不查
            # provider 行、不构造 SDK client，故逐视频单元解析无 DB/网络/client 构造副作用
            # （也不因 api_key 缺失而抛）。
            try:
                from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
                from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository

                endpoint_spec = await resolve_endpoint_spec(model.endpoint, CustomEndpointRepository(session).get)
                caps, applied_overrides = synthesize_video_capabilities_with_overrides(
                    endpoint=model.endpoint,
                    model_id=model_id,
                    overrides=model.capability_overrides,
                    endpoint_spec=endpoint_spec,
                )
            except ValueError as exc:
                raise ValueError(f"cannot resolve video capabilities for {provider_id}/{model_id}: {exc}") from exc
            max_reference_images = caps.max_reference_images
            text_to_video = caps.text_to_video
            first_frame = caps.first_frame
            last_frame = caps.last_frame
            reference_audio_mode = caps.reference_audio_mode
            max_reference_audio_count = caps.max_reference_audio_count
            reference_audio_per_image = caps.reference_audio_per_image
            # Custom providers have no execution tier: the pricing parameter is not narrowed by a default execution tier, so the project request value is kept.
            # 自定义供应商没有执行档：计价参数不因默认执行档收窄，故沿用项目请求值。
            default_tier_generates_audio = True
            # A custom provider's audio track form only recognizes the user's capability override (docs/adr/0104). Same rule as
            # default_tier_generates_audio on the previous line: without an override assume audio, not judging it a truly silent model out of thin air.
            # 自定义供应商的音轨形态只认用户的能力覆盖（docs/adr/0104）。与上一行 default_tier_generates_audio
            # 同口径：没有覆盖时假定有声，不凭空判定为真无声模型。
            audio_track = overridden_audio_track(caps, applied_overrides, generation_type)
            has_audio = audio_track != "always_off" if audio_track is not None else True
            # On ComfyUI endpoints the duration can be entirely not driven by ArcReel (``docs/adr/0082``): when ``frames`` is not
            # bound or no frame rate source can be read, the model row's options are an empty set, no duration is sent at submit,
            # and the workflow produces its own length. This is a legitimate state of that protocol, to which the "empty set means
            # fail loud" rule below does not apply — ADR 0018 guards "missing model declaration", whereas here "this dimension does
            # not exist on this endpoint". Other protocols fail loud as before.
            # ComfyUI 端点的时长可以整维不由 ArcReel 驱动（``docs/adr/0082``）：``frames`` 未绑定
            # 或读不到帧率来源时，该模型行的档位就是空集，提交时不下发时长、让 workflow 出它自己
            # 那一档。这是该协议的合法状态，不适用下面那条「空集即 fail loud」——ADR 0018 守的是
            # 「型号声明缺失」，而这里是「这一维在该端点上不存在」。其余协议照旧 fail loud。
            durations_optional = endpoint_spec.duration_tier_optional
            if endpoint_spec.endpoint_durations is not None:
                # ComfyUI endpoints: duration is derived only from node bindings (docs/adr/0082), not from entries and no name-based
                # fallback. Options the user adds or removes beyond the workflow's native duration are stored as a ``durations``
                # override, the only override this endpoint accepts (docs/adr/0104).
                # The endpoint is still the source of truth for options: when the endpoint says this dimension yields no options (empty
                # set), overrides are all void, the duration control is disabled, and script planning uses a fixed length.
                # ComfyUI 端点：时长只从节点绑定推导（docs/adr/0082），不看条目、不按名字回落。用户在 workflow
                # 原生时长之外增删的档位存为 ``durations`` 覆盖，是这一端点唯一接受的覆盖（docs/adr/0104）。
                # 档位的真相源仍是端点：端点说这一维给不出档位时（空集），覆盖一律作废，时长控件禁着、
                # 剧本规划借固定篇幅。
                override = comfyui_duration_override(model.capability_overrides, model_id=model_id)
                endpoint_durations = list(endpoint_spec.endpoint_durations)
                supported_durations = override if override and endpoint_durations else endpoint_durations
            else:
                # Other endpoints: capability override > matching entry > preset inferred by name (when no entry matches); the fallback
                # result is computed only here, never written back to any field (docs/adr/0105).
                # 其余端点：能力覆盖 > 对应条目 > 按名字推断的预设（对应不上条目时），回落结果只在这里现算，
                # 不写回任何字段（docs/adr/0105）。
                supported_durations = list(
                    video_model_facts(model_id, model.capability_overrides, entry_binding=model.entry_binding).durations
                )
        else:
            source = "registry"
            durations_optional = False
            if provider_id not in PROVIDER_REGISTRY:
                raise ValueError(f"provider not in PROVIDER_REGISTRY: {provider_id}")
            model_info = catalog.get_model(provider_id, model_id)
            if model_info is None:
                raise ValueError(f"model not found in registry: {provider_id}/{model_id}")
            supported_durations = list(model_info.supported_durations or [])
            # Video capability bits and the reference image limit are only declared in the backend: the backend is the party that
            # actually builds requests at execution, and also the rule of the capability gate (`_ensure_video_bucket_capability`)
            # and the bucket candidate dropdown (`lib.backends.generation_type_buckets`), so the display and execution layers are strictly the same source.
            # 视频能力位与参考图上限只在 backend 声明：backend 是执行期真正构造请求的一方，
            # 也是能力闸（`_ensure_video_bucket_capability`）与桶候选下拉
            # （`lib.backends.generation_type_buckets`）的口径，展示层与执行层因此严格同源。
            try:
                builtin_caps = builtin_video_capabilities_for_model(provider_id, model_id)
            except ValueError as exc:
                raise ValueError(f"cannot resolve video capabilities for {provider_id}/{model_id}: {exc}") from exc
            max_reference_images = builtin_caps.max_reference_images
            text_to_video = builtin_caps.text_to_video
            first_frame = builtin_caps.first_frame
            last_frame = builtin_caps.last_frame
            reference_audio_mode = builtin_caps.reference_audio_mode
            max_reference_audio_count = builtin_caps.max_reference_audio_count
            reference_audio_per_image = builtin_caps.reference_audio_per_image
            # The audio track shares the same VideoCapabilities source as the dimensions above, only it takes the path branch by
            # the bucket the task type falls in: reference-route subpaths without an audio track (Kling v3-omni multi-image
            # subject) truthfully derive voice_consistency=none from it.
            # 音轨与上面几维同源同一个 VideoCapabilities，只是要按任务类型落的桶取路径分支：参考
            # 路线无音轨的子路径（可灵 v3-omni 多图主体）据此如实派生出 voice_consistency=none。
            has_audio = builtin_caps.audio_track_for_route(generation_type) != "always_off"
            try:
                default_tier_generates_audio = builtin_effective_generate_audio_for_model(provider_id, model_id)
            except ValueError as exc:
                raise ValueError(
                    f"cannot resolve video pricing capabilities for {provider_id}/{model_id}: {exc}"
                ) from exc

        if not supported_durations and not durations_optional:
            raise VideoSupportedDurationsError(
                "missing",
                provider_id=provider_id,
                model_id=model_id,
                message=f"supported_durations is empty for {provider_id}/{model_id}; cannot derive capabilities",
            )

        # With an empty set ``max_duration`` is 0, synonymous with ``supported_durations == []``: this dimension is not driven
        # by ArcReel, consumers should not derive any selectable options from it, and the UI disables the duration control accordingly.
        # 空集时 ``max_duration`` 为 0，与 ``supported_durations == []`` 同义：这一维不由 ArcReel
        # 驱动，消费方不该从它派生任何可选档位，界面据此禁用时长控件。
        max_duration = max(supported_durations, default=0)
        duration_endpoint_fixed = not supported_durations and durations_optional

        # requested_generate_audio is the **user's silent intent** (global setting ← project.json override), the same source as
        # the video_generate_audio read by the execution layer MediaGenerator; generate_audio below is the **pricing rule**
        # (with always-audio billing and default execution tier verdicts layered on); the two are not equivalent, so both are
        # exposed side by side, and consumers each pick one by semantics: "should this episode assemble reference audio" can only read the former.
        # requested_generate_audio 是**用户的无声意图**（全局设置 ← project.json 覆盖），与执行层
        # MediaGenerator 读的 video_generate_audio 同源；下面的 generate_audio 是**计价口径**（叠加了
        # 恒含音出账与默认执行档判定），两者不等价，故并列透出，消费方按语义各取一个：判「本集要不要
        # 组装参考音频」只能读前者。
        requested_generate_audio = await self._resolve_video_generate_audio_from_project(svc, project)
        # Providers that always bill with audio ignore the request value; for other backends the pricing parameter is True only
        # when the project request turns it on and the context-free capability interface confirms the default execution tier produces voices.
        # 恒含音出账的 provider 无视请求值；其余 backend 只有在项目请求开启、且无上下文能力
        # 接口确认默认执行档会产出人声时，计价参数才为 True。
        generate_audio = (
            True
            if _video_audio_always_billed(provider_id)
            else requested_generate_audio and default_tier_generates_audio
        )

        default_duration: int | None = None
        content_mode: str | None = None
        # The per-episode target duration is not a model capability; it is returned with the capability query only because,
        # like default_duration, it is a "project preference to consult when deciding duration": the script planning subagent
        # gets all the inputs it needs for the decision in one get_video_capabilities, without another tool round trip for a
        # single preference field. Parsing goes through the read-time guard of lib.episode.episode_target_duration.
        # 单集目标时长不是模型能力，随能力查询一起回传只因它与 default_duration 同为「决定时长时
        # 要看的项目偏好」：脚本规划子智能体一次 get_video_capabilities 就能拿齐决策所需的全部输入，
        # 不必为一个偏好字段另开一个工具往返。解析走 lib.episode.episode_target_duration 的读时守卫。
        episode_target_duration = project_episode_target_duration(project)
        if project is not None:
            raw_default = project.get("default_duration")
            if isinstance(raw_default, int):
                default_duration = raw_default
            elif isinstance(raw_default, str) and raw_default.strip().isdigit():
                default_duration = int(raw_default.strip())
            cm = project.get("content_mode")
            if isinstance(cm, str) and cm:
                content_mode = cm
        generation_mode = caps_generation_mode(project)

        voice_consistency = derive_voice_consistency(
            reference_audio_mode=reference_audio_mode,
            generation_mode=generation_mode,
            has_audio=has_audio,
            character_voice_binding=character_voice_binding(project),
        )

        return {
            "provider_id": provider_id,
            "model": model_id,
            "supported_durations": supported_durations,
            "max_duration": max_duration,
            "max_reference_images": max_reference_images,
            "text_to_video": text_to_video,
            "first_frame": first_frame,
            "last_frame": last_frame,
            "generate_audio": generate_audio,
            "requested_generate_audio": requested_generate_audio,
            "max_reference_audio_count": max_reference_audio_count,
            "reference_audio_per_image": reference_audio_per_image,
            "source": source,
            # Empty options and the endpoint allows an empty set = this dimension is fixed by the endpoint (GLOSSARY.md "dimension fixed by endpoint").
            # Consumers use this to tell "this dimension does not exist on this endpoint" from "missing options declaration" — the latter already fails loud above.
            # 档位是空集且该端点允许空集 = 这一维由端点固定（GLOSSARY.md「维度由端点固定」）。
            # 消费方据此区分「这一维在该端点上不存在」与「档位声明缺失」——后者已在上面 fail loud。
            "duration_endpoint_fixed": duration_endpoint_fixed,
            "duration_endpoint_fixed_reason": duration_endpoint_fixed_reason(duration_endpoint_fixed),
            "default_duration": default_duration,
            "episode_target_duration": episode_target_duration,
            "content_mode": content_mode,
            "generation_mode": generation_mode,
            "voice_consistency": voice_consistency,
        }

    async def _resolve_default_image_backend(
        self, svc: ConfigService, session: AsyncSession, generation_type: Literal["t2i", "i2i"] = "t2i"
    ) -> tuple[str, str]:
        """Resolve the default image backend at the global layer only: global bucket > global default key > auto inference.

        Uses the four-level skeleton without a project (project=None skips the project layer). When the global bucket has no
        value, fall back to the global default key.

        仅全局层解析图片默认 backend：全局桶 > 全局默认键 > 自动推断。

        走四级骨架但不带项目（project=None 跳过项目层）。全局桶无值时回退全局默认键。
        """
        return await self._resolve_layered_backend(svc, session, None, _IMAGE_LAYERED_KEYS[generation_type])

    async def _resolve_provider_config(
        self,
        svc: ConfigService,
        session: AsyncSession,
        provider_id: str,
    ) -> dict[str, str]:
        config = await svc.get_provider_config(provider_id)
        cred_repo = CredentialRepository(session)
        active = await cred_repo.get_active(provider_id)
        if active:
            # Credential overlay probes whether the Vertex credential file exists, so run it in a worker thread.
            # 凭证叠加会探测 Vertex 凭证文件是否存在，放进工作线程。
            await asyncio.to_thread(active.overlay_config, config)
        return config

    async def _resolve_reference_payload_limits(
        self,
        svc: ConfigService,
        session: AsyncSession,
        provider_id: str,
    ) -> tuple[int, int]:
        try:
            cfg = await self._resolve_provider_config(svc, session, provider_id)
        except ValueError:
            # Unknown / custom provider (_validate_provider raises ValueError) → fall back to the conservative generic default
            # 未知 / 自定义供应商（_validate_provider 抛 ValueError）→ 回退保守通用默认
            return DEFAULT_REFERENCE_TOTAL_MAX_BYTES, DEFAULT_REFERENCE_SINGLE_MAX_BYTES
        total = _parse_int(cfg.get(_REFERENCE_TOTAL_MAX_BYTES_KEY), DEFAULT_REFERENCE_TOTAL_MAX_BYTES)
        single = _parse_int(cfg.get(_REFERENCE_SINGLE_MAX_BYTES_KEY), DEFAULT_REFERENCE_SINGLE_MAX_BYTES)
        return total, single

    async def _resolve_all_provider_configs(
        self,
        svc: ConfigService,
        session: AsyncSession,
    ) -> dict[str, dict[str, str]]:
        configs = await svc.get_all_provider_configs()
        cred_repo = CredentialRepository(session)
        active_creds = await cred_repo.get_active_credentials_bulk()

        def overlay_all() -> None:
            for provider_id, cred in active_creds.items():
                cred.overlay_config(configs.setdefault(provider_id, {}))

        # Credential overlay probes whether the Vertex credential file exists, so run the whole batch in a worker thread.
        # 凭证叠加会探测 Vertex 凭证文件是否存在，整批放进工作线程。
        await asyncio.to_thread(overlay_all)
        return configs

    async def default_text_backend(self) -> tuple[str, str]:
        """Return (provider_id, model_id). / 返回 (provider_id, model_id)。"""
        async with self._open_session() as (_session, svc):
            return await svc.get_default_text_backend()

    async def text_backend_for_task(
        self,
        task_type: TextTaskType,
        project_name: str | None = None,
    ) -> tuple[str, str]:
        """Resolve the text backend by task tier.

        Priority (project first): project tier > project default model > global tier > global default model > auto inference.
        When the task needs vision, the resolved result's capability is checked; raise directly if not met, no silent model
        switch (docs/adr/0051).

        按任务档位解析文本 backend。

        优先级（项目优先）：项目档位 > 项目默认模型 > 全局档位 > 全局默认模型 > 自动推断。
        任务需要 vision 时校验解析结果的能力，不满足直接报错、不静默换模型（docs/adr/0051）。
        """
        async with self._open_session() as (session, svc):
            return await self._resolve_text_backend(svc, session, task_type, project_name)

    async def _resolve_text_backend(
        self,
        svc: ConfigService,
        session: AsyncSession,
        task_type: TextTaskType,
        project_name: str | None,
    ) -> tuple[str, str]:
        project = await _load_project_snapshot(project_name)
        keys = _TEXT_LAYERED_KEYS[TEXT_TASK_TIERS[task_type]]
        resolved = await self._resolve_layered_backend(svc, session, project, keys)
        if task_type in VISION_REQUIRED_TASKS:
            await self._ensure_text_model_vision_capable(session, task_type, *resolved)
        return resolved

    async def _ensure_text_model_vision_capable(
        self, session: AsyncSession, task_type: TextTaskType, provider_id: str, model_id: str
    ) -> None:
        """Check that the resolved model supports image input; raise directly if not, no silent model switch.

        Built-in models are judged by the catalog; custom provider models by capability override > matching entry; with no
        matching entry and no override there is no constraint, leaving it to the provider API, no guessing.

        校验解析出的模型支持图像输入；不满足直接报错，不静默换模型。

        内置模型按目录判定；自定义供应商的模型按能力覆盖 > 对应条目判定，对应不上条目且没有覆盖时
        不加约束，交由供应商 API 把关，不做猜测。
        """
        if not is_custom_provider(provider_id):
            _ensure_text_model_vision_capable(task_type, provider_id, model_id)
            return
        from lib.custom_provider.model_facts import text_model_facts

        try:
            db_pid = parse_provider_id(provider_id)
        except ValueError:
            return
        model = await CustomProviderRepository(session).get_model_by_ids(db_pid, model_id)
        if (
            model is not None
            and text_model_facts(model_id, model.capability_overrides, entry_binding=model.entry_binding).image_input
            is False
        ):
            raise VisionCapabilityError(task_type=task_type, provider_id=provider_id, model_id=model_id)

    async def _resolve_auto_backends(
        self, svc: ConfigService, session: AsyncSession
    ) -> dict[AutoResolvePurpose, tuple[str, str] | None]:
        """Call the auto inference layer with each purpose's keys, with the same inputs as when execution-time resolution reaches this layer.

        按各用途的键位调用自动推断层，与执行期解析落到这一层时的入参一致。"""
        resolved: dict[AutoResolvePurpose, tuple[str, str] | None] = {}
        for purpose, keys in _AUTO_RESOLVE_PURPOSE_KEYS.items():
            try:
                # Serial one by one: the same session does not support concurrent queries / 逐项串行：同一 session 不支持并发查询
                resolved[purpose] = await self._auto_resolve_backend(
                    svc, session, keys.media_type, keys.generation_type
                )
            except ValueError:
                resolved[purpose] = None
        return resolved

    async def _auto_resolve_backend(
        self,
        svc: ConfigService,
        session: AsyncSession,
        media_type: str,
        generation_type: str | None = None,
    ) -> tuple[str, str]:
        """Walk PROVIDER_REGISTRY (in registration order) and find the first provider that is ready and supports this media_type.

        The custom fallback and default fallback share the same bucket filtering (``lib.custom_provider.default_models``):
        with a bucket, only default models having that bucket count as candidates, otherwise t2i and i2i would silently get
        the same row. Among multiple candidates across providers, take the first (each provider setting its own default is
        normal, unlike "two defaults for the same bucket in the same provider").

        遍历 PROVIDER_REGISTRY（按注册顺序），找到第一个 ready 且支持该 media_type 的供应商。

        自定义兜底与默认回退共用同一份按桶过滤（``lib.custom_provider.default_models``）：带桶时
        只有具备该桶的默认模型算候选，否则 t2i 与 i2i 会静默拿到同一行。跨供应商的多个候选取第
        一个（各供应商各设默认是常态，与「同一供应商同桶两个默认」不同）。
        """
        statuses = await svc.get_all_providers_status()
        ready = {s.name: s for s in statuses if s.status == "ready"}

        for provider_id in PROVIDER_REGISTRY:
            if provider_id not in ready:
                continue
            # A provider-level default is only used for auto picking when there are no default model settings at all; if the user disabled it, it is not picked.
            # 供应商一级的默认只在没有任何默认模型设置时用于自动挑选；用户停用了它就不挑。
            model_id = catalog.default_model(provider_id, media_type)
            if model_id is not None and model_id not in ready[provider_id].disabled_models:
                return provider_id, model_id

        # Deferred import: the layering contract (pyproject.toml [tool.importlinter]) puts lib.config as a lower layer,
        # and the assembly layer holding these symbols in turn depends on lib.config, so a module-level import would form a cycle.
        # 延迟导入：分层契约（pyproject.toml [tool.importlinter]）以 lib.config 为下层，
        # 这些符号所在的装配层反过来依赖 lib.config，模块级导入会成环。
        from lib.custom_provider import make_provider_id
        from lib.custom_provider.default_models import filter_by_bucket
        from lib.db.repositories.custom_provider_repo import CustomProviderRepository

        repo = CustomProviderRepository(session)
        custom_models = await repo.list_enabled_models_by_media_type(media_type)
        defaults = [model for model in custom_models if model.is_default]
        candidates = await filter_by_bucket(session, defaults, generation_type)
        if candidates:
            return make_provider_id(candidates[0].provider_id), candidates[0].model_id

        raise ValueError(f"No available {media_type} provider. Configure at least one provider under Settings → Providers.")
