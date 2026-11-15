"""Combination-matrix regression tests for Wan / happyhorse family classification.

`lib.backends.video_backends.dashscope.classify_wan_model` is the single decision point for family membership, separator
normalization, identifier boundaries, the image-to-video continuation syntax and the videoedit modality exclusion; the three
consumers -- endpoint routing (`lib.custom_provider.endpoints.infer_endpoint`), capability tier
(`DashScopeVideoBackend.video_capabilities_for_model`) and duration tier
(`lib.custom_provider.duration_presets.infer_supported_durations`) -- only consume its conclusions.
This file covers combinations along the axes separator x version x modality x case x decorating prefix, comparing the expected
values of the three decision points case by case, and adds one general consistency assertion for family members whose concrete
modality (t2v/i2v/r2v) can be resolved: hitting the native route implies a non-default capability tier and vice versa -- when
the three decision points each keep their own regex, breadth mismatches easily produce such contradictory combinations;
per-point unit tests cannot find them, only cross-point combination checks can.

万相 / happyhorse 家族判定的组合矩阵回归测试。

`lib.backends.video_backends.dashscope.classify_wan_model` 是家族归属、分隔符归一化、标识符边界、
image-to-video 续接语法、videoedit 模态排除的唯一判定入口；端点路由
（`lib.custom_provider.endpoints.infer_endpoint`）、能力档
（`DashScopeVideoBackend.video_capabilities_for_model`）、时长档
（`lib.custom_provider.duration_presets.infer_supported_durations`）三处判定点都只消费其结论。
本文件覆盖分隔符 × 版本 × 模态 × 大小写 × 装饰前缀等轴的组合，逐条比对三处判定点的期望值，
并对可解析出具体模态（t2v/i2v/r2v）的家族成员做一条通用一致性断言：命中原生路由必有非默认
能力档，反之亦然——三处判定点各自维护一份正则时容易出现宽度不一致，产生这类互斥组合；
逐点单测无法发现，只有跨判定点的组合校验能发现。
"""

from __future__ import annotations

import itertools

import pytest

from lib.backends.video_backends.dashscope import DashScopeVideoBackend
from lib.backends.video_backends.name_family import UNRECOGNIZED_MODEL_CAPABILITIES
from lib.custom_provider.duration_presets import infer_supported_durations
from lib.custom_provider.endpoints import infer_endpoint

_PREFIX_SEPS = ["-", "_", ""]  # wan[-_]?<version>
_MODALITY_SEPS = ["-", "_"]  # <version>[-_]<modality>
_CASE_FNS = [str.lower, str.upper, str.title]


def _build(prefix_sep: str, version: str, modality_sep: str, modality: str) -> str:
    return f"wan{prefix_sep}{version}{modality_sep}{modality}"


def _wan27_ids() -> list[str]:
    return [id_ for id_, _modality in _wan27_ids_with_modality()]


def _wan27_ids_with_modality() -> list[tuple[str, str]]:
    return [
        (case_fn(_build(prefix_sep, "2.7", modality_sep, modality)), modality)
        for prefix_sep, modality_sep, modality, case_fn in itertools.product(
            _PREFIX_SEPS, _MODALITY_SEPS, ["t2v", "i2v", "r2v"], _CASE_FNS
        )
    ]


@pytest.mark.parametrize(("model_id", "modality"), _wan27_ids_with_modality())
def test_wan27_alias_routes_and_profiles_consistently(model_id: str, modality: str) -> None:
    """wan2.7 t2v/i2v/r2v: all combinations of hyphen/underscore prefix x hyphen/underscore modality separator x case.

    wan2.7 t2v/i2v/r2v：连字符/下划线前缀 × 连字符/下划线模态分隔符 × 大小写全组合。"""
    assert infer_endpoint(model_id, "openai") == "dashscope-async-video"
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert caps.first_frame is (modality in ("i2v", "r2v"))
    assert caps.max_prompt_chars == 5000
    assert infer_supported_durations(model_id) == list(range(2, 16))


