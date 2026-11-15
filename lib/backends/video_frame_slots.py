"""Request-time capability validation (``gate_video_request``) and frame slot assembly (``plan_frame_slots``) for video generation.

Pure functions extracted from ``MediaGenerator.generate_video_async``; they don't touch accounting, versioning or the
provider call, and can be imported independently to unit-test each capability combination branch directly.

The slot order is a contract with ``lib.references.reference_compression``: the compressor returns the list of
compressed paths by index unchanged, and the caller uses the three indices on ``FrameSlotPlan`` to map them back to the
``start_image`` / ``end_image`` / ``reference_images`` fields of ``VideoGenerationRequest``. Array reference images
always come after the first/last frames, so a single start index is enough to slice out all array items.

视频生成的请求期能力校验（``gate_video_request``）与帧槽位组装（``plan_frame_slots``）。

从 ``MediaGenerator.generate_video_async`` 抽出的纯函数，不触碰记账、版本管理与
provider 调用，可独立导入并直接单测各能力组合分支。

槽位序位是与 ``lib.references.reference_compression`` 的契约：压缩器按 index 原样返回压缩后的
路径列表，调用方据 ``FrameSlotPlan`` 上的三个索引还原回 ``VideoGenerationRequest``
的 ``start_image`` / ``end_image`` / ``reference_images`` 三个字段。数组参考图恒排在
首/尾帧之后，故用一个起始索引即可切出全部数组项。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from arcreel_market_core.video_backend_contract import ReferenceAudioMode, VideoCapabilities, VideoCapabilityError

if TYPE_CHECKING:
    from PIL import Image

    from lib.references.reference_compression import ReferenceSpec

__all__ = [
    "FIRST_FRAME_ADAPTIVE_RATIO",
    "FrameSlotPlan",
    "VideoCapabilityProbe",
    "gate_video_request",
    "plan_frame_slots",
    "resolve_first_frame_aspect_ratio",
    "resolve_video_capabilities",
]

# Provider-side literal meaning "follow the first frame image's own ratio" rather than a fixed ratio specified by the user. Only meaningful for
# backends declaring ``first_frame_ratio_adaptive_only``; the generic ratio parser doesn't recognize it.
# 供应商侧字面量，表示"跟随首帧图片自身比例"而非用户指定的固定比例。仅对声明
# ``first_frame_ratio_adaptive_only`` 的 backend 有意义，通用比例解析器认不得它。
FIRST_FRAME_ADAPTIVE_RATIO = "adaptive"


class VideoCapabilityProbe(Protocol):
    """The minimal backend interface capability queries actually need: read-only capability declaration, not touching generate / resume.

    Narrower than ``VideoBackend`` on purpose -- narrowed to the one member actually used, so neither callers nor test
    doubles need to implement the whole generation protocol for one capability query. The tier-aware
    ``video_capabilities_for_tier`` isn't included; it is an optional member probed by ``resolve_video_capabilities``.

    能力查询实际需要的最小后端接口：只读能力声明，不碰 generate / resume。

    比 ``VideoBackend`` 窄是有意的——收窄到用得着的那一个成员，调用方与测试替身都不必
    为一次能力查询实现整个生成协议。档位感知的 ``video_capabilities_for_tier`` 不在此列，
    它是可选成员，由 ``resolve_video_capabilities`` 探测。
    """

    @property
    def video_capabilities(self) -> VideoCapabilities: ...


def resolve_video_capabilities(
    backend: VideoCapabilityProbe,
    *,
    service_tier: str = "default",
    resolution: str | None = None,
) -> VideoCapabilities:
    """Resolve the backend's video capabilities under this request's tier.

    Prefer ``video_capabilities_for_tier`` (if the backend implements it): for some backends (e.g. Kling) ``last_frame``
    only takes effect under a specific ``service_tier``, and the ``video_capabilities`` property without request context can
    only declare conservatively, which would make requests on valid tiers be misjudged as unsupported.

    解析后端在本次请求档位下的视频能力。

    优先取 ``video_capabilities_for_tier``（若后端实现）：某些后端（如 Kling）的
    ``last_frame`` 仅在特定 ``service_tier`` 生效，无请求上下文的 ``video_capabilities``
    属性只能保守声明，会让合法档位的请求被误判为不支持。
    """
    tier_aware = getattr(backend, "video_capabilities_for_tier", None)
    if tier_aware is not None:
        return tier_aware(service_tier, resolution=resolution)
    return backend.video_capabilities


def resolve_first_frame_aspect_ratio(
    *,
    caps: VideoCapabilities | None,
    aspect_ratio: str,
    has_first_frame: bool,
) -> str:
    """Apply the ratio override for first-frame tasks per ``caps.first_frame_ratio_adaptive_only``.

    Backends declaring this constraint only accept :data:`FIRST_FRAME_ADAPTIVE_RATIO` on requests with a first frame
    (image-to-video) -- the ``aspect_ratio`` originally held by the caller (the user's ratio intent, still fully applied to
    storyboard image generation) is unaffected; this function only decides the value actually sent with this video request.
    When ``caps`` is None or doesn't declare the constraint, or the request actually sent has no first frame (pure text /
    reference images only), ``aspect_ratio`` is returned as is.

    ``has_first_frame`` is passed by the caller according to "whether the request actually sent carries a first frame",
    not whether the input is non-empty -- :func:`plan_frame_slots` only puts ``str`` / ``Path`` file sources into slots,
    so the decision must match the ``VideoGenerationRequest.start_image`` actually sent, or the constraint would drift from
    the real request.

    按 ``caps.first_frame_ratio_adaptive_only`` 施加首帧任务的 ratio 覆盖。

    声明该约束的后端在带首帧（image-to-video）的请求上只接受
    :data:`FIRST_FRAME_ADAPTIVE_RATIO`——调用方原始持有的 ``aspect_ratio``（用户比例意图，仍
    完整作用于分镜图生成）不受影响，本函数只决定实际下发给该次视频请求的值。``caps`` 为 None
    或未声明该约束、或实际下发的请求不带首帧（纯文生 / 仅参考图）时原样返回 ``aspect_ratio``。

    ``has_first_frame`` 由调用方按"实际下发的请求是否带首帧"传入，而不是按入参是否非空——
    :func:`plan_frame_slots` 只让 ``str`` / ``Path`` 文件源进槽位，判定须与真正下发的
    ``VideoGenerationRequest.start_image`` 一致，否则约束会与实际请求脱节。
    """
    if caps is not None and caps.first_frame_ratio_adaptive_only and has_first_frame:
        return FIRST_FRAME_ADAPTIVE_RATIO
    return aspect_ratio


@dataclass(frozen=True)
class FrameSlotPlan:
    """Compressor input specs and the position of each request field within them.

    ``start_index`` / ``end_index`` being None means that slot isn't sent this time; ``reference_start_index`` being None
    means there are no compressible array reference items, and the caller falls back to the original ``reference_images``
    (keeping the None / [] semantics).

    压缩器输入 specs 及各请求字段在其中的序位。

    ``start_index`` / ``end_index`` 为 None 表示该槽位本次不下发；
    ``reference_start_index`` 为 None 表示无可压缩的数组参考项，调用方回落原
    ``reference_images``（保留 None / [] 语义）。
    """

    specs: "list[ReferenceSpec]"
    start_index: int | None = None
    end_index: int | None = None
    reference_start_index: int | None = None


def gate_video_request(
    *,
    caps: VideoCapabilities | None,
    provider: str,
    model: str,
    prompt: str | None = None,
    has_image: bool = False,
    has_first_frame: bool = False,
    end_image: Path | None = None,
    reference_images: "list[Path] | None" = None,
    reference_audio_files: "list[Path] | None" = None,
    reference_audio_total_seconds: float | None = None,
) -> None:
    """Unified request-time capability pre-check: raises ``VideoCapabilityError`` on violation, returns silently on pass.

    Pure text-to-video capability and the four optional input paths (first frame / last frame / reference images /
    reference audio) are decided in one place here, rather than each backend's payload assembly writing its own -- the
    consequence of scattering is that the same violation hard-fails under some providers and silently truncates under
    others, and users get results billed as usual but not matching their intent without knowing it. So this function only
    rejects, never degrades: unsupported capabilities always raise, rendered by the upper layer into a user-readable error
    (copy in ``lib/i18n/*/errors.py``).

    ``has_image`` is filled in by the caller according to the image slots actually sent; when False, pure text-to-video
    capability is checked. ``has_first_frame`` likewise reflects whether the request actually sent carries a first frame
    (``plan_frame_slots``'s ``start_index``).

    ``caps`` being None means the caller didn't query backend capabilities -- when none of the four paths are used, the
    capability declaration affects nothing, and the caller can skip the query. Passing None with input on any path is
    always rejected as unsupported rather than allowed: a placeholder fake "supported" capability would let requests
    without verified capabilities go out, exactly the degradation this function exists to block.

    ``reference_audio_total_seconds`` is the total duration (seconds) of multiple reference audio clips probed in advance by
    the caller; this function does no I/O and doesn't probe itself -- probing requires reading audio metadata, which can only
    be done in the pre-assembly check where the files are accessible (see
    :func:`lib.generation.media_generator.MediaGenerator.generate_video_async`). None means the total duration is unknown
    (probe failed/environment unsupported); the total duration check is then skipped rather than rejected as over-limit --
    consistent with this repo's existing rule of degrading to allow when media probing is unavailable.

    ``prompt`` differs from the four optional paths: it exists on every request, so when ``caps`` doesn't declare
    ``max_prompt_chars`` (including ``caps`` being None) this item is skipped -- an undeclared constraint is not a limit of 0.

    Separating it from :func:`plan_frame_slots` is deliberate: validation raises, assembly is a pure function, and they are
    called at different times -- validation must precede the ledger bracket (a hard failure must not be billed or leave a
    failed ApiCall row), while assembly can happen after as needed.

    统一的请求期能力前置校验：违约抛 ``VideoCapabilityError``，通过则静默返回。

    纯文生能力与四条可选输入路径（首帧 / 尾帧 / 参考图 / 参考音频）在这里一处判定，而不是散在各 backend 的
    payload 组装里各写一套——散写的后果是同一种违约在不同供应商下有的硬失败、有的静默截断，
    用户拿到照常扣费但与意图不符的结果却无从得知。故本函数只做拒绝，不做降级：
    能力不支持一律抛错，由上层渲染成用户可读错误（文案见 ``lib/i18n/*/errors.py``）。

    ``has_image`` 由调用方按实际下发的图片槽位填写；为 False 时检查纯文生能力。
    ``has_first_frame`` 同样按实际下发的请求是否带首帧填写（``plan_frame_slots`` 的 ``start_index``）。

    ``caps`` 为 None 表示调用方未查询后端能力——四条路径都不走时能力声明不影响任何结果，
    调用方可省去这次查询。传 None 却带任一路径的输入一律按不支持拒绝，而不是放行：占位一份
    "支持"的假能力会让未经能力核实的请求下发出去，正是本函数要堵的降级。

    ``reference_audio_total_seconds`` 是调用方前置探测好的多段参考音频总时长（秒），本函数
    不做 I/O、不自行探测——探测需要读音频元数据，只能在能拿到文件的组装前置校验处完成
    （见 :func:`lib.generation.media_generator.MediaGenerator.generate_video_async`）。传 None 表示总时长
    未知（探测失败/环境不支持），此时跳过总时长校验而不是当作超限拒绝——与本仓库媒体探测
    不可用时降级放行的既有口径一致。

    ``prompt`` 与四条可选路径不同：它在每个请求上都存在，故 ``caps`` 未声明
    ``max_prompt_chars`` 时（含 ``caps`` 为 None）跳过该项——未声明约束不等于上限为 0。

    与 :func:`plan_frame_slots` 分离是有意的：校验会抛、组装是纯函数，两者调用时机不同——
    校验须先于记账括号（硬失败要不扣费、不留 failed ApiCall 行），组装则可在其后按需进行。
    """
    prompt_limit = None if caps is None else caps.max_prompt_chars
    if prompt_limit is not None and prompt is not None and len(prompt) > prompt_limit:
        # Providers generally truncate overlong prompts silently rather than erroring (e.g. the wan2.7 docs say "the excess is truncated automatically",
        # with no matching entry in the error code table), billing as usual but producing a video that doesn't match intent -- reject before paying, don't let it through.
        # 供应商对超长 prompt 普遍是静默截断而非报错（如 wan2.7 文档原文「超过部分会自动截断」，
        # 错误码表无对应条目），照常扣费却产出与意图不符的成片——付费前拒绝，不放行。
        raise VideoCapabilityError(
            "video_prompt_too_long",
            provider=provider,
            model=model,
            limit=prompt_limit,
            count=len(prompt),
        )

    if caps is not None and not has_image and not caps.text_to_video:
        raise VideoCapabilityError("video_capability_missing_t2v", provider=provider, model=model)

    if has_first_frame and (caps is None or not caps.first_frame):
        raise VideoCapabilityError("video_first_frame_unsupported", provider=provider, model=model)

    if end_image is not None and (caps is None or not caps.last_frame):
        raise VideoCapabilityError("video_last_frame_unsupported", provider=provider, model=model)

    if reference_images:
        limit = 0 if caps is None else caps.max_reference_images
        if limit <= 0:
            raise VideoCapabilityError("video_reference_images_unsupported", provider=provider, model=model)
        if len(reference_images) > limit:
            raise VideoCapabilityError(
                "video_reference_images_exceeded",
                provider=provider,
                model=model,
                limit=limit,
                count=len(reference_images),
            )

    if reference_audio_files:
        # Models that don't support voice input don't silently drop audio they receive: silently dropping it would generate a video with a random voice and bill
        # as usual; the user would believe the character's voice is controlled until stitching the final cut reveals voice inconsistency across clips.
        # 不支持音色输入的模型收到音频不静默丢弃：静默丢弃会生成一段音色随机的视频并照常
        # 扣费，用户以为角色声音已受控，直到成片拼接才发现跨片段音色不一致。
        mode = ReferenceAudioMode.NONE if caps is None else caps.reference_audio_mode
        if mode == ReferenceAudioMode.NONE:
            raise VideoCapabilityError("video_reference_audio_unsupported", provider=provider, model=model)
        audio_limit = 0 if caps is None else caps.max_reference_audio_count
        if len(reference_audio_files) > audio_limit:
            raise VideoCapabilityError(
                "video_reference_audio_exceeded",
                provider=provider,
                model=model,
                limit=audio_limit,
                count=len(reference_audio_files),
            )

        total_limit = None if caps is None else caps.max_reference_audio_total_seconds
        if (
            total_limit is not None
            and reference_audio_total_seconds is not None
            and reference_audio_total_seconds > total_limit
        ):
            raise VideoCapabilityError(
                "video_reference_audio_duration_exceeded",
                provider=provider,
                model=model,
                limit=total_limit,
                total=reference_audio_total_seconds,
            )


def plan_frame_slots(
    *,
    start_image: "str | Path | Image.Image | None" = None,
    end_image: Path | None = None,
    reference_images: "list[Path] | None" = None,
) -> FrameSlotPlan:
    """Assemble frame/reference image slots from the inputs -- a pure function that doesn't judge capabilities and doesn't raise.

    Capability decisions belong to :func:`gate_video_request`; this function only lays the three request fields into the
    compressor's specs sequence and records their positions.

    ``start_image`` only enters the compressor when it is a ``str`` / ``Path`` file source; ``PIL.Image`` and None don't
    enter specs (the corresponding request field stays None).

    按输入组装帧/参考图槽位——纯函数，不判定能力、不抛异常。

    能力判定归 :func:`gate_video_request`；本函数只负责把三个请求字段铺进压缩器的 specs
    序列并记下各自序位。

    ``start_image`` 仅 ``str`` / ``Path`` 文件源进压缩器；``PIL.Image`` 与 None 不入
    specs（对应请求字段保持 None）。
    """
    from lib.references.reference_compression import ReferenceSpec, RefRole

    specs: list[ReferenceSpec] = []
    start_index: int | None = None
    end_index: int | None = None
    reference_start_index: int | None = None

    if isinstance(start_image, (str, Path)):
        start_index = len(specs)
        specs.append(ReferenceSpec(source=Path(start_image), role=RefRole.FRAME))
    if end_image is not None:
        end_index = len(specs)
        specs.append(ReferenceSpec(source=Path(end_image), role=RefRole.FRAME))
    if reference_images:
        reference_start_index = len(specs)
        specs.extend(ReferenceSpec(source=Path(r), role=RefRole.ARRAY) for r in reference_images)

    return FrameSlotPlan(
        specs=specs,
        start_index=start_index,
        end_index=end_index,
        reference_start_index=reference_start_index,
    )
