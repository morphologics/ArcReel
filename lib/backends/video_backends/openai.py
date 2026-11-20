"""OpenAIVideoBackend — OpenAI Sora video generation backend. / OpenAIVideoBackend — OpenAI Sora 视频生成后端。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from openai import AsyncOpenAI

from arcreel_market_core.aspect_size import VIDEO_TIER_SHORT_EDGE, parse_aspect_ratio, resolution_to_short_edge
from arcreel_market_core.video_backend_contract import (
    IMAGE_MIME_TYPES,
    TERMINAL_PROVIDER_STATUSES,
    ProviderJobStatus,
    ResumeExpiredError,
    VideoAudioMode,
    VideoCapabilities,
    VideoCapabilityError,
    VideoGenerationRequest,
    VideoGenerationResult,
    normalize_provider_status,
)
from lib.backends.backend_runtime import (
    ProviderJobIdPersistenceMixin,
    faststart_video_artifact,
    poll_with_retry,
    with_artifact_retry,
)
from lib.backends.openai_shared import OPENAI_RETRYABLE_ERRORS, create_openai_client
from lib.backends.providers import PROVIDER_OPENAI
from lib.backends.transport_layer import TransportLayer
from lib.backends.video_backends.metering import video_usage
from lib.backends.video_backends.name_family import UNRECOGNIZED_MODEL_CAPABILITIES, capabilities_by_names
from lib.billing.metering import Usage
from lib.infra.async_thread import run_noninterruptible_sync
from lib.infra.logging_utils import format_kwargs_for_log
from lib.infra.retry import with_retry_async

logger = logging.getLogger(__name__)

# Sora's input_reference only takes a single file; the first frame and reference images share this one slot; both the capability declaration and the pre-submit check read it.
# Sora 的 input_reference 只收单个文件，首帧与参考图共用这一槽位；能力声明与提交前检查都读它。
_INPUT_REFERENCE_SLOTS = 1


# Legal sora sizes are tiered by model capability + resolution tier (OpenAI official changelog / model pages):
# - sora-2 (base): 720p only — 9:16 720x1280 / 16:9 1280x720.
# - sora-2-pro: 720p as above + 1080p — 9:16 1080x1920 / 16:9 1920x1080.
# The SDK's VideoSize Literal lags behind (only lists 720/1024 tiers, misses 1080); the official model page wins;
# legal tiers exclude 1024x1792 / 1792x1024 (4:7, violates ratio-first; the exact 1080p tier already covers that use).
# Not arbitrary WxH, only snapping to legal tiers: pick the tier ratio-first; the resolution tier only decides the 720p vs 1080p subset (sharpness second).
# sora 合法 size 按 model 能力 + 分辨率档分级（OpenAI 官方 changelog / 模型页）：
# - sora-2（base）：仅 720p —— 9:16 720x1280 / 16:9 1280x720。
# - sora-2-pro：720p 同上 + 1080p —— 9:16 1080x1920 / 16:9 1920x1080。
# SDK 的 VideoSize Literal 滞后（只列 720/1024 档、漏 1080），以官方模型页为准；
# 合法档不含 1024x1792 / 1792x1024（4:7，违背比例优先，1080p 精确档已覆盖该用途）。
# 非任意 WxH，只能吸附合法档：比例优先选档，分辨率档只决定 720p vs 1080p 子集（清晰度其次）。
_SORA_SIZES_720P: tuple[str, ...] = ("720x1280", "1280x720")
_SORA_SIZES_1080P: tuple[str, ...] = ("1080x1920", "1920x1080")
# Backward-compatible union export (external code/tests refer to "all legal tiers"). / 向后兼容的并集导出（外部/测试引用「全部合法档」）。
_SORA_LEGAL_SIZES: tuple[str, ...] = _SORA_SIZES_720P + _SORA_SIZES_1080P
_SORA_1080P_MIN_SHORT = 1080


def _video_status(video: object) -> ProviderJobStatus:
    """SDK Video object → canonical status.

    This endpoint serves both built-in Sora and custom providers' openai-video protocol: official Sora only sends
    ``queued`` / ``in_progress`` / ``completed`` / ``failed``, but OpenAI-compatible proxy gateways forwarding non-Sora
    models pass through the underlying vendor's status strings (e.g. ``succeeded``), so everything goes through the
    shared normalization.

    SDK Video 对象 → canonical 状态。

    本端点同时服务内置 Sora 与自定义供应商的 openai-video 协议：官方 Sora 只发
    ``queued`` / ``in_progress`` / ``completed`` / ``failed``，而 OpenAI 兼容代理网关转发
    非 Sora 型号时会透传底层厂商的状态串（如 ``succeeded``），故一律过共享归一。
    """
    return normalize_provider_status(getattr(video, "status", None))


def _video_error_message(video: object) -> str:
    """SDK Video object → provider failure reason text; unknown when unavailable.

    This sentence goes verbatim into ``task.error_message`` and is the whole reason the user reads in the task panel, so
    fields are taken explicitly instead of interpolating the whole object: ``Video.error`` is a model with ``code`` /
    ``message``, and interpolating it directly would write class and field names to the user; ``None`` (the common form
    where the gateway gives only status, no error) would write a failure without a reason. Bare dicts / bare strings
    passed through by proxy gateways are recognized too; unrecognized forms are all unknown.

    SDK Video 对象 → 供应商失败原因文本；取不到返回 unknown。

    这句话原样落进 ``task.error_message``，是用户在任务面板读到的全部原因，故显式取字段而不是
    插值整个对象：``Video.error`` 是带 ``code`` / ``message`` 的模型，直接插值会把类名与字段名
    一并写给用户；``None``（网关只给 status 不给 error 的常见形态）会写出一句没有原因的失败。
    代理网关透传的裸 dict / 裸字符串同样认，认不出的形态一律 unknown。
    """
    err = getattr(video, "error", None)
    if err is None:
        return "unknown"
    if isinstance(err, str):
        return err.strip() or "unknown"
    if isinstance(err, dict):
        candidates = (err.get("message"), err.get("code"))
    else:
        candidates = (getattr(err, "message", None), getattr(err, "code", None))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
        # Numeric error codes (bare HTTP codes are common from gateways) are reasons too; don't drop them just because they are not strings
        # 数字错误码（网关常见的裸 HTTP 码）也是原因，别因为不是字符串就丢掉
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    # If an unrecognized form gives no reason, say unknown: writing the object's own repr into the task panel says nothing
    # 认不出的形态说不出原因就说 unknown：把对象自身的 repr 写进任务面板等于没说
    return "unknown"


def _resolve_size(model: str, resolution: str | None, aspect_ratio: str) -> str:
    """Ratio first: among the legal sora tiers for model+resolution tier, pick the one whose ratio is closest to aspect_ratio; ties go to more pixels.

    The resolution tier only decides the 720p vs 1080p subset, not the ratio: sora-2-pro choosing 1080p gives
    9:16→1080x1920 (exact and high-res), sora-2 (base) or missing resolution lands on 720p (missing resolution defaults to
    720P, no unilateral upgrade to 1080p to avoid overbilling). size is always sent to lock the ratio; never "no size →
    upstream default ratio". Other ratios (1:1/21:9 etc.) have no sora tier and warn after snapping.

    比例优先：在 model+分辨率档对应的 sora 合法档中选比例最接近 aspect_ratio 的；并列取像素更高者。

    分辨率档只决定 720p vs 1080p 子集、不决定比例：sora-2-pro 选 1080p 时 9:16→1080x1920（精确且高清），
    sora-2（base）或缺分辨率时落 720p（缺分辨率默认 720P，不擅自升 1080p 以免超额计费）。size 必传以锁定
    比例，绝不出现「不传 size → 上游默认比例」。其它比例（1:1/21:9 等）sora 无对应档，吸附后告警。
    """
    aw, ah = parse_aspect_ratio(aspect_ratio)
    target = aw / ah
    is_pro = "pro" in model.lower()
    short = resolution_to_short_edge(resolution, tier_map=VIDEO_TIER_SHORT_EDGE)
    # Short-side tiers sora supports: base only 720; pro adds 1080. Pick by "nearest tier" (equidistant goes higher), to
    # avoid needlessly dropping custom resolution values like short=1000 to 720p (floor would drop wrongly, nearest does not).
    # sora 支持的短边档：base 仅 720；pro 增 1080。按「最近档」选（等距取更高档），避免把
    # short=1000 这类自定义分辨率值无故降到 720p（floor 会误降，最近档不会）。
    supported_shorts = [720, _SORA_1080P_MIN_SHORT] if is_pro else [720]
    achieved_short = min(supported_shorts, key=lambda s: (abs(s - short), -s))
    legal = _SORA_SIZES_1080P if achieved_short == _SORA_1080P_MIN_SHORT else _SORA_SIZES_720P
    if short > achieved_short:
        # Request above the model's reachable tier (base asking 1080p, or pro asking 4K): cap it and note that sharpness yields
        # 请求高于模型可达档（base 请 1080p、或 pro 请 4K）：封顶并提示清晰度让位
        logger.warning(
            "OpenAI video: model=%s 无法满足分辨率请求 %s（短边 %d），输出封顶到 %dp 档（清晰度让位比例）",
            model,
            resolution,
            short,
            achieved_short,
        )

    def _score(size: str) -> tuple[float, int]:
        w, h = (int(x) for x in size.split("x"))
        return abs(w / h - target), -(w * h)  # smaller ratio difference first; ties go to more pixels / 比例差小优先；并列取像素更多

    chosen = min(legal, key=_score)
    # 9:16 / 16:9 match exactly; other ratios (e.g. 1:1 / 21:9) have no sora tier, so the ratio necessarily deviates after
    # snapping; warn regardless of the upstream protocol, to avoid silently producing wrong-ratio video (the image path also warns when out of bounds).
    # 9:16 / 16:9 精确命中；其它比例（如 1:1 / 21:9）sora 无对应档，吸附后比例必然偏差，
    # 与上游协议无关地告警，避免静默产出错比例视频（图片路径同样在超界时告警）。
    cw, ch = (int(x) for x in chosen.split("x"))
    if abs(cw / ch - target) > 0.01:
        logger.warning(
            "OpenAI video: aspect_ratio=%s 无精确 sora 档，吸附到 %s（比例偏差，输出非项目设定比例）",
            aspect_ratio,
            chosen,
        )
    # Postcondition invariant: only return sizes within the full set of legal sora tiers, so future tier logic changes cannot silently produce an illegal size.
    # 后置不变量：只返回 sora 合法档全集内的尺寸，防止未来改档逻辑时静默产出非法 size。
    assert chosen in _SORA_LEGAL_SIZES, f"_resolve_size produced illegal sora size: {chosen}"
    return chosen


class OpenAIVideoBackend(ProviderJobIdPersistenceMixin):
    """OpenAI Sora video generation backend. / OpenAI Sora 视频生成后端。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        transport_layer: TransportLayer | None = None,
    ):
        self._client = create_openai_client(api_key=api_key, base_url=base_url, transport_layer=transport_layer)
        if not model:
            raise ValueError("OpenAIVideoBackend requires a model")
        self._model = model

    @property
    def name(self) -> str:
        return PROVIDER_OPENAI

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def video_capabilities_for_model(model: str) -> VideoCapabilities:
        """Pure computation of caps from model_id — no SDK client constructed (no api_key needed).

        Sora's input_reference is a single first frame image, so the reference image limit is 1; the first frame and
        references share that single slot. The Sora 2 series has consistent capabilities and is recognized by name family;
        other names take name_family's generic fallback. The instance property
        delegates here, keeping the backend the single source of truth.

        Audio track always on: Sora output comes with an audio track, and the kwargs assembled by ``generate`` have no audio
        switch field, so the user's intent to turn it off has nowhere to go.

        按 model_id 纯计算 caps —— 不构造 SDK client（无需 api_key）。

        Sora input_reference 为单张首帧图，参考图上限为 1；首帧与参考共享该单槽位。
        Sora 2 系列能力一致，按名字族认定；其余名字取 name_family 的统一回落。instance property
        委托至此，保持 backend 为单一真相源。

        音轨恒有声：Sora 成片自带音轨，``generate`` 组装的 kwargs 里没有音轨开关字段，用户的
        关闭意图无处可下发。
        """
        return capabilities_by_names(
            model,
            lambda name: (
                VideoCapabilities(max_reference_images=_INPUT_REFERENCE_SLOTS, audio_track=VideoAudioMode.ALWAYS_ON)
                if name.startswith("sora-2")
                else None
            ),
            fallback=replace(UNRECOGNIZED_MODEL_CAPABILITIES, audio_track=VideoAudioMode.ALWAYS_ON),
        )

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return self.video_capabilities_for_model(self._model)

    @property
    def _request_base_url(self) -> str:
        """The domain this instance's requests are actually sent to, from the SDK client's public attribute (the SDK adds a trailing slash, removed here).

        本实例请求实际发往的域名，取自 SDK client 的公开属性（SDK 会补尾斜杠，此处去掉）。"""
        return str(self._client.base_url).rstrip("/")

    def _client_for(self, request: VideoGenerationRequest) -> AsyncOpenAI:
        """Client for polling and retrieval: when resuming and the submit domain differs from the current config, derive one bound to the submit domain.

        A task id can only be queried on the endpoint that created it; after the user changes base_url mid-flight, polling
        the old task on the new domain would 404 and be misjudged as expired by ``resume_video``. A client derived via
        ``with_options`` keeps the same credentials, timeout, retry config and connection pool. On the submit path
        ``submitted_base_url`` is always None and the instance client is used.

        轮询与取件用的 client：续跑且提交域名与当下配置不同时，派生一个绑定提交域名的。

        任务 id 只在创建它的 endpoint 上可查，用户在途改 base_url 后按新域名去轮旧任务会 404，
        被 ``resume_video`` 误判成过期。``with_options`` 派生的 client 沿用同一份凭据、超时、重试
        配置与连接池。提交路径的 ``submitted_base_url`` 恒 None，沿用实例 client。
        """
        submitted = request.submitted_base_url
        if not submitted or submitted.rstrip("/") == self._request_base_url:
            return self._client
        return self._client.with_options(base_url=submitted)

    @staticmethod
    def metered_usage(
        model: str, request: VideoGenerationRequest, result: VideoGenerationResult | None = None
    ) -> Usage:
        """Metering of this request (see :mod:`lib.backends.video_backends.metering`).

        Resolution takes the short-side tier of the ``size`` sent (e.g. ``1024x1792`` counts as 1024p). The request has no
        audio switch, so whether there is audio is not reported.

        这次请求的计量（见 :mod:`lib.backends.video_backends.metering`）。

        分辨率取下发的 ``size`` 的短边档（如 ``1024x1792`` 记为 1024p）。请求没有音轨开关，不上报是否有声。
        """
        width, _, height = _resolve_size(model, request.resolution, request.aspect_ratio).partition("x")
        return video_usage(
            request,
            result,
            seconds=request.duration_seconds,
            resolution=f"{min(int(width), int(height))}p",
            audio=None,
        )

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        # Sora request bodies have no end frame or reference audio fields; even when the user overrides capabilities to loosen them they cannot be sent, so reject before paying instead of silently dropping them.
        # Sora 请求体没有尾帧与参考音频字段；能力被用户覆盖放宽时也发不出，付费前拒绝而不是静默丢弃。
        if request.end_image:
            raise VideoCapabilityError("video_last_frame_unsupported", provider=self.name, model=self._model)
        if request.reference_audio_files:
            raise VideoCapabilityError("video_reference_audio_unsupported", provider=self.name, model=self._model)
        # When the first frame and reference images together exceed the slot, the excess would be dropped or rejected upstream; reject before paying.
        # 首帧与参考图合计超过槽位时，超出部分会被丢弃或被上游拒绝，付费前拒绝。
        input_count = bool(request.start_image) + len(request.reference_images or [])
        if input_count > _INPUT_REFERENCE_SLOTS:
            raise VideoCapabilityError(
                "video_reference_images_exceeded",
                provider=self.name,
                model=self._model,
                limit=_INPUT_REFERENCE_SLOTS,
                count=input_count,
            )
        kwargs: dict = {
            "prompt": request.prompt,
            "model": self._model,
            "seconds": str(request.duration_seconds),
        }
        # Always send a legal size to lock the ratio (sora has fixed tiers; without it upstream uses its own default ratio)
        # 始终下传合法 size 以锁定比例（sora 固定档，不传则上游用自家默认比例）
        kwargs["size"] = _resolve_size(self._model, request.resolution, request.aspect_ratio)

        # Collect all reference images (start_image + reference_images): existence checks and image reads are blocking I/O; offload the whole thing to a thread
        # 收集所有参考图（start_image + reference_images）：存在性检查与读图都是阻塞 I/O，整体卸载到线程
        refs = await asyncio.to_thread(_encode_input_references, request, self._model)
        if refs:
            (kwargs["input_reference"],) = refs

        logger.info("OpenAI 视频生成开始: model=%s, seconds=%s", self._model, kwargs["seconds"])
        logger.info("调用 %s 视频 SDK kwargs=%s", self.name, format_kwargs_for_log(kwargs))

        video = await self._create_video(**kwargs)
        # Persist job_id right after a successful submit; a persistence failure raises → finally mark_failed.
        # On non-worker paths (grid / direct generation / tests) request.task_id is None, and persistence is skipped at the single point.
        # Also write back the domain actually requested this time: resume replays it, otherwise a mid-flight base_url change would query the old task on the new domain.
        # submit 成功立即持久化 job_id；持久化失败抛 → finally mark_failed。
        # 非 worker 路径（grid / 直生 / 测试）request.task_id 为 None，统一点内跳过持久化。
        # 一并写回本次实际请求的域名：续跑据此回放，否则在途改了 base_url 会按新域名去查旧任务。
        await self._persist_provider_job_id(
            request, video.id, provider=PROVIDER_OPENAI, endpoint=self._request_base_url
        )
        final = await self._poll_until_complete(self._client, video.id, request.poll_timeout_seconds)

        # On the generate path, expired is a "provider error / input parameters expired" kind of failure;
        # raise RuntimeError so the worker does mark_failed (without the [resume_expired] prefix).
        # generate 路径下 expired 是「provider 异常 / 输入参数过期」类失败，
        # 抛 RuntimeError 让 worker mark_failed（不带 [resume_expired] 前缀）。
        if _video_status(final) is ProviderJobStatus.EXPIRED:
            raise RuntimeError(f"OpenAI Sora job expired during generate: {final.id}")

        return await self._download_and_build_result(self._client, final, request, kwargs)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Resume an already submitted OpenAI job: only poll + download, no videos.create; polling and retrieval replay the submit domain.

        接续已 submit 的 OpenAI job：仅 poll + 下载，不调 videos.create；轮询与取件按提交域名回放。"""
        client = self._client_for(request)
        try:
            final = await self._poll_until_complete(client, job_id, request.poll_timeout_seconds)
        except Exception as exc:
            if _is_openai_not_found(exc):
                raise ResumeExpiredError(job_id=job_id, provider=PROVIDER_OPENAI) from exc
            raise

        # On the resume path, expired = the provider has forgotten it / input assets expired; classify as
        # [resume_expired] so the worker prefixes the error and no longer tries restart self-healing
        # resume 路径下 expired = provider 端已忘 / 输入资产过期，归类
        # [resume_expired] 让 worker 错误前缀化、不再尝试重启自愈
        if _video_status(final) is ProviderJobStatus.EXPIRED:
            raise ResumeExpiredError(
                job_id=job_id,
                provider=PROVIDER_OPENAI,
                message=f"OpenAI Sora job expired: {final.id}",
            )

        return await self._download_and_build_result(client, final, request, {"seconds": str(request.duration_seconds)})

    async def _download_and_build_result(
        self, client: AsyncOpenAI, final, request: VideoGenerationRequest, kwargs: dict
    ) -> VideoGenerationResult:
        content = await self._download_content_with_retry(client, final.id)

        def _write():
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(content.content)

        await run_noninterruptible_sync(_write)
        await faststart_video_artifact(request.output_path)

        logger.info("OpenAI 视频下载完成: %s", request.output_path)

        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_OPENAI,
            model=self._model,
            duration_seconds=int(
                final.seconds if final.seconds is not None else kwargs.get("seconds") or request.duration_seconds
            ),
            task_id=final.id,
        )

    @with_retry_async(retryable_errors=OPENAI_RETRYABLE_ERRORS)
    async def _create_video(self, **kwargs):
        """Only create the video task (with retries); polling is handled by _poll_until_complete itself.

        仅创建视频任务（带重试）；轮询交由 _poll_until_complete 自管。"""
        return await self._client.videos.create(**kwargs)

    async def _poll_until_complete(self, client: AsyncOpenAI, video_id: str, poll_timeout_seconds: int):
        """Poll the task until its status normalizes to a terminal state.

        Does not reuse the SDK's client.videos.poll: it only recognizes in_progress/queued/completed/failed and would exit
        early against OpenAI-compatible gateways that return non-standard statuses (e.g. NOT_START), downloading tasks that are
        not ready.

        轮询任务直到状态归一到终态。

        不复用 SDK 的 client.videos.poll：它仅识别 in_progress/queued/completed/failed，
        对接返回非标状态（如 NOT_START）的 OpenAI 兼容网关时会提前退出，导致下载未就绪任务。
        """
        # is_done is a pure predicate: succeeded / failed / expired all count as "terminal" so poll returns.
        # The caller (generate / resume_video) branches after getting the result:
        #   - succeeded → download
        #   - failed    → is_failed has already raised RuntimeError
        #   - expired   → the caller raises RuntimeError / ResumeExpiredError depending on generate vs resume context
        # Key invariant: is_failed does not recognize expired, so it does not override the caller's branching.
        # is_done 是纯谓词：成功 / 失败 / 过期三档都视为「已终态」让 poll 返回。
        # caller (generate / resume_video) 拿到 result 后再分流：
        #   - succeeded → 下载
        #   - failed    → is_failed 已抛 RuntimeError
        #   - expired   → 在 caller 处按 generate vs resume 上下文抛 RuntimeError / ResumeExpiredError
        # 关键不变量：is_failed 不识别 expired，避免覆盖 caller 分流。
        return await poll_with_retry(
            poll_fn=lambda: client.videos.retrieve(video_id),
            is_done=lambda v: _video_status(v) in TERMINAL_PROVIDER_STATUSES,
            is_failed=lambda v: (
                f"Sora video generation failed: {_video_error_message(v)}"
                if _video_status(v) is ProviderJobStatus.FAILED
                else None
            ),
            max_wait=poll_timeout_seconds,
            retryable_errors=OPENAI_RETRYABLE_ERRORS,
            label="OpenAI",
            on_progress=lambda v, elapsed: logger.info(
                "OpenAI 视频生成中... 状态: %s, 已等待 %d 秒", v.status, int(elapsed)
            ),
        )

    async def _download_content_with_retry(self, client: AsyncOpenAI, video_id: str):
        """Retry the content download separately, so a download failure does not re-trigger video generation.

        SDK retrieval does not raise ``HTTPStatusError``, so fall back to judging by ``OPENAI_RETRYABLE_ERRORS``; the stop
        condition is still the shared artifact download budget.

        单独重试内容下载，避免因下载失败重新触发视频生成。

        SDK 取件抛的不是 ``HTTPStatusError``，故退到按 ``OPENAI_RETRYABLE_ERRORS`` 判定，
        终止条件仍是共用的产物下载预算。
        """
        return await with_artifact_retry(
            lambda: client.videos.download_content(video_id),
            label="OpenAI",
            retry_if=None,
            retryable_errors=OPENAI_RETRYABLE_ERRORS,
        )


def _encode_start_image(image_path: Path) -> tuple[str, bytes, str]:
    mime = IMAGE_MIME_TYPES.get(image_path.suffix.lower(), "image/png")
    return (image_path.name, image_path.read_bytes(), mime)


def _encode_input_references(request: VideoGenerationRequest, model: str) -> list[tuple[str, bytes, str]]:
    """Missing first frame / reference images are not silently skipped: skipping would still produce and bill the video without using the user's images.

    缺失的首帧 / 参考图不静默跳过：跳过后照常出片计费，却没用上用户给的图。"""
    ref_paths: list[Path] = []
    if request.start_image:
        start_path = Path(request.start_image)
        if not start_path.is_file():
            raise VideoCapabilityError("video_start_image_unreadable", model=model, name=start_path.name)
        ref_paths.append(start_path)
    missing = [p.name for p in request.reference_images or [] if not p.is_file()]
    if missing:
        raise VideoCapabilityError("video_reference_images_unreadable", model=model, names=", ".join(missing))
    ref_paths.extend(request.reference_images or [])
    return [_encode_start_image(p) for p in ref_paths]


def _is_openai_not_found(exc: BaseException) -> bool:
    """Recognize OpenAI/Sora "job does not exist" responses (NotFoundError / HTTP 404).

    No ``"not found"`` / ``"expired"`` substring fallback: ``status='expired'`` is turned into ``ResumeExpiredError``
    directly by ``resume_video`` after polling returns a terminal state, and broad strings would misjudge business errors
    like ``"file not found in storage"`` as ghost tasks.

    识别 OpenAI/Sora 「job 不存在」响应（NotFoundError / HTTP 404）。

    不做 ``"not found"`` / ``"expired"`` 子串兜底：``status='expired'`` 由 ``resume_video`` 在轮询
    返回终态后直接转 ``ResumeExpiredError``，宽泛字串会把诸如 ``"file not found in storage"`` 等
    业务错误误判为幽灵任务。
    """
    try:
        from openai import NotFoundError
    except ImportError:
        NotFoundError = None

    if NotFoundError is not None and isinstance(exc, NotFoundError):
        return True
    status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return status_code == 404