@pytest.mark.parametrize(
    "modality_word",
    ["image-to-video", "image2video", "image_to_video"],
)
@pytest.mark.parametrize("prefix_sep", _PREFIX_SEPS)
@pytest.mark.parametrize("case_fn", _CASE_FNS)
def test_wan27_image_to_video_alias_normalizes_to_i2v(prefix_sep: str, modality_word: str, case_fn: type[str]) -> None:
    model_id = case_fn(f"wan{prefix_sep}2.7-{modality_word}")
    assert infer_endpoint(model_id, "openai") == "dashscope-async-video"
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert caps.first_frame is True
    assert caps.max_reference_images == 0  # Distinguished from the r2v tier. / 与 r2v 档区分


@pytest.mark.parametrize("prefix_sep", _PREFIX_SEPS)
@pytest.mark.parametrize("case_fn", _CASE_FNS)
def test_wan27_pure_image_variant_is_not_native_video_route(prefix_sep: str, case_fn: type[str]) -> None:
    """wan2.7-image (without the image-to-video continuation syntax) is an image variant and does not land on the native video endpoint.

    wan2.7-image（无 image-to-video 续接语法）是图像变体，不落原生视频端点。"""
    model_id = case_fn(f"wan{prefix_sep}2.7-image")
    assert infer_endpoint(model_id, "openai") == "openai-images"


def _wan3_ids() -> list[str]:
    return [
        case_fn(_build(prefix_sep, "3", modality_sep, modality))
        for prefix_sep, modality_sep, modality, case_fn in itertools.product(
            _PREFIX_SEPS, _MODALITY_SEPS, ["turbo", "video"], _CASE_FNS
        )
    ]


@pytest.mark.parametrize("model_id", _wan3_ids())
def test_wan3_alias_routes_and_durations_consistently(model_id: str) -> None:
    assert infer_endpoint(model_id, "openai") == "dashscope-async-video"
    assert infer_supported_durations(model_id) == list(range(2, 31))


@pytest.mark.parametrize("version", ["2.1", "2.2"])
@pytest.mark.parametrize("modality", ["t2v", "i2v", "r2v"])
@pytest.mark.parametrize("prefix_sep", _PREFIX_SEPS)
@pytest.mark.parametrize("case_fn", _CASE_FNS)
def test_wan2x_non_27_dash_form_stays_generic_dot_form_stays_native(
    prefix_sep: str, modality: str, version: str, case_fn: type[str]
) -> None:
    """Wan 2.1/2.2: hyphen/underscore prefixes land on the generic video endpoint, the dotted form goes native (this backend always
    requests the video-generation/video-synthesis endpoint; whether to narrow this decision needs provider API facts and a product
    judgment, see the note on classify_wan_model).

    万相 2.1/2.2：连字符/下划线前缀落通用视频端点，点号形态走原生（本后端固定请求
    video-generation/video-synthesis 端点，是否收窄该判定需要供应商 API 事实与产品判断，
    见 classify_wan_model 的说明）。"""
    model_id = case_fn(_build(prefix_sep, version, "-", modality))
    expected_endpoint = "dashscope-async-video" if prefix_sep == "" else "openai-video"
    assert infer_endpoint(model_id, "openai") == expected_endpoint
    assert infer_supported_durations(model_id) == [4, 5]


@pytest.mark.parametrize(
    ("model_id", "expected_endpoint", "expected_first_frame"),
    [
        ("proxy/wan-2.7-r2v", "dashscope-async-video", True),
        ("proxy/wan_2.7_t2v", "dashscope-async-video", False),
        ("vendor-wan2.7-i2v-0715", "dashscope-async-video", True),
    ],
)
def test_decorated_prefix_suffix_still_resolves(
    model_id: str, expected_endpoint: str, expected_first_frame: bool
) -> None:
    """Common prefix/suffix decorations from relay proxies do not affect the classification.

    代理中转常见的前后缀装饰不影响判定。"""
    assert infer_endpoint(model_id, "openai") == expected_endpoint
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert caps.first_frame is expected_first_frame


