"""KlingVideoBackend — Kling video generation backend (dual mode: JWT direct / Bearer relay; async polling).

Uses Kling's native video endpoints: submit ``POST /v1/videos/{text2video|image2video}`` to get ``data.task_id`` →
poll ``GET /v1/videos/{subpath}/{task_id}`` until ``task_status=succeed``, take
``task_result.videos[0].url`` → download locally. Reuses the submit/poll/download helpers of base.py, with a
self-contained async state machine that does not depend on the DashScope async mechanism.

Dual mode (following the ``backend_type`` precedent of ``GeminiVideoBackend``):
- ``auth_mode="jwt"`` (built-in provider): takes access_key + secret_key and uses ``KlingJWTManager``,
  checking expiry before every HTTP call and re-signing on demand when <60s from expiry — async rendering can
  outlive a single token.
- ``auth_mode="bearer"`` (custom endpoint): takes a static api_key + base_url, bypassing the JWT manager.

Per-model video capabilities are table-driven by ``_KLING_VIDEO_CAPS`` (verified first-hand against official docs):
- ``kling-v2-5-turbo``: text/image-to-video with first and end frames, no audio / references (default model).
- ``kling-v3`` / ``kling-v3-omni``: flagship, first and end frames + 4K (``mode="4k"``) + joint audio-video output;
  v3-omni has multi-image subject R2V.
- ``kling-v2-6``: supports in-video voices, officially limited to 1080P.
- ``kling-video-o1``: image-to-video + multi-image subject R2V; can only keep the reference video's original sound,
  does not generate native voices.
Unrecognized models (bearer passes through the native model_name) take name_family's generic fallback capabilities.

KlingVideoBackend — 可灵 Kling 视频生成后端（JWT 直连 / Bearer 中转双模式，异步轮询）。

走可灵原生视频端点：submit ``POST /v1/videos/{text2video|image2video}`` 取 ``data.task_id`` →
轮询 ``GET /v1/videos/{subpath}/{task_id}`` 至 ``task_status=succeed`` 取
``task_result.videos[0].url`` → 下载本地。复用 base.py 的 submit/poll/download helpers，
自包含异步状态机、不依赖 DashScope async 机制。

双模式（对齐 ``GeminiVideoBackend`` 的 ``backend_type`` 先例）：
- ``auth_mode="jwt"``（内置 provider）：接 access_key + secret_key，走 ``KlingJWTManager``，
  每次 HTTP 调用前检查过期、距过期 <60s 按需重签——异步渲染可能超单 token 寿命。
- ``auth_mode="bearer"``（自定义 endpoint）：接静态 api_key + base_url，旁路 JWT 管理器。

各视频模型能力按 ``_KLING_VIDEO_CAPS`` 表驱动（官方一手核实）：
- ``kling-v2-5-turbo``：文/图生视频含首尾帧，无音频/参考（默认 model）。
- ``kling-v3`` / ``kling-v3-omni``：旗舰，首尾帧 + 4K（``mode="4k"``）+ 音画同出；v3-omni 多图主体 R2V。
- ``kling-v2-6``：支持视频内人声，官方限 1080P。
- ``kling-video-o1``：图生 + 多图主体 R2V；只能保留参考视频原声、不生成原生人声。
认不得的 model（bearer 透传原生 model_name）取 name_family 的统一回落能力。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

from arcreel_market_core.video_backend_contract import (
    VideoAudioMode,
    VideoCapabilities,
    VideoCapabilityError,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from lib.backends.artifact_download_guard import artifact_http_client
from lib.backends.backend_runtime import (
    ProviderJobIdPersistenceMixin,
    download_resumable_video,
    recording_poll,
    resume_expiry_gate,
)
from lib.backends.kling_backend_base import KlingBackendBase
from lib.backends.kling_shared import (
    extract_kling_video_url,
    image_to_base64,
)
from lib.backends.providers import PROVIDER_KLING
from lib.backends.transport_layer import TransportLayer
from lib.backends.video_backends.metering import video_usage
from lib.backends.video_backends.name_family import match_by_names
from lib.billing.metering import Usage

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "kling-v2-5-turbo"

_TEXT2VIDEO = "text2video"
_IMAGE2VIDEO = "image2video"
_MULTI_IMAGE2VIDEO = "multi-image2video"
_RESUMABLE_SUBPATHS = frozenset({_TEXT2VIDEO, _IMAGE2VIDEO, _MULTI_IMAGE2VIDEO})

# Reference image limit for multi-image subject (R2V), declared separately in the backend caps (read by both orchestration-layer trimming and generation-time defense).
# Conservative value: the official docs do not state this limit explicitly.
# 多图主体（R2V）参考图上限，由 backend caps 单独声明（编排层裁剪与生成时防御同读）。
# 取保守值：官方文档未明确列出该上限。
_R2V_MAX_REFERENCE_IMAGES = 4


@dataclass(frozen=True)
class _KlingVideoModelCaps:
    """Capability bits of a single Kling video model (verified first-hand against official docs). / 单个可灵视频模型的能力位（官方一手核实）。"""

    # Text-to-video gate: a model with this False rejects requests without a first frame (rejected in _build_payload).
    # Image-to-video has no corresponding bit — a first frame is always usable on every tier, so
    # video_capabilities_for_model declares first_frame=True directly.
    # 文生视频闸：为假的 model 不接受无首帧的请求（_build_payload 处拒）。图生视频没有对应位
    # ——各档首帧恒可用，video_capabilities_for_model 直接声明 first_frame=True。
    text_to_video: bool
    last_frame: bool
    # last_frame=True but only usable on the pro tier (official docs: first and end frames of kling-v2-5-turbo and
    # kling-v2-6 are both marked "pro only"); an image_tail request body submitted on the std tier
    # is accepted, but the end-frame constraint does not take effect — video_capabilities_for_tier narrows last_frame on
    # std/4k tiers by this bit, and _build_payload rejects image_tail accordingly instead of letting through a request the
    # caller thinks takes effect but is actually ignored; when the user has loosened the end frame, the user wins.
    # last_frame=True 但仅 pro 档可用（官方文档：kling-v2-5-turbo、kling-v2-6 首尾帧均标"仅 pro"）；
    # std 档提交 image_tail 请求体虽会
    # 被受理，尾帧约束却不生效——video_capabilities_for_tier 按此位在 std/4k 档收窄 last_frame，
    # _build_payload 据此拒绝 image_tail，而非放行一个调用方以为已生效实则被忽略的请求；
    # 用户放宽了尾帧时以用户为准。
    last_frame_requires_pro: bool
    max_reference_images: int
    # Can produce in-video voices (the "joint audio-video output" column of the official capability map): v2-6 / v3 /
    # v3-omni ✅. This bit also decides whether the request body carries the audio switch sound — default is off on every
    # tier officially, and models without the capability do not send the field rather than sending "off" to suppress it.
    # The external audio track form (VideoCapabilities.audio_track) is projected from this bit by `_audio_track_for`; the
    # display layer and enqueue precheck read that, not this bit directly.
    # 能产出视频内人声（官方能力地图的「音画同出」列）：v2-6 / v3 / v3-omni ✅。该位同时决定请求体
    # 是否携带音频开关 sound——官方各档默认 off，无此能力的 model 不发该字段而非发 "off" 压制。
    # 对外的音轨形态（VideoCapabilities.audio_track）由 `_audio_track_for` 从本位投影，展示层与
    # 入队预检读那一份，不直接读本位。
    generate_audio: bool
    # Audio only works at 1080P (official v2-6 text: "when generating video with audio, only 1080P is supported"). Kling
    # request bodies have no resolution field — the output tier is decided by mode alone, so the execution-time criterion
    # sits on mode (see `_effective_audio`).
    # The v3 line officially declares no resolution or tier restriction, hence False.
    # 有声仅在 1080P 下可用（官方 v2-6 明文「生成有声视频时，仅支持生成 1080P」）。可灵请求体没有
    # 分辨率字段——输出档位只由 mode 决定，故执行期判据落在 mode 上（见 `_effective_audio`）。
    # v3 系官方未声明任何分辨率或档位限制，故为 False。
    audio_requires_1080p: bool


# Unrecognized models (bearer passes through the native model_name) take name_family's generic fallback: text-to-video
# and first frame, no end frame, no reference images; no audio switch sent either, so the output is always silent.
# 认不得的 model（bearer 透传原生 model_name）按 name_family 的统一回落：文生视频与首帧，不开尾帧，
# 无参考图；也不下发音频开关，成片恒无声。
_UNRECOGNIZED_VIDEO_CAPS = _KlingVideoModelCaps(
    text_to_video=True,
    last_frame=False,
    last_frame_requires_pro=False,
    max_reference_images=0,
    generate_audio=False,
    audio_requires_1080p=False,
)

_KLING_VIDEO_CAPS: dict[str, _KlingVideoModelCaps] = {
    # Text/image-to-video, first and end frames (pro tier only), no audio / references. / 文/图生视频、首尾帧（仅 pro 档），无音频/参考。
    "kling-v2-5-turbo": _KlingVideoModelCaps(
        text_to_video=True,
        last_frame=True,
        last_frame_requires_pro=True,
        max_reference_images=0,
        generate_audio=False,
        audio_requires_1080p=False,
    ),
    "kling-v3": _KlingVideoModelCaps(
        text_to_video=True,
        last_frame=True,
        last_frame_requires_pro=False,
        max_reference_images=0,
        generate_audio=True,
        audio_requires_1080p=False,
    ),
    "kling-v3-omni": _KlingVideoModelCaps(
        text_to_video=True,
        last_frame=True,
        last_frame_requires_pro=False,
        max_reference_images=_R2V_MAX_REFERENCE_IMAGES,
        generate_audio=True,
        audio_requires_1080p=False,
    ),
    "kling-v2-6": _KlingVideoModelCaps(
        text_to_video=True,
        last_frame=True,
        last_frame_requires_pro=True,
        max_reference_images=0,
        generate_audio=True,
        audio_requires_1080p=True,
    ),
    "kling-video-o1": _KlingVideoModelCaps(
        text_to_video=False,
        last_frame=True,
        last_frame_requires_pro=False,
        max_reference_images=_R2V_MAX_REFERENCE_IMAGES,
        generate_audio=False,
        audio_requires_1080p=False,
    ),
}


def _audio_track_for(caps: _KlingVideoModelCaps) -> VideoAudioMode:
    """Audio track form of the output on the text-to-video / image-to-video subpaths.

    Request bodies of these two subpaths carry the ``sound`` switch, so models with audio capability have a controllable
    audio track; models without it do not send the field, Kling defaults to off on every tier, and the output is always
    silent. ``audio_requires_1080p`` is not narrowed here — it is a per-request tier (mode) constraint and this function
    has no tier context, so it truthfully declares "switch controllable" for the widest tier; whether voices are actually
    produced is decided at request time by ``_effective_audio`` (for pricing see also
    ``effective_generate_audio_for_model``).

    文生 / 图生子路径的成片音轨形态。

    这两条子路径的请求体带 ``sound`` 开关，故有音频能力的 model 音轨可控；无音频能力的 model
    不发该字段，可灵各档默认 off，成片恒无声。``audio_requires_1080p`` 不在此处收窄——它是逐
    请求档位（mode）维度的约束，本函数没有档位上下文，按最宽档如实声明「开关可控」，实际是否
    产出人声由 ``_effective_audio`` 在请求期定夺（计价侧另见
    ``effective_generate_audio_for_model``）。
    """
    return VideoAudioMode.CONTROLLABLE if caps.generate_audio else VideoAudioMode.ALWAYS_OFF


def _match_video_caps(name: str) -> _KlingVideoModelCaps | None:
    """Get capability bits by name: strip the vendor prefix + trim whitespace + lowercase-normalize, then do an EXACT match on _KLING_VIDEO_CAPS.
    Relay prefix separators only recognize the repo's existing conventions ``/`` (``vendor/kling-v3-omni``) and ``:``
    (``provider:kling-v3-omni``) — ``:`` is unified to ``/`` and the last segment is taken. ``_``/``.`` are deliberately
    not separators: they are legal model name characters (wan2. / image-01 / kling-v3-omni all contain them), and
    treating them as separators would break real model names. No guessing an unknown model's capability limits by
    substring (including future versions like kling-v4): an unknown model's limits may differ from known tiers, and
    falsely reporting reference image capability would trigger a provider 400 at request time or billing drift.

    按名字取能力位：剥厂商前缀后 + 去首尾空白 + lower 归一化，再做【精确】命中 _KLING_VIDEO_CAPS。
    中转前缀分隔符仅认仓库既有约定 ``/``（``vendor/kling-v3-omni``）与 ``:``（``provider:kling-v3-omni``）
    ——把 ``:`` 统一成 ``/`` 后取最后一段。刻意不把 ``_``/``.`` 当分隔符：它们是 model 名合法字符
    （wan2. / image-01 / kling-v3-omni 都含），当分隔符会切坏真实 model 名。不按子串猜未知 model（含未来
    版本 kling-v4）的能力上限：未知 model 的限额可能与已知档不同，误报参考图能力会在请求期触发 provider 400
    或计费漂移。"""
    key = name.replace(":", "/").rsplit("/", 1)[-1].strip().lower()
    return _KLING_VIDEO_CAPS.get(key)


def _lookup_video_caps(model: str) -> _KlingVideoModelCaps:
    """The model's capability bits: names are matched exactly one by one per name_family's rules; generic fallback when none is recognized.

    model 的能力位：名字按 name_family 的规则逐个精确命中，都认不得时取统一回落。"""
    found = match_by_names(model, _match_video_caps)
    return found if found is not None else _UNRECOGNIZED_VIDEO_CAPS


# mode → output resolution tier (Kling official: std 720P, pro 1080P, the 4k tier is 4K).
# mode → 输出分辨率档位（可灵官方：std 720P、pro 1080P，4k 档即 4K）。
_MODE_RESOLUTIONS = {"std": "720p", "pro": "1080p", "4k": "4k"}


def _valid_frames(images: list[Path] | None) -> list[Path]:
    """Filter out valid (non-empty) reference image paths; empty / None becomes an empty list. / 过滤出有效（非空）参考图路径；空 / None 归空列表。"""
    if not images:
        return []
    return [Path(img) for img in images if str(img)]


def _has_start_image(request: VideoGenerationRequest) -> bool:
    start_image = request.start_image
    return isinstance(start_image, (str, Path)) and bool(str(start_image))


def _has_end_image(request: VideoGenerationRequest) -> bool:
    end_image = request.end_image
    return isinstance(end_image, (str, Path)) and bool(str(end_image))


def _subpath_for(request: VideoGenerationRequest) -> str:
    """Which subpath the request takes: reference images → multi-image subject; first frame → image-to-video; neither → text-to-video.

    请求走哪个子路径：有参考图 → 多图主体；有首帧 → 图生视频；都没有 → 文生视频。"""
    if _valid_frames(request.reference_images):
        return _MULTI_IMAGE2VIDEO
    return _IMAGE2VIDEO if _has_start_image(request) else _TEXT2VIDEO


def _encode_job_id(subpath: str, task_id: str, *, generate_audio: bool) -> str:
    """Encode the generation-type subpath + audio flag into the persisted job_id (``subpath:task_id:audio``).

    Kling's query endpoints are split by generation type (``GET /v1/videos/{text2video|image2video}/{id}``), and on a
    restart resume the request no longer has ``start_image`` to infer the subpath from — the subpath must be persisted
    with task_id, otherwise resuming an image2video task would query the text2video endpoint and not find the task.

    The audio flag (0/1) is persisted with task_id for the same reason: resume reuses the audio decision made at submit
    directly instead of recomputing it at resume time (config defaults / the request may have drifted), avoiding audio /
    silent billing drift.

    把生成类型子路径 + 有声标志编进持久化 job_id（``subpath:task_id:audio``）。

    可灵查询端点按生成类型分路径（``GET /v1/videos/{text2video|image2video}/{id}``），
    且重启 resume 时请求已无 ``start_image`` 可推断子路径——必须把子路径随 task_id 一起
    持久化，否则 image2video 任务 resume 会误查 text2video 端点取不到任务。

    有声标志（0/1）同理随 task_id 持久化：resume 直接复用 submit 时算定的有声决策，
    不按 resume 时（config 默认/请求可能已漂移）重算，避免有声/无声计费漂移。
    """
    return f"{subpath}:{task_id}:{1 if generate_audio else 0}"


def _decode_job_id(job_id: str) -> tuple[str, str, bool | None]:
    """Restore ``(subpath, task_id, audio flag)`` from the persisted job_id.

    ``subpath:task_id:audio`` (3 parts, audio is 0/1) carries the audio flag; ``subpath:task_id`` (2 parts) does not and
    returns None — the caller treats it as silent; without a known prefix fall back to text2video with the whole string
    as task_id.

    从持久化 job_id 复原 ``(子路径, task_id, 有声标志)``。

    ``subpath:task_id:audio``（3 段，audio 为 0/1）带有声标志；``subpath:task_id``
    （2 段）不带，返回 None——caller 据此按无声处理；无已知前缀回落 text2video、
    整串作 task_id。
    """
    parts = job_id.split(":")
    if len(parts) == 3 and parts[0] in _RESUMABLE_SUBPATHS and parts[2] in ("0", "1"):
        return parts[0], parts[1], parts[2] == "1"
    prefix, sep, rest = job_id.partition(":")
    if sep and prefix in _RESUMABLE_SUBPATHS:
        return prefix, rest, None
    return _TEXT2VIDEO, job_id, None


class KlingVideoBackend(KlingBackendBase, ProviderJobIdPersistenceMixin):
    """Kling video backend (async polling, JWT / Bearer dual mode).

    Auth / base_url assembly / the submit-poll skeleton are shared by ``KlingBackendBase``; ``provider_job_id``
    persistence is handled by ``ProviderJobIdPersistenceMixin``. This class only fills in the video-side differences:
    subpath derivation, capability bit lookup, resume and download.

    可灵 Kling 视频后端（异步轮询，JWT / Bearer 双模式）。

    鉴权 / base_url 装配 / submit-poll 骨架由 ``KlingBackendBase`` 共享；``provider_job_id`` 持久化由
    ``ProviderJobIdPersistenceMixin`` 收口。本类只填视频侧差异：子路径派生、能力位查表、resume 与下载。
    """

    _media_label = "视频"

    def __init__(
        self,
        *,
        auth_mode: str = "jwt",
        access_key: str | None = None,
        secret_key: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        http_timeout: float = 60.0,
        transport_layer: TransportLayer | None = None,
    ) -> None:
        super().__init__(
            auth_mode=auth_mode,
            access_key=access_key,
            secret_key=secret_key,
            api_key=api_key,
            model=model or DEFAULT_MODEL,
            base_url=base_url,
            http_timeout=http_timeout,
            transport_layer=transport_layer,
        )
        # Get capability bits by model (exact match after normalizing prefix/case); unrecognized models (bearer passthrough) take the generic fallback.
        # 按 model 取能力位（归一化前缀/大小写后精确命中）；认不得的 model（bearer 透传）取统一回落。
        self._caps = _lookup_video_caps(self._model)

    @staticmethod
    def video_capabilities_for_model(model: str) -> VideoCapabilities:
        # first_frame is always true (every tier supports i2v first frame); last_frame / reference_images / limits are read by
        # model from _KLING_VIDEO_CAPS (_lookup_video_caps does an exact match after normalizing prefix/case, generic fallback
        # when unrecognized). max_reference_images is only declared here (read by both orchestration-layer trimming and
        # generation-time defense), with a conservative value — the official docs do not state this limit explicitly. Pure
        # function (no client constructed /
        # no api_key needed), reused by the custom endpoint resolver to read limits by model_id.
        #
        # For models with last_frame_requires_pro true (kling-v2-5-turbo, kling-v2-6): this bit is not tiered by service_tier
        # — service_tier is a per-request field (chosen when generation_tasks are enqueued), and this function only declares by
        # model with no way to know which tier the caller will pick. Submitting an end frame on std/4k tiers is rejected —
        # callers with tier context go through video_capabilities_for_tier and are rejected at media_generator; when the user
        # overrides to loosen the end frame, _build_payload sends it as usual per the execution gate's capabilities. Declaring
        # a True that only holds on a few tiers would let callers without tier context pass end_image by this bit and most
        # requests would hit a hard failure; conservatively declaring False is closer to the real execution result on the
        # default tier, the same principle as unrecognized models taking the conservative fallback.
        # first_frame 恒真（各档均支持 i2v 首帧）；last_frame / reference_images / 上限按 model 从
        # _KLING_VIDEO_CAPS 读（_lookup_video_caps 归一化前缀/大小写后精确命中，认不得时取统一回落）。
        # max_reference_images 只在此处声明（编排层裁剪与生成时防御同读它），取保守值——
        # 官方文档未明确列出该上限。纯函数（不构造 client /
        # 不需 api_key），供 custom endpoint resolver 按 model_id 读上限复用。
        #
        # last_frame_requires_pro 为真的 model（kling-v2-5-turbo、kling-v2-6）：该位不按 service_tier
        # 分档——service_tier 是逐请求字段（generation_tasks 入队时选定），本函数只按 model 声明、
        # 无从得知调用方将选哪档。std/4k 档提交尾帧会被拒绝——有 tier 上下文的调用方走
        # video_capabilities_for_tier 在 media_generator 处拒；用户覆盖放宽尾帧时，_build_payload
        # 按执行闸门那份能力照常发出。declare 一个仅在少数档位成立的 True 会让无 tier
        # 上下文的调用方按此位放行 end_image、多数请求撞硬失败；保守声明 False 更贴近默认档的
        # 真实执行结果，与认不得的 model 取保守回落同一原则。
        caps = _lookup_video_caps(model)
        return VideoCapabilities(
            text_to_video=caps.text_to_video,
            first_frame=True,
            last_frame=caps.last_frame and not caps.last_frame_requires_pro,
            max_reference_images=caps.max_reference_images,
            audio_track=_audio_track_for(caps),
            reference_route_audio_track=VideoAudioMode.ALWAYS_OFF,
        )

    @staticmethod
    def effective_generate_audio_for_model(model: str) -> bool:
        """Without per-request tier context, return the audio pricing parameter that actually takes effect on the default execution tier.

        Models whose audio is bound to 1080P (v2-6) cannot know which quality tier the caller will pick, so conservatively
        return False; unconstrained audio models (v3 / v3-omni) produce voices whenever the request asks, so return True. At
        execution ``_effective_audio`` still decides by the request's actual quality tier and subpath — v3-omni on the
        multi-image subject subpath is actually silent, but this interface cannot know whether reference images are present
        and returns True; the direction is overestimating cost (a conservative estimate), never under-reporting.

        无逐请求档位上下文时，返回默认执行档真正生效的音频计价参数。

        有声受 1080P 约束的 model（v2-6）无从得知调用方将选哪档质量档，保守返回
        False；无约束的有声 model（v3 / v3-omni）只要请求要人声就产出，返回 True。执行期仍由
        ``_effective_audio`` 按请求实际质量档与子路径决定——v3-omni 走多图主体子路径时实际无声，
        本接口无从得知是否带参考图而返回 True，方向是高估成本（预估偏保守），不会低报。
        """
        caps = _lookup_video_caps(model)
        return caps.generate_audio and not caps.audio_requires_1080p

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return self.video_capabilities_for_model(self._model)

    def video_capabilities_for_tier(self, service_tier: str, resolution: str | None = None) -> VideoCapabilities:
        """last_frame declaration narrowed by the actual request tier, for callers that have request context.

        `video_capabilities_for_model(model)` is a pure function without request context and can only conservatively declare
        `last_frame=False` for `last_frame_requires_pro` models (for callers without tier info such as
        `/video-capabilities` and the custom provider resolver). But `media_generator` already knows
        `service_tier`/`resolution` before forwarding `end_image` — narrowing here, it only lets through when the actually
        resolved mode (reusing the same derivation rule `_resolve_mode_from`, `resolution="4k"` taking precedence over
        `service_tier`) is pro, and std/4k tiers are still conservatively rejected, aligned with the pass condition of
        `_build_payload`'s fail-loud guard; this avoids pro-tier requests silently losing the frame upstream (the request
        would actually be accepted by `_build_payload`), and avoids wrongly letting the 4k+pro combination through
        (`_resolve_mode` resolves that combination to ``"4k"``, not ``"pro"``).

        按实际请求档位收窄的 last_frame 声明，供有请求上下文的调用方使用。

        `video_capabilities_for_model(model)` 是无请求上下文的纯函数，对
        `last_frame_requires_pro` 的 model 只能保守声明 `last_frame=False`（供
        `/video-capabilities`、custom provider resolver 等无 tier 信息的调用方）。而
        `media_generator` 转发 `end_image` 前已知 `service_tier`/`resolution`——按此处收窄，
        实际解析出的 mode（复用 `_resolve_mode_from` 同一派生规则，`resolution="4k"` 优先于
        `service_tier`）为 pro 才放行、std/4k 档仍保守拒绝，与 `_build_payload` 的 fail-loud
        护栏放行条件对齐，避免 pro 档请求被上层静默丢帧（该请求实际会被 `_build_payload` 接受），
        也避免 4k+pro 组合被误判放行（`_resolve_mode` 对该组合解出 ``"4k"`` 而非 ``"pro"``）。
        """
        caps = _lookup_video_caps(self._model)
        mode = self._resolve_mode_from(resolution, service_tier)
        last_frame = caps.last_frame and (not caps.last_frame_requires_pro or mode == "pro")
        return VideoCapabilities(
            text_to_video=caps.text_to_video,
            first_frame=True,
            last_frame=last_frame,
            max_reference_images=caps.max_reference_images,
            audio_track=_audio_track_for(caps),
            reference_route_audio_track=VideoAudioMode.ALWAYS_OFF,
        )

    # ── request building ────────────────────────────────────────────────

    @staticmethod
    def _resolve_mode_from(resolution: str | None, service_tier: str | None) -> str:
        """Quality tier → mode: resolution=4k is its own ``4k`` tier (only reachable by v3/v3-omni), 1080p→``pro``, otherwise service_tier→std/pro.

        Consistent with the tier derivation of per_second_tiered pricing (4k takes precedence over std/pro), so the request
        tier and billing tier share one source. `_resolve_mode` and `video_capabilities_for_tier` share this same derivation
        rule, to avoid two independent implementations resolving different modes for the same request (this once made the
        tier-aware capability query wrongly judge last_frame=True for the 4k+pro combination).

        质量档 → mode：resolution=4k 独立成 ``4k`` 档（仅 v3/v3-omni 可达），1080p→``pro``，否则 service_tier→std/pro。

        与 per_second_tiered 定价的档位派生一致（4k 优先于 std/pro），保证请求档与计费档同源。
        `_resolve_mode` 与 `video_capabilities_for_tier` 共用此同一派生规则，避免两处独立实现
        对同一请求解出不同 mode（曾因此让 tier-aware 能力查询对 4k+pro 组合误判 last_frame=True）。
        """
        if (resolution or "").lower() == "4k":
            return "4k"
        if (resolution or "").lower() == "1080p":
            return "pro"  # Kling has no resolution field; 1080p is only reachable via the pro tier / 可灵无分辨率字段，1080p 只能靠 pro 档
        return "pro" if (service_tier or "").lower() == "pro" else "std"

    def _resolve_mode(self, request: VideoGenerationRequest) -> str:
        return self._resolve_mode_from(request.resolution, request.service_tier)

    @staticmethod
    def metered_usage(
        model: str, request: VideoGenerationRequest, result: VideoGenerationResult | None = None
    ) -> Usage:
        """Metering of this request (see :mod:`lib.backends.video_backends.metering`).

        Kling request bodies have no resolution field, the output tier is decided by ``mode``: the resolution dimension takes
        the tier matching mode (std 720p, pro 1080p, 4k 4k). Whether there is audio is judged by the ``sound`` actually sent;
        settlement takes the audio flag on ``result`` under the same rules (on resume it is restored from job_id, since the
        request no longer has image fields to infer the subpath).

        这次请求的计量（见 :mod:`lib.backends.video_backends.metering`）。

        可灵请求体没有分辨率字段，输出档位由 ``mode`` 决定：分辨率维度取 mode 对应的档位（std 720p、pro 1080p、
        4k 4k）。是否有声按实际发出的 ``sound`` 判定；结算取 ``result`` 上同一口径的有声标志（续跑时由 job_id
        复原，请求里已没有图片字段可推断子路径）。
        """
        return video_usage(
            request,
            result,
            seconds=request.duration_seconds,
            resolution=_MODE_RESOLUTIONS[
                KlingVideoBackend._resolve_mode_from(request.resolution, request.service_tier)
            ],
            audio=KlingVideoBackend._effective_audio_for(model, request, subpath=_subpath_for(request)),
        )

    def _effective_audio(self, request: VideoGenerationRequest, *, subpath: str) -> bool:
        return self._effective_audio_for(self._model, request, subpath=subpath)

    @staticmethod
    def _effective_audio_for(model: str, request: VideoGenerationRequest, *, subpath: str) -> bool:
        """Whether in-video voices are actually produced: the request asks + the model has generate_audio capability + the
        subpath reaches the audio switch + the model's resolution constraint is met; when the user overrides the audio track
        to always-on, the resolution constraint is not applied.

        Models without the capability are always False — so they are not mispriced at the audio rate (downstream pricing
        takes ``result.generate_audio``).

        ``subpath`` is required rather than inferred from ``request.reference_images``: resume requests are rebuilt without
        image fields (see ``media_generator.resume_video_async``), and inferring from reference images would judge
        multi-image subject tasks with old-format job_ids as having audio. On the generate side it comes from
        ``_build_payload``, on the resume side from decoding the job_id.

        The audio track form reads the execution gate's capabilities (``request.video_capabilities``, including user
        loosening); directly constructed requests fall back to the declaration of ``video_capabilities_for_model`` — the same
        one as the display layer and enqueue precheck. The native multi-image2video schema has no sound, so this subpath is
        always silent, consistent with ``_build_payload`` not putting sound in multi-image subject request bodies; when this
        path is overridden to require audio, ``_build_payload`` rejects it before submit.

        实际是否产出视频内人声：请求要 + model 有 generate_audio 能力 + 走得到音频开关的子路径
        + 满足该 model 的分辨率约束；用户把音轨覆盖为恒有声时不套分辨率约束。

        无能力的 model 恒 False——不被错配有声价（下游 pricing 取 ``result.generate_audio``）。

        ``subpath`` 必传而非由 ``request.reference_images`` 推断：resume 请求重建时不带图字段
        （见 ``media_generator.resume_video_async``），按参考图推断会把旧格式 job_id 的多图主体
        任务判成有声。generate 侧由 ``_build_payload`` 给出，resume 侧由 job_id 解码得出。

        音轨形态读执行闸门那份能力（``request.video_capabilities``，含用户放宽），直接构造的请求
        回落 ``video_capabilities_for_model`` 的声明——与展示层、入队预检同一份。multi-image2video 原生
        schema 不含 sound，这条子路径恒无声，与 ``_build_payload`` 不在多图主体请求体里带 sound 一致；
        这条路径被覆盖为要求有声时，``_build_payload`` 在提交前拒绝。
        """
        if subpath == _MULTI_IMAGE2VIDEO:
            return False
        audio_track = (request.video_capabilities or KlingVideoBackend.video_capabilities_for_model(model)).audio_track
        if audio_track is VideoAudioMode.ALWAYS_OFF:
            return False
        if audio_track is VideoAudioMode.ALWAYS_ON:
            # Kling models only declare controllable or always-silent audio tracks, so always-on must be a user override: loosening sends sound="on" as usual and upstream decides,
            # without applying the 1080P constraint below.
            # 可灵各型号声明的音轨只有可控与恒无声两种，恒有声必是用户覆盖：放宽照常发 sound="on"、由上游裁定，
            # 不套用下面的 1080P 约束。
            return True
        if not request.generate_audio:
            return False
        if _lookup_video_caps(model).audio_requires_1080p:
            # The official constraint is on resolution (v2-6: "when generating video with audio, only 1080P is supported"), but
            # Kling request bodies have no resolution field: the output tier is decided by mode alone, and ``request.resolution``
            # does not enter the payload except to identify the 4k tier. So the criterion sits on the mode actually sent. Models
            # declaring this bit (v2-6) only have std / pro tiers, pro being 1080P; the 4k tier is v3-line only and an illegal
            # request for these models, so it is not let through as meeting 1080P — otherwise it would send sound="on" and bill at
            # the audio rate, getting back a task that is bound to fail or be silent.
            # 官方约束的维度是分辨率（v2-6「生成有声视频时，仅支持生成 1080P」），但可灵请求体没有
            # 分辨率字段：输出档位只由 mode 决定，``request.resolution`` 除识别 4k 档外不进入 payload。
            # 判据因此落在实际发出的 mode 上。声明该位的 model（v2-6）只有 std / pro 两档，pro 即
            # 1080P；4k 档是 v3 系专有，对这些 model 是非法请求，不当作满足 1080P 放行——否则会发
            # sound="on" 并按有声价出账，换回一个必然失败或无声的任务。
            return KlingVideoBackend._resolve_mode_from(request.resolution, request.service_tier) == "pro"
        return True

    def _build_payload(self, request: VideoGenerationRequest) -> tuple[str, dict]:
        """Return (subpath, request body).

        Subpath priority: reference_images → multi-image2video (multi-image subject R2V);
        start_image → image2video (with optional end frame); neither → text2video.

        返回 (子路径, 请求体)。

        子路径优先级：有 reference_images → multi-image2video（多图主体 R2V）；
        有 start_image → image2video（含可选尾帧）；都无 → text2video。
        """
        payload: dict = {
            "model_name": self._model,
            "prompt": request.prompt,
            "mode": self._resolve_mode(request),
            "duration": str(request.duration_seconds),
            "aspect_ratio": request.aspect_ratio,
        }

        # Kling request bodies have no reference audio field; even when the user overrides capabilities to loosen it, it cannot be sent, so reject before paying instead of silently dropping it.
        # 可灵请求体没有参考音频字段；能力被用户覆盖放宽时也发不出，付费前拒绝而不是静默丢弃。
        if request.reference_audio_files:
            raise VideoCapabilityError("video_reference_audio_unsupported", provider=self.name, model=self._model)

        # Whether it can be sent is judged by the execution gate's capabilities: dimensions the user loosened are sent as usual and upstream decides; directly constructed requests fall back to the tier-aware declaration.
        # 可发与否按执行闸门那份能力判定：用户放宽的维度照常发出、由上游裁定；直接构造的请求回落档位感知声明。
        caps = request.video_capabilities or self.video_capabilities_for_tier(
            request.service_tier, resolution=request.resolution
        )
        subpath = _subpath_for(request)
        if subpath == _MULTI_IMAGE2VIDEO:
            reference_images = _valid_frames(request.reference_images)
            # Multi-image subject request bodies have no first/end frame fields; requests with frames are rejected instead of sending only the reference images.
            # 多图主体请求体没有首尾帧字段，带帧的请求拒绝而不是只发参考图。
            if _has_start_image(request) or _has_end_image(request):
                raise VideoCapabilityError("video_reference_images_with_frames_unsupported", model=self._model)
            # Generation-time defense (fail-loud): models without declared multi-image subject capability must not be upgraded to
            # the R2V subpath, and reference image counts over the limit are blocked too — otherwise requests bound to fail would be sent and billed as usual.
            # 生成时防御（fail-loud）：未声明多图主体能力的 model 不得升级到 R2V 子路径，
            # 超上限的参考图数同样拦截——否则会把必然报错的请求发出去且照常计费。
            if caps.max_reference_images <= 0:
                raise VideoCapabilityError("video_reference_images_unsupported", model=self._model)
            if len(reference_images) > caps.max_reference_images:
                raise VideoCapabilityError(
                    "video_reference_images_exceeded",
                    model=self._model,
                    count=len(reference_images),
                    limit=caps.max_reference_images,
                )
            # Multi-image subject request bodies have no sound field; when the user overrides this path's audio track to on it cannot be sent, so reject before paying instead of silently producing a silent final video.
            # 多图主体请求体没有 sound 字段；用户把这条路径的音轨覆盖为有声时发不出，付费前拒绝而不是静默出无声成片。
            route_audio = caps.audio_track_for_route("r2v")
            if route_audio is VideoAudioMode.ALWAYS_ON or (
                route_audio is VideoAudioMode.CONTROLLABLE and request.generate_audio
            ):
                raise VideoCapabilityError(
                    "video_reference_route_audio_unsupported", provider=self.name, model=self._model
                )
            # Multi-image subject: image_list is [{"image": <base64>}] (Kling native schema), no single first frame concept.
            # 多图主体：image_list 为 [{"image": <base64>}]（可灵原生 schema），无单首帧概念。
            payload["image_list"] = [{"image": self._encode_frame(p)} for p in reference_images]
            return _MULTI_IMAGE2VIDEO, payload

        if subpath == _TEXT2VIDEO:
            # No first frame / no references = text-to-video intent; models that do not support t2v (e.g. kling-video-o1) are rejected.
            # 无首帧/无参考 = 文生视频意图；不支持 t2v 的 model（如 kling-video-o1）即拒绝。
            if not caps.text_to_video:
                raise VideoCapabilityError("video_capability_missing_t2v", provider=self.name, model=self._model)
            # An end frame can only go with a first frame as image2video's image_tail; a lone end frame is rejected rather than dropped as text-to-video.
            # 尾帧只能随首帧走 image2video 的 image_tail；单独的尾帧拒绝而不是按文生视频丢掉。
            if _has_end_image(request):
                raise VideoCapabilityError("video_end_image_requires_start_image", model=self._model)
        else:
            payload["image"] = self._encode_frame(Path(cast(Path, request.start_image)))
            if _has_end_image(request):
                if not caps.last_frame:
                    # When this model's first and end frames only take effect on the pro tier, an image_tail submitted on std/4k tiers is
                    # accepted by the official API but the end-frame constraint does not take effect — the tier-aware declaration is already
                    # narrowed by tier; here report the tier as the blocker instead of letting through a request the caller thinks takes
                    # effect but is actually ignored. Tier blocking only applies when the end frame was not loosened by the user.
                    # 该 model 的首尾帧仅 pro 档生效时，std/4k 档提交 image_tail 虽会被官方接口受理，
                    # 尾帧约束却不生效——档位感知声明已按档收窄，这里报出档位这一卡点，而非放行一个
                    # 调用方以为已生效实则被忽略的请求。按档拦截只在尾帧未被用户放宽时生效。
                    if (
                        self._caps.last_frame
                        and self._caps.last_frame_requires_pro
                        and self._resolve_mode(request) != "pro"
                    ):
                        raise VideoCapabilityError(
                            "video_last_frame_requires_pro", provider=self.name, model=self._model
                        )
                    raise VideoCapabilityError("video_last_frame_unsupported", provider=self.name, model=self._model)
                payload["image_tail"] = self._encode_frame(Path(cast(Path, request.end_image)))

        # The audio switch sound (official parameter, "on"/"off", default off on every tier) is only carried on the text2video /
        # image2video subpaths (the native multi-image2video schema does not have it); when the audio track is always silent
        # (including user narrowing) the field is not sent, to avoid passing unknown parameters to endpoints that do not
        # support it; when the user loosens to controllable or always-on it is sent as usual.
        # 音频开关 sound（官方参数，取 "on"/"off"，各档默认 off）仅 text2video / image2video 子路径
        # 携带（multi-image2video 原生 schema 不含）；音轨恒无声（含用户收窄）时不发该字段，避免向不支持的
        # 端点递未知参数；用户放宽为可控或恒有声时照常发出。
        if caps.audio_track is not VideoAudioMode.ALWAYS_OFF:
            payload["sound"] = "on" if self._effective_audio(request, subpath=subpath) else "off"
        return subpath, payload

    def _encode_frame(self, path: Path) -> str:
        # fail-loud: abort if a declared frame image is missing/unreadable, no silent degradation (it would produce a wrong result and still bill).
        # fail-loud：声明了帧图却缺失/不可读即中止，不静默退化（会产出错误结果且照常计费）。
        if not path.is_file():
            raise VideoCapabilityError("video_start_image_unreadable", model=self._model, name=path.name)
        try:
            return image_to_base64(path)
        except OSError as exc:
            raise VideoCapabilityError("video_start_image_unreadable", model=self._model, name=path.name) from exc

    @staticmethod
    def _safe_log_view(subpath: str, payload: dict) -> dict:
        """Pre-redacted scalar view, fed directly to the logger (bypassing the format_kwargs_for_log sink).

        base64 frame images / prompt are never expanded: only record presence + prompt length.

        预脱敏标量视图，直接喂 logger（避开 format_kwargs_for_log sink）。

        base64 帧图 / prompt 一律不展开：仅记是否存在 + prompt 长度。
        """
        prompt = payload.get("prompt")
        image_list = payload.get("image_list")
        return {
            "endpoint": subpath,
            "model_name": payload.get("model_name"),
            "mode": payload.get("mode"),
            "duration": payload.get("duration"),
            "aspect_ratio": payload.get("aspect_ratio"),
            # Empty string = this tier carries no audio switch, distinct from an explicit "off" / 空串 = 该档不携带音频开关，与显式 "off" 区分开
            "sound": payload.get("sound", ""),
            "has_image": "image" in payload,
            "has_image_tail": "image_tail" in payload,
            "reference_count": len(image_list) if isinstance(image_list, list) else 0,
            "prompt_len": len(prompt) if isinstance(prompt, str) else 0,
        }

    # ── generate / resume ───────────────────────────────────────────────

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        # Building the payload reads and encodes first frame / end frame / reference images; run it in a thread to avoid blocking the event loop
        # 组装 payload 要读首帧 / 尾帧 / 参考图并编码，放线程里跑，避免阻塞事件循环
        subpath, payload = await asyncio.to_thread(self._build_payload, request)
        generate_audio = self._effective_audio(request, subpath=subpath)
        logger.info("调用 Kling 视频 API payload=%s", self._safe_log_view(subpath, payload))
        # Task creation, polling and artifact download share the outbound destination gate: base_url is user-configured and likewise treated as an untrusted target.
        # 建任务与轮询与产物下载共用出站目的地闸：base_url 由用户配置，同按不可信目标处理。
        async with artifact_http_client(timeout=self._http_timeout, transport_layer=self._transport_layer) as client:
            task_id = await self._submit_task(client, f"videos/{subpath}", payload, request)
            logger.info("Kling 视频任务已创建: task_id=%s model=%s", task_id, self._model)
            # Persist "subpath:task_id:audio flag" rather than a bare task_id: resume uses it to restore the query endpoint
            # and the audio decision made at submit (see _encode_job_id). Also write back the domain actually requested this time:
            # resume replays it, otherwise a mid-flight base_url change would query the old task on the new domain.
            # 持久化「子路径:task_id:有声标志」而非裸 task_id：resume 据此复原查询端点
            # 与 submit 时的有声决策（见 _encode_job_id）。一并写回本次实际请求的域名：
            # 续跑据此回放，否则在途改了 base_url 会按新域名去查旧任务。
            await self._persist_provider_job_id(
                request,
                _encode_job_id(subpath, task_id, generate_audio=generate_audio),
                provider=PROVIDER_KLING,
                endpoint=self._base_url,
            )
            return await self._poll_and_build(client, subpath, task_id, request, generate_audio=generate_audio)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Resume an already submitted Kling task: only poll + get url + download, no resubmit (ADR 0007).

        The query subpath is restored from the persisted job_id (encoded at submit) — Kling's query endpoints are split by
        generation type, and the resume request no longer has ``start_image`` to infer from, so it cannot come from request
        (see _encode_job_id).

        The audio flag likewise prefers the persisted value (decided at submit): direct audio/silent billing, avoiding
        recomputation from config defaults / the request that may have drifted by resume time. 2-part job_ids without the
        flag are always judged silent: such job_ids are only produced by implementations without audio capability, their
        output is necessarily silent, and recomputing from the current capability table would bill them at the audio rate.

        接续已 submit 的 Kling task：仅轮询 + 取 url + 下载，不重新提交（ADR 0007）。

        查询子路径从持久化 job_id 复原（submit 时编入）——可灵查询端点按生成类型分路径，
        而 resume 请求已无 ``start_image`` 可推断，故不能再从 request 取（见 _encode_job_id）。

        有声标志同样优先取持久化值（submit 时算定）：直连有声/无声计费，避免按 resume 时
        可能已漂移的 config 默认/请求重算。不含该标志的 2 段 job_id 一律判无声：这类 job_id 只
        由不具备音频能力的实现产出，成片必然无声，按当前能力表重算会让它们按有声价出账。
        """
        subpath, task_id, persisted_audio = _decode_job_id(job_id)
        generate_audio = persisted_audio if persisted_audio is not None else False
        async with artifact_http_client(timeout=self._http_timeout, transport_layer=self._transport_layer) as client:
            return await self._poll_and_build(
                client, subpath, task_id, request, generate_audio=generate_audio, resume_job_id=job_id
            )

    # ── HTTP poll / download ────────────────────────────────────────────

    async def _poll_and_build(
        self,
        client: httpx.AsyncClient,
        subpath: str,
        task_id: str,
        request: VideoGenerationRequest,
        *,
        generate_audio: bool,
        resume_job_id: str | None = None,
    ) -> VideoGenerationResult:
        """Poll to the terminal state, fetch and assemble the result; a non-empty ``resume_job_id`` means a resume, its value the persisted job_id.

        轮询至终态、取件并组装结果；``resume_job_id`` 非空表示续跑，值为持久化的 job_id。"""
        # Resume polling replays the submit-time domain (always None on the submit path): a task id can only be queried on the
        # endpoint that created it; after the user changes base_url mid-flight, polling the old task on the new domain would 404 and be misjudged as expired by the 404 gate below.
        # 续跑轮询回放提交时的域名（提交路径恒 None）：任务 id 只在创建它的 endpoint 上可查，
        # 用户在途改 base_url 后按新域名去轮旧任务会 404，被下方的 404 闸门误判成过期。
        base_url = request.submitted_base_url or self._base_url

        gated_poll = resume_expiry_gate(
            recording_poll(lambda: self._poll_query(client, f"videos/{subpath}/{task_id}", base_url=base_url), request),
            resume_job_id=resume_job_id,
            provider=PROVIDER_KLING,
        )

        final = await self._poll_until_terminal(gated_poll, max_wait=request.poll_timeout_seconds)

        download_url = extract_kling_video_url(final)
        await self._download_with_retry(download_url, request.output_path)
        logger.info("Kling 视频下载完成: %s", request.output_path)

        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_KLING,
            model=self._model,
            duration_seconds=request.duration_seconds,
            video_uri=download_url,
            task_id=task_id,
            # The actual audio flag after audio gating (downstream finish_call uses it to pick the audio/silent price).
            # audio 门控后的实际有声标志（下游 finish_call 取它定有声/无声价）。
            generate_audio=generate_audio,
        )

    @staticmethod
    async def _download_with_retry(download_url: str, output_path: Path) -> None:
        await download_resumable_video(download_url, output_path, label="Kling")
