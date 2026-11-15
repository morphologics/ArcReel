"""Read-time derivation from video unit text: utterances + degradation visibility warnings.

The text is the single source of truth; utterances, like reference images, are derived mechanically and not saved
(see ADR 0064).

Dialogue syntax (same source as the inline splitting primitives of :mod:`lib.script.reference_video.text_parser`):

- ``@[角色]{台词}`` (whitespace or a Chinese/English colon allowed between the mention and the brace) → ``dialogue`` utterance
- bare ``{台词}`` → ``voiceover`` utterance
- Both may appear anywhere in a line, several per line; braces not recognized as utterance markers derive nothing
  and are kept as-is, only emitting a warning -- no "guess the speaker from the nearest mention in the line"
  heuristic, since a wrong guess would silently bind the dialogue line to the wrong character's reference audio

Warnings are locale-neutral ``{"key", "params"}`` entries (same shape as the existing ``result.warnings``),
rendered by the router / task list in the request language.

视频单元正文的读时派生：utterances + 降级可见性 warning。

正文是唯一真相，utterances 与参考图一样机械派生、不落盘（见 ADR 0064）。

台词语法（与 :mod:`lib.script.reference_video.text_parser` 的行内切分原语同源）：

- ``@[角色]{台词}``（mention 与花括号之间允许空白或中英冒号）→ ``dialogue`` utterance
- 裸 ``{台词}`` → ``voiceover`` utterance
- 两者都可出现在行内任意位置，一行可有多条；花括号没被识别成发声记号时不派生、原样保留，
  只出 warning——不做「行内最近 mention 猜 speaker」启发式，推断错误会把台词静默绑到
  错误角色的参考音频上

warning 是 locale-neutral 的 ``{"key", "params"}`` 条目（同 ``result.warnings`` 既有形态），
由 router / 任务列表按请求语言渲染。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from lib.project.asset_types import BUCKET_KEY, asset_name_comparison_key, normalize_asset_bucket
from lib.references.reference_catalog import build_reference_catalog, split_derivative_reference
from lib.script.reference_video.text_parser import (
    derive_references_from_text,
    speech_line_description,
    speech_speaker_references,
    split_speech_line,
)
from lib.script.reference_video.voice_settings import VoiceRenderSettings
from lib.script.script_models import Utterance

#: Maximum length of the original line snippet echoed in the unclosed brace warning. / 未闭合花括号 warning 里回显的原行片段长度上限。
_EXCERPT_LEN = 30

WARN_UNREGISTERED_MENTION = "ref_warn_unregistered_mention"
WARN_UNCLOSED_BRACE = "ref_warn_unclosed_brace"
WARN_BRACES_NOT_SPEECH = "ref_warn_braces_not_speech"
WARN_UNREGISTERED_SPEAKER = "ref_warn_unregistered_speaker"
WARN_SPEAKER_WITHOUT_AUDIO = "ref_warn_speaker_without_audio"
WARN_SPEAKER_AUDIO_UNAVAILABLE = "ref_warn_speaker_audio_unavailable"
WARN_REFERENCE_AUDIO_OVERFLOW = "ref_warn_reference_audio_overflow"
WARN_SILENT_MODEL = "ref_warn_silent_model"
WARN_SILENT_EPISODE = "ref_warn_silent_episode"
WARN_SPEAKER_AUDIO_NEEDS_IMAGE = "ref_warn_speaker_audio_needs_image"
WARN_UNIT_WITHOUT_SCENE = "ref_warn_unit_without_scene"


@dataclass(frozen=True)
class ScriptPreview:
    """The read-time derivation result of one video unit text, i.e. the content source of the editor's "parse preview panel".

    一份视频单元正文的读时派生结果，即编辑器「解析预览面板」的内容源。"""

    utterances: list[Utterance]
    warnings: list[dict[str, Any]] = field(default_factory=list)


def _warning(key: str, **params: Any) -> dict[str, Any]:
    return {"key": key, "params": params}


def derive_utterances(text: str) -> tuple[list[Utterance], list[dict[str, Any]]]:
    """Derive utterances line by line and collect syntax-level warnings (unclosed braces / unrecognized braces).

    Utterance markers may appear anywhere in a line, one line may derive several utterances, in the order they appear
    in the text. Warnings only look at the residual description **outside markers**: a marker already recognized in a
    line should not be swallowed because the same line also has other braces; the ``line`` parameter is a 1-based line
    number so the presentation layer can anchor the warning to a specific line of the text.

    Pure syntax layer: it doesn't know the project asset table; whether a speaker is registered is decided separately
    by :func:`build_script_preview`.

    逐行派生 utterances，并收集语法层 warning（未闭合花括号 / 未识别的花括号）。

    发声记号可出现在行内任意位置，一行可派生多条 utterance，顺序即正文出现顺序。warning
    只看**记号之外**的残余描述：行内已识别的记号不该因为同行还写着别的花括号而被吞掉；
    ``line`` 参数是 1-based 行号，让呈现层把 warning 锚回正文的具体一行。

    纯语法层：不认识项目资产表，speaker 是否登记由 :func:`build_script_preview` 另行判定。
    """
    utterances: list[Utterance] = []
    warnings: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        parts = split_speech_line(line)
        for part in parts:
            if isinstance(part, str):
                continue
            if part.speaker:
                utterances.append(Utterance(kind="dialogue", speaker=part.speaker, text=part.text))
            else:
                utterances.append(Utterance(kind="voiceover", text=part.text))
        rest = speech_line_description(parts)
        if rest.count("{") != rest.count("}"):
            warnings.append(_warning(WARN_UNCLOSED_BRACE, line=index, excerpt=line.strip()[:_EXCERPT_LEN]))
        elif "{" in rest or "}" in rest:
            warnings.append(_warning(WARN_BRACES_NOT_SPEECH, line=index))
    return utterances, warnings


@dataclass(frozen=True)
class VoiceBindings:
    """The voice derivation result of a text: who speaks and who is bound to which reference audio clip.

    ``speakers`` are the registered dialogue speakers (first-appearance order) -- part one's voice trait declarations
    are emitted in this order. ``audio_speakers`` is the subset actually bound to reference audio, **and its order is
    the ``@音频N`` (@audioN) numbering and the field order of ``VideoGenerationRequest.reference_audio_files``** (both
    come from one derivation, not recomputed separately).

    一份文稿的声音派生结果：谁在说话、谁绑到了第几段参考音频。

    ``speakers`` 是已登记的 dialogue speaker（首现顺序）——第一段声音特征声明按此顺序逐条发出。
    ``audio_speakers`` 是其中真正绑上参考音频的子集，**顺序即 ``@音频N`` 编号与
    ``VideoGenerationRequest.reference_audio_files`` 的字段顺序**（两者同一份派生，不各自重算）。
    """

    speakers: list[str]
    audio_speakers: list[str]
    warnings: list[dict[str, Any]]


def derive_voice_bindings(
    utterances: list[Utterance],
    characters: dict,
    settings: VoiceRenderSettings,
    *,
    speakers_with_reference_image: Collection[str] | None = None,
) -> VoiceBindings:
    """Mechanically derive voice bindings from utterances: speaker order, reference audio numbering and degradation warnings.

    ``settings`` gathers the voice input profile of this render (see :class:`~lib.script.reference_video.voice_settings
    .VoiceRenderSettings`). Only ``native`` (class A, native audio reference) involves reference audio binding and
    limits, so the "no reference audio set" and "over the clip limit" warnings are only emitted for that profile; for
    ``none`` (truly silent) a silent notice is emitted instead.

    When ``settings.requested_generate_audio`` is False, ``audio_speakers`` is never produced regardless of the model
    tier, and an "episode is silent" notice is emitted instead. Reference audio is gated only here, so the prompt's
    ``@音频N`` and ``reference_audio_files`` disappear from the same source, and there is no divergence where the text
    promises a voice reference but the request has no matching audio clip; dialogue rendering is independent of this
    switch (see also :func:`lib.script.reference_video.prompt_render
    ._render_segment_two`), and the provider can still use the dialogue text as a lip-sync reference. The silent
    notice takes precedence over the ``none`` profile's ``WARN_SILENT_MODEL``: both mean "this episode has no audible
    sound", and when the user deliberately turned audio off, saying the model produces no audio would point
    troubleshooting in the wrong direction.

    When ``settings.audio_ready`` is None, decide by the character asset's ``reference_audio`` field being non-empty;
    the execution layer passes the names of characters whose files are resolved and actually exist, so that the
    numbering is strictly the same length as the audio clips actually sent with the request -- if the field points to a
    deleted file and the numbering is not in sync, ``@音频N`` would point to a nonexistent clip. Both paths share this
    function, avoiding divergence between the binding the preview promises and the one generation actually sends. When
    not None, two degradation reasons are distinguished: the character's ``reference_audio`` field unset emits
    ``WARN_SPEAKER_WITHOUT_AUDIO``; the field set but not in the set emits ``WARN_SPEAKER_AUDIO_UNAVAILABLE`` -- the
    former means add audio in the character settings, the latter means the field is filled and the audio it points to
    should be checked; different troubleshooting directions, so they can't be merged into one warning. The latter has
    more than one cause (a deleted file, or a field value pointing outside ``characters/refs_audio``, are both excluded
    by :func:`lib.script.reference_video.prompt_render.resolve_reference_audio_paths`), so the copy only says
    "unavailable" without asserting which.

    When ``settings.requires_reference_image`` is True (the target backend requires each audio clip to be attached to a
    specific reference item, e.g. wan2.7-r2v), off-screen-only (no reference image) speakers are not bound even with
    usable audio -- once bound, that character's audio clip would count toward ``max_reference_audio``, but the backend
    has no item to attach it to, so it would either be mismatched to another character/scene or fail hard. The
    degradation emits its own warning (rather than reusing "no reference audio set", since the cause differs: here the
    audio really is usable, there's just no visual to attach to). ``speakers_with_reference_image`` is the set of
    character names for the reference images actually sent with this request, read only when that flag is True.

    The character table and the two name-based sets (``audio_ready`` / ``speakers_with_reference_image``) are all
    first normalized into the asset-name comparison coordinate system
    (:func:`lib.project.asset_types.asset_name_comparison_key`). Missing normalization on one side doesn't error but
    silently degrades -- the character is judged "unregistered" and gets no voice declaration, or "no usable audio" and
    gets no reference audio binding, and the user gets a final cut with the wrong voice plus a non-blocking warning.

    从 utterances 机械派生声音绑定：说话人顺序、参考音频编号与降级 warning。

    ``settings`` 收口本次渲染的声音输入档（见 :class:`~lib.script.reference_video.voice_settings
    .VoiceRenderSettings`）。只有 ``native``（A 类·原生音频参考）才谈得上参考音频的绑定与
    上限，故「未设参考音频」「超出段数上限」两条只在该档发出；``none``（真无声）时改发一条
    无声知会。

    ``settings.requested_generate_audio`` 为 False 时无论模型档位如何都不产出
    ``audio_speakers``，改发一条本集无声知会。参考音频只在这一处收口，prompt 的 ``@音频N``
    与 ``reference_audio_files`` 因此同源消失，不会出现文本承诺了音色参考、请求里却没有对应
    音频段的分叉；台词渲染与本开关无关（另见 :func:`lib.script.reference_video.prompt_render
    ._render_segment_two`），供应商可继续拿台词文本做口型参考。无声知会优先于 ``none`` 档的
    ``WARN_SILENT_MODEL``：两者都是「这一集听不到声音」，用户主动关掉音频时说模型不产音会指错
    排查方向。

    ``settings.audio_ready`` 为 None 时按角色资产的 ``reference_audio`` 字段非空判定；执行层
    传入已解析且确实存在的文件对应的角色名，让编号与实际随请求发出的音频段数严格等长——字段
    指向已删文件时编号若不同步，``@音频N`` 会指向不存在的段。两条路径共用本函数，避免预览
    承诺的绑定与生成实际发出的绑定分叉。非 None 时降级原因区分两种：角色 ``reference_audio``
    字段未设置发 ``WARN_SPEAKER_WITHOUT_AUDIO``；字段有值但不在集合内发
    ``WARN_SPEAKER_AUDIO_UNAVAILABLE``——前者该去角色设置里补音频，后者字段已填好，该去查
    它指向的音频本身，排查方向不同，不能合并成一条 warning。后者的成因不止一种（文件被删、
    字段值指到 ``characters/refs_audio`` 之外都会被
    :func:`lib.script.reference_video.prompt_render.resolve_reference_audio_paths` 排除），故文案
    只说「不可用」，不断言具体是哪一种。

    ``settings.requires_reference_image`` 为 True 时（目标 backend 的音频必须逐段挂在具体
    参考素材项上，如 wan2.7-r2v），纯画外（无参考图）speaker 即使有可用音频也不绑定——绑定后
    该角色的音频段数会算进 ``max_reference_audio``，但 backend 没有素材项可挂，要么错配给
    别的角色/场景要么硬失败。降级发一条独立 warning（而非复用「未设参考音频」，原因不同：
    这里音频确实可用，只是没有画面可挂）。``speakers_with_reference_image`` 是本次实际随
    请求发出的参考图对应的角色名集合，仅在该位为 True 时读取。

    角色表与两个按名字判定的集合（``audio_ready`` / ``speakers_with_reference_image``）都先归一
    到资产名比对坐标系（:func:`lib.project.asset_types.asset_name_comparison_key`）。少归一一侧的后果不是
    报错而是静默降级——该角色被判「未登记」而不发音色声明、或判「无可用音频」而不绑参考音频，
    用户拿到的是一条声音不对的成片加一条非阻断 warning。
    """
    warnings: list[dict[str, Any]] = []
    characters = normalize_asset_bucket(characters)

    seen: list[str] = []
    for entry in utterances:
        speaker = asset_name_comparison_key(entry.speaker or "")
        if speaker and speaker not in seen:
            seen.append(speaker)

    registered: list[str] = []
    for speaker in seen:
        if speaker in characters:
            registered.append(speaker)
        else:
            warnings.append(_warning(WARN_UNREGISTERED_SPEAKER, name=speaker))

    audio_speakers: list[str] = []
    if settings.is_silent:
        # Only notify for attributed dialogue: unattributed narration never enters the prompt (see
        # :func:`lib.script.reference_video.prompt_render._render_segment_two`), and both messages say "dialogue is only used
        # as prompt reference", which would point troubleshooting the wrong way for a text with only narration. Unregistered
        # speakers count as attributed -- their lines are rendered as in the original. The "audio turned off for this
        # episode" notice takes precedence over "model produces no audio" -- the former is a switch the user can change now.
        # 只对带归属的台词知会：无归属旁白整段不进 prompt（见
        # :func:`lib.script.reference_video.prompt_render._render_segment_two`），两条文案都说
        # 「台词仅用于提示词参考」，对一份只有旁白的文稿会指错排查方向。未登记的说话人算带
        # 归属——它的台词按原文照常渲染。本集关闭音频的提示优先于模型不产音——前者是用户
        # 当下可改的开关。
        if any(entry.speaker for entry in utterances):
            if not settings.requested_generate_audio:
                warnings.append(_warning(WARN_SILENT_EPISODE))
            else:
                warnings.append(_warning(WARN_SILENT_MODEL, provider=settings.provider_id, model=settings.model_id))
    elif settings.voice_consistency == "native":
        image_names = {asset_name_comparison_key(name) for name in speakers_with_reference_image or ()}
        audio_ready = (
            {asset_name_comparison_key(name) for name in settings.audio_ready}
            if settings.audio_ready is not None
            else None
        )
        # Audio numbering = dialogue speaker first-appearance order, truncated at the max_reference_audio limit.
        # 音频编号 = dialogue speaker 首现顺序，受 max_reference_audio 上限截断。
        for speaker in registered:
            char_data = characters.get(speaker)
            audio_field_set = bool(char_data.get("reference_audio")) if isinstance(char_data, dict) else False
            has_audio = speaker in audio_ready if audio_ready is not None else audio_field_set
            if not has_audio:
                if audio_ready is not None and audio_field_set:
                    warnings.append(_warning(WARN_SPEAKER_AUDIO_UNAVAILABLE, name=speaker))
                else:
                    warnings.append(_warning(WARN_SPEAKER_WITHOUT_AUDIO, name=speaker))
            elif settings.requires_reference_image and speaker not in image_names:
                warnings.append(_warning(WARN_SPEAKER_AUDIO_NEEDS_IMAGE, name=speaker))
            elif len(audio_speakers) >= settings.max_reference_audio:
                warnings.append(
                    _warning(WARN_REFERENCE_AUDIO_OVERFLOW, limit=settings.max_reference_audio, name=speaker)
                )
            else:
                audio_speakers.append(speaker)

    return VoiceBindings(speakers=registered, audio_speakers=audio_speakers, warnings=warnings)


def unit_lacks_scene_reference(text: str, project: dict) -> bool:
    """The unit text references no registered scene asset at all (only applies when the project has registered scenes).

    Without a scene reference the model freely decides the location, and adjacent units alternating indoors/outdoors
    each improvise and don't match up; when the project has not registered any scene asset this is always False --
    there is no scene to reference and the hint could suggest no action (product-centered ad projects fall into this
    case).

    The single exit for this criterion: the editor preview's read-time warning and the per-unit hint in the split
    receipt both go through this function; if each decided separately, the same text would give opposite conclusions in
    the two panels. Decided on the untrimmed derived result: this asks whether the author wrote a scene reference, not
    which images the capability limit trimmed.

    该单元正文一个已登记场景资产都没引用（项目登记了场景时才成立）。

    未引用场景时画面地点由模型自由决定，室内外交替的相邻单元会各自发挥、对不上；项目一张
    场景资产都没登记时恒为 False——那时没有可引用的场景，提示指不出任何动作（商品为主体的
    广告项目即属此列）。

    判据的唯一出口：编辑器预览的读时 warning 与拆分回执的逐 unit 提示都经本函数，分成两处
    各自判定时，同一份正文会在两个面板给出相反的结论。按未裁剪的派生结果判定：这条问的是
    作者写没写场景引用，与能力上限裁掉了哪几张图无关。
    """
    if not (project.get(BUCKET_KEY["scene"]) or {}):
        return False
    references, _missing = derive_references_from_text(text, project)
    return not any(ref.type == "scene" for ref in references)


def build_script_preview(
    text: str,
    project: dict,
    settings: VoiceRenderSettings,
    *,
    max_reference_images: int | None = None,
) -> ScriptPreview:
    """Derive utterances + degradation visibility warnings from video unit text.

    ``settings`` is required with no fallback (same as ``render_unit_prompt``) and must match the voice input profile
    the execution layer gets, otherwise the preview shows audio bound while the actual request carries no audio clip.
    Its ``requires_reference_image`` (whether the target backend requires each audio clip to be attached to an image)
    involves no IO, so although the preview doesn't touch the filesystem it must be kept in sync too; missing it makes
    the preview show bound and only degrade at execution, and the user only finds out after generation that the voice
    didn't take effect. ``settings.audio_ready`` stays None on the preview side: the preview doesn't resolve files and
    decides by the character asset's ``reference_audio`` field being non-empty.

    ``max_reference_images`` must match the execution layer's capability limit
    (``VideoLaneResult.max_reference_images``): at execution the reference images derived from the text are first
    trimmed to this limit and then rendered (guaranteeing the ``图片N`` numbering is strictly the same length as the
    reference images actually sent, see the ``render_unit_prompt`` docstring). If the preview decided
    ``character_image_names`` from the untrimmed full derivation, the off-screen-only degradation would only apply
    beyond the trim line -- after an over-limit character's image is trimmed, the preview would still show audio bound
    and the warning would only appear at execution. ``None`` means no trimming (the degradation convention when
    capabilities can't be resolved, consistent with the request projection not trimming reference images in that case).

    An unregistered ``@[名称]`` only warns, does not block: the text was written by the author, and the preview has no
    machine contract to protect. Same when the speaker slot names a derivative the character lacks -- that
    ``@[角色/衍生]`` doesn't enter reference image derivation (speaker slots never do), so the unregistered fact has to
    be reported separately, otherwise the editor would be completely silent about it. When the base look itself is
    unregistered it is not repeated here: that is reported by the unregistered speaker warning of
    ``derive_voice_bindings``.

    把视频单元正文派生成 utterances + 降级可见性 warning。

    ``settings`` 必填无兜底（同 ``render_unit_prompt``），须与执行层拿到的同一份声音输入档同步，
    否则预览会显示音频已绑定、实际请求却不带音频段。其中 ``requires_reference_image``（目标 backend 是否
    要求音频逐段挂图）不涉及 IO，预览虽不碰文件系统也须一并同步，遗漏会让预览显示已绑定、
    执行时才降级，用户直到生成后才发现声音没生效。``settings.audio_ready`` 在预览侧留 None：
    预览不解析文件，按角色资产的 ``reference_audio`` 字段非空判定。

    ``max_reference_images`` 须同步执行层的能力上限（``VideoLaneResult.max_reference_images``）：
    执行期会把正文派生的参考图先按此上限裁剪、再渲染（保证 ``图片N`` 编号与实际发出的参考图
    严格等长，见 ``render_unit_prompt`` docstring）。预览若按未裁剪的全量派生结果判定
    ``character_image_names``，纯画外降级会在裁剪线之外才生效——超限角色的图被裁掉后，预览仍
    显示音频已绑定，执行时才补发 warning。``None`` 表示不裁（能力不可解析时的降级口径，与请求
    投影在能力不可解析时不裁参考图的口径一致）。

    未登记的 ``@[名称]`` 只发 warning、不阻断：正文是作者写的，预览没有可保护的机器契约。
    说话人位写了该角色没有的衍生时同判——那个 ``@[角色/衍生]`` 不进参考图派生（说话人位一律
    不进），未登记的事实要另行报出来，否则编辑器对它完全沉默。本体本身未登记时不在此重复：
    那条由 ``derive_voice_bindings`` 的未登记说话人 warning 报出。
    """
    references, missing = derive_references_from_text(text, project)

    character_references = build_reference_catalog(project).reference_names("character")
    # The same name may be written both outside markers and in a speaker slot; the two are deduplicated separately and
    # don't exclude each other; without subtracting missing, the same unregistered derivative would show two identical
    # hints in the preview panel.
    # 同一个名字可能同时写在记号之外与说话人位，两处各自去重、彼此不排除；不减去 missing
    # 会让同一个未登记衍生在预览面板上出现两条一模一样的提示。
    already_reported = set(missing)
    unknown_derivatives = [
        reference
        for reference, (base, derivative) in (
            (reference, split_derivative_reference(reference)) for reference in speech_speaker_references(text)
        )
        if derivative
        and base in character_references
        and reference not in character_references
        and reference not in already_reported
    ]
    warnings = [_warning(WARN_UNREGISTERED_MENTION, name=name) for name in [*missing, *unknown_derivatives]]
    if unit_lacks_scene_reference(text, project):
        warnings.append(_warning(WARN_UNIT_WITHOUT_SCENE))
    utterances, syntax_warnings = derive_utterances(text)
    warnings.extend(syntax_warnings)

    # Audio can only align to character reference images. Also trim to the capability limit before deciding,
    # matching the execution layer's "trim reference images first, then render".
    # 音频只能对齐到 character 参考图。同时按能力上限裁剪后再判定，
    # 与执行层「先裁参考图再渲染」的口径对齐。
    clipped_references = references[:max_reference_images] if max_reference_images is not None else references
    character_image_names = {ref.name for ref in clipped_references if ref.type == "character"}

    bindings = derive_voice_bindings(
        utterances,
        project.get(BUCKET_KEY["character"]) or {},
        settings,
        speakers_with_reference_image=character_image_names,
    )
    warnings.extend(bindings.warnings)

    return ScriptPreview(utterances=utterances, warnings=warnings)