@pytest.mark.parametrize(
    "model_id",
    ["swan2.7-r2v", "vendorwan2.7-t2v", "wan20-i2v", "wan27-r2v", "swan2.1-kf2v"],
)
def test_boundary_false_positives_are_rejected(model_id: str) -> None:
    """Model names containing wan2/wan3 substrings but not in the family must not be misclassified -- identifier boundaries on both sides.

    含 wan2/wan3 子串但并非该家族的型号名不得被误判——两侧标识符边界。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert caps.max_reference_images == 0
    assert caps.max_prompt_chars is None


@pytest.mark.parametrize(
    ("model_id", "expected_endpoint"),
    [
        # WAN_DOT_FORM_PATTERN right boundary: directly followed by a letter is not the 2.x dotted form; lands on the generic video endpoint (the bare "wan"
        # substring still hits _VIDEO_PATTERN).
        # WAN_DOT_FORM_PATTERN 右边界：紧跟字母不算 2.x 点号形态，落通用视频端点（裸 "wan" 子串
        # 仍命中 _VIDEO_PATTERN）。
        ("wan2.7foo-r2v", "openai-video"),
        # HAPPYHORSE_PATTERN right boundary: directly followed by a letter is not the happyhorse family; with no video/image keyword it lands on the default
        # text endpoint.
        # HAPPYHORSE_PATTERN 右边界：紧跟字母不算 happyhorse 家族，不含视频/图像关键字，落默认
        # 文本端点。
        ("happyhorsefoo-t2v", "openai-chat"),
        # WAN_IMAGE_TO_VIDEO_PATTERN left boundary: directly adjacent letters are not the continuation syntax; handled as an image variant by its image semantics.
        # WAN_IMAGE_TO_VIDEO_PATTERN 左边界：紧邻字母不算续接语法，按含 image 语义的图像变体处理。
        ("wan-2.7-fooimage-to-video", "openai-images"),
        # WAN_IMAGE_TO_VIDEO_PATTERN right boundary: same as above. / WAN_IMAGE_TO_VIDEO_PATTERN 右边界：同上。
        ("wan-2.7-image-to-videofoo", "openai-images"),
    ],
)
def test_adjacent_letters_do_not_qualify_as_classification_tokens(model_id: str, expected_endpoint: str) -> None:
    """Identifier boundaries on both sides of classification tokens must be complete: similar substrings directly adjacent to letters/digits must not be a false hit.

    分类 token 两侧标识符边界须完整：紧邻字母/数字的相似子串不应被误判命中。"""
    assert infer_endpoint(model_id, "openai") == expected_endpoint
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert caps is UNRECOGNIZED_MODEL_CAPABILITIES


@pytest.mark.parametrize("model_id", ["swan2.7-image", "vendorwan2.7-image"])
def test_wan_substring_image_variant_routes_to_image_endpoint_despite_rejected_family(
    model_id: str,
) -> None:
    """An id not meeting the family identifier boundary (e.g. "swan2.7-image") still contains the "wan" substring and is hit by the generic
    _VIDEO_PATTERN -- excluding it to the image endpoint must not be gated on the strict family boundary, or it would be pushed to openai-video by mistake.

    不满足家族标识符边界的 id（如 "swan2.7-image"）仍含 "wan" 子串，会被通用 _VIDEO_PATTERN
    命中——排除到图像端点的判定不能拿严格家族边界做门槛，否则会被误推到 openai-video。"""
    assert infer_endpoint(model_id, "openai") == "openai-images"


@pytest.mark.parametrize("model_id", ["image-proxy/wan-2.7-i2v", "proxy-image/wan_2.7-r2v"])
def test_wan27_known_modality_not_downgraded_by_unrelated_image_decoration(model_id: str) -> None:
    """When wan2.7 already resolved to a known t2v/i2v/r2v profile, an unrelated "image" substring elsewhere in the id (e.g. a proxy
    namespace prefix) must not make it an image variant -- the known profile already establishes video semantics and takes precedence over a generic image-substring check.

    wan2.7 已解析出已知 t2v/i2v/r2v profile 时，id 别处（如代理命名空间前缀）另含无关 "image"
    子串不应被误判成图像变体——已知 profile 本身已确立视频语义，须优先于笼统 image 子串判定。"""
    assert infer_endpoint(model_id, "openai") == "dashscope-async-video"


@pytest.mark.parametrize("model_id", ["wan3.0-image-edit", "wan-3-turbo-image-preview"])
def test_wan3_image_variant_still_routes_to_image_endpoint(model_id: str) -> None:
    """wan3 has a single profile key, so has_known_modality is always true and does not tell t2v/i2v/r2v from real image aliases like
    image-edit -- the known-modality exemption above does not apply to wan3; real image aliases must still land on the image endpoint.

    wan3 只有单一 profile key，has_known_modality 恒真，不区分 t2v/i2v/r2v 与 image-edit 等真
    图像别名——上一条已知 modality 豁免不套用到 wan3，真图像别名仍须正确落图像端点。"""
    assert infer_endpoint(model_id, "openai") == "openai-images"


@pytest.mark.parametrize("model_id", ["image-proxy/wan-2.7-s2v", "image-proxy/wan-2.7-v2v", "wan2.7-videoedit"])
def test_wan27_known_unsupported_video_modality_not_downgraded_by_unrelated_image_decoration(
    model_id: str,
) -> None:
    """s2v/v2v/videoedit hit the family regex but this backend does not implement request building for them (has_known_modality=False);
    they are still confirmed video modalities -- an unrelated "image" substring elsewhere in the id must not make them image variants;
    they must land on the generic video endpoint (not the image endpoint).

    s2v/v2v/videoedit 命中家族正则但本后端未实现请求构造（has_known_modality=False），仍是
    已确认的视频模态——id 别处另含无关 "image" 子串时不应被误判成图像变体，须落通用视频端点
    （而非图像端点）。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"


