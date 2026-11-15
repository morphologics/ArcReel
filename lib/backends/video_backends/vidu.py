"""ViduVideoBackend — Vidu video generation backend.

Dispatches to 4 Vidu API endpoints by request fields:
- prompt only            → ``/text2video``
- ``start_image``        → ``/img2video``
- ``start_image + end``  → ``/start-end2video``
- ``reference_images``   → ``/reference2video``

ViduVideoBackend — Vidu 视频生成后端。

按 request 字段分派到 4 个 Vidu API 端点：
- 仅 prompt              → ``/text2video``
- ``start_image``        → ``/img2video``
- ``start_image + end``  → ``/start-end2video``
- ``reference_images``   → ``/reference2video``
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from arcreel_market_core.video_backend_contract import (
    VideoAudioMode,
    VideoCapabilities,
    VideoCapabilityError,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from lib.backends.backend_runtime import (
    download_video,
    poll_with_retry,
    recording_poll,
    should_retry_submit,
    submit_post,
)
from lib.backends.providers import PROVIDER_VIDU
from lib.backends.transport_layer import TransportLayer
from lib.backends.video_backends.metering import video_usage
from lib.backends.video_backends.name_family import UNRECOGNIZED_MODEL_CAPABILITIES, match_by_names
from lib.backends.vidu_shared import (
    VIDU_RETRYABLE_ERRORS,
    assert_vidu_body_size,
    create_vidu_client,
    extract_vidu_url,
    fetch_vidu_task,
    image_to_data_uri,
    is_vidu_done,
    safe_body_for_log,
    vidu_failure_reason,
)
from lib.billing.metering import Usage
from lib.infra.logging_utils import format_kwargs_for_log
from lib.infra.retry import with_retry_async

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "viduq3-turbo"
_MAX_REFERENCE_IMAGES = 7
_PROMPT_MAX_TEXT2VIDEO = 5000
_PROMPT_MAX_REFERENCE2VIDEO = 2000

# The q3 series has direct audio-video output on by default, q2/q1/2.0 are silent by default; audio only takes effect when sent to q3
# q3 系列模型默认开启音视频直出，q2/q1/2.0 默认静音；audio 仅传 q3 时才生效
_Q3_MODELS = frozenset(
    {
        "viduq3-pro",
        "viduq3-turbo",
        "viduq3-pro-fast",
        "viduq3",
        "viduq3-mix",
    }
)

# Legal durations per (model, endpoint) — the docs list them per endpoint, and they differ a lot
# 按 (model, endpoint) 列出合法 duration —— 文档逐端点列出，差异很大
_DURATION_RULES: dict[tuple[str, str], list[int]] = {
    # /text2video
    ("viduq3-pro", "/text2video"): list(range(1, 17)),
    ("viduq3-turbo", "/text2video"): list(range(1, 17)),
    ("viduq2", "/text2video"): list(range(1, 11)),
    ("viduq1", "/text2video"): [5],
    # /img2video
    ("viduq3-pro", "/img2video"): list(range(1, 17)),
    ("viduq3-turbo", "/img2video"): list(range(1, 17)),
    ("viduq3-pro-fast", "/img2video"): list(range(1, 17)),
    # The q2 line on /img2video is 1–10; 1–8 is the value of the /start-end2video (first and end frame) endpoint, the two endpoints differ.
    # q2 系在 /img2video 为 1–10；1–8 是 /start-end2video（首尾帧）端点的值，两端点不同。
    ("viduq2-pro-fast", "/img2video"): list(range(1, 11)),
    ("viduq2-pro", "/img2video"): list(range(1, 11)),
    ("viduq2-turbo", "/img2video"): list(range(1, 11)),
    ("viduq1", "/img2video"): [5],
    ("viduq1-classic", "/img2video"): [5],
    ("vidu2.0", "/img2video"): [4, 8],
    # /start-end2video
    ("viduq3-pro", "/start-end2video"): list(range(1, 17)),
    ("viduq3-turbo", "/start-end2video"): list(range(1, 17)),
    ("viduq2-pro-fast", "/start-end2video"): list(range(1, 9)),
    ("viduq2-pro", "/start-end2video"): list(range(1, 9)),
    ("viduq2-turbo", "/start-end2video"): list(range(1, 9)),
    ("viduq1", "/start-end2video"): [5],
    ("viduq1-classic", "/start-end2video"): [5],
    ("vidu2.0", "/start-end2video"): [4, 8],
    # /reference2video (non-subject) / /reference2video (非主体)
    ("viduq3-turbo", "/reference2video"): list(range(3, 17)),
    ("viduq3", "/reference2video"): list(range(3, 17)),
    ("viduq3-mix", "/reference2video"): list(range(3, 17)),
    ("viduq2-pro", "/reference2video"): list(range(11)),  # 0=auto / 0=自动
    ("viduq2", "/reference2video"): list(range(1, 11)),
    ("viduq1", "/reference2video"): [5],
    ("vidu2.0", "/reference2video"): [4],
}

# Set of models each endpoint supports — for early failure messages (based on the official docs listed in docs/api-docs/providers/vidu.md)
# 端点支持的模型集合 —— 用于早期失败提示（依据 docs/api-docs/providers/vidu.md 所列官方文档）
_ENDPOINT_MODELS: dict[str, frozenset[str]] = {
    "/text2video": frozenset({"viduq3-turbo", "viduq3-pro", "viduq2", "viduq1"}),
    "/img2video": frozenset(
        {
            "viduq3-turbo",
            "viduq3-pro",
            "viduq3-pro-fast",
            "viduq2-pro-fast",
            "viduq2-pro",
            "viduq2-turbo",
            "viduq1",
            "viduq1-classic",
            "vidu2.0",
        }
    ),
    "/start-end2video": frozenset(
        {
            "viduq3-turbo",
            "viduq3-pro",
            "viduq2-pro-fast",
            "viduq2-pro",
            "viduq2-turbo",
            "viduq1",
            "viduq1-classic",
            "vidu2.0",
        }
    ),
    "/reference2video": frozenset(
        {"viduq3-mix", "viduq3-turbo", "viduq3", "viduq2-pro", "viduq2", "viduq1", "vidu2.0"}
    ),
}

# Whether the endpoint accepts aspect_ratio / 端点是否接受 aspect_ratio
_ENDPOINTS_WITH_ASPECT_RATIO = frozenset({"/text2video", "/reference2video"})


_KNOWN_MODELS = frozenset().union(*_ENDPOINT_MODELS.values())


def _known_model(model: str) -> str | None:
    """The Vidu model recognized by name, i.e. a key of the endpoint whitelist and per-model rule tables; ``None`` when unrecognized.

    Names are taken per name_family's rules and compared exactly one by one against the endpoint whitelist.

    模型按名字认得的 Vidu 型号，即端点白名单与逐型号规则表的键；认不得时为 ``None``。

    名字按 name_family 的规则取，逐个精确比对端点白名单。
    """
    return match_by_names(model, lambda name: name if name in _KNOWN_MODELS else None)


def _serves_only_reference2video(model: str) -> bool:
    """The model is dispatched to no endpoint other than /reference2video.

    This is the criterion for whether the narrow r2v prompt limit can be declared statically without request context:
    for multi-endpoint models going r2v, the narrow value is enforced by ``_build_request`` per the actual endpoint instead.

    该 model 除 /reference2video 外不派发到任何端点。

    是无请求上下文时能否按 r2v 的窄 prompt 上限静态声明的判据：多端点 model 走 r2v 时的
    窄值改由 ``_build_request`` 按实际端点兜底。
    """
    return model in _ENDPOINT_MODELS["/reference2video"] and not any(
        model in models for endpoint, models in _ENDPOINT_MODELS.items() if endpoint != "/reference2video"
    )


class ViduVideoBackend:
    """Vidu video generation backend, dispatching to different endpoints by request fields. / Vidu 视频生成后端，按 request 字段分派到不同端点。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        transport_layer: TransportLayer | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._transport_layer = transport_layer
        self._model = model or DEFAULT_MODEL
        # Endpoint whitelist and per-model rules are looked up by the recognized model; unrecognized models get no model-level correction and are sent upstream as is.
        # 端点白名单与逐型号规则按认得的型号查；认不得的模型不做型号级校正，原样发给上游。
        self._known_model = _known_model(self._model)

    @property
    def name(self) -> str:
        return PROVIDER_VIDU

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def video_capabilities_for_model(model: str) -> VideoCapabilities:
        """Pure computation of caps from the model name — no client constructed. Unrecognized models (see ``_known_model``) take
        name_family's generic fallback, the audio track is declared always silent (only the q3 series sends the audio switch),
        and the prompt limit takes the wide value.

        For recognized models first_frame/last_frame/reference_images
        are looked up directly in the endpoint whitelist of `_ENDPOINT_MODELS`, the same source as whether
        `_select_endpoint`/`_build_request` can actually dispatch to /img2video, /start-end2video, /reference2video: e.g.
        `viduq3` is not in the /img2video and /start-end2video whitelists (only /reference2video); if these were always True,
        callers submitting a first/end frame per the declaration would hit a RuntimeError in `_build_request`.
        `max_reference_images` drops to zero together with `reference_images` (the same implementation convention as
        `kling.py`): a model not in the /reference2video whitelist can never submit reference images, and declaring nonzero
        capacity would mislead the display layer (e.g. a custom provider's `/video-capabilities`).

        按模型名纯计算 caps —— 不构造 client。认不得的型号（见 ``_known_model``）取 name_family 的
        统一回落，音轨按恒无声声明（只有 q3 系列下发音轨开关），prompt 上限取宽值。

        认得的型号 first_frame/last_frame/reference_images
        直接查 `_ENDPOINT_MODELS` 的端点白名单，与 `_select_endpoint`/`_build_request` 实际能否
        派发到 /img2video、/start-end2video、/reference2video 同源：例如 `viduq3` 不在
        /img2video、/start-end2video 白名单内（只支持 /reference2video），若恒为 True，
        调用方按声明提交首帧/尾帧会在 `_build_request` 抛 RuntimeError。`max_reference_images`
        随 `reference_images` 联动归零（与 `kling.py` 同一实现约定一致）：不在 /reference2video
        白名单的 model 不可能提交参考图，声明非零容量会误导展示层（如自定义供应商的
        `/video-capabilities`）。
        """
        known = _known_model(model)
        if known is None:
            return replace(
                UNRECOGNIZED_MODEL_CAPABILITIES,
                max_prompt_chars=_PROMPT_MAX_TEXT2VIDEO,
                audio_track=VideoAudioMode.ALWAYS_OFF,
            )
        reference_images = known in _ENDPOINT_MODELS["/reference2video"]
        first_frame = known in _ENDPOINT_MODELS["/img2video"]
        last_frame = known in _ENDPOINT_MODELS["/start-end2video"]
        return VideoCapabilities(
            text_to_video=known in _ENDPOINT_MODELS["/text2video"],
            first_frame=first_frame,
            last_frame=last_frame,
            max_reference_images=_MAX_REFERENCE_IMAGES if reference_images else 0,
            # Reference images and first/end frames are mutually exclusive modes on Vidu: _select_endpoint switches to
            # /reference2video when it sees reference images, that endpoint takes no first/end frames, and _build_request rejects
            # requests carrying both.
            #
            # The prompt limit is tiered by endpoint (/reference2video is narrower), but this function has no endpoint context and
            # can only declare a value that "holds for this model whichever endpoint it takes", rather than wrongly rejecting
            # legitimate t2v/i2v requests of multi-endpoint models by the narrowest value here.
            # 参考图与首尾帧在 Vidu 上是互斥模式：_select_endpoint 见参考图即切 /reference2video，
            # 该端点不收首尾帧，_build_request 拒绝两者同带的请求。
            #
            # prompt 上限按端点分档（/reference2video 更窄），而本函数无端点上下文，只能声明
            # 「该 model 无论走哪条端点都成立」的值，不在此处按最窄值误拒多端点 model 合法的
            # t2v/i2v 请求。
            max_prompt_chars=(
                _PROMPT_MAX_REFERENCE2VIDEO if _serves_only_reference2video(known) else _PROMPT_MAX_TEXT2VIDEO
            ),
            # The audio switch body["audio"] is only sent for the q3 series (_build_request); other series are silent by default and
            # have no switch to send, hence always silent — the same source as the settlement rule
            # `generate_audio=... if self._known_model in _Q3_MODELS else False`.
            # 音轨开关 body["audio"] 只对 q3 系列下发（_build_request），其余系列默认静音且无
            # 开关可下发，故恒无声——与 `generate_audio=... if self._known_model in _Q3_MODELS else False`
            # 的结算口径同源。
            audio_track=(VideoAudioMode.CONTROLLABLE if known in _Q3_MODELS else VideoAudioMode.ALWAYS_OFF),
        )

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return self.video_capabilities_for_model(self._model)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        # Vidu does not support resume: polling is fully inlined in generate, with no separate polling entry to resume from.
        # The orphan handler marks [resume_unsupported] on NotImplementedError
        # Vidu 不支持 resume：poll 完全内联在 generate，没有可供接续的独立轮询入口。
        # orphan handler 据 NotImplementedError 标 [resume_unsupported]
        raise NotImplementedError("ViduVideoBackend 暂不支持 resume_video")

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        endpoint, body = self._build_request(request)
        # _build_request has already normalized duration into body["duration"]; use it throughout to avoid None crashes and inconsistent results.
        # _build_request 已把 duration 归一化到 body["duration"]，统一使用以避免 None 崩溃及结果不一致。
        coerced_duration = int(body["duration"])

        async with create_vidu_client(
            api_key=self._api_key, base_url=self._base_url, transport_layer=self._transport_layer
        ) as client:
            payload = await self._create_task(client, endpoint, body, request)
            task_id = payload["task_id"]
            if request.on_provider_resubmit_unsafe is not None:
                request.on_provider_resubmit_unsafe()
            credits = payload.get("credits")
            logger.info(
                "Vidu 视频任务已创建: endpoint=%s task_id=%s credits=%s model=%s",
                endpoint,
                task_id,
                credits,
                self._model,
            )

            final = await poll_with_retry(
                poll_fn=recording_poll(lambda: fetch_vidu_task(client, task_id), request),
                is_done=is_vidu_done,
                is_failed=vidu_failure_reason,
                max_wait=request.poll_timeout_seconds,
                retryable_errors=VIDU_RETRYABLE_ERRORS,
                label="Vidu",
                on_progress=lambda v, elapsed: logger.info(
                    "Vidu 视频生成中... state=%s elapsed=%ds", v.get("state"), int(elapsed)
                ),
            )
            url = extract_vidu_url(final)
            # credits=0 is a legal value and cannot be merged with `or`; check None explicitly and fall back to the top-level credits.
            # credits=0 是合法值，不能用 `or` 合并；显式判 None 兜底为顶层 credits。
            final_credits = final.get("credits")
            actual_credits = final_credits if final_credits is not None else credits

        await self._download_video(url, request.output_path)
        logger.info("Vidu 视频下载完成: %s", request.output_path)

        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_VIDU,
            model=self._model,
            duration_seconds=coerced_duration,
            seed=request.seed,
            usage_tokens=int(actual_credits) if actual_credits is not None else None,
            task_id=task_id,
            generate_audio=request.generate_audio if self._known_model in _Q3_MODELS else False,
        )

    # ── request building ────────────────────────────────────────────────

    def _build_request(self, request: VideoGenerationRequest) -> tuple[str, dict]:
        endpoint = self._select_endpoint(request)

        # 0) Inputs the endpoint cannot send are rejected before paying, not silently dropped: the gate lets them through when the user overrides capabilities to loosen them.
        # 0) 端点发不出的输入付费前拒绝，不静默丢弃：能力被用户覆盖放宽时 gate 会放行它们。
        if request.reference_audio_files:
            raise VideoCapabilityError("video_reference_audio_unsupported", provider=self.name, model=self._model)
        if endpoint == "/reference2video":
            if request.start_image is not None or request.end_image is not None:
                raise VideoCapabilityError("video_reference_images_with_frames_unsupported", model=self._model)
            count = len(_reference_images(request))
            if count > _MAX_REFERENCE_IMAGES:
                raise VideoCapabilityError(
                    "video_reference_images_exceeded", model=self._model, limit=_MAX_REFERENCE_IMAGES, count=count
                )
        if endpoint == "/text2video" and request.end_image is not None:
            raise VideoCapabilityError("video_end_image_requires_start_image", model=self._model)

        # 1) Validate recognized model × endpoint (failing early is clearer than a 400); unrecognized models are judged upstream
        # 1) 校验认得的型号 × 端点（提前失败比 400 含义更清晰）；认不得的模型由上游判定
        endpoint_models = _ENDPOINT_MODELS.get(endpoint, frozenset())
        if self._known_model is not None and endpoint_models and self._known_model not in endpoint_models:
            raise RuntimeError(f"Vidu 模型 {self._model} 不支持 {endpoint} 端点；该端点支持: {sorted(endpoint_models)}")

        # 2) Take the nearest duration within the legal set / 2) duration 在合法集合内取最近值
        duration = _coerce_duration(self._known_model or self._model, endpoint, request.duration_seconds)

        # 3) prompt length (reference2video limit 2000, others 5000)
        # Overflow is always rejected, no more client-side truncation: truncation would produce a final video not matching the
        # intent while still billing, without the user knowing, consistent with gate_video_request's rule "the same violation
        # only fails hard, never silently degrades". The model-level limit (the one declared by video_capabilities_for_model)
        # is already blocked by the gate before paying; this is the execution-time defense for the narrower endpoint-level
        # limit — the gate's static declaration does not cover multi-endpoint models going to /reference2video.
        # 3) prompt 长度（reference2video 上限 2000，其他 5000）
        # 超限一律拒绝，不再客户端截断：截断会产出与意图不符的成片却照常计费、用户无从知情，
        # 与 gate_video_request 「同一种违约只有硬失败、没有静默降级」的口径一致。model 级上限
        # （video_capabilities_for_model 声明的那个）已由 gate 在付费前拦下；此处是端点级更窄
        # 上限的执行期防御——多端点 model 走 /reference2video 时 gate 的静态声明覆盖不到。
        prompt = request.prompt or ""
        prompt_max = _PROMPT_MAX_REFERENCE2VIDEO if endpoint == "/reference2video" else _PROMPT_MAX_TEXT2VIDEO
        if len(prompt) > prompt_max:
            raise VideoCapabilityError(
                "video_prompt_too_long",
                provider=self.name,
                model=self._model,
                limit=prompt_max,
                count=len(prompt),
            )

        body: dict = {
            "model": self._model,
            "prompt": prompt,
            "duration": duration,
        }

        # 4) resolution follows the model catalog entry and is passed through as is; out-of-range tiers are rejected upstream (docs/adr/0104)
        # 4) resolution 以模型目录条目为准，原样透传；越界档位由上游拒绝（docs/adr/0104）
        if request.resolution:
            body["resolution"] = request.resolution

        # 5) seed
        if request.seed is not None:
            body["seed"] = request.seed

        # 6) audio is only accepted by the q3 series / 6) audio 仅 q3 系列接受
        if self._known_model in _Q3_MODELS:
            body["audio"] = bool(request.generate_audio)

        # 7) aspect_ratio is only accepted by text2video / reference2video / 7) aspect_ratio 仅 text2video / reference2video 接受
        if endpoint in _ENDPOINTS_WITH_ASPECT_RATIO and request.aspect_ratio:
            body["aspect_ratio"] = request.aspect_ratio

        # 8) images are filled per endpoint shape / 8) images 按端点形态填充
        if endpoint == "/reference2video":
            body["images"] = [image_to_data_uri(p) for p in _reference_images(request)]
        elif endpoint == "/start-end2video":
            assert request.start_image is not None
            assert request.end_image is not None
            body["images"] = [
                image_to_data_uri(Path(request.start_image)),
                image_to_data_uri(Path(request.end_image)),
            ]
        elif endpoint == "/img2video":
            assert request.start_image is not None
            body["images"] = [image_to_data_uri(Path(request.start_image))]
        # /text2video carries no images / /text2video 不带 images

        return endpoint, body

    def _select_endpoint(self, request: VideoGenerationRequest) -> str:
        # If the user explicitly passed start/end_image but the file does not exist, fail directly instead of silently degrading to text2video.
        # 若用户显式传了 start/end_image 但文件不存在，应直接失败而不是静默降级到 text2video。
        if request.start_image is not None and not Path(request.start_image).exists():
            raise FileNotFoundError(f"start_image 文件不存在: {request.start_image}")
        if request.end_image is not None and not Path(request.end_image).exists():
            raise FileNotFoundError(f"end_image 文件不存在: {request.end_image}")
        return _endpoint_for(request)

    @staticmethod
    def metered_usage(
        model: str, request: VideoGenerationRequest, result: VideoGenerationResult | None = None
    ) -> Usage:
        """Metering of this request (see :mod:`lib.backends.video_backends.metering`).

        Duration and reference image count take the values sent after ``_build_request`` correction, resolution takes the
        request's original value; reference-to-video is judged by endpoint — Vidu prices reference-to-video differently from
        image-to-video and text-to-video. Audio only applies to the q3 series (only they send ``audio``).

        这次请求的计量（见 :mod:`lib.backends.video_backends.metering`）。

        时长与参考图张数取 ``_build_request`` 校正后的发送值，分辨率取请求原值；参考生视频按端点判定，Vidu 的
        参考生视频与图生、文生视频单价不同。是否有声只对 q3 系列成立（只有它们发送 ``audio``）。
        """
        known = _known_model(model)
        endpoint = _endpoint_for(request)
        seconds = _coerce_duration(known or model, endpoint, request.duration_seconds)
        return video_usage(
            request,
            result,
            seconds=seconds,
            resolution=request.resolution or None,
            audio=bool(request.generate_audio) if known in _Q3_MODELS else False,
            reference_images=len(_reference_images(request)) if endpoint == "/reference2video" else 0,
            reference_to_video=endpoint == "/reference2video",
        )

    # ── HTTP wrappers ───────────────────────────────────────────────────

    @with_retry_async(retry_if=should_retry_submit)
    async def _create_task(
        self, client, endpoint: str, body: dict, request: VideoGenerationRequest | None = None
    ) -> dict:
        assert_vidu_body_size(body)
        logger.info(
            "调用 Vidu 视频 API endpoint=%s kwargs=%s",
            endpoint,
            format_kwargs_for_log(safe_body_for_log(body)),
        )
        # create is a non-idempotent "create + bill" call: submit_post turns ambiguous states (ReadTimeout etc., "the request
        # may have reached the server") into a terminal AmbiguousSubmitError failure with no retry; only connection
        # failures where "the request definitely did not arrive" go to should_retry_submit for retry, avoiding duplicate tasks + duplicate billing.
        # 4xx like 413 surface via raise_for_status as httpx.HTTPStatusError (keeping status_code),
        # so the choke point can recognize 413 and retry with a downgrade; submit_post logs the body to keep it diagnosable.
        # create 是非幂等的「创建 + 计费」调用：submit_post 把歧义态（ReadTimeout 等
        # 「请求可能已送达服务端」）转 AmbiguousSubmitError 终态失败、不重试，仅「请求确定
        # 未送达」的连接建立失败交 should_retry_submit 重试，避免重复建任务 + 重复计费。
        # 413 等 4xx 经 raise_for_status 透出 httpx.HTTPStatusError（保留 status_code），
        # 让咽喉层识别 413 走降档重试；body 由 submit_post 落日志保留可诊断性。
        resp = await submit_post(lambda: client.post(endpoint, json=body), provider=PROVIDER_VIDU, request=request)
        data = resp.json()
        if not data.get("task_id"):
            raise RuntimeError(f"Vidu 视频任务创建响应缺少 task_id: {data}")
        return data

    async def _download_video(self, url: str, output_path: Path) -> None:
        """Download the final video from the CDN; retries use the shared artifact download budget.

        Retrieval is a plain URL GET (not an SDK call) and raises ``HTTPStatusError`` itself, so the shared predicate is used:
        an address just issued at the terminal state may not have propagated on the CDN yet, and 403/404 must be retried
        rather than declaring an already paid video dead.

        下载 CDN 上的成片，重试走共用的产物下载预算。

        取件是普通 URL GET（不是 SDK 调用），抛的就是 ``HTTPStatusError``，故用共用谓词：
        终态刚签发的地址可能尚未在 CDN 侧传播，403/404 要重试而不是判死一条已付费的成片。
        """
        await download_video(url, output_path, label="Vidu", retryable_errors=VIDU_RETRYABLE_ERRORS)


