"""Contract for the video invocation channel: capability declarations, requests and results, provider task states, and the channel protocol.

This module depends only on the standard library, so any module can import it at no cost. It ships in this
package with the endpoint-definition validator: save-time validation and value resolution for declarative
definitions need the provider task states, the reference audio mode, and the audio capability checks. Runtime
support (submit and poll, retry decisions, download and persist, persisting the provider task id) lives on the
ArcReel application side in `lib.backends.backend_runtime`.

视频调用通道的契约：能力声明、请求与结果、供应商任务状态与通道协议。

本模块只依赖标准库，任何模块都可以无代价地导入。它随端点定义校验器进入本包：声明式定义的保存期
校验与取值要用供应商任务状态、参考音频模式与音频能力判定。提交与轮询、重试判定、下载落盘、供应商
任务 id 落库等运行支持在 ArcReel 应用侧的 ``lib.backends.backend_runtime``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

# Default for ``VideoGenerationRequest.poll_timeout_seconds``, and also the default for the app-side user-configurable polling timeout.
#: ``VideoGenerationRequest.poll_timeout_seconds`` 的缺省值，也是应用侧用户可配轮询超时的缺省值。
DEFAULT_VIDEO_POLL_TIMEOUT_SECONDS = 3600

ProviderResponseStage = Literal["submit", "poll", "result"]


class ResumeExpiredError(RuntimeError):
    """The provider-side job has expired or was not found. Restart self-healing cannot resume it, so it must go through mark_failed.

    The worker `finally` block checks ``isinstance(exc, ResumeExpiredError)`` and prefixes error_message
    with ``[resume_expired]`` (agent-facing, exempt from i18n) so ops analysis can see it.

    Provider 端 job 已过期或未找到——重启自愈无法接续，须走 mark_failed。

    Worker finally 据 ``isinstance(exc, ResumeExpiredError)`` 给 error_message
    加 ``[resume_expired]`` 前缀（agent-facing，i18n 豁免），运维分析可见。
    """

    def __init__(self, *, job_id: str, provider: str, message: str = "") -> None:
        self.job_id = job_id
        self.provider = provider
        super().__init__(message or f"resume job {job_id} expired or not found on provider {provider}")


class ResumeEndpointChangedError(RuntimeError):
    """The endpoint at submit time differs from the model row's current endpoint, so resuming must fail explicitly.

    The endpoint determines the protocol, so changing it amounts to changing the backend. Polling a job created under
    the old protocol with a backend for the new one would misread the response and mark a remote job that is still
    running (and still billed) as failed. This is where ADR 0054 ("resuming under a changed identity must raise an
    explicit error") lands on the endpoint dimension: it only blocks resumes that were already submitted and hold a
    job_id; queued tasks that were never submitted are submitted to the new endpoint as usual. Only custom providers
    have this dimension.

    提交本 job 时的 endpoint 与模型行当下的 endpoint 不同——续跑必须显式失败。

    endpoint 决定协议，换 endpoint 等于换 backend；拿新协议 backend 轮旧协议下创建的 job
    会误读响应，把仍在跑仍在计费的远端 job 标成失败。ADR 0054「换身份续跑必须显式报错」在
    endpoint 维度的落点：只拦已提交、持有 job_id 的续跑，排队未提交的任务照常按新 endpoint
    提交。仅自定义供应商有该维度。
    """

    def __init__(self, *, job_id: str, provider: str, submitted_endpoint: str, current_endpoint: str) -> None:
        self.job_id = job_id
        self.provider = provider
        self.submitted_endpoint = submitted_endpoint
        self.current_endpoint = current_endpoint
        super().__init__(
            f"resume job {job_id} was submitted via endpoint {submitted_endpoint} on provider {provider}, "
            f"but the model row now points to {current_endpoint}"
        )


# Image suffix to MIME type mapping (shared by multiple backends). / 图片后缀 → MIME 类型映射（多个后端共用）
IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class ProviderJobStatus(StrEnum):
    """Canonical buckets for provider async task states.

    ``EXPIRED`` is separate from ``FAILED``: the OpenAI / NewAPI paths use it to branch on the generate / resume
    context and raise ``RuntimeError`` / ``ResumeExpiredError``; the latter drives the worker's ``[resume_expired]``
    prefix and the "do not attempt restart self-healing again" decision. Folding it into failed would silently
    swallow that branch. Endpoints without expiry semantics (such as genre C ``/v2/video/generations``) fold it
    themselves on top of these buckets.

    供应商异步任务状态的 canonical 分档。

    ``EXPIRED`` 独立于 ``FAILED``：OpenAI / NewAPI 两条链路据其按 generate / resume 上下文
    分流抛 ``RuntimeError`` / ``ResumeExpiredError``，后者驱动 worker 的 ``[resume_expired]``
    前缀与「不再尝试重启自愈」判定。折进 failed 会静默吃掉这条分流。没有过期语义的端点
    （如流派 C ``/v2/video/generations``）在本分档之上自行折叠。
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


TERMINAL_PROVIDER_STATUSES: frozenset[ProviderJobStatus] = frozenset(
    {ProviderJobStatus.SUCCEEDED, ProviderJobStatus.FAILED, ProviderJobStatus.EXPIRED}
)

# Cross-vendor status synonym table (looked up after lowercase + strip). When an OpenAI-compatible proxy gateway forwards
# a non-native model, it passes the underlying vendor's status string through unchanged. If a backend only recognizes the
# literals in its own vendor's docs, a task that is already ready is treated as "still running" and polled all the way to
# max_wait: the user sees a timeout failure, while the provider has already produced and billed the result.
# 跨厂商状态同义词表（lowercase + strip 后查表）。OpenAI 兼容代理网关转发非原生型号时会把
# 底层厂商的状态串原样透传，各后端若只认自家文档里的字面量，已就绪的任务会被当成"仍在跑"
# 一路轮询到 max_wait —— 用户侧报超时失败，供应商侧成品已生成且已计费。
_PROVIDER_STATUS_SYNONYMS: dict[str, ProviderJobStatus] = {
    "completed": ProviderJobStatus.SUCCEEDED,
    "succeeded": ProviderJobStatus.SUCCEEDED,
    "succeed": ProviderJobStatus.SUCCEEDED,
    "success": ProviderJobStatus.SUCCEEDED,
    "failed": ProviderJobStatus.FAILED,
    "fail": ProviderJobStatus.FAILED,
    "error": ProviderJobStatus.FAILED,
    "canceled": ProviderJobStatus.FAILED,
    "cancelled": ProviderJobStatus.FAILED,
    "expired": ProviderJobStatus.EXPIRED,
    "generating": ProviderJobStatus.RUNNING,
    "in_progress": ProviderJobStatus.RUNNING,
    "running": ProviderJobStatus.RUNNING,
    "processing": ProviderJobStatus.RUNNING,
    "queued": ProviderJobStatus.QUEUED,
    "queueing": ProviderJobStatus.QUEUED,
    "preparing": ProviderJobStatus.QUEUED,
    "submitted": ProviderJobStatus.QUEUED,
    "pending": ProviderJobStatus.QUEUED,
    "created": ProviderJobStatus.QUEUED,
}


def normalize_provider_status(raw: object) -> ProviderJobStatus:
    """Maps any provider status value to a canonical bucket (case and leading/trailing whitespace are ignored).

    An unregistered status string is always treated as ``RUNNING`` so polling continues: treating an unknown string
    as terminal would make a gateway that returns a non-standard in-progress status (such as ``NOT_START``)
    trigger "download of a task that is not ready". The same applies to non-strings (missing field / None).

    任意供应商状态值 → canonical 分档（大小写与首尾空白无关）。

    未登记的状态串一律当 ``RUNNING`` 继续轮询：把未知串判成终态，会让返回非标进行中状态
    （如 ``NOT_START``）的网关触发"下载未就绪任务"。非字符串（缺字段 / None）同理。
    """
    if not isinstance(raw, str):
        return ProviderJobStatus.RUNNING
    return _PROVIDER_STATUS_SYNONYMS.get(raw.strip().lower(), ProviderJobStatus.RUNNING)


class VideoCapabilityError(RuntimeError):
    """Video backend capability mismatch (e.g. duration vs supported_durations).

    Symmetric with ImageCapabilityError: it carries no localized string, only a stable code plus context params.
    The routing layer renders it directly with _t(code, **params), while the worker stores code + params in
    task.error_message, and the text is rendered on the read side per Accept-Language.

    视频后端能力不匹配（如 duration ↔ supported_durations）。

    与 ImageCapabilityError 对称：不携带本地化字符串，只带稳定 code + 上下文 params；
    路由层直接 _t(code, **params) 渲染，Worker 则按 code + params 落 task.error_message，
    文案留到读侧按 Accept-Language 渲染。
    """

    def __init__(self, code: str, **params) -> None:
        self.code = code
        self.params = params
        super().__init__(code)


class ReferenceAudioMode(StrEnum):
    """Transport form in which a backend accepts reference audio.

    ``DIRECT`` means the audio file is sent directly with the generation request and the model clones the voice from
    it (Seedance 2.0's ``role: reference_audio`` content entry, Wan2.7 r2v's ``reference_voice`` attached to a
    reference material item). ``NONE`` means the backend has no voice input channel; a request with audio hard-fails
    at ``gate_video_request`` instead of being silently dropped.

    后端接受参考音频的运输形态。

    ``DIRECT`` 表示随生成请求直传音频文件，模型据其复刻音色（Seedance 2.0 的
    ``role: reference_audio`` content 条目、Wan2.7 r2v 挂在参考素材项上的
    ``reference_voice``）。``NONE`` 表示该后端没有音色输入通道——带音频的请求在
    ``gate_video_request`` 处硬失败，不静默丢弃。
    """

    NONE = "none"
    DIRECT = "direct"


class VideoAudioMode(StrEnum):
    """Three states for the final-cut audio track and its on/off switch.

    ``CONTROLLABLE`` means the request carries an audio-track switch, so the user's on/off intent reaches the
    provider. ``ALWAYS_ON`` means the final cut always has an audio track and the request has no switch to send
    (an intent to turn it off necessarily has no effect). ``ALWAYS_OFF`` means the path produces no audio track and
    has no switch (an intent to turn it on necessarily has no effect).

    This is separate from ``reference_audio_mode``: that one describes the **input** channel (whether the model can be
    given a voice reference), while this enum describes the **output** audio track. Values correspond one-to-one with
    the frontend ``VideoAudioControl`` literals; neither side merges them independently.

    成片音轨与音轨开关的三态。

    ``CONTROLLABLE`` 表示请求携带音轨开关，用户的开/关意图能抵达供应商；``ALWAYS_ON`` 表示
    成片必然带音轨而请求里没有开关可下发（关闭意图必然落空）；``ALWAYS_OFF`` 表示该路径不产
    音轨、也没有开关（开启意图必然落空）。

    与 ``reference_audio_mode`` 是两回事：后者描述**输入**通道（能否给模型一段音色参考），本
    枚举描述**输出**音轨。取值与前端 ``VideoAudioControl`` 字面量一一对应，两侧不各自归并。
    """

    CONTROLLABLE = "controllable"
    ALWAYS_ON = "always_on"
    ALWAYS_OFF = "always_off"


def audio_capability_pair_is_coherent(*, mode: object, count: int) -> bool:
    """Post-merge invariant for the two audio dimensions: declaring voice input support requires a positive segment limit.

    This is the only combination that is valid per dimension but meaningless together (``direct`` plus a limit of 0).
    A custom provider's sparse override can produce it by writing only one dimension: overriding
    ``reference_audio_mode=direct`` while leaving the system-derived 0, or pushing ``max_reference_audio_count`` to 0
    while the mode is still the system-derived ``direct``. A declarative definition can also write both dimensions as
    this combination directly. The reverse combination (``none`` plus a positive limit) is not a violation: when the
    mode is ``none`` the limit does not take part in the check, and "turning voice input off" is a legitimate intent,
    so treating it as a violation would push a capability the user explicitly turned off back on.

    If this combination were not corrected, `gate_video_request` would pass the mode check first and then hit the
    limit of 0, reporting "this model does not support reference audio" as "at most 0 reference audio segments are
    supported". A user following the hint would reduce the number of characters, and even zero segments would not
    pass.

    Three consumers share this check and must not each write their own: the custom provider write side
    (``server/routers/custom_providers.py``), the capability synthesis side
    (``lib/custom_provider/capabilities.py``), and the save-time validator for declarative definitions
    (``arcreel_market_core/endpoint_definition/validator.py``).

    音频两维的合并后不变式：声明支持音色输入就必须给出正的段数上限。

    两维各自合法、合起来无意义的组合只有这一种（``direct`` ⊕ 上限 0）：自定义供应商的稀疏覆盖
    只写其中一维就能凑出——覆盖 ``reference_audio_mode=direct`` 而不动系统判定的 0，或反过来把
    ``max_reference_audio_count`` 压成 0 而模式仍是系统判定的 ``direct``；声明式定义则可以两维
    直接写成这个组合。反向组合（``none`` ⊕ 正上限）不算违约：模式为 ``none`` 时上限本就不参与
    判定，且"关掉音色输入"是正当意图，判违约反会把用户明确关掉的能力顶回开启。

    不修正这组的后果是 ``gate_video_request`` 先过模式判定、再撞上限 0，把"该模型不支持参考
    音频"报成"最多支持 0 段参考音频"——用户按提示去减角色数量，减到零段也过不了。

    三处消费方共用此判定，不得各写一份：自定义供应商的写入侧
    （``server/routers/custom_providers.py``）、能力合成侧
    （``lib/custom_provider/capabilities.py``）与声明式定义的保存期校验器
    （``arcreel_market_core/endpoint_definition/validator.py``）。
    """
    return mode in {ReferenceAudioMode.NONE, ReferenceAudioMode.NONE.value} or count > 0


# Video execution path (task type bucket): ``i2v`` covers text-to-video and first-frame image-to-video, ``r2v`` is reference-to-video.
# Same vocabulary as ``lib.config.resolver.VideoGenerationType``, declared once per layer because of the layered contract (config is the
# lowest layer and backends must not import it in reverse); the guard in
# ``tests/unit/lib/backends/video_backends/test_video_backend_capabilities.py`` locks the values to be identical.
#: 视频执行路径（任务类型桶）：``i2v`` 覆盖文生与图生首帧，``r2v`` 是参考生视频。
#: 与 ``lib.config.resolver.VideoGenerationType`` 同一份词汇表，因分层契约（config 是最底层，
#: backend 不得反向导入）而各层各声明一次，取值一致由
#: ``tests/unit/lib/backends/video_backends/test_video_backend_capabilities.py`` 的守卫锁定。
VideoRoute = Literal["i2v", "r2v"]


@dataclass
class VideoCapabilities:
    """Declares what a video backend supports.

    ``text_to_video`` states whether a pure text-to-video request with no image material is available. The default is
    True to keep the existing backends' compatible semantics; a model that requires an image explicitly declares False.

    ``first_frame`` / ``last_frame`` describe the first-frame and last-frame slots of the image-to-video path.
    ``max_reference_images`` describes the reference-to-video path: the upper limit on how many items the backend
    accepts in the ``reference_images`` request field, and ``> 0`` means the path is available (there is no separate
    boolean, since two declarations could drift into a self-contradictory state such as "claims support but the limit
    is 0"). Whether the two paths can be combined (one request carrying both a first frame and reference images)
    varies by backend and is not a uniform contract: some backends reject the combination (e.g. Agnes raises
    ``VideoCapabilityError``), and some combine them silently (e.g. the v2 relay, Grok, and Sora, where the first frame
    and the references share a single slot). Callers should not assume any uniform behavior and must check the
    specific backend.

    ``audio_track`` / ``reference_route_audio_track`` describe the **final-cut audio track** (whether there is one and
    whether its switch is controllable) and are the single source of truth for this dimension. It shares its source
    with request construction: whether the backend puts an audio-track switch into the request body is the literal
    meaning of this bit. Each of the two execution paths is declared once, which is isomorphic to how ``first_frame`` /
    ``max_reference_images`` flatten the two paths into one object: ``reference_route_audio_track`` being None means
    the reference-to-video path has the same shape as ``audio_track`` (true for the vast majority of backends), and
    non-None means the request shape of that path has its own audio-track behavior (the multi-image subject sub-path
    of Kling v3-omni has no audio-track switch in its native schema, so that path is always silent). The default is
    ``CONTROLLABLE``: undeclared means "no signal, do not tighten", so a model with unknown capability is not
    misreported as having a broken switch. Read the value through :meth:`audio_track_for_route` rather than reading
    the field directly, otherwise every caller has to rewrite the "reference-to-video takes priority" merge rule.

    ``reference_audio_mode`` / ``max_reference_audio_count`` describe the reference audio path and are isomorphic to
    reference images: when the mode is not ``NONE`` the backend accepts the ``reference_audio_files`` request field,
    and the segment count is bounded by the limit. The limit is declared per backend according to each provider's own
    constraints and is not the intersection across providers.

    ``reference_audio_per_image``: whether each audio segment must be attached to a specific reference material item
    (such as the ``reference_voice`` field of wan2.7-r2v) rather than serving as an independent voice input channel
    (such as Seedance 2.0's ``role: reference_audio`` content entry). When True, the caller must provide
    ``VideoGenerationRequest.reference_audio_targets`` along with ``reference_audio_files``, explicitly declaring which
    reference material item each audio segment belongs to; the two lists must not be assumed to be in the same order.
    The reference audio order is the first-appearance order of dialogue speakers, while the reference image order is
    the first-appearance order of mentions; the two are derived independently, and positional alignment is purely
    coincidental.

    ``max_reference_audio_total_seconds``: upper limit on the combined duration of multiple reference audio segments
    (None means the backend declares no aggregate constraint and only the segment count is checked via
    ``max_reference_audio_count``). A segment count limit does not imply a total duration limit: two segments that are
    each within the valid per-segment range can still exceed the provider's total duration limit together, hence the
    separate declaration. The check needs audio metadata, so the caller probes the total duration before
    :func:`lib.backends.video_frame_slots.gate_video_request` and passes it in.

    ``max_prompt_chars``: upper limit on prompt character count (None means the backend declares no constraint). It
    declares a limit that holds **for that model regardless of which endpoint is used**. Some providers set a narrower
    value per endpoint (such as Vidu's reference-to-video endpoint); that narrowing stays in the backend's assembly
    step, which fails loudly against the actual endpoint, and is not put into this static declaration that has no
    endpoint context. It is measured in characters (Chinese and English count equally), matching each provider's
    documentation. The typical failure mode when the limit is exceeded is silent truncation rather than an error: the
    provider still bills, the final cut does not match the intent, and the user has no way to know. This is exactly the
    degradation that :func:`lib.backends.video_frame_slots.gate_video_request` must block before payment.

    ``first_frame_ratio_adaptive_only``: whether the model's first-frame (image-to-video) task accepts only the
    "adaptive" ratio. When True,
    :func:`lib.backends.video_frame_slots.resolve_first_frame_aspect_ratio` rewrites
    ``VideoGenerationRequest.aspect_ratio`` to the literal ``"adaptive"`` for generation requests that carry a first
    frame; requests without a first frame (pure text-to-video / reference images only) and the resume path that
    continues an already-submitted job are unaffected. That literal is a provider-side value and only makes sense for
    backends that recognize it, so only such backends declare this bit. Elsewhere (such as
    :func:`arcreel_market_core.aspect_size.parse_aspect_ratio`) it cannot be parsed and would fall back to the default
    ratio as an invalid value.

    The case "the user's ratio does not apply when a first frame is present" is also expressed in other ways by
    individual backends: dashscope and vidu simply do not send a ratio when assembling the payload (the upstream
    ignores or rejects it). The three have similar shapes but different value strategies (omit vs rewrite to adaptive)
    and have not been converged into one switch; this bit expresses only the "rewrite to adaptive" branch.

    The user's ratio intent still fully applies to storyboard image generation: the first-frame image is generated at
    that ratio in the first place, so "follow the first frame" is equivalent to the ratio the user chose. The rewrite
    only affects the value actually sent in the video request and does not change the original ``aspect_ratio`` the
    caller holds (accounting and version metadata keep using the latter).

    Declares what a video backend supports.

    ``text_to_video`` 表示不带任何图片素材的纯文生视频请求是否可用。默认 True 保持既有
    backend 的兼容语义；必须带图的 model 显式声明 False。

    ``first_frame`` / ``last_frame`` 描述图生视频路径的首帧与尾帧槽位。
    ``max_reference_images`` 描述参考生视频路径：后端接受 ``reference_images`` 请求字段
    的数量上限，``> 0`` 即该路径可用（不另设布尔位——两份声明会漂移出「称支持但上限为 0」
    这类自相矛盾的状态）。两条路径是否可叠加（同一请求同时带首帧与参考图）因后端而异，
    不是统一契约：部分后端拒绝叠加（如 Agnes 抛 ``VideoCapabilityError``），部分静默叠加
    （如 v2 中转、Grok、Sora 首帧与参考共享单槽）。调用方不应假设某种统一行为，需按具体
    后端核实。

    ``audio_track`` / ``reference_route_audio_track`` 描述**成片音轨**（有无音轨、开关是否可
    控），是该维度的唯一真相源——与请求构造同源，backend 是否往请求体里放音轨开关就是这一位
    的字面含义。两条执行路径各声明一次，与 ``first_frame`` / ``max_reference_images`` 把两条
    路径摊平进同一个对象同构：``reference_route_audio_track`` 为 None 表示参考生视频路径与
    ``audio_track`` 同形（绝大多数 backend 如此），非 None 时表示该路径的请求形态另有一套音轨
    行为（可灵 v3-omni 的多图主体子路径原生 schema 不含音轨开关，故该路径恒无声）。默认取
    ``CONTROLLABLE``——未声明即「无信号不收紧」，不把能力不明的 model 谎报成开关失效。
    取值请走 :meth:`audio_track_for_route`，不要直接读字段，否则每个调用方都要重写一遍
    「参考生视频优先」的合并规则。

    ``reference_audio_mode`` / ``max_reference_audio_count`` 描述参考音频路径，与参考图
    同构：模式非 ``NONE`` 时后端接受 ``reference_audio_files`` 请求字段，段数受上限约束。
    上限按 backend 各自的供应商约束声明，不取各家交集。

    ``reference_audio_per_image``：音频是否必须逐段挂在某个具体的参考素材项上（如 wan2.7-r2v
    的 ``reference_voice`` 字段），而非作为独立的音色输入通道（如 Seedance 2.0 的
    ``role: reference_audio`` content 条目）。为 True 时调用方须随 ``reference_audio_files``
    一并提供 ``VideoGenerationRequest.reference_audio_targets``，显式声明每段音频对应哪个
    参考素材项，不能假设两个列表天然同序——参考音频的编排顺序是台词 speaker 首现顺序，
    参考图的编排顺序是 mention 首现顺序，两者独立派生，位置对齐纯属巧合。

    ``max_reference_audio_total_seconds``：多段参考音频叠加的总时长上限（None = 该后端未声明
    聚合约束，仅按 ``max_reference_audio_count`` 卡段数）。段数上限推不出总时长——两段各处于
    单段合法区间的音频，合计仍可能超出供应商总时长上限，故需独立声明。判定需要读音频元数据，
    调用方在 :func:`lib.backends.video_frame_slots.gate_video_request` 前置探测好总时长再传入。

    ``max_prompt_chars``：提示词字符数上限（None = 该后端未声明约束）。声明的是**该 model 无论
    走哪条端点都成立**的上限——部分供应商按端点各设更窄的值（如 Vidu 的参考生视频端点），那层
    收窄留在 backend 组装期按实际端点 fail-loud，不塞进这个无端点上下文的静态声明里。计量口径
    为字符数（中英文同权），与各家文档一致。超限的典型失败模式是静默截断而非报错：供应商照常
    扣费、成片与意图不符、用户无从知情，正是 :func:`lib.backends.video_frame_slots.gate_video_request`
    要在付费前堵住的降级。

    ``first_frame_ratio_adaptive_only``：该模型的首帧（image-to-video）任务是否只接受
    "adaptive" 比例。声明为 True 时，:func:`lib.backends.video_frame_slots.resolve_first_frame_aspect_ratio`
    把带首帧的生成请求的 ``VideoGenerationRequest.aspect_ratio`` 改写为字面量 ``"adaptive"``；
    不带首帧的请求（纯文生 / 仅参考图）与续接已发起 job 的 resume 路径不受影响。该字面量是供应商
    侧的取值，只对认得它的 backend 有意义，故本位只由这类 backend 声明——别处（如
    :func:`arcreel_market_core.aspect_size.parse_aspect_ratio`）解析不了它，会按非法值回退默认比例。

    「首帧在场时用户比例不适用」这一情形另有 backend 各自的表达方式：dashscope 与 vidu 在
    payload 组装期直接不下发 ratio（上游忽略或拒收）。三者形状相近而取值策略不同（省略 vs 改写
    为 adaptive），未收敛到同一开关；本位表达的是"改写为 adaptive"这一支。

    用户的比例意图仍完整作用于分镜图生成——首帧图本就按该比例生成，"跟随首帧"与用户所选比例
    等价；改写只影响视频请求实际下发的值，不改调用方持有的原始 ``aspect_ratio``（记账、版本
    元数据沿用后者）。
    """

    text_to_video: bool = True
    first_frame: bool = True
    last_frame: bool = False
    max_reference_images: int = 0
    reference_audio_mode: ReferenceAudioMode = ReferenceAudioMode.NONE
    max_reference_audio_count: int = 0
    max_reference_audio_total_seconds: float | None = None
    reference_audio_per_image: bool = False
    max_prompt_chars: int | None = None
    first_frame_ratio_adaptive_only: bool = False
    audio_track: VideoAudioMode = VideoAudioMode.CONTROLLABLE
    reference_route_audio_track: VideoAudioMode | None = None

    def audio_track_for_route(self, route: VideoRoute) -> VideoAudioMode:
        """Actual shape of the final-cut audio track on this execution path.

        When the reference-to-video path is not declared separately it follows ``audio_track``. Two paths having the same
        shape is the norm, and repeating the declaration per backend would only add a copy that can drift.

        该执行路径上成片音轨的实际形态。

        参考生视频路径未单独声明时跟随 ``audio_track``——两条路径同形是常态，逐 backend 重复
        声明只会多出一份可漂移的副本。
        """
        if route == "r2v" and self.reference_route_audio_track is not None:
            return self.reference_route_audio_track
        return self.audio_track


@dataclass
class VideoGenerationRequest:
    """Generic video generation request. Each backend ignores fields it does not support.

    通用视频生成请求。各 Backend 忽略不支持的字段。
    """

    prompt: str
    output_path: Path
    aspect_ratio: str = "9:16"
    duration_seconds: int = 5
    resolution: str | None = None
    start_image: Path | None = None
    end_image: Path | None = None  # For first_last mode
    reference_images: list[Path] | None = None  # For multi-reference mode
    # Reference audio (voice cloning). List order is the contract for referring to "audio N" in the prompt: the orchestration layer builds the
    # reference text in this order and the backend sends in the same order, so neither side may reorder or skip. Which character
    # maps to which audio segment is not part of the request; the binding is expressed in the prompt text, since no provider API has a structured "character-audio" field.
    # 参考音频（音色复刻）。列表顺序即 prompt 中「音频N」的指认契约：编排层按该顺序拼指认
    # 文本，后端按同一顺序下发，故任何一侧都不得重排或跳过。哪个角色对应哪段音频不进请求
    # ——绑定由 prompt 文本表达，供应商 API 均无结构化的「角色-音频」字段。
    reference_audio_files: list[Path] | None = None
    # Read only by backends whose ``VideoCapabilities.reference_audio_per_image`` is True (such as wan2.7-r2v):
    # same length and order as ``reference_audio_files``; item i is the (0-based) index into
    # ``reference_images`` that this audio segment belongs to. When None, such backends fall back to positional alignment, used only for
    # callers that were not filled in by the orchestration layer (such as hand-written tests). Reference audio and reference images derive their order
    # independently, so positional alignment is not a contract, and the orchestration layer (the reference_video render pipeline) must provide it explicitly.
    # 仅 ``VideoCapabilities.reference_audio_per_image`` 为 True 的 backend（如 wan2.7-r2v）
    # 读取：与 ``reference_audio_files`` 等长同序，第 i 项是该段音频对应的
    # ``reference_images`` 下标（0-based）。为 None 时这类 backend 按位置回退对齐，仅用于
    # 未经编排层填充的调用方（如手写测试）——参考音频与参考图各自独立派生顺序，位置对齐
    # 不构成契约，编排层（reference_video 渲染管线）必须显式提供。
    reference_audio_targets: list[int] | None = None
    generate_audio: bool = True
    poll_timeout_seconds: int = DEFAULT_VIDEO_POLL_TIMEOUT_SECONDS

    # Project context (used to build file-serving URLs, etc.). / 项目上下文（用于构建文件服务 URL 等）
    project_name: str | None = None

    # Passed in from task["task_id"] on the worker path, so that after submit the backend persists the job_id via
    # `ProviderJobIdPersistenceMixin._persist_provider_job_id`.
    # On non-worker paths (grid / direct generation / tests) it stays None, and the common point skips persistence accordingly.
    # Worker 路径下从 task["task_id"] 传入，让 backend submit 后经
    # `ProviderJobIdPersistenceMixin._persist_provider_job_id` 持久化 job_id。
    # 非 worker 路径（grid / 直生 / 测试）保持 None，统一点据此跳过持久化。
    task_id: str | None = None

    # MediaGenerator uses this one-way signal to close its compression-retry window. Resumable backends signal
    # after the provider job handle is durable; an opaque submit-and-wait backend must signal before entering a
    # call whose failure cannot prove that the provider rejected the request before accepting a paid job.
    on_provider_resubmit_unsafe: Callable[[], None] | None = None

    # Carries a stage-tagged diagnostic trace when a provider JSON response is received. Stays None for non-ledger calls.
    # 收到供应商 JSON 响应时携阶段写入诊断留痕。非账本调用保持 None。
    on_provider_response: Callable[[ProviderResponseStage, object], Awaitable[None]] | None = None

    # Protocol identifier injected by the custom provider wrapper (`CustomVideoBackend`) before forwarding to the protocol backend; it is persisted
    # together with the job_id in `tasks.provider_endpoint`, recording which protocol this provider task belongs to. Built-in providers have no such dimension
    # and keep None. Resume compares protocols using the checkpoint's endpoint_guard, not this column.
    # 自定义供应商包装层（`CustomVideoBackend`）在转发给协议 backend 前注入的协议标识，与 job_id
    # 一并持久化到 `tasks.provider_endpoint`，记录本笔供应商任务的协议归属。内置供应商无此维度，
    # 保持 None。续跑比对协议读的是 checkpoint 的 endpoint_guard，不读该列。
    execution_endpoint: str | None = None

    # Resume path only: the request domain actually used when this job was submitted, replayed by resume_executor from `tasks.submitted_base_url`.
    # When polling, the backend prefers it over the domain resolved from the current config: the domain is a connection dimension, not a protocol dimension,
    # and if the user changes the config in flight, polling an old job on the new domain returns not found (404) and is misjudged as expired. Always None on the submit path.
    # 续跑路径专用：提交本 job 时实际使用的请求域名，由 resume_executor 从 `tasks.submitted_base_url` 回放。
    # backend 轮询时优先用它而非当下配置解析出的域名——域名是连接维度而非协议维度，
    # 用户在途改配置后按新域名轮旧 job 会查无（404）而被误判成过期。提交路径恒 None。
    submitted_base_url: str | None = None

    # The capabilities the execution gate used for its decision (tier-aware base with user capability overrides already applied), filled in by the caller that runs the execution gate (MediaGenerator, connection test).
    # Backends that decide which inputs may be sent from the model profile read this instead of their own profile, so dimensions the user relaxed are actually sent and the upstream makes the call;
    # when None (callers that construct the request directly) it falls back to the backend's own profile.
    # 执行闸门判定所依据的能力（档位感知基底已叠加用户能力覆盖），由跑执行闸门的调用方（MediaGenerator、测试连接）填入。
    # 按型号档案判定可发输入的 backend 读它而不是自身档案，让用户放宽的维度真实发出、由上游裁定；
    # None（直接构造请求的调用方）时回落 backend 自身档案。
    video_capabilities: VideoCapabilities | None = None

    # Seedance-specific. / Seedance 特有
    service_tier: str = "default"
    seed: int | None = None


@dataclass
class VideoGenerationResult:
    """Generic video generation result.

    通用视频生成结果。
    """

    video_path: Path
    provider: str
    model: str
    duration_seconds: int

    video_uri: str | None = None
    seed: int | None = None
    usage_tokens: int | None = None
    task_id: str | None = None
    generate_audio: bool | None = None

    # Facts that are only determined by this generation and must be archived with the final cut (such as the workflow fingerprint ComfyUI actually sent). The caller
    # merges them into the artifact version metadata: the request-side metadata is finalized before submit and cannot hold values known only after one execution.
    # Key names are chosen by each backend; the content must be JSON-serializable, since it is written into the version record as is.
    # 这一次生成才确定、且要随成片一起留档的事实（如 ComfyUI 的实发 workflow 指纹）。调用方把
    # 它并进产物版本元数据：请求侧的那份元数据在提交前就已定稿，装不下只有执行过一次才知道的值。
    # 键名由各 backend 自己定，内容必须可 JSON 序列化——它会原样落进版本记录。
    provenance: Mapping[str, Any] | None = None

    # Non-blocking notices produced at execution time, with the same shape as the task ``result.warnings`` (``{"key", "params"}``): this
    # generation succeeded, but there is something the user should know (e.g. ComfyUI produced several files in one run and only the first was taken). The caller
    # merges them into the task result and the read endpoint renders them in the current language; ``key`` must have an entry in every language table of ``lib/i18n``.
    # 执行期产生的非阻断提示，形状与任务 ``result.warnings`` 同为 ``{"key", "params"}``：这一次
    # 生成是成功的，但有一件事该让用户知道（如 ComfyUI 一次产出多个文件、只取了第一个）。调用方
    # 把它并进任务结果，由读接口按当前语言渲染；``key`` 须在 ``lib/i18n`` 的各语言表里都有一条。
    warnings: tuple[Mapping[str, Any], ...] = ()


class VideoBackend(Protocol):
    """Video generation backend protocol.

    视频生成后端协议。
    """

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def video_capabilities(self) -> VideoCapabilities: ...

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult: ...

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Resume a job already submitted on the provider: poll and download, without submitting again (ADR 0007).

        A backend that does not implement it raises ``NotImplementedError``, and the orphan handler takes the
        ``[resume_unsupported]`` path based on that. A provider-side job that has expired or was not found raises
        ``ResumeExpiredError``, taking the ``[resume_expired]`` path.

        接续 provider 上已发起的 job：轮询 + 下载，不重新 submit（ADR 0007）。

        未实现的 backend 抛 ``NotImplementedError``；orphan handler 据此走
        ``[resume_unsupported]``。provider 端 job 过期/未找到抛 ``ResumeExpiredError``
        走 ``[resume_expired]``。
        """
        raise NotImplementedError
