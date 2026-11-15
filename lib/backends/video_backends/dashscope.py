"""DashScopeVideoBackend — Alibaba Bailian HappyHorse / Wan video generation backend (async, two-step).

Uses the native video-generation/video-synthesis async endpoint: submit to get task_id → poll
GET /tasks/{id} until SUCCEEDED → download video_url. Covers t2v / i2v / r2v of happyhorse-1.0 / happyhorse-1.1
and the wan2.7 series, plus wan3.0, where a single model handles all three paths.

How well the schema is confirmed comes in two tiers by model: happyhorse and wan2.7 are verified against the
first-hand official docs listed in docs/api-docs/providers/dashscope.md; wan3.0 has no usable official page, its
request shape is inferred from the 2.7 shape; see the notes at the _WAN3_* constants for sources and the extent of
the inference.

Note: t2v/i2v start frames use media[{type:"first_frame"}] (the first_frame type is confirmed in the r2v media
enum); end-frame / continuation fields are not confirmed in the first-hand docs, so the i2v of happyhorse and
wan2.7 only declares first-frame capability, nothing invented. wan3.0's end frame is part of the inferred tier
above and is not bound by this rule.

DashScopeVideoBackend — 阿里百炼 HappyHorse / 万相视频生成后端（异步两步式）。

走原生 video-generation/video-synthesis 异步端点：submit 取 task_id → 轮询
GET /tasks/{id} 至 SUCCEEDED → 下载 video_url。覆盖 happyhorse-1.0 / happyhorse-1.1
与 wan2.7 系列的 t2v / i2v / r2v，以及单模型通吃三条路径的 wan3.0。

schema 的确权程度按型号分两档：happyhorse 与 wan2.7 依据 docs/api-docs/providers/dashscope.md 所列一手
官方文档核实；wan3.0 无可用官方页面，其请求形态按 2.7 形状类推，出处与类推范围见 _WAN3_*
常量处的说明。

注：t2v/i2v 起始帧用 media[{type:"first_frame"}]（first_frame type 在 r2v media
枚举中确权）；尾帧 / 续写字段在一手 docs 未确权，故 happyhorse 与 wan2.7 的 i2v 仅
声明首帧能力，不臆造。wan3.0 的尾帧是上述类推档的一部分，不受这条约束。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from arcreel_market_core.video_backend_contract import (
    ReferenceAudioMode,
    ResumeExpiredError,
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
    poll_with_retry,
    recording_poll,
    resume_expiry_gate,
    should_retry_poll,
    should_retry_submit,
    submit_post,
)
from lib.backends.dashscope_shared import (
    dashscope_failure_reason,
    dashscope_headers,
    dashscope_native_base_url,
    extract_billing_duration,
    extract_task_id,
    extract_video_url,
    image_to_data_uri,
    is_dashscope_expired,
    is_dashscope_terminal,
    resolve_dashscope_api_key,
    safe_body_for_log,
)
from lib.backends.data_uri import file_to_data_uri
from lib.backends.http_status_errors import raise_for_status_redacted
from lib.backends.providers import PROVIDER_DASHSCOPE
from lib.backends.transport_layer import TransportLayer
from lib.backends.video_backends.metering import video_usage
from lib.backends.video_backends.name_family import capabilities_by_names
from lib.billing.metering import Usage
from lib.infra.logging_utils import format_kwargs_for_log
from lib.infra.retry import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    with_retry_async,
)

logger = logging.getLogger(__name__)


def _read_image_or_none(path: Path) -> str | None:
    """Read as a data URI; returns None when missing (directory / non-regular file, including "." resolved from an empty string) or on IO failure (permissions / concurrent deletion).

    读成 data URI；缺失（目录/非常规文件，含空串解析出的 "."）或 IO 失败（权限/并发删除）返回 None。"""
    if not path.is_file():
        return None
    try:
        return image_to_data_uri(path)
    except OSError as exc:
        logger.warning("DashScope 图片读取失败: %s (%s)", path, exc)
        return None


# wan2.7's reference_voice accepts wav / mp3 (official "Wan 2.7 reference-to-video" doc, reference_voice section);
# URL forms are the same as media.url: http / oss / base64 data URI.
# wan2.7 的 reference_voice 接受 wav / mp3（官方《万相2.7-参考生视频》reference_voice 章节），
# URL 形态与 media.url 同为 http / oss / base64 data URI。
_REFERENCE_AUDIO_MIME_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg"}


def _read_reference_audio_or_none(path: Path) -> str | None:
    """Reference audio → base64 data URI; returns None when the file is missing or on IO failure (format is rejected beforehand by the caller).

    参考音频 → base64 data URI；文件缺失或 IO 失败返回 None（格式另由调用方先行拒绝）。"""
    mime = _REFERENCE_AUDIO_MIME_TYPES[path.suffix.lower()]
    if not path.is_file():
        return None
    try:
        return file_to_data_uri(path, mime)
    except OSError as exc:
        logger.warning("DashScope 参考音频读取失败: %s (%s)", path, exc)
        return None


DEFAULT_MODEL = "happyhorse-1.1-i2v"

_VIDEO_ENDPOINT = "/services/aigc/video-generation/video-synthesis"


# wan2.7-r2v attaches reference_voice per segment to reference items, so the audio segment limit equals the total
# reference item limit (official: reference images + reference videos ≤ 5).
# wan2.7-r2v 的 reference_voice 逐段挂在参考素材项上，故音频段数上限等同参考素材总数上限
# （官方：参考图像 + 参考视频 ≤ 5）。
_WAN27_R2V_MAX_REFERENCE = 5

# The whole wan2.7 family has a 5000-character prompt limit (official parameter table: "the excess is truncated
# automatically", no matching entry in the error code table — exceeding it raises no error, it is silently truncated
# and billed as usual, so gate_video_request rejects it before paying).
# wan2.7 全家族 prompt 上限 5000 字符（官方参数表原文「超过部分会自动截断」，错误码表无对应
# 条目——超限不报错、直接静默截断并照常计费，故由 gate_video_request 在付费前拒绝）。
_WAN27_MAX_PROMPT_CHARS = 5000

# wan3.0: one model covers text-to-video / image-to-video / reference-to-video: first frame + end frame, 10 reference
# images, 5 reference audio segments, 15 seconds total, 20000-character prompt limit. Prompt overflow is silently
# truncated and billed as usual, same as 2.7, and likewise rejected up front by gate_video_request.
# Source: capability limits listed in the Wan 3.0 release notes. Unlike the other models, wan3.0 has no citable
# first-hand API schema — the media entry types below (last_frame / reference_audio) and the literal of
# parameters["audio"] are all inferred from the 2.7 shape; if the remote reports parameter errors, check here first.
# wan3.0 单模型覆盖文生/图生/参考生三条路径：首帧 + 尾帧，参考图 10 张，参考音频 5 段、
# 总时长 15 秒，prompt 上限 20000 字符。prompt 超限与 2.7 同为静默截断且照常计费，同样由
# gate_video_request 前置拒绝。
# 出处：万相 3.0 发布说明所列能力上限。与其余型号不同，wan3.0 没有可引的一手 API schema——
# 下方 media 条目类型（last_frame / reference_audio）与 parameters["audio"] 的字面量均按 2.7
# 形状类推，对端如报参数错误应以此处为首查点。
_WAN3_MAX_REFERENCE_IMAGES = 10
_WAN3_MAX_REFERENCE_AUDIO = 5
_WAN3_MAX_REFERENCE_AUDIO_TOTAL_SECONDS = 15.0
_WAN3_MAX_PROMPT_CHARS = 20000
_WAN3_MODEL_KEY = "wan3.0-video"

# Wan 3.0 family model_id detection (hyphen/underscore optional, version not anchored): this is the request-shape
# dispatch of this backend, and also the single regex source shared by lib.custom_provider.duration_presets
# (duration option inference) and endpoints.py (endpoint routing inference) — all three must judge wan3 with the same
# match width, otherwise a model_id could be routed to this backend while this backend fails to recognize it as wan3
# and falls back to the generic profile (losing reference image / end frame / audio track parameters). The version
# prefix and the modality token (WAN_IMAGE_TO_VIDEO_PATTERN below) must accept the same separator set, to avoid
# asymmetric tolerance within one rule group. Identifier boundaries on both sides require non-alphanumerics, to avoid
# matching third-party model names like "swan3" or "vendorwan3" that contain the wan3 substring but are not this family.
# 万相 3.0 家族 model_id 识别（连字符/下划线可选、不锚版本号）：此处是本后端的请求形态分派，
# 也是 lib.custom_provider.duration_presets（时长档位推断）与 endpoints.py（端点路由推断）共用
# 的唯一正则来源——三处须按同一匹配宽度判 wan3，否则会出现某个 model_id 被路由到本后端、却因
# 本后端认不出它是 wan3 而退回通用档案（丢参考图/尾帧/音轨参数）的矛盾。版本前缀与模态 token
# （下方 WAN_IMAGE_TO_VIDEO_PATTERN）须接受相同的分隔符集合，避免同一规则组内宽容度不对称。
# 两侧标识符边界要求非字母数字，避免匹配到 "swan3"、"vendorwan3" 这类含 wan3 子串但并非该家族
# 的第三方型号名。
WAN3_PATTERN = re.compile(r"(?<![a-z0-9])wan[-_]?3(?![a-z0-9])", re.I)

# Wan 2.7 family model_id detection (hyphen/underscore optional; identifier boundaries avoid swallowing "swan2",
# "wan20"): makes the hyphen form ("wan-2.7") and the dot form ("wan2.7") reach the same conclusion on image/video
# ownership and endpoint routing — this is the only scope this regex is confirmed for.
#
# Only 2.7 is anchored, other 2.x minor versions are not covered: this backend always requests
# `/services/aigc/video-generation/video-synthesis` (_VIDEO_ENDPOINT), while wan2.1 / wan2.2-s2v
# use the old endpoint
# `/services/aigc/image2video/video-synthesis/` with different payload fields (e.g. wan2.6 uses `size` instead of
# 2.7's `resolution`+`ratio`); folding them into this regex would send protocol-incompatible requests to this endpoint.
# The dot form (e.g. "wan2.1-kf2v") is not governed by this regex but by WAN_DOT_FORM_PATTERN; whether its routing
# should also narrow to 2.7 needs provider API facts and a product decision, not decided by this regex's match width.
# 万相 2.7 家族 model_id 识别（连字符/下划线可选、标识符边界避免误吞 "swan2"、"wan20"）：
# 使连字符形态（"wan-2.7"）与点号形态（"wan2.7"）在图像/视频归属与端点路由上得出一致结论——
# 这是本正则唯一确权的范围。
#
# 只锚 2.7、不覆盖其余 2.x 小版本：本后端固定请求
# `/services/aigc/video-generation/video-synthesis`（_VIDEO_ENDPOINT），而 wan2.1 / wan2.2-s2v
# 走的是旧端点
# `/services/aigc/image2video/video-synthesis/`、payload 字段也不同（如 wan2.6 用 `size` 而非
# 2.7 的 `resolution`+`ratio`），并入本正则会把协议不兼容的请求送到这个端点。
# 点号形态（如 "wan2.1-kf2v"）不受本正则约束，归 WAN_DOT_FORM_PATTERN 判定，其路由是否也应
# 收窄到 2.7 需要供应商 API 事实与产品判断，不由本正则的匹配宽度代为决定。
WAN2_PATTERN = re.compile(r"(?<![a-z0-9])wan[-_]?2\.7(?![a-z0-9])", re.I)

# Family detection for dot-form Wan 2.x (other than 2.7, e.g. "wan2.1-kf2v"): identifier boundaries require
# non-alphanumerics, so third-party names like "swan2.7-r2v", "vendorwan2.7-t2v" containing "wan2." are not misjudged.
# 点号形态万相 2.x（2.7 以外，如 "wan2.1-kf2v"）的家族判定：标识符边界要求非字母数字，避免
# "swan2.7-r2v"、"vendorwan2.7-t2v" 这类含 "wan2." 子串但并非该家族的第三方型号名被误判。
WAN_DOT_FORM_PATTERN = re.compile(r"(?<![a-z0-9])wan2\.\d+(?![a-z0-9])", re.I)

# happyhorse family detection: identifier boundaries require non-alphanumerics, so third-party model names like
# "myhappyhorse-1.0-r2v" / "happyhorsefoo" are not swallowed by a literal substring match.
# happyhorse 家族判定：标识符边界要求非字母数字，避免 "myhappyhorse-1.0-r2v" / "happyhorsefoo"
# 这类第三方型号名被字面子串误吞。
HAPPYHORSE_PATTERN = re.compile(r"(?<![a-z0-9])happyhorse(?![a-z0-9])", re.I)

# wan family image-to-video continuation syntax ("wan-2.7-image-to-video" / "wan_2.7-image2video" /
# "wan-3-turbo-image-to-video"): only the one definite spelling "image-(to|2)-video" is recognized, not img2vid/i2v
# or other abbreviations. wan2.7 normalization uses it to fold this suffix into "i2v" before looking up
# _MODEL_PROFILES; the wan3/wan2x_dot families use it to tell image from video ownership (real image aliases like
# wan3.0-video-image do not contain this syntax and are unaffected). Identifier boundaries on both sides prevent
# adjacent letters like "wan2.7-fooimage-to-video" / "wan2.7-image-to-videofoo" from matching by mistake.
# wan 家族 image-to-video 续接语法（"wan-2.7-image-to-video" / "wan_2.7-image2video" /
# "wan-3-turbo-image-to-video"）：只识别 "image-(to|2)-video" 这一种确定拼写，不识别 img2vid/i2v
# 等其他缩写。wan2.7 归一化用它把该后缀折成 "i2v" 再查 _MODEL_PROFILES；wan3/wan2x_dot 家族用它
# 区分图像/视频归属（wan3.0-video-image 等真图像别名不含该语法，不受影响）。两侧标识符边界避免
# "wan2.7-fooimage-to-video" / "wan2.7-image-to-videofoo" 这类相邻字母被误判命中。
WAN_IMAGE_TO_VIDEO_PATTERN = re.compile(r"(?<![a-z0-9])image[-_]?(?:to|2)[-_]?video(?![a-z0-9])", re.I)

# wan2.7-videoedit (instruction-based video editing) is
# a real, separate modality within the Wan family, but this backend only implements request construction for
# t2v/i2v/r2v and has no input-video transport field that modality needs. IDs that hit the family regex but fall into
# this modality must be excluded from native routing and the known capability profiles, otherwise they would carry the
# generic fallback (losing the capability declaration the modality actually needs) and send requests this backend
# cannot build correctly. Identifier boundaries on both sides keep unrelated word forms like "videoeditor" (a letter
# right after "edit") from matching.
# wan2.7-videoedit（指令式视频编辑）是
# 万相家族内真实存在的独立模态，但本后端只实现了 t2v/i2v/r2v 三档的请求构造，没有该模态所需的
# 输入视频传输字段。命中家族正则但落这个模态的 id 须排除出原生路由与已知能力档，否则会带着
# 统一回落（丢失该模态实际所需的能力声明）发出本后端无法正确构造的请求。两侧标识符边界
# 避免 "videoeditor" 一类无关词形（"edit" 后紧邻字母）被误判命中。
WAN_VIDEOEDIT_PATTERN = re.compile(r"(?<![a-z0-9])video[-_]?edit(?![a-z0-9])", re.I)

# wan2.7-s2v (image-to-video continued with speech) / wan2.7-v2v (video-to-video): modalities that hit the family
# regex but, like videoedit, have no request construction implemented. Semantic difference from has_known_modality:
# this pattern only marks "this is a known video modality" (for routing to decide whether to fall to the image
# endpoint), it does not mean request construction is implemented (it still must take has_known_modality's exclusion path).
# wan2.7-s2v（图生视频续接语音）/ wan2.7-v2v（视频生视频）：命中家族正则但同 videoedit 一样未
# 实现请求构造的模态。与 has_known_modality 的语义区分：本模式只标记"这是已知的视频模态"（供路由
# 判定是否落图像端点用），不代表已实现该模态的请求构造（仍须落 has_known_modality 的排除路径）。
WAN_S2V_V2V_PATTERN = re.compile(r"(?<![a-z0-9])[sv]2v(?![a-z0-9])", re.I)

WanFamily = Literal["happyhorse", "wan2.7", "wan3", "wan2x_dot"]


@dataclass(frozen=True)
class WanClassification:
    """Structured verdict for a model_id along the Wan/happyhorse family classification chain.

    The single classification entry point — family ownership, separator normalization, identifier boundaries,
    image-to-video continuation syntax and exclusion of unimplemented modalities are all implemented once, in
    `classify_wan_model`. The three decision points, endpoint routing (endpoints.py::infer_endpoint), capability profile
    (`_profile_for_model` in this module) and duration options (duration_presets.py), only consume this verdict and must
    not each regex-match model_id again, to keep one family's boundary rules from drifting into mutually exclusive
    combinations across places (e.g. hitting native routing but getting a capability profile incompatible with it).

    model_id 在万相/happyhorse 家族判定链上的结构化结论。

    唯一判定入口——家族归属、分隔符归一化、标识符边界、image-to-video 续接语法、未实现模态排除
    均只在 `classify_wan_model` 里实现一次。端点路由（endpoints.py::infer_endpoint）、
    能力档（本模块 `_profile_for_model`）、时长档（duration_presets.py）三处判定点都只消费这份
    结论，不得再各自对 model_id 做正则匹配，避免同一家族的边界规则在多处漂移出互斥组合（例如
    命中原生路由却拿到与该路由不兼容的能力档）。
    """

    family: WanFamily | None
    # wan series image-to-video continuation alias (really video, not an image variant)
    is_image_to_video: bool  # wan 系列 image-to-video 续接别名（本质视频，非图像变体）
    # wan2.7-videoedit: a modality whose request construction this backend does not implement
    is_videoedit: bool  # wan2.7-videoedit：本后端未实现请求构造的模态
    # normalized key usable directly on _MODEL_PROFILES; None when the exact modality cannot be determined
    profile_key: str | None  # 可直接查 _MODEL_PROFILES 的归一化 key；无法确定具体模态时为 None
    # Whether the wan2.7 family resolved to a concrete modality whose request construction this backend implements
    # (t2v/i2v/r2v). wan2.7-s2v / wan2.7-v2v / wan2.7-videoedit etc. hit the family regex but are unimplemented and must be
    # treated apart from implemented modalities — mixing them would silently send modalities with missing request-body
    # fields to an endpoint this backend cannot handle correctly. Always True for wan3/happyhorse/wan2x_dot: wan3 is one
    # model covering all modalities, and modality narrowing for happyhorse and wan2x_dot is not decided by this field (the
    # former has no modality seen that needs excluding, for the latter see the note at profile_key).
    # wan2.7 家族是否解析出本后端已实现请求构造的具体模态（t2v/i2v/r2v）。wan2.7-s2v /
    # wan2.7-v2v / wan2.7-videoedit 等命中家族正则但模态未实现，须与已实现模态区分对待——两者混同
    # 会让请求体缺字段的模态被静默送去本后端无法正确处理的端点。wan3/happyhorse/wan2x_dot 恒为
    # True：wan3 单模型覆盖全部模态、happyhorse 与 wan2x_dot 的模态收窄不由本字段判定（前者未见
    # 需要排除的模态，后者见 profile_key 处的说明）。
    has_known_modality: bool
    # Whether the wan2.7 family can be confirmed as a video modality (any of t2v/i2v/r2v/s2v/v2v/videoedit), even if this
    # backend does not implement request construction for some of them (s2v/v2v/videoedit). Different semantics from
    # has_known_modality: that one is true only for t2v/i2v/r2v, which have request construction; this field additionally
    # counts known but unimplemented video modalities, to tell them apart from real image variants (wan2.7-image etc.,
    # which fall into no known modality token) — if the id elsewhere carries an unrelated "image" decoration (e.g. a proxy
    # namespace prefix), a blanket image judgment must not override confirmed video semantics. The distinction only applies
    # to wan2.7; otherwise always False: wan3 is one model covering all modalities, and image/video ownership for
    # happyhorse/wan2x_dot is not decided by this field (see the notes at profile_key and has_known_modality).
    # wan2.7 家族是否可确认属于视频模态（t2v/i2v/r2v/s2v/v2v/videoedit 任一），即便本后端未实现
    # 其中部分模态（s2v/v2v/videoedit）的请求构造。与 has_known_modality 语义不同：后者只对已实现
    # 请求构造的 t2v/i2v/r2v 为真，本字段额外把已知但未实现的视频模态也计入，用于和真图像变体
    # （wan2.7-image 等未落入任何已知模态 token 的情形）区分——id 别处若另含无关 "image" 装饰
    # （如代理命名空间前缀），不能让笼统 image 判定盖过已确认的视频语义。该区分只对 wan2.7 生效，
    # 恒为 False：wan3 单模型覆盖全部模态、happyhorse/wan2x_dot 的图像/视频归属不由本字段判定
    # （见 profile_key 与 has_known_modality 处的说明）。
    is_known_video_modality: bool


def classify_wan_model(model_id: str | None) -> WanClassification:
    """Classify model_id once so routing / capability profile / duration options reuse the same verdict.

    对 model_id 做一次判定，供路由/能力档/时长档复用同一结论。"""
    normalized = (model_id or "").strip().lower()
    if HAPPYHORSE_PATTERN.search(normalized):
        # happyhorse has no image-to-video continuation syntax and no videoedit modality; the concrete t2v/i2v/r2v profile is
        # resolved by the fallback substring match at the end of _profile_for_model, not normalized in advance here.
        # happyhorse 无 image-to-video 续接语法与 videoedit 模态；t2v/i2v/r2v 具体档位交由
        # _profile_for_model 末尾的兜底子串匹配解析，此处不预先归一化。
        return WanClassification(
            family="happyhorse",
            is_image_to_video=False,
            is_videoedit=False,
            profile_key=None,
            has_known_modality=True,
            is_known_video_modality=False,
        )
    wan3_match = WAN3_PATTERN.search(normalized)
    wan2_match = None if wan3_match else WAN2_PATTERN.search(normalized)
    wan_dot_match = None if (wan3_match or wan2_match) else WAN_DOT_FORM_PATTERN.search(normalized)
    family_match = wan3_match or wan2_match or wan_dot_match
    if wan3_match:
        family: WanFamily = "wan3"
    elif wan2_match:
        family = "wan2.7"
    elif wan_dot_match:
        family = "wan2x_dot"
    else:
        # Identifier-boundary matching of the image-to-video continuation syntax is independent of family detection: an id
        # that fails the family's strict boundary (e.g. "wan-2.2-image-to-video", where the hyphen between "wan" and the
        # version does not satisfy the dot-form boundary) may still be a video model explicitly named with the continuation
        # syntax; a family miss does not void that syntax info, it must be carried out as is for endpoints.py's image-variant
        # exclusion check — otherwise such ids get swallowed into the image endpoint by the blanket image judgment. The search
        # must still start splitting from the literal "wan" substring itself (no strict identifier boundary required, the
        # "wan" in "swan2.7-image" is located too), excluding any decoration prefix before it — otherwise an unrelated proxy
        # namespace prefix like "image-to-video-proxy/swan2.7-image" would misjudge a real image variant as video
        # continuation. Without a literal "wan" substring this field is not consumed downstream (endpoints.py checks
        # `"wan" in lowered` before reading it), so falling back to a whole-string search is fine.
        # image-to-video 续接语法的标识符边界匹配与家族归属判定相互独立：不满足家族严格边界的 id
        # （如 "wan-2.2-image-to-video"，"wan" 与版本号间的连字符不满足点号形态边界）依然可能是
        # 视频模型的显式续接语法命名，家族未命中不代表该语法信息作废，须原样带出，供 endpoints.py
        # 的 image 变体排除判定消费——否则这类 id 会被笼统 image 判定误吞成图像端点。搜索范围仍须
        # 从字面 "wan" 子串本身开始切分（不要求满足严格标识符边界，"swan2.7-image" 里的 "wan" 同样
        # 定位），不含其前的装饰前缀——否则 "image-to-video-proxy/swan2.7-image" 这类与模态无关的
        # 代理命名空间前缀会把真图像变体误判成视频续接。字面无 "wan" 子串时该字段不被下游消费
        # （endpoints.py 先决 `"wan" in lowered` 才读取本字段），退回全串搜索即可。
        wan_locator = normalized.find("wan")
        fallback_scope = normalized[wan_locator:] if wan_locator != -1 else normalized
        return WanClassification(
            family=None,
            is_image_to_video=bool(WAN_IMAGE_TO_VIDEO_PATTERN.search(fallback_scope)),
            is_videoedit=False,
            profile_key=None,
            has_known_modality=True,
            is_known_video_modality=False,
        )

    # The image-to-video continuation syntax and videoedit/s2v/v2v are all in-family modality markers; search only in the
    # modality segment after the family marker itself, excluding decoration prefixes before the marker — otherwise
    # modality-unrelated proxy namespace prefixes like "image-to-video-proxy/wan2.7-image",
    # "videoedit-proxy/wan2.7-image", "s2v-proxy/wan2.7-image" would be misjudged as the corresponding modality, masking
    # the real one (a real image variant misjudged as video continuation, or a real t2v/i2v/r2v misjudged as an
    # unimplemented modality).
    # image-to-video 续接语法与 videoedit/s2v/v2v 均是家族内的模态标记，只在家族标记本身之后的
    # 模态段内搜索，不含标记前的装饰前缀——否则 "image-to-video-proxy/wan2.7-image"、
    # "videoedit-proxy/wan2.7-image"、"s2v-proxy/wan2.7-image" 这类与模态无关的代理命名空间前缀
    # 会被误判成对应模态，掩盖其真实模态（真图像变体被误判成视频续接，或真实 t2v/i2v/r2v 被误判
    # 成未实现模态）。
    assert family_match is not None
    family_suffix = normalized[family_match.start() :]
    is_image_to_video = bool(WAN_IMAGE_TO_VIDEO_PATTERN.search(family_suffix))
    is_videoedit = False
    profile_key: str | None = None
    has_known_modality = True
    is_known_video_modality = False
    if family == "wan3":
        profile_key = _WAN3_MODEL_KEY
    elif family == "wan2.7":
        # wan27_suffix is taken directly from the normalized modality segment located by family_match (family_suffix), not
        # located by literal text search: the raw decoration prefix before the marker may itself contain a literal "wan2.7"
        # substring (e.g. "vendorwan2.7-videoedit-proxy/wan2.7-r2v": the "wan2.7" in the prefix fails the WAN2_PATTERN boundary
        # and is not judged a family marker, but is still that literal substring); a text search cannot tell such prefix noise
        # from the real family marker position, only family_match's match position is a reliable anchor. profile_key still
        # needs the original decoration prefix re-attached (not normalized, only to tolerate proxy relay naming, see the
        # boundary matching in _find_known_profile_key).
        # wan27_suffix 直接取自 family_match 定位到的模态段（family_suffix）归一化结果，不基于
        # 字面文本搜索定位：标记前的原始装饰前缀可能本身含字面 "wan2.7" 子串（如
        # "vendorwan2.7-videoedit-proxy/wan2.7-r2v"，前缀里的 "wan2.7" 不满足 WAN2_PATTERN 边界、
        # 未被判定为家族标记，但仍是该字面子串），按文本搜索定位无法区分这类前缀噪音与真正的家族
        # 标记位置，只有 family_match 的匹配位置本身是可靠锚点。profile_key 仍需拼回原始装饰前缀
        # （不参与归一化，只用于容忍代理中转命名，见 _find_known_profile_key 的边界匹配）。
        wan27_suffix = _normalize_wan27_alias(family_suffix)
        profile_key = normalized[: family_match.start()] + wan27_suffix
        is_videoedit = bool(WAN_VIDEOEDIT_PATTERN.search(wan27_suffix))
        is_s2v_or_v2v = bool(WAN_S2V_V2V_PATTERN.search(wan27_suffix))
        # wan2.7 payload construction only implements t2v/i2v/r2v; the other known but unimplemented modalities such as
        # videoedit/s2v/v2v must not take native routing either. Match the known wan2.7 keys in _MODEL_PROFILES by identifier
        # boundary (instead of requiring wan27_suffix to equal some key exactly): wan27_suffix keeps proxy relay decoration
        # suffixes ("wan2.7-r2v-0715"), and exact equality would judge these legitimate decorated names as unknown modalities.
        # If a known key is hit while videoedit/s2v/v2v is hit too (e.g. "wan2.7-i2v-s2v"), the implemented token must not mask
        # the coexisting unimplemented modality segment; it is excluded from the implemented scope as well.
        # wan2.7 的 payload 构造只实现了 t2v/i2v/r2v；videoedit/s2v/v2v 等其余已知但未实现模态
        # 同样不能落原生路由。按标识符边界匹配 _MODEL_PROFILES 里的 wan2.7 已知 key（而非要求
        # wan27_suffix 与某个 key 完全相等）：wan27_suffix 保留了代理中转的装饰后缀
        # （"wan2.7-r2v-0715"），精确相等会把这些合法装饰名也判成未知模态。命中已知 key 的同时
        # 若又命中 videoedit/s2v/v2v（如 "wan2.7-i2v-s2v"），已实现的 token 不能掩盖未实现模态
        # 段共存的事实，一并排除出已实现范围。
        has_known_modality = (
            not is_videoedit
            and not is_s2v_or_v2v
            and _find_known_profile_key(wan27_suffix, (k for k in _MODEL_PROFILES if k.startswith("wan2.7-")))
            is not None
        )
        # t2v/i2v/r2v (has_known_modality) / videoedit / s2v / v2v are all confirmed video modalities, even if some lack
        # request construction; any other unsettled naming (falling into no known modality token) is conservatively treated as
        # an image variant, making no assumption about image variant naming (consistent with endpoints.py's principle).
        # t2v/i2v/r2v（has_known_modality）/ videoedit / s2v / v2v 均是已确认的视频模态，即便部分
        # 未实现请求构造；其余未收敛命名（不落入任一已知模态 token）保守按图像变体处理，不对图像
        # 变体的命名形态做任何假设（与 endpoints.py 的判定原则一致）。
        is_known_video_modality = has_known_modality or is_videoedit or is_s2v_or_v2v
    elif family == "wan2x_dot" and is_image_to_video:
        # wan2x_dot has no registered VideoCapabilities (profile_key is always None, see the next comment); if native routing
        # were still allowed when the image-to-video continuation syntax hits, _match_profile would not recognize it and take
        # the generic fallback (first_frame defaults to True, which happens to mask the problem) — but this backend never
        # declared a verified first-frame request construction for these un-narrowed 2.x minor versions; ids without verified
        # capability / request schema are excluded from native routing and fall to the generic video endpoint in 5) below.
        # wan2x_dot 没有登记任何 VideoCapabilities（profile_key 恒 None，下条注释），image-to-video
        # 续接语法命中时若仍放行原生路由，_match_profile 会认不得、取统一回落（first_frame
        # 默认 True，恰好掩盖问题）——但本后端并未为这些未收窄的 2.x 小版本声明过已验证的首帧
        # 请求构造，没有已验证能力/请求 schema 的 id 排除出原生路由，同落下方 5) 的通用视频端点。
        has_known_modality = False
    # wan2x_dot cannot normalize an exact t2v/i2v/r2v profile directly from model_id (its naming is unsettled), so
    # profile_key stays empty and is left to the fallback substring match at the end of _match_profile (mostly not
    # recognized, generic fallback); whether to narrow it by known-modality gating like wan2.7 needs provider API facts and
    # a product decision, not decided by this field.
    # wan2x_dot 无法从 model_id 直接归一化出确切 t2v/i2v/r2v 档位（其命名形态未收敛），
    # profile_key 留空，交由 _match_profile 末尾的兜底子串匹配处理（多数认不得、取统一回落）；
    # 是否收窄同 wan2.7 一样按已知模态门控需要供应商 API 事实与产品判断，不由本字段代为决定。
    return WanClassification(
        family=family,
        is_image_to_video=is_image_to_video,
        is_videoedit=is_videoedit,
        profile_key=profile_key,
        has_known_modality=has_known_modality,
        is_known_video_modality=is_known_video_modality,
    )


def _normalize_wan27_alias(family_suffix: str) -> str:
    """Fold a wan2.7 alias matched by WAN2_PATTERN into the fixed form of the _MODEL_PROFILES keys:
    "wan[-_]?2.7[-_]<modality>", whatever separator each of the wan / version / modality parts uses, becomes
    "wan2.7-<modality>" (further folded into "i2v" when the modality contains the image-to-video continuation syntax).

    Two steps because the two separators vary independently ("wan_2.7-r2v" has an underscore only in the first,
    "wan-2.7_r2v" only in the second, "wan_2.7_r2v" in both); missing normalization of either one leaves the result not a
    substring match of the _MODEL_PROFILES keys, silently taking the generic fallback. The caller must pass a substring
    starting from the wan2.7 marker itself (without decoration prefix before the marker), and must first confirm the
    hit with WAN2_PATTERN.search.

    把 WAN2_PATTERN 命中的 wan2.7 别名折成 _MODEL_PROFILES key 固定使用的形态：
    "wan[-_]?2.7[-_]<modality>"，无论 wan/版本号/模态三段各自用哪种分隔符，统一成
    "wan2.7-<modality>"（modality 含 image-to-video 续接语法时进一步折成 "i2v"）。

    分两步是因为两段分隔符独立可变（"wan_2.7-r2v" 只有前段是下划线、"wan-2.7_r2v" 只有后段
    是下划线、"wan_2.7_r2v" 两段都是），任一段漏归一化都会导致结果不与 _MODEL_PROFILES 的 key
    构成子串关系，静默取统一回落。调用方须传入从 wan2.7 标记本身开始的子串（不含标记前
    的装饰前缀），且须先用 WAN2_PATTERN.search 确认命中。
    """
    family_suffix = WAN2_PATTERN.sub("wan2.7", family_suffix)
    family_suffix = re.sub(r"wan2\.7_", "wan2.7-", family_suffix)
    return WAN_IMAGE_TO_VIDEO_PATTERN.sub("i2v", family_suffix)


# Dispatch capability declarations by model id. happyhorse-r2v only has reference_image (no first_frame);
# wan2.7-r2v additionally supports a first frame and reference voice.
#
# audio_track: only wan3.0 requests carry an audio track switch (``parameters["audio"]`` in ``_build_payload``); the
# other models always have audio — sending that parameter gets rejected upstream as illegal. Both paths share one
# declaration: these models have no reference-to-video-specific request shape difference, so no separate
# reference_route_audio_track.
# 按 model id 派发能力声明。happyhorse-r2v 仅 reference_image（无 first_frame）；
# wan2.7-r2v 额外支持首帧与参考音色。
#
# audio_track：只有 wan3.0 的请求带音轨开关（``_build_payload`` 里的 ``parameters["audio"]``），
# 其余型号恒有声——下发该参数会被上游当非法参数拒。两条路径共用同一份声明：这些型号没有参考
# 生视频专属的请求形态差异，故不另设 reference_route_audio_track。
_MODEL_PROFILES: dict[str, VideoCapabilities] = {
    "happyhorse-1.1-t2v": VideoCapabilities(first_frame=False, audio_track=VideoAudioMode.ALWAYS_ON),
    "happyhorse-1.1-i2v": VideoCapabilities(first_frame=True, audio_track=VideoAudioMode.ALWAYS_ON),
    "happyhorse-1.1-r2v": VideoCapabilities(
        first_frame=False, max_reference_images=9, audio_track=VideoAudioMode.ALWAYS_ON
    ),
    "happyhorse-1.0-t2v": VideoCapabilities(first_frame=False, audio_track=VideoAudioMode.ALWAYS_ON),
    "happyhorse-1.0-i2v": VideoCapabilities(first_frame=True, audio_track=VideoAudioMode.ALWAYS_ON),
    "happyhorse-1.0-r2v": VideoCapabilities(
        first_frame=False, max_reference_images=9, audio_track=VideoAudioMode.ALWAYS_ON
    ),
    "wan2.7-t2v": VideoCapabilities(
        first_frame=False, max_prompt_chars=_WAN27_MAX_PROMPT_CHARS, audio_track=VideoAudioMode.ALWAYS_ON
    ),
    "wan2.7-i2v": VideoCapabilities(
        first_frame=True, max_prompt_chars=_WAN27_MAX_PROMPT_CHARS, audio_track=VideoAudioMode.ALWAYS_ON
    ),
    # Reference-to-video with a first frame is the official form of wan2.7-r2v (_build_media assembles
    # first_frame + reference_image in one request).
    # 带首帧的参考生视频是 wan2.7-r2v 的官方形态（_build_media 同请求组装
    # first_frame + reference_image）。
    "wan2.7-r2v": VideoCapabilities(
        first_frame=True,
        max_reference_images=_WAN27_R2V_MAX_REFERENCE,
        reference_audio_mode=ReferenceAudioMode.DIRECT,
        max_reference_audio_count=_WAN27_R2V_MAX_REFERENCE,
        # The voice attaches to a specific reference item (_attach_reference_voices), it is not a separate voice input
        # channel; the orchestration layer must give an explicit "whose voice goes with which image" mapping and cannot assume
        # the same order as reference_audio_files.
        # 音色挂在具体参考素材项上（_attach_reference_voices），不是独立的音色输入通道，
        # 编排层必须显式给出「谁的声音配哪张图」的映射，不能假设与 reference_audio_files 同序。
        reference_audio_per_image=True,
        max_prompt_chars=_WAN27_MAX_PROMPT_CHARS,
        audio_track=VideoAudioMode.ALWAYS_ON,
    ),
    # wan3.0's reference audio is a separate entry in the media array (not attached to reference items as in 2.7), so it
    # does not declare reference_audio_per_image and is bounded by max_reference_audio_total_seconds instead.
    # wan3.0 的参考音频是 media 数组里的独立条目（不像 2.7 挂在参考素材项上），故不声明
    # reference_audio_per_image，改由 max_reference_audio_total_seconds 约束总量。
    _WAN3_MODEL_KEY: VideoCapabilities(
        first_frame=True,
        last_frame=True,
        max_reference_images=_WAN3_MAX_REFERENCE_IMAGES,
        reference_audio_mode=ReferenceAudioMode.DIRECT,
        max_reference_audio_count=_WAN3_MAX_REFERENCE_AUDIO,
        max_reference_audio_total_seconds=_WAN3_MAX_REFERENCE_AUDIO_TOTAL_SECONDS,
        max_prompt_chars=_WAN3_MAX_PROMPT_CHARS,
    ),
}

# Unrecognized models (e.g. custom proxy relay names) take name_family's generic fallback (text-to-video and first
# frame); the audio track switch stays controllable under "no signal, no tightening". The request-side counterpart is
# "no signal, no unknown parameters" (_build_payload only sends audio to models hit by _is_wan3); the opposite
# directions are intentional: a declaration-side misjudgment as always-audio would lock the user's switch, while a
# request-side unknown parameter gets rejected directly upstream.
# 认不得的 model（如代理中转自定义命名）取 name_family 的统一回落（文生视频与首帧），音轨开关按
# 「无信号不收紧」保持可控。请求侧的对应判定是「无信号不发未知参数」（_build_payload 只对 _is_wan3
# 命中的型号下发 audio），两者方向不同是有意的：声明侧误判恒有声会把用户的开关锁死，请求侧误发未知
# 参数会被上游直接拒。


def _sent_resolution(request: VideoGenerationRequest) -> str:
    """The resolution tier sent: Bailian requires uppercase; sends 720P when unspecified. / 下发的分辨率档位：百炼要求大写，未指定时下发 720P。"""
    return (request.resolution or "720p").upper()


def _is_wan3(model: str | None) -> bool:
    """Detect the wan3.0 series: it differs structurally from the other models in request shape in three ways.

    First, one model handles text-to-video / image-to-video / reference-to-video, so a missing reference image is a
    valid request (the other reference-capable models are r2v-only, no reference image means no input); second, the
    audio track is controlled by a request parameter instead of always on; third, it can use a separate maas domain.
    All three dispatch by model name; the profile table only carries VideoCapabilities declarations.

    识别 wan3.0 系列：它与其余型号在请求形态上有三处结构差异。

    一是单模型通吃文生/图生/参考生，参考图缺席是合法请求（其余带参考能力的型号都是 r2v
    专用，无参考图即无输入）；二是音轨由请求参数控制而非恒开；三是可走独立 maas 域名。
    三处都按型号名分派，profile 表只承载 VideoCapabilities 声明。
    """
    return bool(WAN3_PATTERN.search((model or "").strip().lower()))


def _find_known_profile_key(normalized: str, keys: Iterable[str]) -> str | None:
    """Find the first known key from keys occurring in normalized by identifier boundary; None if none hit.

    Both boundaries require non-alphanumerics: the left avoids third-party names like "swan2.7-r2v" ("s" right before
    "wan2.7-r2v") or "myhappyhorse-1.0-r2v" being swallowed by a literal substring; the right avoids unknown variant
    suffixes like "wan2.7-i2vfoo", "happyhorse-1.0-r2vfoo" being truncated and misjudged as known keys. Proxy relay
    decoration prefixes/suffixes ("proxy/xxx", "xxx-0715") naturally satisfy both boundaries via non-alphanumeric
    separators and are unaffected.

    在 normalized 里按标识符边界查找 keys 中出现的第一个已知 key，均未命中则 None。

    两侧边界要求非字母数字：左侧避免 "swan2.7-r2v"（"s" 紧贴 "wan2.7-r2v"）、
    "myhappyhorse-1.0-r2v" 这类第三方型号名被字面子串误吞；右侧避免 "wan2.7-i2vfoo"、
    "happyhorse-1.0-r2vfoo" 这类未知变体后缀被截断误判成已知 key。代理中转的装饰前缀/后缀
    （"proxy/xxx"、"xxx-0715"）靠非字母数字分隔符天然满足两侧边界，不受影响。
    """
    for key in keys:
        if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", normalized):
            return key
    return None


def _match_profile(model: str) -> VideoCapabilities | None:
    """Resolve the capability profile by name: exact hit first, then tolerate proxy relay prefix/suffix decoration; ``None`` when unrecognized.

    infer_endpoint routes to dashscope-async-video using the same verdict from classify_wan_model, so this must also
    tolerate substrings, otherwise decorated names like "proxy/happyhorse-1.0-r2v" / "wan2.7-r2v-0715" fall back to the generic fallback, losing r2v's max_reference_images, and _build_media builds a wrong payload from that.
    Names with only the series and no variant suffix (e.g. bare "happyhorse") cannot distinguish t2v/i2v/r2v and are unrecognized by design.

    按名字解析能力档：先精确命中，再容忍代理中转的前后缀装饰；认不得时为 ``None``。

    infer_endpoint 用 classify_wan_model 的同一结论路由到 dashscope-async-video，故此处也须
    子串容忍，否则 "proxy/happyhorse-1.0-r2v" / "wan2.7-r2v-0715" 这类装饰名会退回统一回落、丢掉 r2v 的 max_reference_images，_build_media 据此构造出错误 payload。
    仅带系列名而无变体后缀（如裸 "happyhorse"）无法判别 t2v/i2v/r2v，按设计认不得。
    """
    normalized = (model or "").strip().lower()
    if not normalized:
        return None
    if normalized in _MODEL_PROFILES:
        return _MODEL_PROFILES[normalized]
    classification = classify_wan_model(normalized)
    # has_known_modality=False means infer_endpoint already excluded this id from native routing (unimplemented
    # modalities like videoedit/s2v/v2v, or wan2x_dot's unverified image-to-video continuation); this must stay unrecognized
    # in sync — otherwise substring-tolerant matching could still find a coexisting known token in a decoration suffix
    # (e.g. "i2v" in "wan2.7-i2v-s2v"), giving an id that does not take native routing that route's capability profile,
    # contradicting the routing decision.
    # has_known_modality=False 表示 infer_endpoint 已把该 id 排除出原生路由（videoedit/s2v/v2v
    # 等未实现模态、或 wan2x_dot 的未验证 image-to-video 续接），此处必须同步认不得
    # ——否则子串容忍匹配仍可能在装饰后缀里找到共存的已知 token（如 "wan2.7-i2v-s2v"
    # 里的 "i2v"），让不落原生路由的 id 反而拿到该路由的能力档，与路由判定互斥。
    if not classification.has_known_modality:
        return None
    if classification.profile_key is not None:
        normalized = classification.profile_key
    # The profile keys (happyhorse-{1.0,1.1}-{t2v,i2v,r2v} / wan2.7-{t2v,i2v,r2v} /
    # wan3.0-video) are not substrings of each other, so there is no ambiguity and _find_known_profile_key's boundary
    # matching can safely try them one by one.
    # 各 profile key（happyhorse-{1.0,1.1}-{t2v,i2v,r2v} / wan2.7-{t2v,i2v,r2v} /
    # wan3.0-video）互不为子串，无歧义，_find_known_profile_key 的边界匹配可安全逐一试探。
    known = _find_known_profile_key(normalized, _MODEL_PROFILES)
    if known is not None:
        return _MODEL_PROFILES[known]
    return None


def _profile_for_model(model: str | None) -> VideoCapabilities:
    """The model's capability profile: names are resolved one by one per name_family's rules; generic fallback when none is recognized.

    __init__ and video_capabilities_for_model share this function, keeping a single source of truth.

    model 的能力档：名字按 name_family 的规则逐个解析，都认不得时取统一回落。

    __init__ 与 video_capabilities_for_model 共用本函数，保持单一真相源。
    """
    return capabilities_by_names(model or "", _match_profile)


class DashScopeVideoBackend(ProviderJobIdPersistenceMixin):
    """Alibaba Bailian video backend (async video-synthesis endpoint). / 阿里百炼视频后端（异步 video-synthesis 端点）。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        wan3_base_url: str | None = None,
        http_timeout: float = 60.0,
        transport_layer: TransportLayer | None = None,
    ) -> None:
        self._api_key = resolve_dashscope_api_key(api_key)
        self._base_url = dashscope_native_base_url(base_url)
        # The wan3.0-specific maas domain contains region and workspace, which can be neither inferred nor normalized, so the
        # full URL the user entered is used as is (only a trailing slash removed); if empty, fall back to the generic domain
        # and let the remote report the error truthfully.
        # wan3.0 专用 maas 域名含地域与 workspace，推不出也归一化不了，故按用户填写的
        # 完整 URL 原样使用（仅去掉尾部斜杠），未填则回落通用域名、由对端如实报错。
        self._wan3_base_url = (wan3_base_url or "").strip().rstrip("/") or None
        self._model = model or DEFAULT_MODEL
        self._http_timeout = http_timeout
        self._transport_layer = transport_layer
        self._video_capabilities = _profile_for_model(self._model)

    @property
    def name(self) -> str:
        return PROVIDER_DASHSCOPE

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def video_capabilities_for_model(model: str) -> VideoCapabilities:
        """Pure computation of reference image and other caps from model_id — no SDK client constructed (no api_key needed).

        The resolver just calls this method when resolving the reference image limit, without building a whole backend;
        the instance property delegates here, keeping the backend the single source of truth.

        按 model_id 纯计算参考图等 caps —— 不构造 SDK client（无需 api_key）。

        resolver 解析参考图上限时调本方法即可，不必构造整个 backend；instance property 委托至此，
        保持 backend 为单一真相源。
        """
        return _profile_for_model(model)

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return self.video_capabilities_for_model(self._model)

    @property
    def _request_base_url(self) -> str:
        """The domain this model's requests actually use.

        Submit and polling share the same one: a task id can only be queried on the endpoint that created it; splitting
        them would make a successfully submitted wan3.0 task get 404 when polled.

        本型号请求实际走的域名。

        提交与轮询共用同一个：任务 id 只在创建它的 endpoint 上可查，两者分家会让 wan3.0
        任务提交成功后轮询到 404。
        """
        if self._wan3_base_url and _is_wan3(self._model):
            return self._wan3_base_url
        return self._base_url

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        # Building the payload reads and encodes first frame / reference images / audio; run it in a thread to avoid blocking the event loop
        # 组装 payload 要读首帧 / 参考图 / 音频并编码，放线程里跑，避免阻塞事件循环
        payload = await asyncio.to_thread(self._build_payload, request)
        logger.info(
            "调用 %s 视频 API model=%s body=%s",
            self.name,
            self._model,
            format_kwargs_for_log(safe_body_for_log(payload)),
        )
        async with artifact_http_client(timeout=self._http_timeout, transport_layer=self._transport_layer) as client:
            task_id = await self._create_task(client, payload, request)
            logger.info("DashScope 视频任务已创建: task_id=%s model=%s", task_id, self._model)
            # Also write back the actually used submit domain (wan3.0 uses a separate maas domain, and both vary with user config):
            # resume replays the original domain from it, otherwise after a config change polling would hit a host that cannot find the task.
            # 一并写回实际提交域名（wan3.0 走独立 maas 域名，且两者都随用户配置可变）：
            # 续跑据此回放原域名，不然改配置后轮询会打到查不到该任务的主机。
            await self._persist_provider_job_id(
                request, task_id, provider=PROVIDER_DASHSCOPE, endpoint=self._request_base_url
            )
            return await self._poll_and_build(client, task_id, request, is_resume=False)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Resume an already submitted DashScope task: only poll + download (ADR 0007).

        接续已 submit 的 DashScope task：仅 poll + 下载（ADR 0007）。"""
        async with artifact_http_client(timeout=self._http_timeout, transport_layer=self._transport_layer) as client:
            return await self._poll_and_build(client, job_id, request, is_resume=True)

    # ── request building ────────────────────────────────────────────────

    @staticmethod
    def metered_usage(
        model: str, request: VideoGenerationRequest, result: VideoGenerationResult | None = None
    ) -> Usage:
        """Metering of this request (see :mod:`lib.backends.video_backends.metering`).

        Resolution takes the tier actually sent (720P when unspecified); billed output seconds take ``usage.duration``. The
        audio track switch is only sent for wan3.0, other models always have audio. The reference image count is capped by
        the limit of the execution gate's capabilities (the model profile when absent).

        这次请求的计量（见 :mod:`lib.backends.video_backends.metering`）。

        分辨率取实际下发的档位（未指定时下发 720P）；结算的输出秒数取 ``usage.duration``。音轨开关只对 wan3.0
        下发，其余型号恒有声。参考图张数按执行闸门那份能力（缺省时型号档案）的上限截断后计。
        """
        caps = request.video_capabilities or DashScopeVideoBackend.video_capabilities_for_model(model)
        provided = [r for r in (request.reference_images or []) if r] if caps.max_reference_images > 0 else []
        return video_usage(
            request,
            result,
            seconds=request.duration_seconds,
            resolution=_sent_resolution(request),
            audio=request.generate_audio if _is_wan3(model) else True,
            reference_images=min(len(provided), caps.max_reference_images),
        )

    def _build_payload(self, request: VideoGenerationRequest) -> dict:
        media = self._build_media(request)
        input_block: dict = {"prompt": request.prompt}
        if media:
            input_block["media"] = media

        parameters: dict = {
            "resolution": _sent_resolution(request),
            "duration": request.duration_seconds,
            # HappyHorse adds a "Happy Horse" watermark by default; disable it explicitly / HappyHorse 默认带 "Happy Horse" 水印，显式关闭
            "watermark": False,
        }
        # ratio is only sent without a first frame: image-to-video / reference-to-video with a first frame take the aspect
        # ratio from the first frame and upstream ignores ratio (wan2.7: "ignored automatically when first_frame is passed"),
        # HappyHorse image-to-video even rejects ratio as an illegal parameter. The default aspect_ratio is non-empty; without
        # gating, requests with a first frame would be rejected upstream. Only without a first frame (text-to-video / reference without first frame) is ratio needed.
        # ratio 仅在无首帧时下传：图生视频/带首帧的参考生视频按首帧定宽高比，上游会忽略 ratio
        # （wan2.7「传 first_frame 时自动忽略」），HappyHorse 图生视频更直接把 ratio 当非法参数拒绝。
        # 默认 aspect_ratio 非空，若不门控会让带首帧的请求被上游拒。首帧缺席（文生视频/无首帧参考）才需 ratio。
        has_first_frame = any(m.get("type") == "first_frame" for m in media)
        if request.aspect_ratio and not has_first_frame:
            parameters["ratio"] = request.aspect_ratio
        if request.seed is not None:
            parameters["seed"] = request.seed
        # The audio track switch is only sent for wan3.0: other models always have audio and sending this parameter gets it
        # rejected upstream as illegal. This line is the execution-side counterpart of the audio_track declarations in
        # `_MODEL_PROFILES` (always-audio models declare ALWAYS_ON, wan3.0 takes the default CONTROLLABLE); changing one side
        # requires changing the other.
        # 音轨开关只对 wan3.0 下发：其余型号恒有声，下发该参数会被上游当非法参数拒绝。本行就是
        # `_MODEL_PROFILES` 里 audio_track 声明的执行侧对应物（恒有声型号声明 ALWAYS_ON，wan3.0
        # 取默认的 CONTROLLABLE），改一侧须同改另一侧。
        if _is_wan3(self._model):
            parameters["audio"] = request.generate_audio
        elif not request.generate_audio and self._video_capabilities.audio_track is VideoAudioMode.ALWAYS_ON:
            # Always-audio models cannot send a silent request; this is reached only when the user overrides execution capabilities
            # to loosen them, so reject before paying instead of producing an audio final cut as usual.
            # Unrecognized models declare controllable under "no signal, no tightening"; no tightening here either, keep not sending the switch.
            # 恒有声型号发不出静音请求；执行期能力被用户覆盖放宽时才会走到这里，付费前拒绝而不是照常出有声成片。
            # 认不得的型号按「无信号不收紧」声明可控，这里同样不收紧，沿用不下发开关。
            raise VideoCapabilityError("video_audio_switch_not_supported", provider=self.name, model=self._model)

        return {
            "model": self._model,
            "input": input_block,
            "parameters": parameters,
        }

    def _build_media(self, request: VideoGenerationRequest) -> list[dict]:
        # Whether it can be sent is judged by the execution gate's capabilities: dimensions the user loosened are sent as usual and upstream decides; directly constructed requests fall back to the model profile.
        # 可发与否按执行闸门那份能力判定：用户放宽的维度照常发出、由上游裁定；直接构造的请求回落型号档案。
        caps = request.video_capabilities or self._video_capabilities
        media: list[dict] = []
        if request.start_image and not caps.first_frame:
            raise VideoCapabilityError("video_first_frame_unsupported", provider=self.name, model=self._model)
        if request.start_image:
            p = Path(request.start_image)
            # fail-loud: if a first frame image is declared but missing (directory / non-regular file, including "." resolved from
            # an empty string) or fails to read, abort instead of silently ignoring it — otherwise the user gets a result that did
            # not use the first frame without knowing.
            # fail-loud：声明了首帧图却缺失（目录/非常规文件，含空串解析出的 "."）或读取失败即中止，
            # 不静默忽略 —— 否则用户拿到一个没用上首帧的结果却不知情。
            uri = _read_image_or_none(p)
            if uri is None:
                raise VideoCapabilityError("video_start_image_unreadable", model=self._model, name=p.name)
            media.append({"type": "first_frame", "url": uri})
        if request.end_image and not caps.last_frame:
            raise VideoCapabilityError("video_last_frame_unsupported", provider=self.name, model=self._model)
        if request.end_image:
            p = Path(request.end_image)
            uri = _read_image_or_none(p)
            # fail-loud like the first frame: abort if a declared end frame cannot be read, rather than silently producing a result without it.
            # 与首帧同为 fail-loud：声明了尾帧却读不到就中止，不静默产出一个没用上尾帧的结果。
            if uri is None:
                raise VideoCapabilityError("video_end_image_unreadable", model=self._model, name=p.name)
            media.append({"type": "last_frame", "url": uri})
        reference_items: list[dict] = []
        provided = [r for r in (request.reference_images or []) if r]
        # r2v must have reference images: this is a fact of the model profile; it does not transfer to i2v models when the
        # user raises the reference image limit, nor is a request without reference images let through because the user
        # narrowed the limit to 0.
        # r2v 必须有参考图：这是型号档案的事实，不随用户放宽参考图上限转移到 i2v 型号上，也不因用户把上限
        # 收窄为 0 而放行无参考图的请求。
        if not provided and self._video_capabilities.max_reference_images > 0 and not _is_wan3(self._model):
            raise VideoCapabilityError("video_reference_images_required", model=self._model)
        if caps.max_reference_images <= 0 and provided:
            raise VideoCapabilityError("video_reference_images_unsupported", model=self._model)
        if caps.max_reference_images > 0:
            # fail-loud: if any declared reference image is missing / unreadable (fails is_file or read_bytes raises OSError) →
            # abort with an error listing the file names. Do not silently degrade to no-reference / subset generation (wrong result,
            # billed as usual); let the user notice that an image was not used.
            # fail-loud：任一声明的参考图缺失/不可读（is_file 不过或 read_bytes 抛 OSError）→ 报错列出文件名
            # 中止。不静默退化为无参考/子集生成（会产出错误结果且照常计费），让用户感知到有图未被使用。
            data_uris: list[str] = []
            unreadable: list[str] = []
            for r in provided:
                p = Path(r)
                uri = _read_image_or_none(p)
                if uri is None:
                    unreadable.append(p.name)
                else:
                    data_uris.append(uri)
            if unreadable:
                raise VideoCapabilityError(
                    "video_reference_images_unreadable", model=self._model, names=", ".join(unreadable)
                )
            limit = caps.max_reference_images
            if len(data_uris) > limit:
                raise VideoCapabilityError(
                    "video_reference_images_exceeded", model=self._model, limit=limit, count=len(data_uris)
                )
            reference_items = [{"type": "reference_image", "url": uri} for uri in data_uris]
        # The audio check is outside the reference item loop: it must run even when there is no reference item to attach to,
        # otherwise a model without reference image capability like wan2.7-i2v would silently drop the audio and charge as
        # usual. When the user loosens reference_audio_mode to direct, audio is sent as usual and upstream decides; capability
        # profiles not loosened that carry audio fail loud below. Two attach forms: per_image attaches to reference items (wan2.7-r2v), the rest use
        # separate reference_audio entries in the media array (wan3.0).
        # 音频判定在参考素材循环之外：无参考素材可挂时也要走一遍，否则 wan2.7-i2v 这类无参考图
        # 能力的 model 收到音频会静默丢弃、照常扣费。用户把 reference_audio_mode 放宽成 direct 时
        # 音频照常发出、由上游裁定；未放宽的能力档带音频在下方 fail-loud。两种挂载形态：per_image 的挂在参考素材项上（wan2.7-r2v），其余走 media
        # 数组里的独立 reference_audio 条目（wan3.0）。
        standalone_audio_items: list[dict] = []
        if caps.reference_audio_per_image:
            self._attach_reference_voices(reference_items, request)
        else:
            standalone_audio_items = self._build_reference_audio_items(request)
        media.extend(reference_items)
        media.extend(standalone_audio_items)
        return media

    def _build_reference_audio_items(self, request: VideoGenerationRequest) -> list[dict]:
        """Turn reference audio into separate ``reference_audio`` entries in the media array (wan3.0 form).

        The only difference from the per-segment attach form is where it lands: here each audio segment is its own media
        entry and takes no reference item slot, so slot alignment does not apply. Decoding and fail-loud rules: see
        :meth:`_decode_reference_audio_uris`.

        把参考音频转成 media 数组里的独立 ``reference_audio`` 条目（wan3.0 形态）。

        与逐段挂载形态的差别只在落点：这里每段音频自成一个 media 条目，不占参考素材槽位，
        因而没有 slots 对齐一说。解码与 fail-loud 口径见 :meth:`_decode_reference_audio_uris`。
        """
        return [{"type": "reference_audio", "url": uri} for uri in self._decode_reference_audio_uris(request)]

    def _decode_reference_audio_uris(self, request: VideoGenerationRequest) -> list[str]:
        """Read the request's reference audio segment by segment into data URIs, in the same order as the input.

        Both attach forms (separate media entries and per-segment ``reference_voice``) share this method, so decoding rules
        live in one place: capability profile not supporting audio, extension not among supported formats, any segment
        unreadable — all fail loud here. No segment is skipped — the order is the identification contract of "audio N" in
        the prompt; silently sending one fewer segment would void that character's voice declaration without notice while
        still billing. Segment count and total duration limits are checked by ``gate_video_request`` before paying.

        把请求里的参考音频逐段读成 data URI，顺序与入参一一对应。

        两种挂载形态（独立 media 条目与逐段 ``reference_voice``）共用本方法，解码口径因而只有
        一处：能力档不支持音频、扩展名不在受支持格式内、任一段读不出来，都在此 fail-loud。
        不跳过任何一段——顺序即 prompt 中「音频N」的指认契约，静默少发一段会让该角色的音色
        声明无声失效且照常计费。段数与总时长上限由 ``gate_video_request`` 在付费前校验。
        """
        audio_files = list(request.reference_audio_files or [])
        if not audio_files:
            return []
        if (request.video_capabilities or self._video_capabilities).reference_audio_mode == ReferenceAudioMode.NONE:
            raise VideoCapabilityError("video_reference_audio_unsupported", provider=self.name, model=self._model)
        uris: list[str] = []
        unreadable: list[str] = []
        for audio in audio_files:
            path = Path(audio)
            if path.suffix.lower() not in _REFERENCE_AUDIO_MIME_TYPES:
                raise VideoCapabilityError(
                    "video_reference_audio_format_unsupported",
                    name=path.name,
                    supported=", ".join(sorted(_REFERENCE_AUDIO_MIME_TYPES)),
                )
            uri = _read_reference_audio_or_none(path)
            if uri is None:
                unreadable.append(path.name)
            else:
                uris.append(uri)
        if unreadable:
            raise VideoCapabilityError(
                "video_reference_audio_unreadable", model=self._model, names=", ".join(unreadable)
            )
        return uris

    def _attach_reference_voices(self, reference_items: list[dict], request: VideoGenerationRequest) -> None:
        """Attach reference audio segment by segment to the ``reference_voice`` field of reference items (in place).

        Alignment prefers ``request.reference_audio_targets`` (which index of ``reference_items`` the i-th audio segment maps
        to) — reference audio order is the order of first speaker appearance in dialogue lines, reference image order is the
        order of first mention; the two are derived independently, and the orchestration layer (``reference_video`` render
        pipeline) has already computed the "whose voice goes with which image" mapping, which must not be re-guessed by
        position here. When ``reference_audio_targets`` is ``None``, fall back to positional alignment (the N-th audio
        segment attaches to the N-th reference item) — the same order on both sides is not a contract; the fallback only
        serves callers not filled by the orchestration layer (e.g. hand-written tests).

        When there are more audio segments than attachable reference items, fail hard instead of dropping the extras:
        dropping would void some character's voice declaration silently, the user would only find out in the final cut that
        the character's voice is still random, and would already be charged. Out-of-range indexes in
        ``reference_audio_targets`` also fail hard — that means the mapping computed by the orchestration layer does not
        match the reference images actually sent with the request, which must be exposed, not silently swallowed.

        把参考音频逐段挂到参考素材项的 ``reference_voice`` 字段上（就地修改）。

        对齐优先用 ``request.reference_audio_targets``（第 i 段音频对应 ``reference_items``
        的哪个下标）——参考音频的顺序是台词 speaker 首现顺序，参考图的顺序是 mention 首现
        顺序，两者独立派生，编排层（``reference_video`` 渲染管线）已算出「谁的声音配哪张图」
        的映射，此处不得自行按位置重新猜测。``reference_audio_targets`` 为 ``None`` 时回退
        按位置对齐（第 N 段音频挂第 N 个参考素材）——两侧同序本身不是契约，回退仅服务未经
        编排层填充的调用方（如手写测试）。

        音频段数多于可挂载的参考素材时硬失败而非丢弃多余段：丢弃会让某个角色的音色声明无声
        失效，用户直到成片才发现该角色声音仍是随机的，且已照常扣费。``reference_audio_targets``
        携带越界下标同样按此硬失败——那意味着编排层算出的映射与实际随请求发出的参考图对不上，
        必须暴露而非静默吞掉。
        """
        audio_files = list(request.reference_audio_files or [])
        if not audio_files:
            return
        if (request.video_capabilities or self._video_capabilities).reference_audio_mode == ReferenceAudioMode.NONE:
            raise VideoCapabilityError("video_reference_audio_unsupported", provider=self.name, model=self._model)

        targets = request.reference_audio_targets
        if targets is not None:
            # Duplicate indexes are the same kind of mismatch as out-of-range ones: when two audio segments point at the same
            # reference item, assigning one by one silently overwrites the earlier binding and some character's voice declaration
            # is lost without notice — must fail hard, never let it happen quietly.
            # 重复下标与越界下标同类错配：两段音频指向同一个参考素材项时，逐条赋值会静默
            # 覆盖前一条绑定，某个角色的音色声明无声丢失——必须硬失败，不能让它悄悄发生。
            valid = (
                len(targets) == len(audio_files)
                and len(set(targets)) == len(targets)
                and all(0 <= t < len(reference_items) for t in targets)
            )
        else:
            valid = len(audio_files) <= len(reference_items)
        if not valid:
            # Split from the gate's video_reference_audio_exceeded into two codes: that one's limit is the model's capability limit
            # (reducing characters gets past it), this one's limit is how many attachable reference items the request actually has
            # (adding reference images also gets past it). Sharing one code would make the message suggest a fix that does not match the actual blocker.
            # 与 gate 的 video_reference_audio_exceeded 分成两个 code：那条的 limit 是模型的
            # 能力上限（减角色数就能过），这条的上限是该请求实际有几个可挂载的参考素材
            # （加参考图也能过）。共用一个 code 会让文案给出与实际卡点不符的处置建议。
            raise VideoCapabilityError(
                "video_reference_audio_slots_insufficient",
                provider=self.name,
                model=self._model,
                slots=len(reference_items),
                count=len(audio_files),
            )
        # Decode after the slot check: insufficient slots ("this request does not have enough reference images to attach to")
        # explains the blocker earlier than some file being unreadable; reporting it first lets the user see what really needs changing in one go.
        # 解码放在槽位校验之后：槽位不足是「这次请求的参考图不够挂」，比某段文件读不出来更靠前
        # 地说明卡点，先报它能让用户一次看到真正要改的东西。
        uris = self._decode_reference_audio_uris(request)
        if targets is not None:
            for idx, uri in zip(targets, uris, strict=True):
                reference_items[idx]["reference_voice"] = uri
        else:
            for item, uri in zip(reference_items, uris, strict=False):
                item["reference_voice"] = uri

    # ── HTTP submit / poll / download ───────────────────────────────────

    @with_retry_async(
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        backoff_seconds=DEFAULT_BACKOFF_SECONDS,
        retry_if=should_retry_submit,
    )
    async def _create_task(
        self, client: httpx.AsyncClient, payload: dict, request: VideoGenerationRequest | None = None
    ) -> str:
        # Creating a task is a non-idempotent "create task + bill" POST: submit_post turns ambiguous transport errors (the
        # request may have reached the server but the response was lost in transit) into a terminal AmbiguousSubmitError
        # failure, to avoid automatic retries creating duplicate tasks + duplicate billing; for >=400 it logs the body +
        # raise_for_status raises HTTPStatusError (keeping status_code so the choke point can recognize 413 downgrade), and
        # should_retry_submit branches on status code — 4xx fail fast, 5xx/429 retry.
        # 创建任务是非幂等的「建任务 + 计费」POST：submit_post 把歧义传输错误（请求可能已送达
        # 服务端但响应在途丢失）转 AmbiguousSubmitError 终态失败，避免自动重试重复建任务 + 重复计费；
        # >=400 由其落 body 日志 + raise_for_status 抛 HTTPStatusError（保留 status_code 供咽喉层识别
        # 413 降档），交 should_retry_submit 按状态码分流——4xx fail-fast、5xx/429 重试。
        resp = await submit_post(
            lambda: client.post(
                f"{self._request_base_url}{_VIDEO_ENDPOINT}",
                json=payload,
                headers=dashscope_headers(self._api_key, async_mode=True),
            ),
            provider=PROVIDER_DASHSCOPE,
            request=request,
        )
        return extract_task_id(resp.json())

    async def _poll_once(self, client: httpx.AsyncClient, task_id: str, base_url: str) -> dict:
        resp = await client.get(
            f"{base_url}/tasks/{task_id}",
            headers=dashscope_headers(self._api_key),
        )
        raise_for_status_redacted(resp)
        return resp.json()

    async def _poll_and_build(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        request: VideoGenerationRequest,
        *,
        is_resume: bool,
    ) -> VideoGenerationResult:
        # Resume polling replays the submit-time domain: a task id can only be queried on the endpoint that created it; if the
        # user changes base_url mid-flight, polling the old task on the newly resolved domain would 404 and be misjudged as expired by the 404 branch below.
        # 续跑轮询回放提交时的域名：任务 id 只在创建它的 endpoint 上可查，用户在途改 base_url
        # 后按当下配置解析出的新域名去轮旧任务会 404，被下方的 404 分支误判成过期。
        base_url = request.submitted_base_url or self._request_base_url

        # On resume, a completely nonexistent task shows as GET 404, which the gate turns into ResumeExpiredError; a task_id
        # expired after 24h shows as 200 + task_status=UNKNOWN, caught by is_dashscope_expired below (judged after the terminal state returns).
        # 续跑时 task 完全不存在表现为 GET 404，由闸门转 ResumeExpiredError；task_id 24h 过期表现为
        # 200 + task_status=UNKNOWN，由下方 is_dashscope_expired 兜底（终态返回后判定）。
        gated_poll = resume_expiry_gate(
            recording_poll(lambda: self._poll_once(client, task_id, base_url), request),
            resume_job_id=task_id if is_resume else None,
            provider=PROVIDER_DASHSCOPE,
        )

        final = await poll_with_retry(
            poll_fn=gated_poll,
            is_done=is_dashscope_terminal,
            is_failed=dashscope_failure_reason,
            max_wait=request.poll_timeout_seconds,
            retry_if=should_retry_poll,
            label="DashScope",
            on_progress=lambda v, elapsed: logger.info(
                "DashScope 视频生成中... status=%s elapsed=%ds",
                (v.get("output") or {}).get("task_status"),
                int(elapsed),
            ),
        )
        if is_dashscope_expired(final):
            if is_resume:
                raise ResumeExpiredError(
                    job_id=task_id,
                    provider=PROVIDER_DASHSCOPE,
                    message=f"DashScope task expired: {task_id}",
                )
            raise RuntimeError(f"DashScope task expired during generate: {task_id}")

        video_url = extract_video_url(final)
        await self._download_with_retry(video_url, request.output_path)
        logger.info("DashScope 视频下载完成: %s", request.output_path)

        # usage.duration is the real billed duration (wan2.7-r2v includes input video duration); falls back to the request duration when missing
        # usage.duration 是真实计费时长（wan2.7-r2v 含输入视频时长），缺失回落请求时长
        billing_duration = extract_billing_duration(final)
        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_DASHSCOPE,
            model=self._model,
            duration_seconds=billing_duration if billing_duration is not None else request.duration_seconds,
            video_uri=video_url,
            task_id=task_id,
            generate_audio=request.generate_audio,
        )

    @staticmethod
    async def _download_with_retry(video_url: str, output_path: Path) -> None:
        await download_resumable_video(video_url, output_path, label="DashScope")