def _endpoint_for(request: VideoGenerationRequest) -> str:
    """Which endpoint the request is dispatched to: reference images → reference-to-video; first + end frame → first/end-frame-to-video; first frame → image-to-video; otherwise text-to-video.

    请求派发到哪个端点：有参考图 → 参考生视频；首尾帧 → 首尾帧生视频；首帧 → 图生视频；否则文生视频。"""
    if any(request.reference_images or []):
        return "/reference2video"
    if request.start_image is not None and request.end_image is not None:
        return "/start-end2video"
    if request.start_image is not None:
        return "/img2video"
    return "/text2video"


def _reference_images(request: VideoGenerationRequest) -> list[Path]:
    """Reference images sent for reference-to-video (the count limit is checked by ``_build_request``).

    参考生视频发出的参考图（张数上限由 ``_build_request`` 校验）。"""
    return [Path(p) for p in (request.reference_images or []) if p]


def _coerce_duration(model: str, endpoint: str, requested: int | None) -> int:
    """Correct the request duration to the nearest value in the legal set for (model, endpoint). / 按 (model, endpoint) 的合法集合，把请求时长校正到最近值。"""
    allowed = _DURATION_RULES.get((model, endpoint))
    if not allowed:
        # When the table has no entry, no strict check (let the API use its default), but pass the value through if one is given
        # 表里无项时不强校（让接口按默认处理），但若给了值就透传
        return int(requested) if requested else 5
    if requested is None:
        return allowed[0] if 5 not in allowed else 5
    if requested in allowed:
        return int(requested)
    nearest = min(allowed, key=lambda v: abs(v - requested))
    logger.warning(
        "Vidu duration %s 不在 model=%s endpoint=%s 合法集合 %s，使用最近值 %s",
        requested,
        model,
        endpoint,
        allowed,
        nearest,
    )
    return int(nearest)