@pytest.mark.parametrize("model_id", ["wan-2.7-fooimage-to-video", "wan-2.7-image-to-videofoo"])
def test_wan27_unrecognized_modality_with_image_substring_stays_image_variant(model_id: str) -> None:
    """Other names that match none of the known modality tokens (t2v/i2v/r2v/s2v/v2v/videoedit) are still conservatively treated as image
    variants even if they hit the family regex, and must not be swallowed by the broad rule "family==wan2.7 means video".

    未落入任一已知模态 token（t2v/i2v/r2v/s2v/v2v/videoedit）的其余命名，即便命中家族正则，
    仍保守按图像变体处理，不能被"family==wan2.7 即视为视频"的宽泛判定误吞。"""
    assert infer_endpoint(model_id, "openai") == "openai-images"


@pytest.mark.parametrize("model_id", ["wan2.7-i2v-s2v", "wan-2.7-r2v-v2v"])
def test_wan27_known_token_does_not_mask_coexisting_s2v_v2v(model_id: str) -> None:
    """When an id contains both a known profile token (i2v/r2v) and s2v/v2v: the fact that s2v/v2v has no request building takes
    precedence over the known-token hit and must not be masked by it into wrongly allowing the native route (videoedit coexisting
    with a known token is excluded with the same precedence).

    id 同时含已知 profile token（i2v/r2v）与 s2v/v2v 时：s2v/v2v 未实现请求构造这一事实优先于
    已知 token 命中，不能被后者掩盖而误放行原生路由（videoedit 与已知 token 共存时同样按此优先级
    排除）。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"
    assert infer_supported_durations(model_id) != list(range(2, 16))


@pytest.mark.parametrize("model_id", ["s2v-proxy/wan2.7-image", "v2v-service/wan-2.7-image"])
def test_wan27_image_variant_not_upgraded_by_s2v_v2v_decoration_before_marker(model_id: str) -> None:
    """s2v/v2v recognition only applies within the modality segment after the wan2.7 marker, not the decorating prefix before it -- a real
    image variant (modality segment literally "image") must not be misjudged as a known video modality because "s2v"/"v2v" happens to appear in the prefix.

    s2v/v2v 的识别只在 wan2.7 标记之后的模态段内生效，不含标记前的装饰前缀——真图像变体
    （模态段字面即 "image"）不应被装饰前缀里恰好出现的 "s2v"/"v2v" 误判成已知视频模态。"""
    assert infer_endpoint(model_id, "openai") == "openai-images"


@pytest.mark.parametrize("model_id", ["minimax-s2v-swan", "minimax-s2v-swan2"])
def test_minimax_s2v_routing_requires_the_exact_model_name(model_id: str) -> None:
    """MiniMax S2V only recognizes exact model names (S2V-01 / minimax-s2v-01, compared after stripping the namespace prefix): other word
    forms containing s2v have no knowable upstream protocol and are not swallowed into the S2V protocol that requires reference images --
    these two ids happen to contain "wan", hit the generic video family regex and land on openai-video.

    MiniMax S2V 只认精确型号名（S2V-01 / minimax-s2v-01，剥离命名空间前缀后比较）：其余含
    s2v 子串的词形无从确知上游协议，不被误吞成参考图必需的 S2V 协议——这两个 id 恰含 "wan"
    子串，命中通用视频家族正则，落 openai-video。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"


