"""GrokVideoBackend — xAI Grok video generation backend. / GrokVideoBackend — xAI Grok 视频生成后端。"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from arcreel_market_core.video_backend_contract import (
    IMAGE_MIME_TYPES,
    VideoAudioMode,
    VideoCapabilities,
    VideoCapabilityError,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from lib.backends.backend_runtime import download_video
from lib.backends.data_uri import image_to_data_uri
from lib.backends.grok_shared import create_grok_client
from lib.backends.providers import PROVIDER_GROK
from lib.backends.video_backends.metering import video_usage
from lib.backends.video_backends.name_family import capabilities_by_names
from lib.billing.metering import Usage
from lib.db.repositories.usage_repo import MAX_BILLED_DURATION_SECONDS
from lib.infra.logging_utils import format_kwargs_for_log

logger = logging.getLogger(__name__)

_MODEL_CLASSIC = "grok-imagine-video"
_MODEL_1_5 = "grok-imagine-video-1.5"
_MODEL_1_5_LITE = "grok-imagine-video-1.5-lite"


def _match_video_capabilities(name: str) -> VideoCapabilities | None:
    """Capabilities recognized by name family; ``None`` when not in the ``grok-imagine-video`` series.

    按名字族认定的能力；不属于 ``grok-imagine-video`` 系列时为 ``None``。"""
    if not name.startswith(_MODEL_CLASSIC):
        return None
    return VideoCapabilities(
        last_frame=name == _MODEL_1_5,
        max_reference_images=0 if name == _MODEL_1_5_LITE else 7,
        audio_track=VideoAudioMode.CONTROLLABLE,
    )


# Resolution cap for reference-to-video across the series (with reference images, or with an end frame): "Reference-to-video is capped at 720p", and a first frame combined with
# last_frame is reference-to-video (https://docs.x.ai/developers/model-capabilities/video/generation).
# In the SDK's resolution enum the only value above that cap is 1080p.
# 全系参考生视频（带参考图，或带尾帧）的分辨率上限：「Reference-to-video is capped at 720p」，且首帧与
# last_frame 组合即参考生视频（https://docs.x.ai/developers/model-capabilities/video/generation）。
# SDK 的分辨率枚举里高于该上限的只有 1080p。
_REFERENCE_ROUTE_MAX_RESOLUTION = "720p"


class GrokVideoBackend:
    """xAI Grok video generation backend. / xAI Grok 视频生成后端。"""

    DEFAULT_MODEL = "grok-imagine-video"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ):
        self._client = create_grok_client(api_key=api_key)
        self._model = model or self.DEFAULT_MODEL

    @property
    def name(self) -> str:
        return PROVIDER_GROK

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def video_capabilities_for_model(model: str) -> VideoCapabilities:
        """Pure computation of caps from model_id — no SDK client constructed (no api_key needed).

        The instance property delegates here, keeping the backend the single source of truth. Capabilities come from the xAI
        video docs (https://docs.x.ai/developers/model-capabilities/video/generation and
        https://docs.x.ai/developers/model-capabilities/video/reference-to-video.md):

        - 1.5 and classic support reference-to-video; that path is capped at 720p, checked at request time by ``generate``.
          The reference image limit of 7 comes from the reference-to-video page: "A maximum of 7 reference images can be
          provided per request".
        - ``grok-imagine-video-1.5``: additionally supports ``last_frame`` to pin the end frame. Reference audio only accepts
          preset ``voice_id``s; custom audio is only open to trusted partners and cannot express the project's reference audio
          files, so not declared.
        - ``grok-imagine-video-1.5-lite``: officially only text-to-video and image-to-video, no reference images declared
          (real calls return ``reference_images is not supported for this model``); first/end frames are only documented for
          1.5, so no end frame declared.
        - ``grok-imagine-video``: officially rejects ``last_frame``, and rejects a first frame together with reference
          images; the latter is checked at request time by ``generate``.

        The audio track is switchable: the request sends the audio intent as the SDK's ``generate_audio`` (audio by default,
        ``False`` gives silent video), and ``generate`` records the same sent value at settlement.

        Names outside the ``grok-imagine-video`` series take name_family's generic fallback.

        按 model_id 纯计算 caps —— 不构造 SDK client（无需 api_key）。

        instance property 委托至此，保持 backend 为单一真相源。能力取自 xAI 视频文档
        （https://docs.x.ai/developers/model-capabilities/video/generation 与
        https://docs.x.ai/developers/model-capabilities/video/reference-to-video.md）：

        - 1.5 与经典版支持参考生视频，该路径分辨率上限 720p，由 ``generate`` 请求期校验。参考图上限 7
          取自参考生视频页「A maximum of 7 reference images can be provided per request」。
        - ``grok-imagine-video-1.5``：另支持 ``last_frame`` 钉住尾帧。参考音频只收预置
          ``voice_id``，自备音频仅向受信合作方开放，表达不了项目的参考音频文件，不声明。
        - ``grok-imagine-video-1.5-lite``：官方只写了文生与图生视频，不声明参考图（真实调用返回
          ``reference_images is not supported for this model``）；首尾帧官方只写在 1.5 上，不声明尾帧。
        - ``grok-imagine-video``：官方写明拒收 ``last_frame``，也拒收首帧与参考图并存，后者由 ``generate``
          请求期校验。

        音轨可开关：请求把音轨意图下发为 SDK 的 ``generate_audio``（缺省有声，``False`` 出无声
        视频），``generate`` 结算按同一下发值记录。

        ``grok-imagine-video`` 系列之外的名字取 name_family 的统一回落。
        """
        return capabilities_by_names(model, _match_video_capabilities)

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return self.video_capabilities_for_model(self._model)

    @staticmethod
    def metered_usage(
        model: str, request: VideoGenerationRequest, result: VideoGenerationResult | None = None
    ) -> Usage:
        """Metering of this request (see :mod:`lib.backends.video_backends.metering`); billed output seconds take the duration reported by xAI.

        这次请求的计量（见 :mod:`lib.backends.video_backends.metering`）；结算的输出秒数取 xAI 回报的时长。"""
        del model
        return video_usage(
            request,
            result,
            seconds=request.duration_seconds,
            resolution=request.resolution,
            audio=request.generate_audio,
        )

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        # Grok is a synchronous API with no job_id to resume; the orphan handler marks [resume_unsupported] on NotImplementedError
        # Grok 同步型 API，无 job_id 可接续；orphan handler 据 NotImplementedError 标 [resume_unsupported]
        raise NotImplementedError("GrokVideoBackend 不支持 resume_video（同步型 API）")

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Generate video; black-box generation is not retried, only the download after the URL is obtained can retry independently.

        生成视频；黑盒生成不重试，只有已取得 URL 后的下载可以独立重试。"""
        # The request body has no reference audio field; even when the user overrides capabilities to loosen it, it cannot be sent, so reject before paying instead of silently dropping it.
        # 请求体没有参考音频字段；能力被用户覆盖放宽时也发不出，付费前拒绝而不是静默丢弃。
        if request.reference_audio_files:
            raise VideoCapabilityError("video_reference_audio_unsupported", provider=self.name, model=self._model)
        self._check_reference_route_resolution(request)
        self._check_classic_first_frame_with_references(request)
        # The SDK combines submit and provider-side waiting in one opaque call. Once it starts, an exception
        # cannot prove the provider rejected the request before accepting a paid job, so close MediaGenerator's
        # reference-payload compression retry window before entering it.
        if request.on_provider_resubmit_unsafe is not None:
            request.on_provider_resubmit_unsafe()
        response = await self._create_video(request)

        video_url = response.url
        # SDK response fields are untyped; narrow to int so it can go into the ledger's Integer column as the actual billed
        # duration; go through float first to accept float strings like "15.0". Missing / unparsable (including inf/nan) /
        # non-positive / beyond a sane upper bound values fall back to the request duration, so the result is always positive and storable.
        # SDK 响应字段未类型化，收窄为 int 才能作为实际计费时长落账本的 Integer 列；
        # 先经 float 接受 "15.0" 这类浮点字符串。缺失/不可解析（含 inf/nan）/非正/
        # 超出合理上限的值回落请求时长，保证结果恒为正且可落库。
        raw_duration = getattr(response, "duration", None)
        actual_duration = request.duration_seconds
        try:
            if raw_duration is not None:
                parsed = float(raw_duration)
                # Bounds are checked on the raw value before rounding: 86400.9 already exceeds 24h and must not be accepted by rounding back within the limit
                # 上下限基于取整前的原始数值判断：86400.9 已超 24h，不得因取整落回上限内被接受
                if 0 < parsed <= MAX_BILLED_DURATION_SECONDS:
                    # half-up rounding, same as dashscope extract_billing_duration, to avoid truncation under-billing seconds;
                    # (0, 0.5) rounding to 0 also falls back, keeping the result always positive
                    # half-up 取整与 dashscope extract_billing_duration 同口径，避免截断少计费秒数；
                    # (0, 0.5) 取整到 0 时同样回落，保持结果恒为正
                    rounded = int(parsed + 0.5)
                    if rounded > 0:
                        actual_duration = rounded
        except (TypeError, ValueError, OverflowError):
            # A parse failure is an expected fallback (SDK fields are untyped); keeping the request duration is enough, no need to raise
            # 解析失败属预期内回落（SDK 字段未类型化），保留请求时长即可，无需上抛
            logger.debug("Grok 回报的 duration 无法解析: %r，回落请求时长 %s 秒", raw_duration, actual_duration)

        await download_video(video_url, request.output_path, label="Grok")
        logger.info("Grok 视频下载完成: %s", request.output_path)

        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_GROK,
            model=self._model,
            duration_seconds=actual_duration,
            video_uri=video_url,
            generate_audio=request.generate_audio,
        )

    def _check_reference_route_resolution(self, request: VideoGenerationRequest) -> None:
        """Reject before the paid call when the reference-to-video path exceeds the 720p cap, rather than leaving it to a provider error or silent downgrade.

        参考生视频路径超出 720p 上限时在付费调用前拒绝，不交给供应商报错或静默降档。"""
        resolution = (request.resolution or "").strip().lower()
        if resolution != "1080p":
            return
        if request.reference_images or request.end_image:
            raise VideoCapabilityError(
                "video_reference_resolution_unsupported",
                model=self._model,
                resolution=resolution,
                max_resolution=_REFERENCE_ROUTE_MAX_RESOLUTION,
            )

    def _check_classic_first_frame_with_references(self, request: VideoGenerationRequest) -> None:
        """The classic model rejects a first frame together with reference images (reference-to-video page: "Classic grok-imagine-video ... rejects combining
        ``image`` with reference inputs"), so reject before the paid call instead of sending both and letting the provider fail it.

        classic 模型拒收首帧与参考图并存（参考生视频页「Classic grok-imagine-video ... rejects combining
        ``image`` with reference inputs」），在付费调用前拒绝，而不是两者一并下发后由供应商判失败。"""
        if self._model == _MODEL_CLASSIC and request.start_image and request.reference_images:
            raise VideoCapabilityError("video_reference_images_with_frames_unsupported", model=self._model)

    async def _create_video(self, request: VideoGenerationRequest):
        """Generate video through an SDK call whose acceptance boundary cannot be determined. / 通过不可判定收单边界的 SDK 调用生成视频。"""
        generate_kwargs = {
            "prompt": request.prompt,
            "model": self._model,
            "duration": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
            "generate_audio": request.generate_audio,
            # Polling happens inside the SDK; still bound it by the global timeout in the request snapshot, otherwise that setting alone would not apply to Grok.
            # 轮询在 SDK 内部，仍按请求快照里的全局超时收口，否则该设置独独对 Grok 不生效。
            "timeout": timedelta(seconds=request.poll_timeout_seconds),
            "interval": timedelta(seconds=5),
        }
        if request.resolution is not None:
            generate_kwargs["resolution"] = request.resolution

        # Existence checks and reading images into data URIs are blocking I/O; offload the whole thing to a thread
        # 存在性检查与读图转 data URI 都是阻塞 I/O，整体卸载到线程
        generate_kwargs.update(await asyncio.to_thread(self._encode_image_inputs, request))

        logger.info("Grok 视频生成开始: model=%s, duration=%ds", self._model, request.duration_seconds)
        logger.info("调用 %s 视频 SDK kwargs=%s", self.name, format_kwargs_for_log(generate_kwargs))
        return await self._client.video.generate(**generate_kwargs)

    def _encode_image_inputs(self, request: VideoGenerationRequest) -> dict[str, Any]:
        """First frame / end frame / reference images → the SDK's data URI parameters (synchronous: reads local images, caller runs it in a thread).

        首帧 / 尾帧 / 参考图 → SDK 的 data URI 参数（同步：读本地图片，调用方放线程里跑）。"""
        kwargs: dict[str, Any] = {}
        if request.start_image:
            image_path = Path(request.start_image)
            # A missing first frame is not silently skipped: skipping would still produce and bill the video, but without using the first frame.
            # 首帧缺失不静默跳过：跳过后照常出片计费，成片却没用上首帧。
            if not image_path.is_file():
                raise VideoCapabilityError("video_start_image_unreadable", model=self._model, name=image_path.name)
            kwargs["image_url"] = image_to_data_uri(image_path, IMAGE_MIME_TYPES)

        if request.end_image:
            # Whether an end frame is allowed is decided beforehand by gate_video_request from video_capabilities.last_frame; only 1.5 gets here.
            # 能否带尾帧由 gate_video_request 按 video_capabilities.last_frame 前置判定，到这里只剩 1.5。
            end_path = Path(request.end_image)
            if not end_path.is_file():
                # A missing end frame is not silently skipped: skipping would still produce and bill the video, but it would not end on the frame the shot requires.
                # 尾帧缺失不静默跳过：跳过后照常出片计费，成片却落不到分镜要求的结尾画面。
                raise VideoCapabilityError(
                    "video_end_image_unreadable", model=self._model, name=end_path.name or str(end_path)
                )
            kwargs["last_frame_url"] = image_to_data_uri(end_path, IMAGE_MIME_TYPES)

        if request.reference_images:
            missing = [p.name for p in request.reference_images if not p.is_file()]
            if missing:
                raise VideoCapabilityError(
                    "video_reference_images_unreadable", model=self._model, names=", ".join(missing)
                )
            kwargs["reference_image_urls"] = [image_to_data_uri(p, IMAGE_MIME_TYPES) for p in request.reference_images]
        return kwargs
