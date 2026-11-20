"""ENDPOINT_REGISTRY: the single source of truth for endpoints available to custom providers.

Each endpoint is an EndpointSpec binding media_type, family, HTTP call form and a build_backend closure.
There are two implementation forms: a Python backend, or a bundled declarative definition (``builtin_endpoints/*.json``,
loaded into this registry at import time via builtin_definitions, distinguished from the former by EndpointSpec.definition).
Both share the same key domain.
factory.create_custom_backend dispatches by looking up the endpoint string;
server.routers.custom_providers exposes the catalog to the frontend via GET /custom-providers/endpoints,
so the frontend's dropdown options and path display derive entirely from this source of truth.

ENDPOINT_REGISTRY — 自定义供应商可用 endpoint 单一真相源。

每条 endpoint 是一个 EndpointSpec，绑定 media_type、family、HTTP 调用形态与 build_backend 闭包。
实现形态有两种：Python backend，或随版声明式定义（``builtin_endpoints/*.json``，import 期经
builtin_definitions 装入本注册表，用 EndpointSpec.definition 与前者区分）。两者共用同一键域。
factory.create_custom_backend 通过 endpoint 字符串查表派发；
server.routers.custom_providers 通过 GET /custom-providers/endpoints 把目录暴露给前端，
让前端的下拉选项、路径展示完全派生自此真相源。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

from arcreel_market_core.aspect_size import IMAGE_TIER_SHORT_EDGE, VIDEO_TIER_SHORT_EDGE, short_edge_to_resolution
from arcreel_market_core.endpoint_definition.kinds import COMFYUI_KIND
from arcreel_market_core.endpoint_definition.media_type import definition_media_type
from arcreel_market_core.video_backend_contract import ReferenceAudioMode, VideoCapabilities
from lib.backends.audio_backends.dashscope import DashScopeAudioBackend
from lib.backends.audio_backends.elevenlabs import ElevenLabsAudioBackend, elevenlabs_base_url
from lib.backends.audio_backends.gemini import GeminiAudioBackend
from lib.backends.audio_backends.minimax import MiniMaxAudioBackend
from lib.backends.audio_backends.openai import OpenAIAudioBackend
from lib.backends.dashscope_shared import dashscope_native_base_url
from lib.backends.image_backends.ark import ArkImageBackend
from lib.backends.image_backends.base import ImageCapability
from lib.backends.image_backends.dashscope import DashScopeImageBackend
from lib.backends.image_backends.gemini import GeminiImageBackend
from lib.backends.image_backends.kling import KlingImageBackend
from lib.backends.image_backends.minimax import MiniMaxImageBackend
from lib.backends.image_backends.openai import OpenAIImageBackend
from lib.backends.image_backends.openai_chat import OpenAIChatImageBackend
from lib.backends.image_backends.openrouter import IMAGES_PATH as OPENROUTER_IMAGES_PATH
from lib.backends.image_backends.openrouter import MODELS_PATH as OPENROUTER_MODELS_PATH
from lib.backends.image_backends.openrouter import OpenRouterImageBackend
from lib.backends.minimax_shared import minimax_text_base_url
from lib.backends.text_backends.anthropic import AnthropicTextBackend
from lib.backends.text_backends.gemini import GeminiTextBackend
from lib.backends.text_backends.openai import OpenAITextBackend
from lib.backends.text_backends.openai_responses import OpenAIResponsesTextBackend
from lib.backends.video_backends.ark import ArkVideoBackend
from lib.backends.video_backends.dashscope import DashScopeVideoBackend, classify_wan_model
from lib.backends.video_backends.gemini import GeminiVideoBackend
from lib.backends.video_backends.kling import KlingVideoBackend
from lib.backends.video_backends.openai import OpenAIVideoBackend
from lib.backends.video_backends.vidu import ViduVideoBackend
from lib.config.url_utils import (
    ANTHROPIC_MESSAGES_PATH,
    ensure_google_base_url,
    ensure_openai_base_url,
    normalize_anthropic_base_url,
)
from lib.custom_provider import is_custom_endpoint
from lib.custom_provider.backends import (
    CustomAudioBackend,
    CustomImageBackend,
    CustomTextBackend,
    CustomVideoBackend,
)
from lib.custom_provider.builtin_definitions import (
    BuiltinDefinitionError,
    declarative_display_name,
    declarative_family,
    declarative_request_path,
    declarative_video_capabilities,
    load_builtin_definitions,
)
from lib.custom_provider.comfyui.capabilities import (
    default_supported_durations,
    duration_is_fixed,
    frame_rate_is_missing,
    native_short_edge,
    size_is_fixed,
)
from lib.custom_provider.comfyui.comfyui_backend import ComfyuiVideoBackend, binding_video_capabilities
from lib.custom_provider.comfyui.comfyui_image_backend import ComfyuiImageBackend, binding_image_capabilities
from lib.custom_provider.comfyui.failures import ComfyuiError
from lib.custom_provider.declarative_backend import (
    REQUEST_SECTIONS,
    DeclarativeVideoBackend,
    request_urls,
    strips_base_url_version,
)
from lib.custom_provider.declarative_image_backend import DeclarativeImageBackend, image_capabilities_from_definition
from lib.custom_provider.path_overrides import (
    NO_PATH_OVERRIDES,
    BaseUrlRule,
    EndpointRequest,
    PathOverrideLayer,
    PathOverrides,
    path_override_layer,
    placeholder_names,
)

if TYPE_CHECKING:
    from lib.db.models.custom_provider import CustomProvider


#: Family identifier of user-defined endpoints in the catalog. Built-in endpoints' family points to the protocol origin (openai / kling …);
#: a user endpoint's protocol is described by the definition itself, and there is no external family to attribute it to.
#: catalog 里用户自定义端点的家族标识。内置端点的 family 指向协议出处（openai / kling …），
#: 用户端点的协议由定义自身描述，没有可归属的外部家族。
CUSTOM_ENDPOINT_FAMILY = "custom"


# ── EndpointSpec data type ── / ── EndpointSpec 数据类型 ───────────────────────────────────────────


class BackendBuilder(Protocol):
    """The endpoint's backend construction closure.

    ``path_overrides`` are the path overrides in effect for this model (``docs/adr/0101``), all keys in
    :attr:`EndpointSpec.requests`; endpoints that declare no requests always receive an empty mapping.

    endpoint 的 backend 构造闭包。

    ``path_overrides`` 是该模型生效的路径覆盖（``docs/adr/0101``），键都在
    :attr:`EndpointSpec.requests` 里；没有声明请求的端点恒收到空映射。
    """

    def __call__(
        self, provider: CustomProvider, model_id: str, path_overrides: PathOverrides = ..., /
    ) -> CustomTextBackend | CustomImageBackend | CustomVideoBackend | CustomAudioBackend: ...


@dataclass(frozen=True)
class EndpointSpec:
    """Metadata of a single endpoint + backend construction closure. / 单条 endpoint 的元数据 + backend 构造闭包。"""

    key: str  # "openai-chat"
    media_type: str  # "text" | "image" | "video" | "audio"
    family: str  # "openai" | "google" | "newapi"
    # Frontend i18n key (dashboard ns). Display names of declarative endpoints (bundled and user-defined) are in the definition's
    # meta.name and not in the i18n catalog; this key is set to an empty string and the name comes from display_name.
    # 前端 i18n key（dashboard ns）。声明式端点（随版与用户自定义）的显示名写在定义的 meta.name
    # 里、不进 i18n 目录，此键置空串，取名走 display_name。
    display_name_key: str
    request_method: str  # "POST"
    request_path_template: str  # "/v1/chat/completions", may contain placeholders like {model} / "/v1/chat/completions"，可含 {model} 等占位
    build_backend: BackendBuilder
    # Endpoint source: built-in (shipped with the release, cannot be edited or deleted) or user-defined (stored in the custom_endpoint table).
    # 端点来源：内置（随版发布，不可编辑删除）或用户自定义（落 custom_endpoint 表）。
    source: Literal["builtin", "custom"] = "builtin"
    # only filled for image endpoints; omitted for others
    image_capabilities: frozenset[ImageCapability] | None = None  # image 类才填，非 image 类省略
    # Reference image limit for a single reference-to-video call; only meaningful for video endpoints.
    # Explicit int: passed down as is as a hard constraint (0 means no reference images; the executor trims references to 0 accordingly).
    # None: undeclared: when one endpoint has several models with different capacities the endpoint level can't give an exact
    # number, and the resolver calls video_caps_for_model to read that model's real limit by model_id.
    # 单次参考生视频调用的参考图上限；仅 video 类有意义。
    # 显式 int：原样下传作为硬约束（0 表示不接受参考图，executor 据此将 references 裁剪为 0 张）。
    # None：未声明 —— 一个 endpoint 多 model、容量不同时 endpoint 维度给不出准数，由 resolver
    # 调 video_caps_for_model 按 model_id 读取该 model 的真实上限。
    video_max_reference_images: int | None = None
    # When video_max_reference_images is None, the resolver uses this pure function to read the caps the backend declares by
    # model_id, without building an SDK client or querying the provider row. When video_max_reference_images is an int this field
    # should be None (the endpoint level already gives a hard limit). Exactly one of the two is filled for each video endpoint (see the invariant at the end of the registry).
    # 当 video_max_reference_images 为 None 时，resolver 用此纯函数按 model_id 读 backend 声明的
    # caps —— 不构造 SDK client、不查 provider 行。video_max_reference_images 为 int 时此字段应为
    # None（endpoint 维度已能给出硬上限）。二者对每个 video endpoint 恰填其一（见注册表末尾不变式）。
    video_caps_for_model: Callable[[str], VideoCapabilities] | None = None
    # Whether this endpoint's delegate.generate() actually reads VideoGenerationRequest.end_image and passes down the end frame
    # constraint. Only meaningful for video; when False, even if system judgement or user override sets last_frame to True, the
    # execution layer silently drops the end frame and generates unconstrained; write-side last_frame overrides narrow the set of enableable endpoints by this.
    # 该 endpoint 的 delegate.generate() 是否真的读取 VideoGenerationRequest.end_image 并下传
    # 尾帧约束。仅 video 类有意义；False 时即便系统判定或用户覆盖把 last_frame 置为 True，执行层
    # 也会静默丢弃尾帧、按无约束生成——写入侧 last_frame 覆盖据此收窄可开启的 endpoint 范围。
    end_image_capable: bool = False
    # Same as end_image_capable: whether this endpoint's delegate.generate() actually reads
    # VideoGenerationRequest.reference_audio_files and assembles it into the provider request. Only meaningful for video;
    # when False, overriding reference_audio_mode to direct only distorts the capability declaration, and the execution layer still sends no voice input.
    # 同构于 end_image_capable：该 endpoint 的 delegate.generate() 是否真的读取
    # VideoGenerationRequest.reference_audio_files 并组装进供应商请求。仅 video 类有意义；
    # False 时把 reference_audio_mode 覆盖为 direct 只会让能力声明失真，执行层照旧不带音色输入。
    reference_audio_capable: bool = False
    # The whole JSON of a declarative definition: bundled definitions are read from builtin_endpoints/, user definitions from the custom_endpoint table.
    # None for Python-implemented endpoints. Non-None means this endpoint's call form is described by this definition; the fields
    # above are all mirrors derived from it, and this field always wins.
    # 声明式定义的整份 JSON：随版定义读自 builtin_endpoints/，用户定义读自 custom_endpoint 表。
    # Python 实现的 endpoint 为 None。非 None 即该 endpoint 的调用形态由这份定义描述，上面各字段
    # 都是从它派生出来的镜像，取值一律以本字段为准。
    definition: Mapping[str, Any] | None = None
    # Requests with overridable paths (docs/adr/0101): path overrides only accept request names listed here. Empty tuple = this
    # endpoint does not open path overrides (ComfyUI endpoints, and Python endpoints that declare no requests).
    # 可覆盖路径的请求（docs/adr/0101）：路径覆盖只接受这里列出的请求名。空元组 = 该端点不开放
    # 路径覆盖（ComfyUI 端点，以及没有声明请求的 Python 端点）。
    requests: tuple[EndpointRequest, ...] = ()
    # The wrapped built-in call channel class (filled only for Python-implemented video and image endpoints): pricing reads the metering
    # declaration on its class, ``metered_usage`` for video and ``estimate_usage`` for image estimates (see :mod:`lib.custom_provider.metering`).
    # Must be the same class as the delegate ``build_backend`` builds; guarded by the endpoint registry tests. None = metered per definition or generically.
    # 被包装的内置调用通道类（Python 实现的视频与图片端点才填）：计价读它类上的计量声明，视频取
    # ``metered_usage``、图片预估取 ``estimate_usage``（见 :mod:`lib.custom_provider.metering`）。须与
    # ``build_backend`` 构造的 delegate 同类，端点注册表测试守着。None = 按定义或通用口径计量。
    metering_backend: type | None = None

    @property
    def kind(self) -> str:
        """Implementation form: the definition's ``kind`` when there is a definition, ``python`` for endpoints implemented by a Python backend.

        ``python`` is not a definition container kind: there is no definition to read; it is the name of "having no definition" itself.

        实现形态：有定义时取定义的 ``kind``，Python backend 实现的端点为 ``python``。

        ``python`` 不是定义容器的 kind——它没有定义可读，是「没有定义」这件事本身的名字。
        """
        return "python" if self.definition is None else str(self.definition["kind"])

    @property
    def duration_tier_optional(self) -> bool:
        """Whether this endpoint's ``supported_durations`` may be an empty set.

        A ComfyUI endpoint's duration may be entirely not driven by ArcReel (``docs/adr/0082``): when ``frames`` is unbound or the
        frame rate source can't be read, the workflow outputs its own single tier, and an empty tier set is an honest declaration,
        not something missing. Other endpoints follow ADR 0018: an empty set is a config defect, failing loud at resolve time to send the user back to the config page.

        The criterion hangs on the spec rather than leaving each consumer to compare ``kind``: capability resolution is in the
        ``lib.config`` layer, which per the layering contract cannot reach the module holding the ``kind`` constants, while reading
        an attribute on an already resolved spec needs no import.

        该端点的 ``supported_durations`` 允不允许是空集。

        ComfyUI 端点的时长可以整维不由 ArcReel 驱动（``docs/adr/0082``）：``frames`` 未绑定或读不到
        帧率来源时，这份 workflow 出它自己那一档，档位空集是如实的声明而非缺失。其余端点照 ADR 0018
        办——空集即配置缺陷，解析期 fail loud 把用户引回配置页。

        判据挂在 spec 上而不是留给各消费方比 ``kind``：能力解析在 ``lib.config`` 层，那一层按分层
        契约够不到 ``kind`` 常量所在的模块，取一个已解析好的 spec 上的属性则无需 import。
        """
        return self.kind == COMFYUI_KIND

    @property
    def size_fixed(self) -> bool:
        """Whether the size dimension is fixed by the endpoint, so aspect ratio and resolution choices have no effect on it.

        True only for ComfyUI endpoints: when width and height are not both bound to nodes ArcReel can't drive size; the UI then
        disables the resolution selector and says so, instead of accepting the user's choice and dropping it at fill time. Other
        endpoints' size is always decided by request parameters; there is no "fixed" form.

        尺寸这一维是不是由端点固定、比例与分辨率选择对它无效。

        只有 ComfyUI 端点会为真：宽高两侧不都有节点绑定时 ArcReel 驱动不动尺寸，界面据此禁用
        分辨率选择器并明示，而不是照收用户的选择再在填值期丢掉。其余端点的尺寸一律由请求参数
        决定，没有「固定」这一形态。
        """
        return self.definition is not None and self.kind == COMFYUI_KIND and size_is_fixed(self.definition["bindings"])

    @property
    def duration_fixed(self) -> bool:
        """Whether the duration dimension is fixed by the endpoint: true on ComfyUI endpoints when ``frames`` is unbound.

        A different thing from :attr:`duration_tier_optional`: that one says "whether tiers may be an empty set", this one says
        "whether the user can edit tiers". With ``frames`` bound but no frame rate source the tiers are also empty, but that is a
        fixable definition (add an ``fps`` binding or enter a frame rate), not a workflow whose duration is inherently fixed.

        时长这一维是不是由端点固定：ComfyUI 端点上 ``frames`` 未绑定即为真。

        与 :attr:`duration_tier_optional` 是两件事：后者说的是「档位允不允许是空集」，本属性说的
        是「用户能不能编辑档位」。绑了 ``frames`` 却读不到帧率来源时档位同样是空集，但那是一份
        可修的定义（补一个 ``fps`` 绑定或手填帧率），不是这份 workflow 的时长天生固定。
        """
        return (
            self.definition is not None and self.kind == COMFYUI_KIND and duration_is_fixed(self.definition["bindings"])
        )

    @property
    def duration_frame_rate_missing(self) -> bool:
        """Whether the empty tiers are caused by "no frame rate source": ``frames`` is bound but there is neither an ``fps`` binding nor a manually entered frame rate.

        Like :attr:`duration_fixed` it only picks the message and is not part of the read-only / disabled criterion (that always
        uses :attr:`duration_tier_empty`). Both false while tiers are still empty means the third branch: frames bound and frame rate
        readable, but no whole-second duration that can be written back as is comes out of the conversion; that branch is neither
        "inherently fixed" nor fixable by adding a frame rate.

        档位为空的成因是不是「读不到帧率来源」：``frames`` 绑了却既无 ``fps`` 绑定也没手填帧率。

        与 :attr:`duration_fixed` 一样只挑文案，不参与只读 / 禁用判据（那一律取
        :attr:`duration_tier_empty`）。两位都为假而档位仍为空，说的是第三支：帧数已绑定、帧率也
        读得到，只是换算不出一档能原样写回的整秒时长——那一支既不是「天生固定」也补不出帧率来。
        """
        return self.definition is not None and self.kind == COMFYUI_KIND and frame_rate_is_missing(self.definition)

    @property
    def endpoint_durations(self) -> list[int] | None:
        """The endpoint's own duration tiers; ``None`` when tiers are not up to the endpoint.

        Only ComfyUI video endpoints can give them: derived from the binding table, the same for every model row on this endpoint.
        Other endpoints' tiers are declared on the model row / registry, and this property does not overstep.

        An empty list and ``None`` are two things: an empty list says "this endpoint did answer, and the answer is it can't drive this
        dimension"; ``None`` says "this question should not be asked of the endpoint".

        端点自己那一份时长档位；档位不由端点说了算时为 ``None``。

        只有 ComfyUI 视频端点给得出——它由绑定表推导，对每个挂在这个端点上的模型行都是同一份。
        其余端点的档位声明在模型行 / 注册表上，本属性不越俎代庖。

        空列表与 ``None`` 是两件事：空列表说「这个端点确实答了，答案是这一维它驱动不了」，
        ``None`` 说「这个问题不该问端点」。
        """
        if self.definition is None or self.kind != COMFYUI_KIND or self.media_type != "video":
            return None
        return default_supported_durations(self.definition)

    @property
    def duration_tier_empty(self) -> bool:
        """The duration dimension cannot give any tiers at all: true on ComfyUI endpoints when the native duration cannot be derived.

        All three branches land here: ``frames`` unbound (no literal frame count), ``frames`` bound but no frame rate source (neither
        an ``fps`` read-only binding nor a manually entered fps on the entry), and frame rate readable but no whole-second tier from
        the conversion. The UI's read-only and disabled criterion is this property, not :attr:`duration_fixed`: what the user can't
        edit is "tiers are empty"; which sentence is told to the user is picked by :attr:`duration_fixed` and
        :attr:`duration_frame_rate_missing`: "duration is inherently fixed", "no frame rate provided", and when both are false
        "frame count and frame rate are present, but no whole-second tier comes out of the conversion".

        时长这一维根本给不出档位：ComfyUI 端点上原生时长推不出来即为真。

        三支都落在这里——``frames`` 未绑定（读不到字面帧数）、绑了 ``frames`` 却没有帧率来源
        （既无 ``fps`` 只读绑定、也无条目手填 fps），以及帧率读得到但换算不出整秒档位。界面的
        只读与禁用判据是本属性而不是 :attr:`duration_fixed`：用户编不动的是「档位为空」这件事，
        说给用户听的是哪一句则由 :attr:`duration_fixed` 与 :attr:`duration_frame_rate_missing`
        挑——「时长天生固定」、「没提供帧率」，两位都为假时是「帧数与帧率都在、换算不出整秒档位」。
        """
        return self.endpoint_durations == []

    @property
    def native_resolution(self) -> str | None:
        """The tier this workflow actually outputs when no resolution tier is chosen; None for non-ComfyUI endpoints or when the literal size can't be read.

        这份 workflow 不选分辨率档位时实际会出的那一档；非 ComfyUI 端点或读不出字面尺寸时 None。"""
        if self.definition is None or self.kind != COMFYUI_KIND:
            return None
        short_edge = native_short_edge(self.definition)
        if short_edge is None:
            return None
        tier_map = IMAGE_TIER_SHORT_EDGE if self.media_type == "image" else VIDEO_TIER_SHORT_EDGE
        return short_edge_to_resolution(short_edge, tier_map=tier_map)

    @property
    def display_name(self) -> str | None:
        """Display name of declarative endpoints (from ``meta.name``); None for Python built-ins, whose text comes from display_name_key.

        声明式端点的显示名（取 ``meta.name``）；Python 内置为 None，按 display_name_key 取文案。"""
        return None if self.definition is None else declarative_display_name(self.definition)


# ── build_backend closures of each endpoint ── / ── 各 endpoint 的 build_backend 闭包 ──────────────────────────────


def _build_openai_chat(provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES) -> CustomTextBackend:
    base_url = ensure_openai_base_url(provider.base_url)
    delegate = OpenAITextBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=path_override_layer(
            _OPENAI_CHAT_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomTextBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_openai_responses(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomTextBackend:
    base_url = ensure_openai_base_url(provider.base_url)
    delegate = OpenAIResponsesTextBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=path_override_layer(
            _OPENAI_RESPONSES_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomTextBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_gemini_generate(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomTextBackend:
    base_url = ensure_google_base_url(provider.base_url) or None
    delegate = GeminiTextBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=_google_path_layer(_GEMINI_GENERATE_REQUESTS, provider.base_url, model_id, path_overrides),
    )
    return CustomTextBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_anthropic_messages(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomTextBackend:
    # The SDK appends /v1/messages after base_url itself; here we only strip an endpoint suffix the user copied in whole, neither adding nor stripping /vN
    # SDK 自己在 base_url 后拼 /v1/messages，这里只剥掉用户整条复制进来的端点后缀，不补也不剥 /vN
    base_url = normalize_anthropic_base_url(provider.base_url or "") or None
    delegate = AnthropicTextBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=path_override_layer(
            _ANTHROPIC_MESSAGES_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomTextBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_openai_images(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    base_url = ensure_openai_base_url(provider.base_url)
    delegate = OpenAIImageBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=path_override_layer(
            (_OPENAI_IMAGES_GENERATE, _OPENAI_IMAGES_EDIT), path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_openai_images_generations(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    base_url = ensure_openai_base_url(provider.base_url)
    delegate = OpenAIImageBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        mode="generations_only",
        transport_layer=path_override_layer(
            (_OPENAI_IMAGES_GENERATE,), path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_openai_images_edits(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    base_url = ensure_openai_base_url(provider.base_url)
    delegate = OpenAIImageBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        mode="edits_only",
        transport_layer=path_override_layer(
            (_OPENAI_IMAGES_EDIT,), path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_openai_chat_image(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    base_url = ensure_openai_base_url(provider.base_url)
    delegate = OpenAIChatImageBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        provider_name=provider.provider_id,
        transport_layer=path_override_layer(
            _OPENAI_CHAT_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_openrouter_images(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    """OpenRouter unified /images endpoint (T2I + I2I).

    OpenRouter provides no /images/edits; I2I and T2I go through the same JSON endpoint; base_url already contains /api/v1, so
    ensure_openai_base_url's /v1 append is not done.

    OpenRouter 统一 /images 端点（T2I + I2I）。

    OpenRouter 不提供 /images/edits，I2I 与 T2I 同走一个 JSON 端点；base_url 已含 /api/v1，
    不做 ensure_openai_base_url 的 /v1 追加。
    """
    base_url = (provider.base_url or "").strip().rstrip("/") or None
    delegate = OpenRouterImageBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=path_override_layer(
            _OPENROUTER_IMAGES_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_gemini_image(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    base_url = ensure_google_base_url(provider.base_url) or None
    delegate = GeminiImageBackend(
        api_key=provider.api_key,
        base_url=base_url,
        image_model=model_id,
        transport_layer=_google_path_layer(_GEMINI_GENERATE_REQUESTS, provider.base_url, model_id, path_overrides),
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_openai_tts(provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES) -> CustomAudioBackend:
    base_url = ensure_openai_base_url(provider.base_url)
    # provider_name makes delegate logs and AudioSynthesisResult.provider attribute to the real provider,
    # consistent with the wrapper's .name accounting identity, rather than the built-in openai.
    # provider_name 让 delegate 日志与 AudioSynthesisResult.provider 归因到真实 provider，
    # 与包装层 .name 的记账身份一致，而非内置 openai。
    delegate = OpenAIAudioBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        provider_name=provider.provider_id,
        transport_layer=path_override_layer(
            _OPENAI_TTS_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomAudioBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_dashscope_tts(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomAudioBackend:
    # The backend derives /api/v1 from the host internally (tolerating with/without suffix); pass the raw base_url here, no duplicate normalization
    # backend 内部由 host 派生 /api/v1（容忍带/不带后缀），此处传原始 base_url 即可，不重复归一化
    layer = path_override_layer(
        _DASHSCOPE_GENERATION_REQUESTS,
        path_overrides,
        provider.base_url,
        dashscope_native_base_url(provider.base_url),
        model_id,
    )
    delegate = DashScopeAudioBackend(
        api_key=provider.api_key, base_url=provider.base_url, model=model_id, transport_layer=layer
    )
    return CustomAudioBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_minimax_tts(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomAudioBackend:
    # The backend normalizes base_url to {host}/v1 internally (tolerating host or a /v1 suffix); pass the raw base_url here
    # backend 内部把 base_url 归一化为 {host}/v1（容忍 host 或带 /v1 后缀），此处传原始 base_url 即可
    delegate = MiniMaxAudioBackend(
        api_key=provider.api_key,
        base_url=provider.base_url,
        model=model_id,
        provider_name=provider.provider_id,
        transport_layer=path_override_layer(
            _MINIMAX_TTS_REQUESTS,
            path_overrides,
            provider.base_url,
            minimax_text_base_url(provider.base_url),
            model_id,
        ),
    )
    return CustomAudioBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_elevenlabs_tts(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomAudioBackend:
    # YouReel: the backend tolerates the host with or without /v1; pass the raw base_url
    delegate = ElevenLabsAudioBackend(
        api_key=provider.api_key,
        base_url=provider.base_url,
        model=model_id,
        provider_name=provider.provider_id,
        transport_layer=path_override_layer(
            _ELEVENLABS_TTS_REQUESTS,
            path_overrides,
            provider.base_url,
            elevenlabs_base_url(provider.base_url),
            model_id,
        ),
    )
    return CustomAudioBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_gemini_tts(provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES) -> CustomAudioBackend:
    base_url = ensure_google_base_url(provider.base_url) or None
    delegate = GeminiAudioBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        provider_name=provider.provider_id,
        transport_layer=_google_path_layer(_GEMINI_GENERATE_REQUESTS, provider.base_url, model_id, path_overrides),
    )
    return CustomAudioBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_openai_video(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomVideoBackend:
    base_url = ensure_openai_base_url(provider.base_url)
    delegate = OpenAIVideoBackend(
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=path_override_layer(
            _OPENAI_VIDEO_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomVideoBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_gemini_video(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomVideoBackend:
    # Custom providers only use AI Studio mode (Vertex credentials don't go via base_url); the SDK appends api_version itself
    # 自定义供应商只走 AI Studio 模式（Vertex 凭证不经 base_url）；SDK 自己拼 api_version
    base_url = ensure_google_base_url(provider.base_url) or None
    delegate = GeminiVideoBackend(
        backend_type="aistudio",
        api_key=provider.api_key,
        base_url=base_url,
        video_model=model_id,
        transport_layer=_google_path_layer(_GEMINI_VIDEO_REQUESTS, provider.base_url, model_id, path_overrides),
    )
    return CustomVideoBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _python_requests(rule: BaseUrlRule, *requests: tuple[str, str, str]) -> tuple[EndpointRequest, ...]:
    """Request declarations of Python endpoints, each item (request name, display name key, default path); placeholders allowed are the model name and those already in the default path.

    Python 端点的请求声明，每项为（请求名、显示名 key、默认路径）；占位符放行模型名与默认路径里已有的。"""
    return tuple(
        EndpointRequest(
            name=name,
            display_name_key=display_name_key,
            default_path=path,
            placeholders=tuple(sorted({"model", *placeholder_names(path)})),
            base_url_rule=rule,
        )
        for name, display_name_key, path in requests
    )


# Requests sent by endpoints using direct httpx and the Ark SDK; download URLs taken from responses get no request name (docs/adr/0101).
# 直连 httpx 与方舟 SDK 的端点各自发出的请求；取自响应的下载地址不提供请求名（docs/adr/0101）。
_ARK_SEEDANCE_REQUESTS = _python_requests(
    "ensure_api_v3",
    ("submit", "cp_request_submit", "/contents/generations/tasks"),
    ("poll", "cp_request_poll", "/contents/generations/tasks/{{ task_id }}"),
)
# Ark Seedream text-to-image and multi-reference generation share one JSON interface (reference images in the image field); there is no /images/edits
# 方舟 Seedream 文生图与多参考图生成同走一条 JSON 接口（参考图放在 image 字段），没有 /images/edits
_ARK_SEEDREAM_REQUESTS = _python_requests("ensure_api_v3", ("generate", "cp_request_generate", "/images/generations"))
_VIDU_VIDEO_REQUESTS = _python_requests(
    "ensure_ent_v2",
    ("submit_text2video", "cp_request_submit_text2video", "/text2video"),
    ("submit_img2video", "cp_request_submit_img2video", "/img2video"),
    ("submit_start_end2video", "cp_request_submit_start_end2video", "/start-end2video"),
    ("submit_reference2video", "cp_request_submit_reference2video", "/reference2video"),
    ("poll", "cp_request_poll", "/tasks/{{ task_id }}/creations"),
)
_KLING_IMAGE_REQUESTS = _python_requests(
    "ensure_v1",
    ("submit", "cp_request_submit", "/images/generations"),
    ("poll", "cp_request_poll", "/images/generations/{{ task_id }}"),
)
_KLING_VIDEO_REQUESTS = _python_requests(
    "ensure_v1",
    ("submit_text2video", "cp_request_submit_text2video", "/videos/text2video"),
    ("poll_text2video", "cp_request_poll_text2video", "/videos/text2video/{{ task_id }}"),
    ("submit_image2video", "cp_request_submit_img2video", "/videos/image2video"),
    ("poll_image2video", "cp_request_poll_img2video", "/videos/image2video/{{ task_id }}"),
    ("submit_multi_image2video", "cp_request_submit_multi_image2video", "/videos/multi-image2video"),
    ("poll_multi_image2video", "cp_request_poll_multi_image2video", "/videos/multi-image2video/{{ task_id }}"),
)
_DASHSCOPE_GENERATION_REQUESTS = _python_requests(
    "dashscope_api_v1", ("generate", "cp_request_generate", "/services/aigc/multimodal-generation/generation")
)
_DASHSCOPE_ASYNC_VIDEO_REQUESTS = _python_requests(
    "dashscope_api_v1",
    ("submit", "cp_request_submit", "/services/aigc/video-generation/video-synthesis"),
    ("poll", "cp_request_poll", "/tasks/{{ task_id }}"),
)
_MINIMAX_IMAGE_REQUESTS = _python_requests("ensure_v1", ("generate", "cp_request_generate", "/image_generation"))
# The model catalog query reads parameter declarations per model on every generation and sits under base_url like generation,
# so when a relay changes paths both change together; it is not provider-level model discovery (the kind in docs/adr/0101 that
# is not overridden), so it is open to overrides too.
# 模型目录查询在每次生成时按模型读参数声明，与生成同挂在 base_url 下，中转站改路径时两条一起改；
# 它不是供应商层面的模型发现（docs/adr/0101 中不用覆盖的那一类），所以同样开放覆盖。
_OPENROUTER_IMAGES_REQUESTS = _python_requests(
    "as_is",
    ("generate", "cp_request_generate", OPENROUTER_IMAGES_PATH),
    ("model_catalog", "cp_request_model_catalog", OPENROUTER_MODELS_PATH),
)
_MINIMAX_TTS_REQUESTS = _python_requests("ensure_v1", ("generate", "cp_request_generate", "/t2a_v2"))
# YouReel: ElevenLabs puts the voice in the path; the backend adds the host, so the path is relative to it
_ELEVENLABS_TTS_REQUESTS = _python_requests(
    "strip_version", ("generate", "cp_request_generate", "/v1/text-to-speech/{{ voice }}")
)
# The SDK appends /v1beta itself after the base_url with /vN stripped / SDK 在剥去 /vN 的 base_url 后自己拼 /v1beta
_GEMINI_GENERATE_REQUESTS = _python_requests(
    "strip_version", ("generate", "cp_request_generate", "/v1beta/models/{{ model }}:generateContent")
)
_GEMINI_VIDEO_REQUESTS = _python_requests(
    "strip_version",
    ("submit", "cp_request_submit", "/v1beta/models/{{ model }}:predictLongRunning"),
    # operation names look like models/<model>/operations/<id>, containing /, expanded as is into a multi-level path
    # operation name 形如 models/<model>/operations/<id>，含 /，原样展开为多级路径
    ("poll", "cp_request_poll", "/v1beta/{{ task_id }}"),
    # The SDK takes the file id from the artifact uri and builds the download URL from base_url, so downloads are also relative to base_url
    # SDK 从产物 uri 取出文件 id，再按 base_url 拼下载地址，所以下载也相对 base_url
    ("download", "cp_request_download", "/v1beta/files/{{ file_id }}:download"),
)


def _google_path_layer(
    requests: tuple[EndpointRequest, ...], base_url: str | None, model_id: str, path_overrides: PathOverrides
) -> PathOverrideLayer | None:
    # Placeholders don't cross ``:``: operation names containing / are captured whole, and the SDK's method suffixes (:predictLongRunning, :download) remain distinguishable
    # 占位符不跨 ``:``：含 / 的 operation name 整段捕获，SDK 的方法后缀（:predictLongRunning、:download）仍区分得开
    return path_override_layer(
        requests, path_overrides, base_url, ensure_google_base_url(base_url), model_id, placeholder="[^:]+"
    )


# OpenAI SDK side: default paths are relative to the base_url with /v1 added by ensure_openai_base_url
# OpenAI SDK 一侧：默认路径相对 ensure_openai_base_url 补过 /v1 的 base_url
_OPENAI_CHAT_REQUESTS = _python_requests("append_v1", ("generate", "cp_request_generate", "/chat/completions"))
_OPENAI_RESPONSES_REQUESTS = _python_requests("append_v1", ("generate", "cp_request_generate", "/responses"))
_OPENAI_IMAGES_GENERATE, _OPENAI_IMAGES_EDIT = _python_requests(
    "append_v1",
    ("generate", "cp_request_generate", "/images/generations"),
    ("edit", "cp_request_edit", "/images/edits"),
)
_OPENAI_TTS_REQUESTS = _python_requests("append_v1", ("generate", "cp_request_generate", "/audio/speech"))
_OPENAI_VIDEO_REQUESTS = _python_requests(
    "append_v1",
    ("submit", "cp_request_submit", "/videos"),
    ("poll", "cp_request_poll", "/videos/{{ task_id }}"),
    ("download", "cp_request_download", "/videos/{{ task_id }}/content"),
)
# The anthropic SDK appends /v1/messages after base_url itself; base_url only has its trailing slash (and a whole copied-in /v1/messages) removed
# anthropic SDK 自己在 base_url 后拼 /v1/messages，base_url 只去尾斜杠（和整条复制进来的 /v1/messages）
_ANTHROPIC_MESSAGES_REQUESTS = _python_requests(
    "strip_messages", ("generate", "cp_request_generate", ANTHROPIC_MESSAGES_PATH)
)


def _ensure_url_path_suffix(base_url: str | None, suffix: str) -> str | None:
    """Complete the protocol's known mount path (ark /api/v3, vidu /ent/v2, kling /v1);
    trust it as is when the protocol path is already present, avoiding duplicate stacking. Reused by the ark/vidu/kling closures.

    A bare domain (no scheme, e.g. ``relay.example.com``) would be taken wholly as path by urlsplit, so ``https://`` is added
    before checking; otherwise a host-only config would get neither the scheme nor the path.

    补全协议已知挂载路径（ark /api/v3、vidu /ent/v2、kling /v1）；
    已带对应协议路径则原样信任，避免重复叠加。供 ark/vidu/kling 闭包复用。

    纯域名（无 scheme，如 ``relay.example.com``）会被 urlsplit 整体当作 path，
    先补 ``https://`` 再判定，否则 host-only 配置既补不上协议也挂不上路径。
    """
    s = (base_url or "").strip().rstrip("/")
    if not s:
        return None
    normalized = s if "://" in s else f"https://{s}"
    if urlsplit(normalized).path.rstrip("/").endswith(suffix):
        return normalized
    return normalized + suffix


def _build_ark_seedance(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomVideoBackend:
    base_url = _ensure_url_path_suffix(provider.base_url, "/api/v3")
    layer = path_override_layer(_ARK_SEEDANCE_REQUESTS, path_overrides, provider.base_url, base_url, model_id)
    delegate = ArkVideoBackend(api_key=provider.api_key, base_url=base_url, model=model_id, transport_layer=layer)
    return CustomVideoBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_ark_seedream(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    # Wrap the built-in ArkImageBackend directly: size derivation, reference image limit and encoding, and metering are the same as the preset Ark provider
    # 直接包装内置 ArkImageBackend：尺寸推导、参考图上限与编码、计量都与预置方舟供应商同一份
    base_url = _ensure_url_path_suffix(provider.base_url, "/api/v3")
    layer = path_override_layer(_ARK_SEEDREAM_REQUESTS, path_overrides, provider.base_url, base_url, model_id)
    delegate = ArkImageBackend(api_key=provider.api_key, base_url=base_url, model=model_id, transport_layer=layer)
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_vidu_video(provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES) -> CustomVideoBackend:
    base_url = _ensure_url_path_suffix(provider.base_url, "/ent/v2")
    layer = path_override_layer(_VIDU_VIDEO_REQUESTS, path_overrides, provider.base_url, base_url, model_id)
    delegate = ViduVideoBackend(api_key=provider.api_key, base_url=base_url, model=model_id, transport_layer=layer)
    return CustomVideoBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_dashscope_image(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    # The backend derives /api/v1 from the host internally (tolerating with/without suffix); pass the raw base_url here, no duplicate normalization
    # backend 内部由 host 派生 /api/v1（容忍带/不带后缀），此处传原始 base_url 即可，不重复归一化
    layer = path_override_layer(
        _DASHSCOPE_GENERATION_REQUESTS,
        path_overrides,
        provider.base_url,
        dashscope_native_base_url(provider.base_url),
        model_id,
    )
    delegate = DashScopeImageBackend(
        api_key=provider.api_key, base_url=provider.base_url, model=model_id, transport_layer=layer
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_dashscope_async_video(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomVideoBackend:
    layer = path_override_layer(
        _DASHSCOPE_ASYNC_VIDEO_REQUESTS,
        path_overrides,
        provider.base_url,
        dashscope_native_base_url(provider.base_url),
        model_id,
    )
    delegate = DashScopeVideoBackend(
        api_key=provider.api_key, base_url=provider.base_url, model=model_id, transport_layer=layer
    )
    return CustomVideoBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_minimax_image(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    # The backend normalizes base_url to {host}/v1 internally (tolerating host or a /v1 suffix); pass the raw base_url here
    # backend 内部把 base_url 归一化为 {host}/v1（容忍 host 或带 /v1 后缀），此处传原始 base_url 即可
    layer = path_override_layer(
        _MINIMAX_IMAGE_REQUESTS,
        path_overrides,
        provider.base_url,
        minimax_text_base_url(provider.base_url),
        model_id,
    )
    delegate = MiniMaxImageBackend(
        api_key=provider.api_key, base_url=provider.base_url, model=model_id, transport_layer=layer
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_kling_image(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomImageBackend:
    # Relay "proxying Kling as is": bearer mode bypasses the JWT manager and sends directly to Kling's native async image endpoint with the static api_key.
    # Only for a bare host is Kling's protocol mount path /v1 added (explicit paths are trusted as is); the native model_name is passed through, aliases not decoupled.
    # 中转站「原样代理可灵」：bearer 模式旁路 JWT 管理器，用静态 api_key 直发可灵原生异步图像端点。
    # 仅 host 时补全可灵协议挂载路径 /v1（含显式路径则原样信任）；原生 model_name 透传不解耦别名。
    base_url = _ensure_url_path_suffix(provider.base_url, "/v1")
    delegate = KlingImageBackend(
        auth_mode="bearer",
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=path_override_layer(
            _KLING_IMAGE_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


def _build_kling_video(
    provider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
) -> CustomVideoBackend:
    # Relay "proxying Kling as is": bearer mode bypasses the JWT manager and sends directly to Kling's native async video endpoint with the static api_key.
    # 中转站「原样代理可灵」：bearer 模式旁路 JWT 管理器，用静态 api_key 直发可灵原生异步视频端点。
    base_url = _ensure_url_path_suffix(provider.base_url, "/v1")
    delegate = KlingVideoBackend(
        auth_mode="bearer",
        api_key=provider.api_key,
        base_url=base_url,
        model=model_id,
        transport_layer=path_override_layer(
            _KLING_VIDEO_REQUESTS, path_overrides, provider.base_url, base_url, model_id
        ),
    )
    return CustomVideoBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)


# ── ENDPOINT_REGISTRY registry ── / ── ENDPOINT_REGISTRY 注册表 ───────────────────────────────────────


ENDPOINT_REGISTRY: dict[str, EndpointSpec] = {
    "openai-chat": EndpointSpec(
        key="openai-chat",
        media_type="text",
        family="openai",
        display_name_key="endpoint_openai_chat_display",
        request_method="POST",
        request_path_template="/v1/chat/completions",
        build_backend=_build_openai_chat,
        requests=_OPENAI_CHAT_REQUESTS,
    ),
    "openai-responses": EndpointSpec(
        key="openai-responses",
        media_type="text",
        family="openai",
        display_name_key="endpoint_openai_responses_display",
        request_method="POST",
        request_path_template="/v1/responses",
        build_backend=_build_openai_responses,
        requests=_OPENAI_RESPONSES_REQUESTS,
    ),
    "gemini-generate": EndpointSpec(
        key="gemini-generate",
        media_type="text",
        family="google",
        display_name_key="endpoint_gemini_generate_display",
        request_method="POST",
        request_path_template="/v1beta/models/{model}:generateContent",
        build_backend=_build_gemini_generate,
        requests=_GEMINI_GENERATE_REQUESTS,
    ),
    "anthropic-messages": EndpointSpec(
        key="anthropic-messages",
        media_type="text",
        family="anthropic",
        display_name_key="endpoint_anthropic_messages_display",
        request_method="POST",
        request_path_template=ANTHROPIC_MESSAGES_PATH,
        build_backend=_build_anthropic_messages,
        requests=_ANTHROPIC_MESSAGES_REQUESTS,
    ),
    "openai-images": EndpointSpec(
        key="openai-images",
        media_type="image",
        family="openai",
        display_name_key="endpoint_openai_images_display",
        request_method="POST",
        # /generations and /edits are dispatched automatically by whether reference images are passed; the braces express both paths
        # /generations 与 /edits 由是否传参考图自动派发，brace 表达两条路径
        request_path_template="/v1/images/{generations,edits}",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_openai_images,
        requests=(_OPENAI_IMAGES_GENERATE, _OPENAI_IMAGES_EDIT),
        metering_backend=OpenAIImageBackend,
    ),
    "openai-images-generations": EndpointSpec(
        key="openai-images-generations",
        media_type="image",
        family="openai",
        display_name_key="endpoint_openai_images_generations_display",
        request_method="POST",
        request_path_template="/v1/images/generations",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE}),
        build_backend=_build_openai_images_generations,
        requests=(_OPENAI_IMAGES_GENERATE,),
        metering_backend=OpenAIImageBackend,
    ),
    "openai-images-edits": EndpointSpec(
        key="openai-images-edits",
        media_type="image",
        family="openai",
        display_name_key="endpoint_openai_images_edits_display",
        request_method="POST",
        request_path_template="/v1/images/edits",
        image_capabilities=frozenset({ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_openai_images_edits,
        requests=(_OPENAI_IMAGES_EDIT,),
        metering_backend=OpenAIImageBackend,
    ),
    "openai-chat-image": EndpointSpec(
        key="openai-chat-image",
        media_type="image",
        family="openai",
        display_name_key="endpoint_openai_chat_image_display",
        request_method="POST",
        # Same API as the text openai-chat, keyed separately by media type; not part of infer_endpoint auto-inference
        # 与文本的 openai-chat 同一个 API，按媒体类型分键；不参与 infer_endpoint 自动推断
        request_path_template="/v1/chat/completions",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_openai_chat_image,
        requests=_OPENAI_CHAT_REQUESTS,
        metering_backend=OpenAIChatImageBackend,
    ),
    "openrouter-images": EndpointSpec(
        key="openrouter-images",
        media_type="image",
        family="openrouter",
        display_name_key="endpoint_openrouter_images_display",
        request_method="POST",
        # OpenRouter unified image endpoint: T2I and I2I both hit /images (no /images/edits)
        # OpenRouter 统一图片端点：T2I 与 I2I 同 hit /images（不提供 /images/edits）
        request_path_template="/images",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_openrouter_images,
        requests=_OPENROUTER_IMAGES_REQUESTS,
        metering_backend=OpenRouterImageBackend,
    ),
    "gemini-image": EndpointSpec(
        key="gemini-image",
        media_type="image",
        family="google",
        display_name_key="endpoint_gemini_image_display",
        request_method="POST",
        request_path_template="/v1beta/models/{model}:generateContent",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_gemini_image,
        requests=_GEMINI_GENERATE_REQUESTS,
        metering_backend=GeminiImageBackend,
    ),
    "openai-video": EndpointSpec(
        key="openai-video",
        media_type="video",
        family="openai",
        display_name_key="endpoint_openai_video_display",
        request_method="POST",
        request_path_template="/v1/videos",
        build_backend=_build_openai_video,
        requests=_OPENAI_VIDEO_REQUESTS,
        metering_backend=OpenAIVideoBackend,
        # OpenAI Sora input_reference is a single first-frame image. / OpenAI Sora input_reference 为单张首帧图。
        video_max_reference_images=1,
    ),
    "ark-seedance": EndpointSpec(
        key="ark-seedance",
        media_type="video",
        family="ark",
        display_name_key="endpoint_ark_seedance_display",
        request_method="POST",
        request_path_template="/api/v3/contents/generations/tasks",
        build_backend=_build_ark_seedance,
        requests=_ARK_SEEDANCE_REQUESTS,
        metering_backend=ArkVideoBackend,
        video_caps_for_model=ArkVideoBackend.video_capabilities_for_model,
        end_image_capable=True,
        # _create_task assembles audio_url + role: reference_audio per segment for reference_audio_files
        # _create_task 为 reference_audio_files 逐段组装 audio_url + role: reference_audio
        reference_audio_capable=True,
    ),
    "ark-seedream": EndpointSpec(
        key="ark-seedream",
        media_type="image",
        family="ark",
        display_name_key="endpoint_ark_seedream_display",
        request_method="POST",
        # Text-to-image and generation with reference images both go via /images/generations; reference images go into the JSON image field in Ark format
        # 文生图与带参考图生成同走 /images/generations，参考图按方舟格式放进 JSON 的 image 字段
        request_path_template="/api/v3/images/generations",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_ark_seedream,
        requests=_ARK_SEEDREAM_REQUESTS,
        metering_backend=ArkImageBackend,
    ),
    "vidu-video": EndpointSpec(
        key="vidu-video",
        media_type="video",
        family="vidu",
        display_name_key="endpoint_vidu_video_display",
        request_method="POST",
        request_path_template="/ent/v2/img2video",
        build_backend=_build_vidu_video,
        requests=_VIDU_VIDEO_REQUESTS,
        metering_backend=ViduVideoBackend,
        video_caps_for_model=ViduVideoBackend.video_capabilities_for_model,
        end_image_capable=True,
    ),
    "dashscope-image": EndpointSpec(
        key="dashscope-image",
        media_type="image",
        family="dashscope",
        display_name_key="endpoint_dashscope_image_display",
        request_method="POST",
        request_path_template="/api/v1/services/aigc/multimodal-generation/generation",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_dashscope_image,
        requests=_DASHSCOPE_GENERATION_REQUESTS,
        metering_backend=DashScopeImageBackend,
    ),
    "openai-tts": EndpointSpec(
        key="openai-tts",
        media_type="audio",
        family="openai",
        display_name_key="endpoint_openai_tts_display",
        request_method="POST",
        request_path_template="/v1/audio/speech",
        build_backend=_build_openai_tts,
        requests=_OPENAI_TTS_REQUESTS,
    ),
    "dashscope-tts": EndpointSpec(
        key="dashscope-tts",
        media_type="audio",
        family="dashscope",
        display_name_key="endpoint_dashscope_tts_display",
        request_method="POST",
        request_path_template="/api/v1/services/aigc/multimodal-generation/generation",
        build_backend=_build_dashscope_tts,
        requests=_DASHSCOPE_GENERATION_REQUESTS,
    ),
    "gemini-video": EndpointSpec(
        key="gemini-video",
        media_type="video",
        family="google",
        display_name_key="endpoint_gemini_video_display",
        request_method="POST",
        request_path_template="/v1beta/models/{model}:predictLongRunning",
        build_backend=_build_gemini_video,
        requests=_GEMINI_VIDEO_REQUESTS,
        metering_backend=GeminiVideoBackend,
        # Reference image limit and audio track form are read per model from the backend's pure caps function (no client built)
        # 参考图上限、音轨形态按 model 读 backend 纯 caps 函数（不构造 client）
        video_caps_for_model=GeminiVideoBackend.video_capabilities_for_model,
        # generate() passes end_image down as config.last_frame / generate() 把 end_image 下传为 config.last_frame
        end_image_capable=True,
    ),
    "minimax-tts": EndpointSpec(
        key="minimax-tts",
        media_type="audio",
        family="minimax",
        display_name_key="endpoint_minimax_tts_display",
        request_method="POST",
        request_path_template="/v1/t2a_v2",
        build_backend=_build_minimax_tts,
        requests=_MINIMAX_TTS_REQUESTS,
    ),
    # YouReel (owner 2026-11-17): ElevenLabs speech, base URL https://api.elevenlabs.io, key from the account
    "elevenlabs-tts": EndpointSpec(
        key="elevenlabs-tts",
        media_type="audio",
        family="elevenlabs",
        display_name_key="endpoint_elevenlabs_tts_display",
        request_method="POST",
        request_path_template="/v1/text-to-speech/{voice}",
        build_backend=_build_elevenlabs_tts,
        requests=_ELEVENLABS_TTS_REQUESTS,
    ),
    "gemini-tts": EndpointSpec(
        key="gemini-tts",
        media_type="audio",
        family="google",
        display_name_key="endpoint_gemini_tts_display",
        request_method="POST",
        request_path_template="/v1beta/models/{model}:generateContent",
        build_backend=_build_gemini_tts,
        requests=_GEMINI_GENERATE_REQUESTS,
    ),
    "dashscope-async-video": EndpointSpec(
        key="dashscope-async-video",
        media_type="video",
        family="dashscope",
        display_name_key="endpoint_dashscope_async_video_display",
        request_method="POST",
        request_path_template="/api/v1/services/aigc/video-generation/video-synthesis",
        build_backend=_build_dashscope_async_video,
        requests=_DASHSCOPE_ASYNC_VIDEO_REQUESTS,
        metering_backend=DashScopeVideoBackend,
        # Multiple models (happyhorse-r2v=9 / wan2.7-r2v=5) have different capacities → no int cap declared at endpoint level,
        # read the backend caps per model (no client built).
        # 多 model（happyhorse-r2v=9 / wan2.7-r2v=5）容量不同 → endpoint 维度不声明 int cap，
        # 按 model 读 backend caps（不构造 client）。
        video_caps_for_model=DashScopeVideoBackend.video_capabilities_for_model,
        # _build_media attaches reference_audio_files per segment to reference_voice on the reference asset items
        # _build_media 把 reference_audio_files 逐段挂到参考素材项的 reference_voice 上
        reference_audio_capable=True,
    ),
    "minimax-image": EndpointSpec(
        key="minimax-image",
        media_type="image",
        family="minimax",
        display_name_key="endpoint_minimax_image_display",
        request_method="POST",
        request_path_template="/image_generation",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_minimax_image,
        requests=_MINIMAX_IMAGE_REQUESTS,
        metering_backend=MiniMaxImageBackend,
    ),
    "kling-image": EndpointSpec(
        key="kling-image",
        media_type="image",
        family="kling",
        display_name_key="endpoint_kling_image_display",
        request_method="POST",
        request_path_template="/v1/images/generations",
        image_capabilities=frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}),
        build_backend=_build_kling_image,
        requests=_KLING_IMAGE_REQUESTS,
        metering_backend=KlingImageBackend,
    ),
    "kling-video": EndpointSpec(
        key="kling-video",
        media_type="video",
        family="kling",
        display_name_key="endpoint_kling_video_display",
        request_method="POST",
        # No first frame → text2video, first frame → image2video (with optional end frame), multi-image subjects → multi-image2video (R2V)
        # 无首帧走 text2video、有首帧走 image2video（含可选尾帧）、有多图主体走 multi-image2video（R2V）
        request_path_template="/v1/videos/{text2video,image2video,multi-image2video}",
        build_backend=_build_kling_video,
        requests=_KLING_VIDEO_REQUESTS,
        metering_backend=KlingVideoBackend,
        # The reference image limit varies by model (v3-omni / video-o1 multi-image subject R2V max=4, the rest are first/end frame with no references, 0) → no
        # int cap declared at endpoint level; read the backend's pure caps function per model.
        # 参考图上限随 model 异质（v3-omni / video-o1 多图主体 R2V max=4，其余首尾帧无参考为 0）→ 不在
        # endpoint 维度声明 int cap，按 model 读 backend 纯 caps 函数。
        video_caps_for_model=KlingVideoBackend.video_capabilities_for_model,
        end_image_capable=True,
    ),
}


def validate_video_caps_declaration(spec: EndpointSpec) -> None:
    """Validate a single spec's reference image limit source: caps_fn, if declared, must be callable; a video endpoint must have
    exactly one of "int cap" XOR "caps_fn not None", with a non-negative int cap; non-video endpoints have both None.
    Misconfigurations (caps_fn set to a non-callable, a multi-model shared endpoint missing caps_fn, both declared, or a negative
    cap declared) fail fast at construction rather than waiting for the resolver to raise at request time.

    The built-in registry passes this check entry by entry at import time; declarative endpoints pass it right after being built
    in declarative_endpoint_spec; specs from every source go through the same invariant, so the capability layer need not care where a spec came from.

    校验单条 spec 的参考图上限来源：caps_fn 若声明必须可调用；video endpoint 必须「int cap」
    XOR「caps_fn 非 None」恰一、且 int cap 非负；非 video endpoint 两者皆 None。misconfig（caps_fn
    填成非 callable、多 model 共享端点漏配 caps_fn、同时声明二者、或声明负数 cap）在构造处
    fail-fast，而非等到 request 期 resolver 才抛。

    内置注册表在 import 期逐条过此校验；声明式端点在 declarative_endpoint_spec 里构造完即过这里
    ——各来源的 spec 走同一条不变式，能力判定层因此不必区分 spec 从哪来。
    """
    key = spec.key
    cap = spec.video_max_reference_images
    caps_fn = spec.video_caps_for_model
    has_int = cap is not None
    # The resolver runs it as caps_fn(model_id), so it must be callable. Non-empty non-callable values like strings/integers must be
    # blocked at import time rather than let through to blow up in the resolver at request time, in line with this function's fail-fast intent.
    # resolver 会以 caps_fn(model_id) 执行它，故必须是 callable。误填字符串/整数等非空非 callable
    # 值要在 import 期就挡掉，而非放行到请求期才在 resolver 里炸——与本函数的 fail-fast 初衷一致。
    if caps_fn is not None and not callable(caps_fn):
        raise ValueError(f"endpoint {key!r} declares non-callable video_caps_for_model: {caps_fn!r}")
    has_fn = callable(caps_fn)
    if spec.media_type != "video" and spec.reference_audio_capable:
        raise ValueError(f"non-video endpoint {key!r} must not declare reference_audio_capable")
    if spec.media_type == "video":
        if has_int == has_fn:
            raise ValueError(
                f"video endpoint {key!r} must declare exactly one of video_max_reference_images "
                f"(int) or video_caps_for_model (callable), got "
                f"video_max_reference_images={cap!r}, "
                f"video_caps_for_model={caps_fn!r}"
            )
        if cap is not None and cap < 0:
            # The int cap is a hard limit on reference image count; a negative number downstream would act as a negative slice references[:-1],
            # wrongly dropping the last image instead of trimming to 0 → blocked at construction, so the resolver's int branch always gets a valid non-negative number.
            # int cap 是参考图张数硬上限；负数到了下游会被当负切片 references[:-1] 误丢最后一张
            # 而非裁成 0 张 → 构造期挡掉，保证 resolver int 分支取到的恒为合法非负数。
            raise ValueError(f"video endpoint {key!r} declares negative video_max_reference_images: {cap}")
    elif has_int or has_fn:
        raise ValueError(
            f"non-video endpoint {key!r} must not declare video caps, got "
            f"video_max_reference_images={cap!r}, "
            f"video_caps_for_model={caps_fn!r}"
        )


#: Match only the placeholder itself: a hard-coded address that happens to contain the text "base_url" (e.g. https://api.test/base_url/video)
#: does not require the provider to fill in an address.
#: 只认占位符本身：写死的地址里恰好含 "base_url" 字样（如 https://api.test/base_url/video）
#: 不需要供应商填地址。
_BASE_URL_PLACEHOLDER = re.compile(r"\{\{\s*base_url\s*\}\}")


def declarative_requires_base_url(definition: Mapping[str, Any]) -> bool:
    """Whether this definition requires the provider to have filled in base_url.

    All three request section URLs are checked, not just submit: a definition whose submit hard-codes an absolute address while
    only poll references base_url is valid, and checking only submit would let such endpoints run until after the paid submit
    succeeded, only to fail when poll renders a relative address without a scheme.

    The assembly closure and callers (e.g. input validation of the test-connection route) share this one check: the assembly
    layer raises a ValueError without an i18n key, so the user-facing path must give a translatable rejection by this check before assembly.

    这份定义是否要求供应商填了 base_url。

    三节请求 URL 逐个查，不只看提交：提交写死绝对地址、轮询才引用 base_url 的定义是合法的，
    只查提交会让这类端点跑到付费提交成功之后，才在轮询渲染出一个没有协议的相对地址上失败。

    装配闭包与调用方（如测试连接路由的入参校验）共用这一条判定：装配层抛的是不带 i18n key
    的 ValueError，面向用户的那条路要在装配前按本判定给出可翻译的拒绝。
    """
    return any(_BASE_URL_PLACEHOLDER.search(url) for url in request_urls(definition))


def declarative_requires_api_key(definition: Mapping[str, Any]) -> bool:
    """Whether this definition requires the caller to provide api_key.

    The validator guarantees a non-empty ``auth`` references the api_key placeholder at least once and api_key appears only in
    the auth section, so checking whether the auth section is empty suffices. Together with :func:`declarative_requires_base_url`
    it is the seam for credential necessity: a definition where both are false (empty auth and all three section URLs hard-coded
    absolute addresses) can be called without any credentials.

    这份定义是否要求调用方提供 api_key。

    校验器保证非空 ``auth`` 至少引用一次 api_key 占位符、且 api_key 只允许出现在 auth 节，
    故判 auth 节是否为空即可。与 :func:`declarative_requires_base_url` 同为凭证必需性的判定缝：
    两者皆否（auth 为空且三节 URL 全写死绝对地址）的定义不需要任何凭证即可调用。
    """
    return bool(definition.get("auth"))


def _require_declarative_base_url(definition: Mapping[str, Any], provider: CustomProvider) -> None:
    if declarative_requires_base_url(definition) and not provider.base_url:
        raise ValueError("Declarative call endpoint requires base_url")


def _build_declarative_video(
    definition: Mapping[str, Any],
) -> BackendBuilder:
    """Backend construction closure of declarative video endpoints. / 声明式视频端点的 backend 构造闭包。"""

    def build(
        provider: CustomProvider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
    ) -> CustomVideoBackend:
        _require_declarative_base_url(definition, provider)
        delegate = DeclarativeVideoBackend(
            api_key=provider.api_key,
            base_url=provider.base_url,
            model=model_id,
            definition=definition,
            provider=provider.provider_id,
            path_overrides=path_overrides,
        )
        return CustomVideoBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)

    return build


def _build_declarative_image(
    definition: Mapping[str, Any],
) -> BackendBuilder:
    """Backend construction closure of declarative image endpoints. / 声明式图片端点的 backend 构造闭包。"""

    def build(
        provider: CustomProvider, model_id: str, path_overrides: PathOverrides = NO_PATH_OVERRIDES
    ) -> CustomImageBackend:
        _require_declarative_base_url(definition, provider)
        delegate = DeclarativeImageBackend(
            api_key=provider.api_key,
            base_url=provider.base_url,
            model=model_id,
            definition=definition,
            provider=provider.provider_id,
            path_overrides=path_overrides,
        )
        return CustomImageBackend(provider_id=provider.provider_id, delegate=delegate, model=model_id)

    return build


#: Display names (frontend i18n keys) of the three request sections of declarative definitions, and the placeholders usable in
#: override paths: only values the request itself already has; submit has only the model name, poll adds a task id, second fetch adds a result id.
#: 声明式定义三节请求的显示名（前端 i18n key）与覆盖路径里可用的占位符：只限该请求本身
#: 已有的值，提交只有模型名，轮询多一个任务 id，二次取件再多一个结果 id。
_DECLARATIVE_REQUESTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "submit": ("cp_request_submit", ("model",)),
    "poll": ("cp_request_poll", ("model", "task_id")),
    "result": ("cp_request_result", ("model", "result_id", "task_id")),
}
_BASE_URL_PREFIX = re.compile(r"^\s*\{\{\s*base_url\s*\}\}")


def declarative_requests(definition: Mapping[str, Any]) -> tuple[EndpointRequest, ...]:
    """Requests with overridable paths in a declarative definition: the sections whose URL starts with ``{{ base_url }}``.

    Sections with hard-coded absolute addresses have no path to replace and get no request name (``docs/adr/0101``); artifact
    URLs taken from responses are not request sections of the definition and are not included anyway. Placeholders already used
    in the default path are allowed as well.

    声明式定义里可覆盖路径的请求：URL 以 ``{{ base_url }}`` 开头的那几节。

    写死绝对地址的节没有可替换的路径，不提供请求名（``docs/adr/0101``）；从响应里取得的产物
    地址不是定义里的请求节，本就不在其列。默认路径里已用到的占位符一并放行。
    """
    rule = "strip_version" if strips_base_url_version(definition) else "as_is"
    requests: list[EndpointRequest] = []
    for section in REQUEST_SECTIONS:
        node = definition.get(section)
        if not isinstance(node, Mapping):
            continue
        url = str(node.get("url", ""))
        prefix = _BASE_URL_PREFIX.match(url)
        if prefix is None:
            continue
        default_path = url[prefix.end() :]
        display_name_key, placeholders = _DECLARATIVE_REQUESTS[section]
        requests.append(
            EndpointRequest(
                name=section,
                display_name_key=display_name_key,
                default_path=default_path,
                placeholders=tuple(sorted({*placeholders, *placeholder_names(default_path)})),
                base_url_rule=rule,
            )
        )
    return tuple(requests)


def declarative_endpoint_spec(
    key: str,
    definition: Mapping[str, Any],
    *,
    source: Literal["builtin", "custom"] = "builtin",
) -> EndpointSpec:
    """Derive an EndpointSpec from a declarative definition. Pure function: no DB reads, no requests.

    Bundled definitions (``builtin_endpoints/*.json``) and user definitions (``custom_endpoint`` table) go through the same
    projection: the definitions are equally expressive, and two implementations would only quietly diverge in places like
    capability defaults. The only difference is the family attribution decided by ``source``: bundled endpoints' family is the
    first key segment (protocol origin); user endpoints' protocol is described by the definition itself, with no external family to attribute.

    The media type is read via :func:`definition_media_type`, the same reading as the market index and mirror columns, and
    decides whether the backend goes through the image or video channel.

    Capabilities are declared explicitly and fully by the definition, independent of model. Video goes through the
    video_caps_for_model "all four fields declared" path (returning the same constant), rather than video_max_reference_images,
    which can only express the reference image limit; image capabilities are read together with the backend via
    :func:`image_capabilities_from_definition`, so the pre-generation gate and the projection here cannot diverge.

    把一份声明式定义派生成 EndpointSpec。纯函数：不读库、不发请求。

    随版定义（``builtin_endpoints/*.json``）与用户定义（``custom_endpoint`` 表）走同一条投影：
    定义的表达力一样，两份实现只会在能力缺省这类地方悄悄分叉。差别只在 ``source`` 决定的家族归属
    ——随版端点的家族取键首段（协议出处），用户端点的协议由定义自身描述、没有可归属的外部家族。

    媒体类型经 :func:`definition_media_type` 读，与市场索引、镜像列同一读法，并决定 backend 走
    图片还是视频通道。

    能力由定义显式全量声明，与 model 无关。视频走 video_caps_for_model 这条「四字段全量声明」的
    通路（返回同一份常量），而不是只能表达参考图上限的 video_max_reference_images；图片的能力
    与 backend 共读 :func:`image_capabilities_from_definition`，生成前的闸门与这里投影的不会分叉。
    """
    media_type = definition_media_type(definition)
    is_video = media_type == "video"
    caps = declarative_video_capabilities(definition) if is_video else None
    spec = EndpointSpec(
        key=key,
        media_type=media_type,
        family=CUSTOM_ENDPOINT_FAMILY if source == "custom" else declarative_family(key),
        # Display names of declarative endpoints come from meta.name, not the i18n catalog (see EndpointSpec.display_name).
        # 声明式端点的显示名取 meta.name，不进 i18n 目录（见 EndpointSpec.display_name）。
        display_name_key="",
        source=source,
        request_method=definition["submit"]["method"],
        request_path_template=declarative_request_path(definition),
        build_backend=_build_declarative_video(definition) if is_video else _build_declarative_image(definition),
        image_capabilities=None if is_video else image_capabilities_from_definition(definition),
        video_caps_for_model=(lambda _model_id: caps) if caps is not None else None,
        end_image_capable=caps.last_frame if caps is not None else False,
        reference_audio_capable=caps is not None and caps.reference_audio_mode is not ReferenceAudioMode.NONE,
        definition=definition,
        requests=declarative_requests(definition),
    )
    # The built-in registry passes the same invariant entry by entry at import time (see _validate_registry); user definitions are
    # built on the fly with no import time to rely on, so they pass it at construction; specs from both sources thus need no
    # different treatment by the capability layer.
    # 内置注册表在 import 期逐条过同一条不变式（见 _validate_registry）；用户定义现构造、没有
    # import 期可依托，故在构造处就过——两种来源的 spec 因此不必被能力判定层区分对待。
    validate_video_caps_declaration(spec)
    return spec


def _build_comfyui_runtime(
    definition: Mapping[str, Any],
) -> BackendBuilder:
    """Backend construction closure of ComfyUI endpoints, branching on the ``media_type`` the definition declares.

    The same thing for both kinds: whether a workflow produces images or video only the definition itself knows (the key prefix
    can't tell), and the two channels each have their own backend protocol and wrapper class. A media type other than these two
    raises a ``ComfyuiError`` with a failure code: the schema only lets these two values through, so reaching here means the DB was
    edited by hand; the worker then records the task as a structured failure, with the message rendered per language on the read
    side; storing raw text would show non-Chinese users a Chinese sentence in the task list. It borrows
    ``provider_unsupported_media`` instead of a new code: it says exactly "this provider cannot do this kind of generation", and
    the read side's ``CONFIGURE_PROVIDER`` pointer fits too.

    It does not raise ``ValueError``: that already means "unknown endpoint" at this layer, and several ``except ValueError``
    along the way would downgrade it to "endpoint no longer exists", when actually the endpoint is valid and only its media type
    is not one of the two this layer knows.

    The image path's wrapper stays a pure forwarder (``CustomImageBackend`` only overrides name / model anyway): capability
    injection is a video-specific form (system judgement ⊕ user override), while ``capability_overrides`` is closed entirely for
    the ComfyUI protocol (``docs/adr/0081``); image capabilities are derived only from the binding table, with no second source to layer on.

    ComfyUI 端点的 backend 构造闭包，按定义声明的 ``media_type`` 分叉。

    两种 kind 的同一件事：一份 workflow 产图还是产视频只有定义自己知道（键前缀推不出来），而两条
    通道各有自己的 backend 协议与包装类。媒体类型不是这两种时抛带失败码的 ``ComfyuiError``——
    schema 只放行这两个值，走到这里意味着手工改过库；worker 据此把任务落成结构化失败，文案留到
    读侧按语言渲染，落一段裸文本的话非中文用户在任务列表里看到的是一句中文。借
    ``provider_unsupported_media`` 而不另立新码：它说的正是「这个供应商给不出这一类生成」，读侧的
    ``CONFIGURE_PROVIDER`` 指向也对。

    不抛 ``ValueError``：那是本层「端点不认识」的既有含义，沿途多处 ``except ValueError`` 会把它
    降级成「端点已不在」，而实情是端点合法、只是媒体类型不是本层认得的那两个。

    图像那一路的包装保持纯转发（``CustomImageBackend`` 本就只覆写 name / model）：能力注入是视频
    专属的形态（系统判定 ⊕ 用户覆盖），而 ComfyUI 协议的 ``capability_overrides`` 整节关闭
    （``docs/adr/0081``），图像能力只从绑定表推导，没有可叠加的第二个来源。
    """

    def build(
        provider: CustomProvider, model_id: str, _path_overrides: PathOverrides = NO_PATH_OVERRIDES
    ) -> CustomImageBackend | CustomVideoBackend:
        media_type = str(definition.get("media_type"))
        if media_type not in ("image", "video"):
            raise ComfyuiError("provider_unsupported_media", provider_id=provider.provider_id, media_type=media_type)
        if not provider.base_url:
            raise ValueError("ComfyUI call endpoint requires base_url")
        if media_type == "image":
            image_delegate = ComfyuiImageBackend(
                provider_id=provider.provider_id,
                model=model_id,
                base_url=provider.base_url,
                api_key=provider.api_key,
                definition=definition,
            )
            return CustomImageBackend(provider_id=provider.provider_id, delegate=image_delegate, model=model_id)
        video_delegate = ComfyuiVideoBackend(
            provider_id=provider.provider_id,
            model=model_id,
            base_url=provider.base_url,
            api_key=provider.api_key,
            definition=definition,
        )
        return CustomVideoBackend(provider_id=provider.provider_id, delegate=video_delegate, model=model_id)

    return build


def comfyui_endpoint_spec(key: str, definition: Mapping[str, Any]) -> EndpointSpec:
    """Derive an EndpointSpec from a ComfyUI definition. Pure function: no DB reads, no requests.

    The media type is read via :func:`definition_media_type` from the ``media_type`` the definition itself declares: only the
    workflow knows whether it produces images or video. Capabilities are all derived from node bindings (``docs/adr/0082``),
    neither looking at the model name nor reading capability declarations in the definition (there is no such section): the
    capabilities are the same for every model, because there is only one workflow, and renaming a model row doesn't change what it can do.

    Implemented in this module rather than the ``comfyui`` subpackage: the subpackage is bound by an import contract of "not
    depending on the declarative runtime", while ``EndpointSpec`` and its invariants are here, and a subpackage reaching this
    module would indirectly reach the declarative backend. The derivation itself is in the subpackage's ``comfyui.capabilities``:
    it depends only on the binding table and the workflow, unrelated to ``EndpointSpec``; boxing for each of the two media types
    borrows the one in the corresponding backend module, which the backend's own capability declaration also uses.

    把一份 ComfyUI 定义派生成 EndpointSpec。纯函数：不读库、不发请求。

    媒体类型经 :func:`definition_media_type` 读定义自身声明的 ``media_type``——一份 workflow 产图
    还是产视频只有它自己知道。能力全部从节点绑定推导（``docs/adr/0082``），不看模型名也不读定义
    里的能力声明（定义里没有那一节）：能力对每个 model 是同一份，因为 workflow 只有一份，模型行
    换名字不改它能做什么。

    实现落在本模块而非 ``comfyui`` 子包：子包受「不依赖声明式运行时」的 import 契约约束，而
    ``EndpointSpec`` 与它的不变式都在这里，子包够到本模块即间接够到声明式 backend。推导本身在
    子包的 ``comfyui.capabilities`` 里——它只依赖绑定表与 workflow，与 ``EndpointSpec`` 无关；
    两种媒体类型的装箱各借对应 backend 模块那一份，backend 自己的能力声明也用它。
    """
    media_type = definition_media_type(definition)
    is_video = media_type == "video"
    # The pre-generation capability gate reads the backend's copy, not the caps projected here; writing it in two places would make
    # them clash at the gate, so both media types' capabilities are computed with the backend modules' boxing functions.
    # 生成前的能力闸门读的是 backend 那一份、不是这里投影出来的 caps，两处各写一份就会在闸门上
    # 打架，故两种媒体类型的能力都借 backend 模块的装箱函数算。
    caps = binding_video_capabilities(definition) if is_video else None
    spec = EndpointSpec(
        key=key,
        media_type=media_type,
        family=CUSTOM_ENDPOINT_FAMILY,
        # Display name comes from meta.name, handled the same as declarative endpoints (see EndpointSpec.display_name).
        # 显示名取 meta.name，与声明式端点同处理（见 EndpointSpec.display_name）。
        display_name_key="",
        source="custom",
        # The submit form is fixed: a whole workflow is POSTed to /prompt, with no path segment varying by model and no configurable
        # method; neither is a value in the definition, so they are written at the projection rather than read from the definition.
        # 提交形态固定：一份 workflow 整体 POST 给 /prompt，没有随模型变化的路径段，也没有
        # 可配的方法——两者都不是定义里的可取值，故写在投影处而非读自定义。
        request_method="POST",
        request_path_template="/prompt",
        build_backend=_build_comfyui_runtime(definition),
        image_capabilities=None if is_video else binding_image_capabilities(definition),
        video_caps_for_model=(lambda _model_id: caps) if caps is not None else None,
        # The two transport bits are handled the same as declarative endpoints: inferred back from caps rather than each writing its own criterion.
        # 两个运输位与声明式端点同处理：从 caps 反推，而不是各写一份判据。
        end_image_capable=caps.last_frame if caps is not None else False,
        definition=definition,
    )
    validate_video_caps_declaration(spec)
    return spec


def merge_builtin_definitions(registry: dict[str, EndpointSpec], directory: Path | None = None) -> None:
    """Merge bundled declarative definitions into the registry. Keys share a namespace with Python built-ins; duplicates are rejected.

    Invalid definitions, a missing directory and key conflicts all raise at import time: a load failure that keeps the process
    from starting is better than running with an endpoint that can't build a backend until the moment a user starts a generation.

    把随版声明式定义并入注册表。键与 Python 内置同一命名空间，重复即拒。

    定义不合法、目录缺失、键冲突在 import 期一律抛错——装载失败让进程起不来，好过带着一个装不出
    backend 的 endpoint 跑到用户发起生成那一刻。
    """
    for key, definition in load_builtin_definitions(directory).items():
        if key in registry:
            raise BuiltinDefinitionError(
                f"Bundled definition {key}.json has a built-in key that duplicates an existing endpoint"
            )
        registry[key] = declarative_endpoint_spec(key, definition)


merge_builtin_definitions(ENDPOINT_REGISTRY)


ENDPOINT_KEYS_BY_MEDIA_TYPE: dict[str, tuple[str, ...]] = {
    media_type: tuple(k for k, s in ENDPOINT_REGISTRY.items() if s.media_type == media_type)
    for media_type in {s.media_type for s in ENDPOINT_REGISTRY.values()}
}


def _validate_registry() -> None:
    """Validate the built-in registry's invariants at import time. / import 期校验内置注册表的不变式。"""
    for key, spec in ENDPOINT_REGISTRY.items():
        # ce- is the key prefix of custom call endpoints, allocated from the DB auto-increment id. A built-in key using that prefix would
        # break the uniqueness of resolve's prefix split; the two namespaces share a domain but never overlap precisely because of this invariant.
        # ce- 是自定义调用端点的键前缀，由 DB 自增 id 分配。内置键占用该前缀会让 resolve 的
        # 前缀分流失去唯一性——两个命名空间同域但永不重叠，正是靠这条不变式。
        if is_custom_endpoint(key):
            raise ValueError(f"builtin endpoint key {key!r} must not use the custom endpoint prefix")
        validate_video_caps_declaration(spec)


_validate_registry()


# ── Utility functions ── / ── 工具函数 ───────────────────────────────────────────────────────


def get_endpoint_spec(endpoint: str) -> EndpointSpec:
    spec = ENDPOINT_REGISTRY.get(endpoint)
    if spec is None:
        raise ValueError(f"unknown endpoint: {endpoint!r}")
    return spec


def endpoint_to_media_type(endpoint: str) -> str:
    return get_endpoint_spec(endpoint).media_type


def static_media_type(endpoint: str) -> str:
    """Decide a built-in endpoint's media type without reading the DB; ``ce-`` keys are rejected here.

    A custom endpoint's media type is decided by its definition (read differently per ``kind``); the key prefix implies no media
    type. The endpoint column of a model row may hold either kind of key; places that split media type by endpoint always first
    get the spec via ``endpoint_resolution.resolve_endpoint_spec``, then read ``EndpointSpec.media_type``.

    Raises:
        ValueError: a ``ce-`` key was passed (the definition must be read), or the built-in registry has no such key.

    不读库判定内置端点的媒体类型；``ce-`` 键在此拒绝。

    自定义端点的媒体类型由它那份定义决定（按 ``kind`` 各有取法），键前缀不蕴含媒体类型。模型行
    的 endpoint 列两种键都可能是，按端点分媒体类型的地方一律先
    ``endpoint_resolution.resolve_endpoint_spec`` 取 spec、再读 ``EndpointSpec.media_type``。

    Raises:
        ValueError: 传入 ``ce-`` 键（须读定义），或内置注册表里没有该键。
    """
    # Lazy import: builtin_definitions consumes this module's registry; a module-level import would form a cycle.
    # 延迟导入：builtin_definitions 消费本模块的注册表，模块级导入会成环。
    from lib.custom_provider import is_custom_endpoint

    if is_custom_endpoint(endpoint):
        raise ValueError(f"custom endpoint media type comes from its definition: {endpoint!r}")
    return endpoint_to_media_type(endpoint)


def endpoint_to_image_capabilities(endpoint: str) -> frozenset[ImageCapability]:
    """Return the capability set of an image endpoint. Raises ValueError for non-image endpoints.

    返回 image 类 endpoint 的 capability 集合。非 image 类抛 ValueError。"""
    spec = get_endpoint_spec(endpoint)
    if spec.image_capabilities is None:
        raise ValueError(f"endpoint {endpoint!r} is not an image endpoint")
    return spec.image_capabilities


def list_endpoints_by_media_type(media_type: str) -> list[EndpointSpec]:
    return [ENDPOINT_REGISTRY[k] for k in ENDPOINT_KEYS_BY_MEDIA_TYPE.get(media_type, ())]


def endpoint_spec_to_dict(spec: EndpointSpec) -> dict:
    """Convert an EndpointSpec into a serializable plain-data dict (stripping the non-JSON-able build_backend closure).

    把 EndpointSpec 转成可序列化的纯数据 dict（剥掉不可 JSON 化的 build_backend 闭包）。"""
    data = asdict(spec)
    data.pop("build_backend", None)
    # same as build_backend: callables aren't JSON-able, strip
    data.pop("video_caps_for_model", None)  # 同 build_backend：callable 不可 JSON 化，剥掉
    data.pop("metering_backend", None)  # class object, only used by pricing to get the metering rules / 类对象，只供计价取计量口径
    # The catalog does not embed definitions: the whole JSON is fetched separately via GET /custom-providers/endpoints/{key}/definition.
    # catalog 不内嵌定义：整份 JSON 由 GET /custom-providers/endpoints/{key}/definition 单独取。
    data.pop("definition", None)
    data["kind"] = spec.kind
    data["display_name"] = spec.display_name
    # Projection of parameter constraints: the UI uses it to disable resolution / duration controls and write empty placeholders, with criteria from the same source as the execution layer.
    # 参数约束的投影：界面据此禁用分辨率 / 时长控件并写出空值占位，判据与执行层同源。
    data["size_fixed"] = spec.size_fixed
    data["duration_fixed"] = spec.duration_fixed
    data["duration_frame_rate_missing"] = spec.duration_frame_rate_missing
    data["duration_tier_empty"] = spec.duration_tier_empty
    data["native_resolution"] = spec.native_resolution
    if spec.image_capabilities is not None:
        data["image_capabilities"] = sorted(c.value for c in spec.image_capabilities)
    else:
        data["image_capabilities"] = None
    return data


# ── Heuristic: infer the default endpoint from model_id + discovery_format ── / ── 启发式：从 model_id + discovery_format 推默认 endpoint ─────────


_IMAGE_PATTERN = re.compile(r"image|dall|img|imagen|flux|seedream|jimeng|viduq[12](?:[-_].*)?", re.IGNORECASE)
_VIDEO_PATTERN = re.compile(
    r"video|sora|kling|wan|seedance|cog|mochi|veo|pika|runway|"
    r"vidu2(?:\.0)?(?:[-_].*)?|viduq3(?:[-_].*)?",
    re.IGNORECASE,
)
# TTS model id recognition (tts-1 / gpt-4o-mini-tts / speech-1.5 / cosyvoice, etc.).
# Deliberately excludes bare "audio": chat audio-modality models like gpt-4o-audio-preview would be misclassified as TTS.
# TTS 模型 id 识别（tts-1 / gpt-4o-mini-tts / speech-1.5 / cosyvoice 等）。
# 刻意不含裸 "audio"：gpt-4o-audio-preview 等 chat 音频模态模型会被误归 TTS。
_AUDIO_PATTERN = re.compile(r"tts|speech|cosyvoice", re.IGNORECASE)
# The Qwen TTS family (qwen-tts / qwen3-tts-flash, etc.) uses Bailian's native sync endpoint, not /v1/audio/speech
# 通义 TTS 家族（qwen-tts / qwen3-tts-flash 等）走百炼原生同步端点，不是 /v1/audio/speech
_QWEN_TTS_PATTERN = re.compile(r"qwen3?-tts", re.IGNORECASE)
# MiniMax speech models (speech-2.8-hd / speech-02-turbo, etc., compared whole after stripping namespaces) use t2a_v2.
# Must carry an -hd / -turbo tier suffix: same-prefix ids like Fish Audio's speech-1.5 still use /v1/audio/speech.
# MiniMax 语音型号（speech-2.8-hd / speech-02-turbo 等，剥离命名空间后整段比较）走 t2a_v2。
# 必须带 -hd / -turbo 档位后缀：Fish Audio 的 speech-1.5 等同名前缀 id 仍是 /v1/audio/speech。
_MINIMAX_TTS_PATTERN = re.compile(r"speech-[\d.]+-(?:hd|turbo)")
# Bare "speech" collides with ASR (speech-to-text) family ids; excluded by content so recognition models aren't defaulted to TTS endpoints
# 裸 "speech" 会撞上 ASR（语音转文字）家族 id，按内容排除，避免把识别模型默认归到 TTS 端点
_ASR_PATTERN = re.compile(r"transcribe|speech.?to.?text|recognition", re.IGNORECASE)

#: Exact model names of MiniMax input-restricted endpoints (lowercase; compared after stripping namespace prefixes). Fast only accepts
#: image-to-video, S2V requires a reference image: creating an alias on these two keys would add input requirements that can't be
#: verified, so only the official model ids listed are accepted, same source as alembic ``8c2b1e7d4a90`` and
#: backend_assembly.specs._minimax_video_endpoint (three places each keep a literal copy; changing one requires changing the other two).
#: S2V also accepts "minimax-s2v-01": a common alias form where aggregators paste the vendor name prefix straight into the id,
#: still meaning that one exact model.
#: MiniMax 输入受限端点的精确型号名（小写；剥离命名空间前缀后比较）。Fast 只接受图生视频、
#: S2V 必须带参考图——把一个别名建到这两键等于给它加上无从核实的输入要求，故只认表列的
#: 官方型号 id，与 alembic ``8c2b1e7d4a90`` 和 backend_assembly.specs._minimax_video_endpoint
#: 同源（三处各存一份字面量，改其一须同改另两处）。S2V 另收 "minimax-s2v-01"：聚合商把厂商名
#: 前缀直接粘进 id 的常见别名形态，语义仍是那一个精确型号。
_MINIMAX_FAST_MODELS = frozenset({"minimax-hailuo-2.3-fast"})
_MINIMAX_S2V_MODELS = frozenset({"s2v-01", "minimax-s2v-01"})


#: With a known media type, pick the endpoint by name family (docs/adr/0105): when the name contains a vendor token, take that vendor's endpoint for the media type.
#: Only vendors whose names may land on several media types are listed (amphibious models, model names without media semantics);
#: other vendors' name rules already give a unique media type.
#: 已知媒体类型时按名字族选端点（docs/adr/0105）：名字含厂商 token 时取这个厂商在该媒体类型下的端点。
#: 只收一个名字可能落在多种媒体类型上的厂商（两栖模型、名字不带媒体语义的型号），其余厂商的名字规则
#: 本身已经给出唯一的媒体类型。
_FAMILY_ENDPOINTS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("kling", {"image": "kling-image", "video": "kling-video"}),
    ("gemini", {"text": "gemini-generate", "image": "gemini-image", "audio": "gemini-tts"}),
    ("seedance", {"video": "ark-seedance"}),
    ("seedream", {"image": "ark-seedream"}),
    ("viduq3", {"video": "vidu-video"}),
    ("hailuo", {"video": "minimax-hailuo-v1"}),
)

#: Default endpoint of each media type under the model discovery protocol: the google protocol takes Gemini native endpoints, others take OpenAI-compatible ones.
#: 各媒体类型在模型发现协议下的默认端点：google 协议取 Gemini 原生端点，其余取 OpenAI 兼容端点。
_DEFAULT_ENDPOINTS: Mapping[str, tuple[str, str]] = {
    "text": ("gemini-generate", "openai-chat"),
    "image": ("gemini-image", "openai-images"),
    "video": ("gemini-video", "openai-video"),
    "audio": ("gemini-tts", "openai-tts"),
}


def infer_endpoint(model_id: str, discovery_format: str, media_type: str | None = None) -> str:
    """Infer the default call endpoint; ``media_type`` is the model's known media type (given by the matched entry); when not given it is inferred from the name too.

    When a media type is given, use the endpoint the name rules infer if it belongs to that media type; otherwise pick by name
    family within that media type (see :data:`_FAMILY_ENDPOINTS`), and when none is found take that media type's default endpoint
    under the current model discovery protocol (docs/adr/0105).

    推默认调用端点；``media_type`` 是模型已知的媒体类型（对应上的条目给出），不给时按名字一并推断。

    给了媒体类型时，名字规则推出的端点属于这个媒体类型就用它；否则在这个媒体类型内按名字族选
    （见 :data:`_FAMILY_ENDPOINTS`），选不到时取该媒体类型在当前模型发现协议下的默认端点（docs/adr/0105）。
    """
    by_name = _infer_endpoint_by_name(model_id, discovery_format)
    if media_type is None or endpoint_to_media_type(by_name) == media_type:
        return by_name
    lowered = model_id.lower()
    for token, endpoints in _FAMILY_ENDPOINTS:
        if token in lowered and media_type in endpoints:
            return endpoints[media_type]
    google_default, other_default = _DEFAULT_ENDPOINTS[media_type]
    return google_default if discovery_format == "google" else other_default


def infer_media_type(model_id: str, discovery_format: str) -> str:
    """Media type inferred from the name, i.e. the media type of the endpoint the name rules infer. / 按名字推断的媒体类型，即名字规则推出的端点的媒体类型。"""
    return endpoint_to_media_type(_infer_endpoint_by_name(model_id, discovery_format))


def _infer_endpoint_by_name(model_id: str, discovery_format: str) -> str:
    """Infer the default endpoint from model id and discovery_format (content-first).

    Model id content takes precedence over discovery_format: relays commonly use discovery_format="openai", but their model
    lists often include native gemini-* ids, which must be corrected to Google endpoints by content; otherwise they get wrongly
    inferred to openai-chat/openai-images and have to be changed back manually every time.

    1) Alibaba Bailian video → the happyhorse / wan2.7 / wan3 families (incl. hyphenated wan-2.7-xxx / wan-3-xxx forms and
       image-to-video continuation aliases) use "dashscope-async-video" (native async endpoint). happyhorse is not in
       _VIDEO_PATTERN and must be explicit; Wan video is intercepted before the generic is_video. Real image variants are not
       auto-inferred to dashscope (relays may be OpenAI-compatible): qwen-image / wan2.7-image / wan-2.7-image /
       wan3.0-video-image and similar ids with version/date suffixes fall to image family inference; explicit image-to-video
       continuation syntax like wan-3-turbo-image-to-video / wan3-image2video is still classed as video (same principle as
       kling-image2video in section 2.5), with classify_wan_model's is_image_to_video picking out exactly this form, making no
       assumptions about image variant naming (trailing tokens, etc.). Native routing only accepts 2.7 (incl. the dotted form
       "wan2.x", see the note on classify_wan_model below) and wan3; other 2.x hyphen/underscore forms (wan-2.1, wan_2.2-s2v,
       etc.) fall to the generic video endpoint in 4) below. Within the 2.7 family the videoedit modality (wan2.7-videoedit)
       has no request construction in this backend and is likewise excluded from native routing (see the note at is_videoedit in classify_wan_model).
    2) MiniMax native tokens → Hailuo (one key each for Fast and non-Fast) / S2V / H3 each use their own declarative endpoint,
       image-01 uses "minimax-image". Intercepted before the generic
       is_video/is_image: s2v is not in _VIDEO_PATTERN, and image-01 contains "image" and would otherwise be pushed to the generic image family.
       Input-restricted Fast / S2V accept only exact model names (compared after stripping relay namespace prefixes); inexact
       Hailuo aliases fall to the generic Hailuo key, other s2v forms fall to the generic video endpoint in 4).
    2.5) Kling kling token → video semantics first go to "kling-video" (i2v like kling-image2video has image semantics but is
       really video); other image semantics go to "kling-image", otherwise "kling-video". kling also matches _VIDEO_PATTERN and
       must be intercepted before the generic is_video, otherwise video would land on openai-video; v3-omni, with the same name for
       image/video, defaults to video, image is picked manually.
    3) gemini native models (non-video) → TTS uses "gemini-tts", image forms use "gemini-image", otherwise text uses
       "gemini-generate" (regardless of discovery_format)
    4) video family → seedance→"ark-seedance", viduq3→"vidu-video", Veo with discovery_format=google→
       "gemini-video", otherwise "openai-video"
    5) image family → seedream→"ark-seedream" (regardless of discovery_format), others with discovery_format=google use
       "gemini-image", otherwise "openai-images" (imagen follows image family inference, no longer intercepted separately)
    6) TTS family (tts/speech/cosyvoice) → qwen-tts / qwen3-tts use "dashscope-tts", MiniMax's
       speech-<version>-hd|turbo uses "minimax-tts", the rest "openai-tts"
       (regardless of discovery_format; precedence before the text default)
    7) default (text) → discovery_format=google uses "gemini-generate", otherwise "openai-chat"

    根据模型 id 与 discovery_format 推默认 endpoint（content-first）。

    model id 内容优先于 discovery_format：中转站普遍 discovery_format="openai"，但模型
    列表常夹带 gemini-* 原生 id，必须按内容纠偏到 Google 端点，否则被错推到
    openai-chat/openai-images，每次都要手动改回。

    1) 阿里百炼视频 → happyhorse / wan2.7 / wan3 家族（含 wan-2.7-xxx / wan-3-xxx 连字符形态、
       image-to-video 续接别名）走 "dashscope-async-video"（原生异步端点）。happyhorse 不在
       _VIDEO_PATTERN 须显式；万相视频抢在通用 is_video 前拦截。真正的图像变体不自动推 dashscope
       （中转可能是 OpenAI 兼容）：qwen-image / wan2.7-image / wan-2.7-image / wan3.0-video-image
       及带版本/日期后缀的同类 id 落到图像家族推断；wan-3-turbo-image-to-video /
       wan3-image2video 这类显式 image-to-video 续接语法仍归视频（同 2.5 节 kling-image2video
       的处理原则），按 classify_wan_model 的 is_image_to_video 精确挑出这一种形态，不对图像变体
       的命名形态（结尾 token 等）做任何假设。原生路由只认 2.7（含点号形态 "wan2.x"，见下方
       classify_wan_model 的说明）与 wan3；其余 2.x 连字符/下划线形态（wan-2.1、wan_2.2-s2v 等）
       落到下方 4) 的通用视频端点。2.7 家族内 videoedit 模态（wan2.7-videoedit）本后端未实现
       请求构造，同样排除出原生路由（见 classify_wan_model 的 is_videoedit 处的说明）。
    2) MiniMax 原生 token → 海螺（Fast 与非 Fast 各一键）/ S2V / H3 分别走各自声明式端点，
       image-01 走 "minimax-image"。先于通用
       is_video/is_image 拦截：s2v 不在 _VIDEO_PATTERN、image-01 含 "image" 否则会被推到通用图像家族。
       输入受限的 Fast / S2V 只认精确型号名（剥离中转命名空间前缀后比较），非精确海螺别名落
       通用海螺键，其余 s2v 形态落 4) 的通用视频端点。
    2.5) 可灵 kling token → 含 video 语义优先归 "kling-video"（kling-image2video 等 i2v 含 image
       语义但本质是视频）；其余含 image 语义走 "kling-image"，否则走 "kling-video"。kling 同时命中
       _VIDEO_PATTERN，须先于通用 is_video 拦截，否则视频会落到 openai-video；v3-omni 图像/视频同名
       默认归视频、图像手动选。
    3) gemini 原生模型（非 video）→ TTS 走 "gemini-tts"，image 形态走 "gemini-image"，否则文本走
       "gemini-generate"（不分 discovery_format）
    4) 视频家族 → seedance→"ark-seedance"、viduq3→"vidu-video"、Veo 且 discovery_format=google→
       "gemini-video"、否则 "openai-video"
    5) 图像家族 → seedream→"ark-seedream"（不分 discovery_format），其余 discovery_format=google 走
       "gemini-image" 否则 "openai-images"（imagen 随图像家族推断，不再单独拦截）
    6) TTS 家族（tts/speech/cosyvoice）→ qwen-tts / qwen3-tts 走 "dashscope-tts"，MiniMax 的
       speech-<版本>-hd|turbo 走 "minimax-tts"，其余 "openai-tts"
       （不分 discovery_format；precedence 在 text 默认之前）
    7) 默认（文本）→ discovery_format=google 走 "gemini-generate" 否则 "openai-chat"
    """
    lowered = model_id.lower()
    # YouReel: ElevenLabs model ids (eleven_multilingual_v2, eleven_v3 …) and every model of an ElevenLabs provider
    if discovery_format == "elevenlabs" or lowered.startswith("eleven_"):
        return "elevenlabs-tts"
    is_image = bool(_IMAGE_PATTERN.search(model_id))
    # Wan/happyhorse family ids that use the Bailian native endpoint (both video and image variants match), used once each by the
    # routing and the is_video exclusion below. Family membership, separator normalization, identifier boundaries, image-to-video
    # continuation syntax and videoedit modality exclusion are all decided only once, in classify_wan_model
    # (lib.backends.video_backends.dashscope); this function, DashScopeVideoBackend._profile_for_model and
    # duration_presets.infer_supported_durations only consume its conclusion and no longer each regex-match model_id, so the
    # breadth of the three can't drift into mutually exclusive combinations like "routed to this backend but no matching capability tier".
    # 走百炼原生端点的万相/happyhorse 家族 id（视频与图像变体都命中），下面路由与 is_video 排除
    # 各用一次。家族归属、分隔符归一化、标识符边界、image-to-video 续接语法、videoedit 模态排除
    # 全部只在 classify_wan_model（lib.backends.video_backends.dashscope）里判定一次，本函数与
    # DashScopeVideoBackend._profile_for_model、duration_presets.infer_supported_durations 三处
    # 只消费其结论，不再各自对 model_id 做正则匹配——避免三处宽度各自漂移，出现"路由到本后端却拿
    # 不到对应能力档"一类互斥组合。
    classification = classify_wan_model(model_id)
    is_wan_family = classification.family is not None and classification.family != "happyhorse"
    # Wan family image-to-video aliases (e.g. wan-3-turbo-image-to-video / wan3-image2video) contain the "image" substring but are
    # really video models, the same trap as kling-image2video below: a blanket is_image would misjudge them as image variants.
    # Conversely "only counts as an image variant if it ends with image" is wrong too: real image aliases like wan3.0-image-edit /
    # wan-3-turbo-image-preview / wan3.0-video-image-20260801 with a date suffix don't end with "image" and would be misjudged as video.
    # So only image-to-video continuation syntax ("image" directly followed by "to"/"2" and then "video") is picked out as the
    # exception; everything else with image semantics is treated as an image variant, with no assumptions about image variant naming.
    #
    # This exclusion cannot use is_wan_family (strict identifier boundaries) as a gate: the "wan" branch of _VIDEO_PATTERN itself has
    # no boundary, so ids like "swan2.7-image" / "vendorwan2.7-image" that don't satisfy the family boundary still match
    # _VIDEO_PATTERN; if the exclusion required passing the strict family check first, such ids would be misjudged as video by
    # is_video instead of falling to image family inference. So the exclusion only checks "contains the wan substring" (the same
    # breadth as _VIDEO_PATTERN), without requiring the family identifier boundary; the family boundary is only used below for
    # eligibility for native routing (dashscope-async-video).
    # wan 家族的 image-to-video 别名（如 wan-3-turbo-image-to-video / wan3-image2video）含 "image"
    # 子串但本质是视频模型，与下方 kling-image2video 同类陷阱：笼统 is_image 会把它们错判成图像
    # 变体。反过来"以 image 结尾才算图像变体"同样错——wan3.0-image-edit / wan-3-turbo-image-preview /
    # 带日期后缀的 wan3.0-video-image-20260801 这类真图像别名不以 "image" 结尾，会被误判成视频。
    # 故只把 image-to-video 续接语法（"image" 后紧跟 "to"/"2" 再接 "video"）当例外挑出来，其余
    # 含 image 语义一律按图像变体处理，不对图像变体的命名形态做任何假设。
    #
    # 该排除不能拿 is_wan_family（严格标识符边界）做门槛：_VIDEO_PATTERN 的 "wan" 分支本身无边界，
    # "swan2.7-image" / "vendorwan2.7-image" 这类不满足家族边界的 id 依然会命中 _VIDEO_PATTERN，
    # 若排除逻辑要求先通过严格家族判定，这类 id 就会被 is_video 误判为视频而不是落到图像家族推断。
    # 因此排除只看"是否含 wan 子串"（与 _VIDEO_PATTERN 的宽度一致），不要求满足家族标识符边界；
    # 家族边界只用于下面原生路由（dashscope-async-video）的资格判定。
    contains_wan_token = "wan" in lowered
    # When a wan2.7 family id is confirmed to be a video modality (any of t2v/i2v/r2v/s2v/v2v/videoedit, even if some lack request
    # construction, see the note at is_known_video_modality in classify_wan_model), the profile itself establishes video semantics;
    # even if the id elsewhere (e.g. a proxy namespace prefix "image-proxy/wan-2.7-s2v") contains an unrelated "image" substring,
    # it should not be judged an image variant. Other names not matching any known modality token (e.g. "wan2.7-image") are
    # conservatively treated as image variants. The same check is not applied to wan3: wan3 has a single profile key that doesn't
    # distinguish t2v/i2v/r2v/s2v from real image aliases like image-edit, so applying the same check would in turn wrongly hit real
    # image aliases like wan3.0-image-edit (see the comment above).
    #
    # This check likewise cannot use is_wan_family as a gate, for the same reason as the wan_image_variant exclusion scope above:
    # for ids like "wan-2.2-image-to-video" that don't satisfy the strict family boundary (wan and version separated by a hyphen),
    # classify_wan_model still recognizes their image-to-video continuation syntax by identifier boundary (is_image_to_video);
    # requiring the family check first here would misjudge such explicit i2v names as image variants.
    # wan2.7 家族已确认属于视频模态（t2v/i2v/r2v/s2v/v2v/videoedit 任一，即便部分未实现请求构造，
    # 见 classify_wan_model 的 is_known_video_modality 处的说明）时，该 profile 本身已确立视频
    # 语义——即便 id 别处（如代理命名空间前缀 "image-proxy/wan-2.7-s2v"）另含无关 "image" 子串，
    # 也不应被判成图像变体。未落入任一已知模态 token 的其余命名（如 "wan2.7-image"）保守按图像
    # 变体处理。不对 wan3 套用同一判定：wan3 只有单一 profile key，不区分 t2v/i2v/r2v/s2v 与
    # image-edit 等真图像别名，套用同一判定会反过来误伤 wan3.0-image-edit 一类真图像别名（见上方
    # 注释）。
    #
    # 该判定同样不能拿 is_wan_family 做门槛，理由与上面 wan_image_variant 的排除范围一致：
    # "wan-2.2-image-to-video" 一类不满足家族严格边界（连字符隔开 wan 与版本号）的 id，
    # classify_wan_model 仍会按标识符边界识别出其 image-to-video 续接语法（is_image_to_video），
    # 若在这里再要求先通过家族判定，这类显式 i2v 命名会被误判成图像变体。
    wan_video_continuation = contains_wan_token and (
        classification.is_image_to_video
        or (classification.family == "wan2.7" and classification.is_known_video_modality)
    )
    wan_image_variant = contains_wan_token and is_image and not wan_video_continuation
    # Modalities without request construction (wan2.7-videoedit / wan2.7-s2v / wan2.7-v2v, etc.) don't use the native endpoint even
    # if they match the family regex (see the note at has_known_modality in classify_wan_model), falling to the generic video
    # endpoint in 4) below, so this backend doesn't receive requests it can't build correctly.
    # 未实现请求构造的模态（wan2.7-videoedit / wan2.7-s2v / wan2.7-v2v 等）即便命中家族正则也不
    # 走原生端点（见 classify_wan_model 的 has_known_modality 处的说明），落到下方 4) 的通用视频
    # 端点，避免本后端收到无法正确构造的请求。
    wan_unsupported_modality = is_wan_family and not classification.has_known_modality

    # Alibaba Bailian video is intercepted to the native async endpoint before the generic is_video / 阿里百炼视频先于通用 is_video 拦截到原生异步端点
    if classification.family == "happyhorse":
        return "dashscope-async-video"
    if is_wan_family and not wan_image_variant and not wan_unsupported_modality:
        return "dashscope-async-video"

    # Second-level routing of MiniMax native tokens: Hailuo (incl. minimax-hailuo) / S2V / H3 → video endpoints with two-step or single-step retrieval;
    # image-01 → single-step image endpoint. Before the generic is_video/is_image: neither s2v nor h3 is covered by _VIDEO_PATTERN,
    # and image-01 contains "image" and would otherwise be taken by the generic image family. Matches "minimax-h3" rather than bare
    # "h3": the latter is too short and easily collides with other vendors' model ids that happen to contain an h3 substring.
    #
    # Input-restricted Fast and S2V accept only exact model names (see _MINIMAX_FAST_MODELS / _MINIMAX_S2V_MODELS):
    # "MiniMax-Hailuo-2.3-Fast" and "MiniMax-Hailuo-2.3" collide on prefix, and a loose substring would put non-Fast models or relay
    # aliases with unverifiable upstreams onto the Fast definition, which requires a first frame. Relay namespace prefixes are stripped
    # before comparing, taking the last segment, multi-level namespaces ("openrouter/minimax/MiniMax-Hailuo-2.3-Fast") stripped
    # too: namespaces are just resale wrapping; what decides is that the last segment equals the official model id exactly.
    # Inexact Hailuo aliases fall to the generic Hailuo key (no input requirements), and other s2v forms (e.g.
    # "wan2.7-s2v") are no longer wrongly swallowed as the MiniMax S2V protocol, falling to the generic video endpoint in 4) below.
    # MiniMax 原生 token 二级路由：海螺（含 minimax-hailuo）/ S2V / H3 → 两步或单步取回的视频端点；
    # image-01 → 单步图像端点。先于通用 is_video/is_image：s2v 与 h3 均不被 _VIDEO_PATTERN 覆盖，
    # image-01 含 "image" 否则会被通用图像家族抢走。匹配 "minimax-h3" 而非裸 "h3"——后者过短，
    # 容易撞上其它厂商恰好含 h3 子串的型号 id。
    #
    # 输入受限的 Fast 与 S2V 只认精确型号名（见 _MINIMAX_FAST_MODELS / _MINIMAX_S2V_MODELS）：
    # "MiniMax-Hailuo-2.3-Fast" 与 "MiniMax-Hailuo-2.3" 前缀碰撞，宽松子串会把非 Fast 型号或
    # 无从核实上游的中转别名建到首帧必需的 Fast 定义。比较前剥离中转命名空间前缀——取末段，
    # 多层命名空间（"openrouter/minimax/MiniMax-Hailuo-2.3-Fast"）一并剥掉：命名空间只是
    # 转售包装，承担判定的是末段必须逐字等于官方型号 id。非精确的海螺别名落通用海螺键
    # （无输入要求），其余 s2v 形态（如
    # "wan2.7-s2v"）不再被误吞成 MiniMax S2V 协议，落下方 4) 的通用视频端点。
    canonical = re.split(r"[/:]", lowered)[-1]
    if "hailuo" in lowered:
        # Fast only accepts image-to-video, so endpoint-level capabilities live in two keys accordingly (see the same-source rules of
        # backend_assembly.specs._minimax_video_endpoint); it can't share one definition with 2.3.
        # Fast 只接受图生视频，端点级能力据此分居两键（见 backend_assembly.specs
        # ._minimax_video_endpoint 的同源口径），不能与 2.3 共用一个定义。
        return "minimax-hailuo-v1-fast" if canonical in _MINIMAX_FAST_MODELS else "minimax-hailuo-v1"
    if canonical in _MINIMAX_S2V_MODELS:
        return "minimax-s2v-01"
    if "minimax-h3" in lowered:
        return "minimax-h3"
    if "image-01" in lowered:
        return "minimax-image"

    # Second-level routing of Kling native relays: kling matches both _VIDEO_PATTERN (contains kling) and (with image semantics)
    # _IMAGE_PATTERN, and must be split explicitly before the generic is_video/is_image. Video semantics take precedence over image:
    # image-to-video like kling-image2video / kling-img2video has image semantics but is really a video model, and looking at
    # is_image directly would wrongly push it to kling-image, so the video keyword is caught first for kling-video; other image
    # semantics → kling-image, otherwise → kling-video. The kling-v3-omni image/video same-name ambiguity can't be resolved by tokens
    # alone; defaults to video, image is picked manually; regardless of discovery_format (each Kling endpoint is unique).
    # 可灵原生中转二级路由：kling 同时命中 _VIDEO_PATTERN（含 kling）与（含 image 语义时）
    # _IMAGE_PATTERN，须在通用 is_video/is_image 之前显式分流。video 语义优先于 image——
    # kling-image2video / kling-img2video 这类 image-to-video 含 image 语义但本质是视频模型，
    # 若直接看 is_image 会被误推到 kling-image，故先拦 video 关键字归 kling-video；其余含 image
    # 语义 → kling-image，否则 → kling-video。kling-v3-omni 图像/视频同名歧义无法纯靠 token 区分，
    # 默认归视频、图像手动选；不分 discovery_format（可灵端点各自唯一）。
    if "kling" in lowered:
        if "video" in lowered:
            return "kling-video"
        return "kling-image" if is_image else "kling-video"

    # wan2.x-image / wan3.0-video-image contain "wan" and would be misjudged as video by _VIDEO_PATTERN; explicit exclusion lets them fall to
    # image family inference. Reuses wan_image_variant above (the image check after excluding image-to-video continuation aliases).
    # wan2.x-image / wan3.0-video-image 含 "wan" 会被 _VIDEO_PATTERN 误判为视频；显式排除让它落到
    # 图像家族推断。复用上面的 wan_image_variant（排除 image-to-video 续接别名后的图像判定）。
    is_video = bool(_VIDEO_PATTERN.search(model_id)) and not wan_image_variant

    if "gemini" in lowered and not is_video:
        if "tts" in lowered:
            return "gemini-tts"
        return "gemini-image" if is_image else "gemini-generate"
    if is_video:
        if "seedance" in lowered:
            return "ark-seedance"
        if "viduq3" in lowered:
            return "vidu-video"
        # Veo's OpenAI-compatible layer keeps /v1/videos; only the google discovery protocol is native predictLongRunning
        # Veo 的 OpenAI 兼容层沿用 /v1/videos，只有 google 发现协议才是原生 predictLongRunning
        if "veo" in lowered and discovery_format == "google":
            return "gemini-video"
        return "openai-video"
    if is_image:
        # Ark Seedream has no /images/edits; generation with reference images also goes via /images/generations; regardless of discovery_format
        # 方舟 Seedream 没有 /images/edits，带参考图的生成也走 /images/generations；不分 discovery_format
        if "seedream" in lowered:
            return "ark-seedream"
        return "gemini-image" if discovery_format == "google" else "openai-images"
    if _AUDIO_PATTERN.search(model_id) and not _ASR_PATTERN.search(model_id):
        if _QWEN_TTS_PATTERN.search(model_id):
            return "dashscope-tts"
        return "minimax-tts" if _MINIMAX_TTS_PATTERN.fullmatch(canonical) else "openai-tts"
    return "gemini-generate" if discovery_format == "google" else "openai-chat"