@pytest.mark.parametrize("model_id", ["wan2-s2v", "wan-2-s2v", "wan_2-s2v"])
def test_wan_glued_bare_digit_version_excluded_from_minimax_routing(model_id: str) -> None:
    """When "wan" is glued directly to a pure-integer version (no dot), as long as the identifier left boundary holds ("wan2", not "swan2"),
    it should still be recognized as a wan version token and excluded from the MiniMax S2V route, the same conclusion as the separated
    equivalents ("wan-2"/"wan_2").

     "wan" 与纯整数版本号（无小数点）完全粘连时，只要满足标识符左边界（"wan2" 非 "swan2"），
    仍应识别为 wan 版本号 token、排除 MiniMax S2V 路由，与带分隔符的等价形态（"wan-2"/"wan_2"）
    结论一致。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"


@pytest.mark.parametrize(
    "model_id",
    ["image-to-video-proxy/swan2.7-image", "image-to-video-proxy/vendorwan2.7-image"],
)
def test_fallback_image_to_video_syntax_scoped_to_wan_marker_not_decoration_prefix(model_id: str) -> None:
    """On the family=None fallback path, recognizing the image-to-video continuation syntax must likewise start searching from the literal
    "wan" substring itself -- "swan2.7-image"/"vendorwan2.7-image" both fail the strict family boundary (family=None), and the continuation
    spelling inside the decorating prefix "image-to-video-proxy/" is not part of the id's own modality segment, so a real image variant
    must not be misjudged as a video continuation.

    family=None 兜底路径的 image-to-video 续接语法识别同样须从字面 "wan" 子串本身开始搜索——
    "swan2.7-image"/"vendorwan2.7-image" 均不满足家族严格边界（family=None），装饰前缀
    "image-to-video-proxy/" 里的续接语法拼写不属于该 id 自身的模态段，不能把真图像变体误判成
    视频续接。"""
    assert infer_endpoint(model_id, "openai") == "openai-images"


@pytest.mark.parametrize(
    "model_id",
    ["vendorwan2.7-videoedit-proxy/wan2.7-r2v", "wan2.7s2v-proxy/wan2.7-i2v"],
)
def test_wan27_suffix_not_polluted_by_literal_marker_text_in_decoration_prefix(model_id: str) -> None:
    """wan27_suffix must be taken from the match position of the family marker itself (family_match), not located by searching the literal
    text "wan2.7" in the concatenated full profile_key -- a decorating prefix before the marker may itself contain the literal substring
    "wan2.7" (not meeting the family identifier boundary, not judged a family marker, like "wan2.7" in "vendorwan2.7-videoedit-proxy/"),
    and a text search cannot tell such prefix noise from the real family marker position, so it would take prefix content
    ("videoedit"/"s2v") as part of the modality segment.

    wan27_suffix 须取自家族标记本身的匹配位置（family_match），不能靠在拼接后的完整
    profile_key 上搜索 "wan2.7" 字面文本来定位——标记前的装饰前缀可能自身就含 "wan2.7" 字面子串
    （不满足家族标识符边界、未被判定为家族标记，如 "vendorwan2.7-videoedit-proxy/" 里的
    "wan2.7"），文本搜索无法区分这类前缀噪音与真正的家族标记位置，会把装饰前缀内容
    （"videoedit"/"s2v"）误当模态段的一部分。"""
    assert infer_endpoint(model_id, "openai") == "dashscope-async-video"
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert caps is not UNRECOGNIZED_MODEL_CAPABILITIES


def test_wan27_videoedit_excluded_from_family_duration_preset() -> None:
    """This backend does not implement request building for wan2.7-videoedit, so its duration does not use the t2v/i2v/r2v family tier
    (it lands on the generic preset, not the family-specific full 2-15s range).

    wan2.7-videoedit 本后端未实现该模态的请求构造，时长不套用 t2v/i2v/r2v 家族档
    （落到通用预设，而非家族专属的 2-15s 全档）。"""
    assert infer_supported_durations("wan2.7-videoedit") != list(range(2, 16))


def test_wan27_videoedit_excluded_even_alongside_recognized_modality_token() -> None:
    """"wan2.7-i2v-videoedit" contains both a known profile token (i2v) and the videoedit marker: the fact that videoedit has no request
    building takes precedence over the known-token hit and must not be masked by it into wrongly allowing the native route.

     "wan2.7-i2v-videoedit" 同时含已知 profile token（i2v）与 videoedit 标记：videoedit
    未实现请求构造这一事实优先于已知 token 命中，不能被后者掩盖而误放行原生路由。"""
    assert infer_endpoint("wan2.7-i2v-videoedit", "openai") == "openai-video"
    assert infer_supported_durations("wan2.7-i2v-videoedit") != list(range(2, 16))


@pytest.mark.parametrize("model_id", ["proxy-videoeditor/wan2.7-i2v", "proxy-videoedit-service/wan_2.7-r2v"])
def test_videoedit_detection_ignores_decoration_before_wan27_marker(model_id: str) -> None:
    """The "videoedit" check only applies within the modality segment after the wan2.7 marker, not the decorating prefix before it: unrelated
    word forms like "videoeditor" fail the identifier boundary anyway; decorating prefixes like "videoedit-service" do not count even if
    they meet the boundary -- real modalities unrelated to the proxy namespace (e.g. i2v/r2v) should land on the native route normally.

     "videoedit" 判定只在 wan2.7 标记之后的模态段内生效，不含标记前的装饰前缀："videoeditor"
    一类无关词形本就不满足标识符边界；"videoedit-service" 这类装饰前缀即便满足边界也不计入——
    与代理命名空间无关的真实模态（如 i2v/r2v）应正常落原生路由。"""
    assert infer_endpoint(model_id, "openai") == "dashscope-async-video"


@pytest.mark.parametrize("model_id", ["wan2.7-r2v-videoedit", "wan_2.7-videoedit-0715"])
def test_videoedit_detected_within_modality_segment_after_marker(model_id: str) -> None:
    """When a real videoedit marker appears in the modality segment after the wan2.7 marker (with or without a known token or suffix
    decoration), it must still be hit and excluded from the native route.

    真正的 videoedit 标记出现在 wan2.7 标记之后的模态段内（无论是否伴随已知 token 或后缀
    装饰）时，仍要正确命中并排除出原生路由。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"


@pytest.mark.parametrize("model_id", ["proxy-videoedit/wan3-turbo", "wan-3-turbo-videoedit"])
def test_videoedit_exclusion_scoped_to_wan27_only(model_id: str) -> None:
    """The videoedit exclusion only applies to the wan2.7 family -- a wan3 id containing the "videoedit" substring (decorating prefix or other
    source) must not be excluded from the native route; the missing capability for that modality is recorded only under the wan2.7 family.

    videoedit 排除只对 wan2.7 家族生效——wan3 的 id 即便含 "videoedit" 子串（装饰前缀或
    其他来源），也不应被误排除出原生路由，该模态的能力欠缺只记录在 wan2.7 家族下。"""
    assert infer_endpoint(model_id, "openai") == "dashscope-async-video"
    assert infer_supported_durations(model_id) == list(range(2, 31))


@pytest.mark.parametrize(
    ("model_id", "expected_endpoint"),
    [
        ("wan-2.7-v2v", "openai-video"),
        ("wan_2.7-foo", "openai-video"),
        ("wan-2.7-s2v-0715", "openai-video"),
    ],
)
def test_wan27_unrecognized_modality_excluded_from_native_route_and_family_duration(
    model_id: str, expected_endpoint: str
) -> None:
    """When the wan2.7 family is hit but the modality has no request building (not t2v/i2v/r2v, other unknown suffixes besides videoedit),
    it does not land on the native endpoint and its duration does not use the family tier -- the same exclusion path as videoedit.

    wan2.7 家族命中但模态未实现请求构造（非 t2v/i2v/r2v，videoedit 之外的其余未知后缀）时，
    不落原生端点、时长也不套用家族档——与 videoedit 走同一条排除路径。"""
    assert infer_endpoint(model_id, "openai") == expected_endpoint
    assert infer_supported_durations(model_id) != list(range(2, 16))


@pytest.mark.parametrize("model_id", ["wan2.6-image-to-video", "wan2.1-image-to-video"])
def test_wan2x_dot_image_to_video_has_no_registered_capability_falls_back_to_generic(model_id: str) -> None:
    """wan2x_dot (dotted forms other than 2.7) has no registered VideoCapabilities; if a hit on the image-to-video continuation syntax still
    allowed the native route, it would silently get the unified fallback (which happens to have first_frame=True, masking the problem).
    Ids without verified capabilities/request schema are excluded from the native route and land on the generic video endpoint.

    wan2x_dot（2.7 以外的点号形态）没有登记任何 VideoCapabilities；image-to-video 续接语法
    命中时若仍放行原生路由，会静默拿到 统一回落（恰好 first_frame=True，掩盖问题）。
    没有已验证能力/请求 schema 的 id 排除出原生路由，落通用视频端点。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"


@pytest.mark.parametrize("model_id", ["wan-2.2-image-to-video", "wan_2.6-image2video"])
def test_image_to_video_syntax_recognized_even_when_family_boundary_unmet(model_id: str) -> None:
    """Recognizing the image-to-video continuation syntax does not depend on the strict family boundary: when the family separator (a
    hyphen between wan and the version) fails the dotted-form boundary and classify_wan_model returns family=None, the continuation
    syntax info must still be carried out as is, not swallowed into the image endpoint by the generic image check.

    image-to-video 续接语法的识别不依赖家族严格边界：家族分隔符（连字符隔开 wan 与版本号）
    未满足点号形态边界、classify_wan_model 判定 family=None 时，续接语法信息仍须原样带出，
    不能被笼统 image 判定误吞成图像端点。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"


@pytest.mark.parametrize("model_id", ["wan-2.2-s2v", "wan_2.6-s2v", "vendorwan2.7-s2v"])
def test_wan_substring_s2v_excluded_from_minimax_routing_even_without_family_match(model_id: str) -> None:
    """The bare "s2v" exclusion from the MiniMax route must likewise not be gated on the strict family boundary: these ids contain "wan" but
    fail the family boundary (family=None), and should still land on the generic video endpoint rather than be swallowed into the MiniMax S2V protocol.

    裸 "s2v" 排除 MiniMax 路由的判定同样不能拿家族严格边界做门槛：这些 id 含 "wan" 子串但
    家族边界未满足（family=None），仍应落通用视频端点而非被误吞成 MiniMax S2V 协议。"""
    assert infer_endpoint(model_id, "openai") == "openai-video"


@pytest.mark.parametrize(
    ("model_id", "expected_max_reference_images"),
    [
        ("proxy/happyhorse-1.0-r2v", 9),
        ("happyhorse-1.0-r2v-0715", 9),
        ("myhappyhorse-1.0-r2v", 0),  # Rejected by the identifier boundary: not the happyhorse family. / 标识符边界拒绝：非 happyhorse 家族
    ],
)
def test_happyhorse_boundary_and_decoration(model_id: str, expected_max_reference_images: int) -> None:
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert caps.max_reference_images == expected_max_reference_images


@pytest.mark.parametrize(
    "model_id",
    [
        *_wan27_ids(),
        *_wan3_ids(),
        "happyhorse-1.0-t2v",
        "happyhorse-1.1-i2v",
        "happyhorse-1.0-r2v",
    ],
)
def test_native_route_and_non_default_profile_agree(model_id: str) -> None:
    """Consistency assertion: for family members with a resolvable modality (t2v/i2v/r2v), hitting the native route implies a non-default capability tier, and vice versa.

    The assertion only covers members with a resolvable modality -- bare family names (e.g. "happyhorse"), wan2.7-videoedit and dotted
    2.x forms other than 2.7 ("wan2.1-*") land on the default tier by design because their modality/protocol is not established, and are
    not constrained by this assertion (see the notes on classify_wan_model and _profile_for_model).

    一致性断言：家族成员且模态可解析（t2v/i2v/r2v）时，命中原生路由必有非默认能力档，反之亦然。

    该断言只覆盖模态可解析的成员——裸家族名（如 "happyhorse"）、wan2.7-videoedit、非 2.7 的点号形态
    2.x（"wan2.1-*"）均因模态/协议未确权而按设计落默认档，不受本断言约束（见 classify_wan_model
    与 _profile_for_model 的说明）。
    """
    routed_native = infer_endpoint(model_id, "openai") == "dashscope-async-video"
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    # Use object identity rather than field equality to decide "did it resolve to a known tier" -- happyhorse-i2v's declared field values
    # happen to equal VideoCapabilities()'s defaults item by item (first_frame defaults to True), so a field-level comparison would miss it.
    # `_profile_for_model` returns the original object from `_MODEL_PROFILES` on a known-key hit and the `UNRECOGNIZED_MODEL_CAPABILITIES`
    # singleton otherwise; the two differ in identity, reliably telling "resolved" from "fell back to default".
    # 用对象身份而非字段相等判定"是否解析出已知档"——happyhorse-i2v 的声明字段值恰好与
    # VideoCapabilities() 的默认值逐项相同（first_frame 默认即 True），字段级比较会漏判。
    # `_profile_for_model` 命中已知 key 时返回 `_MODEL_PROFILES` 里的原对象，未命中时返回
    # `UNRECOGNIZED_MODEL_CAPABILITIES` 单例，两者身份不同，可靠区分"解析成功"与"落回默认"。
    has_known_profile = caps is not UNRECOGNIZED_MODEL_CAPABILITIES
    assert routed_native is True
    assert has_known_profile is True


@pytest.mark.parametrize(
    "model_id",
    [
        # Known token coexists with an unimplemented modality; has_known_modality should be excluded by the latter.
        "wan2.7-i2v-s2v",  # 已知 token 与未实现模态共存，has_known_modality 应被后者排除
        "wan-2.7-r2v-v2v",
        "wan2.7-r2v-videoedit",
        # The unimplemented modality is excluded on its own; the decorating prefix does not change the conclusion.
        "image-proxy/wan-2.7-s2v",  # 未实现模态本身即被排除，装饰前缀不改变结论
    ],
)
def test_excluded_from_native_route_agrees_with_default_profile(model_id: str) -> None:
    """The exclusion direction of the consistency assertion: when has_known_modality=False both decision points must fall back to the default
    tier consistently, with no contradictory combination of "not on the native route yet resolving a real capability tier" -- if the
    substring-tolerant matching in `_profile_for_model` did not check has_known_modality, it would find a coexisting known token in the
    decorating suffix (e.g. "i2v" in "wan2.7-i2v-s2v") and wrongly resolve the capability tier of that implemented modality.

    一致性断言的排除方向：has_known_modality=False 时两个判定点须一致落回默认档，不能出现
    「不落原生路由却仍解析出真实能力档」的互斥组合——`_profile_for_model` 的子串容忍匹配若不
    检查 has_known_modality，会在装饰后缀里找到共存的已知 token（如 "wan2.7-i2v-s2v" 里的
    "i2v"）而误判出该已实现模态的能力档。"""
    routed_native = infer_endpoint(model_id, "openai") == "dashscope-async-video"
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert routed_native is False
    assert caps is UNRECOGNIZED_MODEL_CAPABILITIES


@pytest.mark.parametrize(
    "model_id",
    [
        # The decorating prefix contains the full "image-to-video" spelling.
        "image-to-video-proxy/wan2.7-image",  # 装饰前缀含完整 "image-to-video" 拼写
        "video-continuation-proxy/wan-2.7-image",
    ],
)
def test_wan27_image_variant_not_upgraded_by_image_to_video_decoration_before_marker(model_id: str) -> None:
    """Like videoedit/s2v/v2v, recognizing the image-to-video continuation syntax only applies within the modality segment after the wan2.7
    marker: even if the decorating prefix before the marker literally spells the full "image-to-video", a real image variant must not be
    misjudged as a video continuation.

    image-to-video 续接语法的识别同 videoedit/s2v/v2v 一样只在 wan2.7 标记之后的模态段内生效：
    标记前的装饰前缀即便字面拼出完整 "image-to-video"，也不能把真图像变体误判成视频续接。"""
    assert infer_endpoint(model_id, "openai") == "openai-images"


@pytest.mark.parametrize("model_id", ["wan2.7-image-to-video", "wan_2.7-image2video"])
def test_wan27_image_to_video_detected_within_modality_segment_after_marker(model_id: str) -> None:
    """When the real image-to-video continuation syntax appears in the modality segment after the wan2.7 marker, it must still be hit and folded
    into i2v, landing on the native route and a non-default capability tier.

    真正的 image-to-video 续接语法出现在 wan2.7 标记之后的模态段内时，仍要正确命中并折算成
    i2v，落原生路由与非默认能力档。"""
    assert infer_endpoint(model_id, "openai") == "dashscope-async-video"
    caps = DashScopeVideoBackend.video_capabilities_for_model(model_id)
    assert caps is not UNRECOGNIZED_MODEL_CAPABILITIES
