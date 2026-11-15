"""Three-part rendering for the reference-to-video path: unit content + asset table + capability tier → the prompt
sent to the video model.

Part one (subject binding + voice declarations) and part three (style block + negative constraints) are
generated mechanically at render time, not left to the LLM's diligence; the whole prompt is rendered by the
built-in template ``reference_video/unit``, and this module produces the slot values for the first two parts.
Rendering is a pure function and the result is not saved, so existing content gets the new rendering without
migration.

Division of the three parts:

- **Part one**: short-form ``<X>@图片N`` binding (image number = order of reference images sent with the
  request) + a central voice declaration block (``<X>的台词音色参考 @音频N，只取音色、语速与情绪，台词以正文为准，声音特征：…``,
  i.e. "X's dialogue voice reference @audioN, take only timbre, pace and emotion, dialogue follows the text,
  voice traits: ..."). Audible classes A/B both get voice traits injected; neither silent path (class C models
  that produce no audio, audio turned off for this episode) gets them
- **Part two**: unit text + character dialogue markers (``<X>说 {台词}``, "X says {line}"); unattributed narration
  (bare ``{台词}``) is not sent to the video model, it is handled by TTS / post-production voice-over
- **Part three**: shared style block + ``Avoid`` line (the video negative prompt; all negative constraints on this
  path are carried by it); when two or more distinct characters appear in the frame, clone or twin exclusions are
  appended. With product reference images, a product high-fidelity statement follows

The input for every content mode is the unit text, a single free-text block, and the text only covers part two;
subject binding and reference image order are both derived by parsing ``@[X]`` mentions
(:func:`render_unit_prompt`). The text contains no absolute seconds; duration goes through the request field.

参考生视频路径的三段论渲染：unit 内容 + 资产表 + 能力档 → 发给视频模型的 prompt。

第一段（主体绑定 + 声音声明）与第三段（风格块 + 负向约束）在渲染期机械生成，不依赖 LLM 自觉；
整段提示词由内置模版 ``reference_video/unit`` 渲染，前两段由本模块产出槽位值。渲染是纯函数、
结果不落盘，存量内容无需迁移即获得新渲染。

三段分工：

- **第一段**：``<X>@图片N`` 简式绑定（图片编号 = 随请求发出的参考图顺序）+ 声音声明集中
  声明区（``<X>的台词音色参考 @音频N，只取音色、语速与情绪，台词以正文为准，声音特征：…``）。听得到声音的 A/B 类均注入声音特征，
  两条无声路径（模型不产音的 C 类、本集关闭音频）都不注入
- **第二段**：单元正文 + 角色台词记号（``<X>说 {台词}``）；无归属旁白（裸 ``{台词}``）
  不下发视频模型，由 TTS / 后期配音承担
- **第三段**：共享风格块 + ``Avoid`` 行（视频负向提示词，本路径的反向约束全部由它承担）；
  画面里有两个及以上不同角色时追加分身或双胞胎排除项。带商品参考图时其后再接商品高保真声明

所有创作类型的输入均是 unit 正文这一段自由文本，正文只写第二段；主体绑定与参考图顺序都经
``@[X]`` mention 解析派生（:func:`render_unit_prompt`）。文本不含绝对秒数，时长走请求字段。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib.project.asset_types import BUCKET_KEY, asset_name_comparison_key, normalize_asset_bucket
from lib.prompts.prompt_style import normalize_style_value
from lib.prompts.prompt_templates.builtin import builtin_templates
from lib.references.reference_catalog import ReferenceCatalog, build_reference_catalog
from lib.script.reference_video.script_preview import (
    WARN_UNREGISTERED_MENTION,
    derive_utterances,
    derive_voice_bindings,
)
from lib.script.reference_video.text_parser import (
    SpeechMark,
    derive_references_from_text,
    extract_mentions,
    render_mentions_as_subjects,
    resolve_references,
    split_speech_line,
)
from lib.script.reference_video.voice_settings import VoiceRenderSettings
from lib.script.script_models import ReferenceResource
from lib.speech.audio_utils import resolve_audio_ref_path

#: Fixed in-project directory for character reference audio (same convention as upload / TTS sample storage).
#: 角色参考音频的项目内固定目录（与上传 / TTS 样本落盘口径一致）。
ASSET_AUDIO_SUBDIR = "characters/refs_audio"


def _character_bucket(project: dict) -> dict[str, Any]:
    """The project character table, keys already normalized into the asset-name comparison coordinate system; non-dict
    (project.json broken by external editing) is treated as empty.

    The validator only checks inner entries when the field itself is a dict (see ``lib.project.data_validator``), so a
    malformed project whose whole field is not a dict is not rejected; this module indexes the character table by name
    in several places, so it normalizes once where the value is taken, to avoid each call site adding its own
    isinstance check.

    Name-side normalization likewise lives here (see :func:`lib.project.asset_types.normalize_asset_bucket`): the
    speakers and mention names compared against the character table come from the parser and are already normalized,
    while the form in which the character table is stored is out of our control.

    项目角色表，key 已归一到资产名比对坐标系；非 dict（外部编辑写坏的 project.json）按空处理。

    校验器只在该字段本身是 dict 时才校验内部条目（见 ``lib.project.data_validator``），字段整体
    非 dict 的畸形项目不会被拒绝；本模块多处按名字索引角色表，统一在取值处归一化，
    避免每个调用点各自补一遍 isinstance 判断。

    名字侧的归一同理落在这一处（见 :func:`lib.project.asset_types.normalize_asset_bucket`）：与角色表
    比对的说话人与 mention 名都出自解析器、已是归一形式，角色表以哪种形式落盘则不可控。
    """
    return normalize_asset_bucket(project.get(BUCKET_KEY["character"]))


@dataclass(frozen=True)
class RenderedUnitPrompt:
    """The rendered output of one unit.

    The order of ``audio_speakers`` is the ``@音频N`` (@audioN) numbering; callers must assemble
    ``VideoGenerationRequest.reference_audio_files`` in the same order -- this is the only binding contract between
    the prompt text and the request fields (which character maps to which audio is not in the request).

    ``audio_speaker_reference_index`` has the same length and order as ``audio_speakers``: item i is the
    ``reference_images`` index (0-based) for that speaker, None when there is no reference image (off-screen-only
    character). Reference audio order (first appearance of dialogue speakers) and reference image order (first
    appearance of mentions) are derived independently; if the backend requires each audio clip to be attached to a
    specific reference item (``VideoCapabilities.reference_audio_per_image``), callers must assemble
    ``VideoGenerationRequest.reference_audio_targets`` from this field and must not assume the two lists are
    naturally in the same order.

    一个 unit 的渲染产物。

    ``audio_speakers`` 的顺序即 ``@音频N`` 编号，调用方须按同一顺序组装
    ``VideoGenerationRequest.reference_audio_files``——这是 prompt 文本与请求字段之间唯一的
    绑定契约（哪个角色对应哪段音频不进请求）。

    ``audio_speaker_reference_index`` 与 ``audio_speakers`` 等长同序：第 i 项是该 speaker
    对应的 ``reference_images`` 下标（0-based），没有参考图（纯画外角色）时为 None。参考音频
    的顺序（台词 speaker 首现顺序）与参考图的顺序（mention 首现顺序）各自独立派生，backend
    若要求音频逐段挂在具体参考素材项上（``VideoCapabilities.reference_audio_per_image``），
    调用方须按本字段组装 ``VideoGenerationRequest.reference_audio_targets``，不能假设两个
    列表天然同序。
    """

    prompt: str
    audio_speakers: list[str]
    audio_speaker_reference_index: list[int | None] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


def render_unit_prompt(
    text: str,
    project: dict,
    references: list[ReferenceResource],
    settings: VoiceRenderSettings,
) -> RenderedUnitPrompt:
    """Render one unit's written text into the three-part backend prompt.

    ``references`` is the list of reference images **actually sent with the request** (already trimmed to the
    capability limit), and its order is the ``图片N`` (imageN) numbering -- strictly the same length and order as
    ``reference_images``; trimmed names degrade to plain text and produce no dangling binding. The caller derives the
    reference image order from the same text (first appearance of ``@mention``; speaker slots of dialogue markers don't
    count); this function only consumes it and does not recompute.

    ``settings`` is the voice input profile used for rendering (see :class:`~lib.script.reference_video.voice_settings
    .VoiceRenderSettings`), required with no fallback: it decides whether this episode has audible sound, and
    defaulting either way would be guessing for the caller -- guessing audible would inject voice traits into silent
    projects; erroring when it is omitted makes new callers find out at wiring time.
    When ``requires_reference_image`` is True (the backend requires each audio clip to be attached to a specific
    reference item), off-screen-only speakers get no audio binding (degraded + warning) -- once bound, the ``@音频N``
    number is written into the prompt text, and filtering it later at the backend layer would make the binding promised
    by the text diverge from the ``reference_audio_files`` actually sent, so it must be excluded before numbering.

    The silent path (``settings.is_silent``) produces no audio binding: neither ``@音频N`` nor "声音特征：…" (voice
    traits) enters the prompt, ``audio_speakers`` is empty, and the ``reference_audio_files`` the caller assembles is
    empty accordingly. Dialogue rendering in part two ignores this flag -- in silent video the dialogue text is still
    sent, and the provider may use it as a lip-sync reference.

    Warnings are the same batch of ``{key, params}`` entries as the parse preview panel; the caller merges them into the
    task ``result.warnings``.

    The part three style block takes ``style`` and ``style_description`` from ``project``. Clone exclusions are decided
    from the complete list of registered characters in the text, not trimmed by the provider's reference image limit;
    the base look and derivatives of the same character still count as one (see ``docs/adr/0072``). The product
    high-fidelity statement is only injected when product reference images are actually sent with the request -- the
    statement points at "the product reference image", and injecting it when the reference is absent would only
    mislead the model.

    把一个 unit 的书写文稿渲染成三段论 backend prompt。

    ``references`` 是**实际随请求发出**的参考图列表（已按能力上限裁剪），其顺序即
    ``图片N`` 编号——与 ``reference_images`` 严格等长同序，被裁掉的名字退化为原文不产生
    悬空绑定。参考图顺序由调用方从同一份正文派生（``@mention`` 首现、台词记号的 speaker 位
    不计入），本函数只消费不重算。

    ``settings`` 是渲染所用的声音输入档（见 :class:`~lib.script.reference_video.voice_settings
    .VoiceRenderSettings`），必填无兜底：这一档决定这一集听不听得到声音，缺省成任何一个方向都是
    替调用方猜——猜有声会给无声项目注入声音特征，漏传时报错才让新调用方在接线阶段就发现。
    ``requires_reference_image`` 为 True 时（backend 要求音频逐段挂在
    具体参考素材项上），纯画外 speaker 不绑定音频（降级 + warning）——绑定后 ``@音频N`` 编号会
    写进 prompt 文本，若随后才在 backend 层过滤会让文本承诺的绑定与实际发出的
    ``reference_audio_files`` 分叉，必须在编号生成前就排除。

    无声路径（``settings.is_silent``）不产出任何音频绑定：``@音频N`` 与「声音特征：…」都不进
    prompt、``audio_speakers`` 为空，调用方组装出的 ``reference_audio_files`` 随之为空。第二段的
    台词渲染不看这一位——无声视频里台词文本照常下发，供应商可用作口型参考。

    warning 与解析预览面板同一批 ``{key, params}`` 条目，由调用方并入任务 ``result.warnings``。

    第三段的风格块取 ``project`` 的 ``style`` 与 ``style_description``。分身排除项按正文中已登记的
    完整角色列表判断，不随供应商参考图上限裁剪；同一角色的本体与衍生仍合并计一个（见 ``docs/adr/0072``）。
    商品高保真声明只在商品参考图实际随请求发出时注入——声明指向「商品参考图」，参考缺席时
    注入只会误导模型。
    """
    mentions = extract_mentions(text)
    utterances, warnings = derive_utterances(text)

    # ``references`` is an input (derived results persisted upstream), and the encoding form its names were stored in
    # is out of our control; the text side comes from the parser and is already normalized. Only when both sides have the
    # same form do subject markers and image numbers match -- without normalization that character's binding line would
    # be missing and the audio could not attach to an image, all without any error.
    # ``references`` 是入参（上游持久化的派生结果），其名字以哪种编码形式落盘不可控；正文一侧
    # 出自解析器、已归一。两侧同形，主体记号与图号才对得上——不归一时该角色的绑定行会缺位、
    # 音频也挂不到图上，且全程不报错。
    references = [ReferenceResource(type=ref.type, name=asset_name_comparison_key(ref.name)) for ref in references]

    registered, missing = resolve_references(mentions, project)
    warnings = [_warning_unregistered(name) for name in missing] + warnings
    # Subject markers are decided by **asset table registration**, decoupled from reference image numbering: a name
    # trimmed by the capability limit is still a subject in the frame, it just didn't get an image sent this time (same
    # for off-screen-only characters -- subject, no image). Only unregistered mentions stay as plain text.
    # 主体记号按**资产表登记**判定，与参考图编号解耦：被能力上限裁掉的名字仍是画面主体，
    # 只是这次没随请求发图（纯画外角色同理——有主体、无图）。未登记的 mention 才留原文。
    subjects = {ref.name for ref in registered}

    # Audio can only attach to character reference images; filter by type, then build the name → index map.
    # 音频只能挂到 character 参考图；按类型过滤后建 name → 序号映射。
    character_image_no = {ref.name: i for i, ref in enumerate(references, start=1) if ref.type == "character"}

    characters = _character_bucket(project)
    catalog = build_reference_catalog(project)
    bindings = derive_voice_bindings(
        utterances,
        characters,
        settings,
        speakers_with_reference_image=set(character_image_no),
    )
    warnings.extend(bindings.warnings)

    audio_no, audio_speaker_reference_index = _number_audio_speakers(bindings.audio_speakers, character_image_no)

    request_character_forms = _character_forms_by_entity(references, catalog)
    co_present_characters = list(_character_forms_by_entity(registered, catalog))
    prompt = builtin_templates.render(
        "reference_video/unit",
        declarations=_render_segment_one(
            [ref.name for ref in references],
            bindings.speakers,
            audio_no,
            characters,
            settings,
            request_character_forms,
        ),
        body=_render_segment_two(text, subjects, characters, catalog),
        style=normalize_style_value(project.get("style")),
        style_description=normalize_style_value(project.get("style_description")),
        co_present_characters=co_present_characters if len(co_present_characters) >= 2 else [],
        products=list(dict.fromkeys(ref.name for ref in references if ref.type == "product" and ref.name)),
    )
    return RenderedUnitPrompt(
        prompt=prompt,
        audio_speakers=list(bindings.audio_speakers),
        audio_speaker_reference_index=audio_speaker_reference_index,
        warnings=warnings,
    )


def render_video_unit_prompt(
    unit: dict,
    project: dict,
    settings: VoiceRenderSettings,
    *,
    request_references: list[ReferenceResource] | None = None,
) -> RenderedUnitPrompt:
    """Render the exact reference-video prompt from one current projected unit."""

    raw_text = unit.get("text")
    text = raw_text if isinstance(raw_text, str) else ""
    if not text.strip():
        raise ValueError("reference video unit prompt is empty: text is blank")
    references = request_references
    if references is None:
        references, _missing = derive_references_from_text(text, project)
    return render_unit_prompt(text, project, references, settings)


def _warning_unregistered(name: str) -> dict[str, Any]:
    return {"key": WARN_UNREGISTERED_MENTION, "params": {"name": name}}


def _render_voice_declarations(
    speakers: list[str],
    audio_no: dict[str, int],
    characters: dict,
    settings: VoiceRenderSettings,
) -> list[str]:
    """Voice declaration line: ``<X>的台词音色参考 @音频N，只取音色、语速与情绪，台词以正文为准，声音特征：…``. Shared by the
    episode and ad paths -- both use mention-derived ``ReferenceResource`` for subject binding lines; voice
    declarations only recognize "registered dialogue speakers", are decoupled from subject binding lines, and can be
    reused as a whole.

    Neither silent path (``settings.is_silent``: class C models that produce no audio, audio turned off for this
    episode) injects voice declarations; audible classes A/B both get voice traits -- the official advice is to add a
    description when voice reproduction is poor. Dialogue rendering is unaffected and still goes into part two (see
    :func:`_render_segment_two`).

    A non-dict character record (project.json broken by external editing) is treated as having no voice traits; dirty
    values are not indexed, keeping soft degradation rather than crashing.

    声音声明行：``<X>的台词音色参考 @音频N，只取音色、语速与情绪，台词以正文为准，声音特征：…``。剧集与 ad 路径共用——两者的
    主体绑定行统一使用 mention 派生的 ``ReferenceResource``；声音声明只认「已登记的
    dialogue speaker」，与主体绑定行解耦，可整段复用。

    两条无声路径（``settings.is_silent``：模型不产音的 C 类、本集关闭音频）都不注入声音声明；
    听得到声音的 A/B 类均注入声音特征——官方建议音色还原不佳时补描述。台词渲染不受影响，
    照常进第二段（见 :func:`_render_segment_two`）。

    角色记录非 dict（外部编辑写坏的 project.json）按无声音特征处理，不索引脏值，保持
    软降级口径而非崩溃。
    """
    if settings.is_silent:
        return []
    lines: list[str] = []
    for name in speakers:
        parts: list[str] = []
        if name in audio_no:
            parts.append(f"台词音色参考 @音频{audio_no[name]}，只取音色、语速与情绪，台词以正文为准")
        char_data = characters.get(name)
        voice_style = str((char_data.get("voice_style") if isinstance(char_data, dict) else None) or "").strip()
        if voice_style:
            parts.append(f"声音特征：{voice_style}")
        if parts:
            lines.append(f"<{name}>的" + "，".join(parts) + "。")
    return lines


def _number_audio_speakers(
    audio_speakers: list[str], character_image_no: dict[str, int]
) -> tuple[dict[str, int], list[int | None]]:
    """``@音频N`` numbering and the index of "which reference image each audio clip maps to", one derivation shared by
    both paths.

    The number is the position in ``audio_speakers`` (speaker first-appearance order), which is also the request field
    order of ``reference_audio_files``; the index list has the same length and order, with None for off-screen-only
    speakers without a reference image. Both must be derived in one place: computing them separately would let the
    ``@音频N`` in the prompt text and the request field indexes drift apart.

    ``@音频N`` 编号与「每段音频对应哪张参考图」的下标，两条路径共用同一份派生口径。

    编号即 ``audio_speakers`` 的位置（speaker 首现顺序），也是 ``reference_audio_files``
    的请求字段顺序；下标列表与之等长同序，无参考图的纯画外 speaker 落 None。两者必须在
    同一处派生：分开算会让 prompt 文本里的 ``@音频N`` 与请求字段的下标各自漂移。
    """
    audio_no = {name: i for i, name in enumerate(audio_speakers, start=1)}
    reference_index = [
        (character_image_no[name] - 1) if name in character_image_no else None for name in audio_speakers
    ]
    return audio_no, reference_index


def _render_segment_one(
    labels: list[str],
    speakers: list[str],
    audio_no: dict[str, int],
    characters: dict,
    settings: VoiceRenderSettings,
    character_forms: dict[str, list[str]],
) -> str:
    """Subject binding + voice declarations.

    Part one of the official three-part structure is the reference source declaration area (face / camera movement /
    voice references side by side), so voice references and voice traits are concentrated here and dialogue only
    keeps a uniform sentence pattern. Declarations iterate over "registered characters with dialogue" rather than the
    reference image list: off-screen-only characters have no reference image (speaker slots don't count toward
    reference image derivation), but still get a voice declaration.

    ``labels`` are mention-derived subject marker texts, one per reference image sent with the request. Image numbers
    are assigned directly by position (not by name lookup); an empty label placeholder produces no binding line but
    the number still advances, so later image numbers don't fall out of step with the request order.

    ``character_forms`` is the grouping from :func:`_character_forms_by_entity`, used to declare multiple forms of
    the same character; it is grouped by asset entry and has a different source than the position-numbered ``labels``.

    Returns the ``declarations`` slot value of the ``reference_video/unit`` template.

    主体绑定 + 声音声明。

    官方三段论第一段即参考来源声明区（人脸 / 运镜 / 音色参考同位），故音色参考与声音特征
    集中于此，台词只留统一句式。声明遍历「有台词的已登记角色」而非参考图列表：纯画外角色
    没有参考图（speaker 位不计入参考图派生），但音色声明照常。

    ``labels`` 是 mention 派生的主体记号文本，逐项对应随请求发出的参考图。图号按位置
    直接编号（非名字查表）；空 label 占位不产出绑定行，编号照样前进，以免后续图号与请求
    顺序错位。

    ``character_forms`` 是 :func:`_character_forms_by_entity` 的分组，用来声明同一角色的
    多个形态；它按资产条目分组，与按位置编号的 ``labels`` 不同源。

    返回 ``reference_video/unit`` 模版的 ``declarations`` 槽位值。
    """
    lines: list[str] = []
    bindings = "、".join(f"<{label}>@图片{i}" for i, label in enumerate(labels, start=1) if label)
    if bindings:
        lines.append(bindings + "。")
    lines.extend(_render_form_declarations(character_forms))
    lines.extend(_render_voice_declarations(speakers, audio_no, characters, settings))
    return "\n".join(lines)


def _character_forms_by_entity(references: list[ReferenceResource], catalog: ReferenceCatalog) -> dict[str, list[str]]:
    """Group character reference images by the asset entry carrying them: ``base look name → [form markers of that
    entry]``, in reference image order.

    A derivative and its base look are two reference names with two reference images, yet refer to the same person
    (see ``docs/adr/0072``). Part one calls this function on the reference images actually sent with the request, part
    three on the complete registered references in the text; both merge character identity by the same project
    catalog. An unregistered name stands alone: it points to no entry and should not be merged with other names.

    Only ``character`` type references are considered: in legacy projects with names duplicated across types, looking
    up a same-named scene in the character table would find an entry and describe two unrelated images as two looks of
    the same character.

    角色参考图按承载它的资产条目分组：``本体名 → [该条目出现的形态记号]``，顺序随参考图顺序。

    衍生与本体是两个引用名、两张参考图，指的却是同一个人（见 ``docs/adr/0072``）。第一段对实际
    随请求发出的参考图调用本函数，第三段对正文中完整的已登记引用调用；两处都按同一项目目录合并
    角色身份。未登记的名字自成一条：它指不到任何条目，与别的名字不该合并。

    只看 ``character`` 类型的引用：跨类型重名的存量项目里，同名场景按名字查角色表会查出条目，
    把两张毫无关系的图说成同一角色的两套外观。
    """
    grouped: dict[str, list[str]] = {}
    for reference in references:
        if reference.type != "character" or not reference.name:
            continue
        entry = catalog.lookup("character", reference.name)
        grouped.setdefault(entry.asset_name if entry else reference.name, []).append(reference.name)
    return grouped


def _render_form_declarations(character_forms: dict[str, list[str]]) -> list[str]:
    """When multiple forms of the same character appear together, declare that they are different looks of the same person.

    A character's base look and derivatives are separate reference names, each with its own sheet (see
    ``docs/adr/0072``), and may appear together without limit. Without a declaration the model only sees two similar
    character reference images and treats them as two people in the frame, so ownership is made clear in the reference
    source declaration area. Characters appearing in only one form produce no declaration.

    同一角色的多个形态同现时，声明它们是同一个人的不同外观。

    角色的本体与衍生是各自独立的引用名、各带一张资产图（见 ``docs/adr/0072``），同现不设限。
    不声明时模型只看到两张长相相近的角色参考图，会当成两个人物同框，故在参考来源声明区就把
    归属讲清楚。只出现一个形态的角色不产出声明。
    """
    lines: list[str] = []
    for forms in character_forms.values():
        if len(forms) < 2:
            continue
        marks = [f"<{form}>" for form in forms]
        lines.append("、".join(marks[:-1]) + f"与{marks[-1]}是同一角色的不同形态，各自按对应参考图呈现。")
    return lines


#: Separator punctuation and whitespace (Chinese and English forms) used to decide whether both sides need merging after
#: a narration marker is dropped. Terminal punctuation is listed separately: at line end it legitimately closes the
#: sentence, whereas connecting punctuation in the middle of a line would dangle at line end.
#: 旁白记号被丢弃后，用于判定其两侧是否需要合并的分隔标点与空白（中英两形）。
#: 终止标点单列：它在行尾是合法收句，行中的连接标点在行尾则是悬空的。
_MARK_JOINERS = "，,、；;：:"
_MARK_TERMINATORS = "。.！!？?"
_MARK_SEPARATORS = _MARK_JOINERS + _MARK_TERMINATORS
_MARK_SPACES = " \t\u3000"


def _render_segment_two(text: str, subjects: Collection[str], characters: dict, catalog: ReferenceCatalog) -> str:
    """Unit text part: visual description gets mention substitution, and utterance markers are reassembled in place into
    the official sentence pattern.

    ``subjects`` are registered mention names (not trimmed by the capability limit) -- the subject marker ``<X>``
    expresses "this person / thing in the frame" and does not point at an image number, so it is decoupled from
    reference image numbering: a name whose image was trimmed is still a subject, and only unregistered mentions keep
    the editor's original text (with ``ref_warn_unregistered_mention``).

    The text is rendered line by line as-is, without reordering or section prefixes: the text the author wrote is the
    single source of truth, and its order is the delivery order. Markers may sit anywhere in a line; reassembly replaces
    them in place by position and the description stays where it is. Speakers are decided by the **asset table** rather
    than the reference image list: off-screen-only characters have no reference image but their dialogue is still
    reassembled. Unregistered speakers are sent as in the original (the warning is already emitted by
    :func:`derive_voice_bindings`), and braces not recognized as markers are also sent as-is -- not stripped, so the
    author can see the part they broke in the final cut.

    The form written in the speaker slot matches the description slot: ``@[张三/劲装]{台词}`` renders as
    ``<张三/劲装>说 {台词}``, so one character has only one kind of subject marker in a prompt (see
    ``docs/adr/0072``). The voice still binds to the base look -- that is the job of ``SpeechMark.speaker``,
    independent of which look appears on screen. If the written derivative is unregistered it falls back to the base
    look marker, unlike "unregistered mentions keep original text" in the description slot: the speaker slot marker is
    reassembled at render time and has no original text to keep.

    Unattributed narration (bare ``{台词}``) is dropped entirely and does not enter the prompt: narration voice-over is
    delivered only via TTS and post-production voice-over (ADR 0040 / 0061); a video model that produces audio given
    this text would read it aloud along with the prompt text, or make on-screen characters lip-sync to the narration.
    Visual description on the same line is still rendered; only the marker itself disappears.

    单元正文段：画面描述做 mention 替换，发声记号就地重组为官方句式。

    ``subjects`` 是已登记的 mention 名（未经能力上限裁剪）——主体记号 ``<X>`` 表达「画面里的
    这个人 / 物」，不指向图号，故与参考图编号解耦：裁掉图的名字照样是主体，只有未登记的
    mention 才留编辑器原文（配 ``ref_warn_unregistered_mention``）。

    正文逐行原样渲染，不再重排或加分段前缀：正文是作者写下的唯一真相，行文顺序即传达顺序。
    记号可写在行内任意位置，重组按位置就地替换、描述部分留在原处。说话人按**资产表**判定
    而非参考图列表：纯画外角色无参考图，台词照常重组。未登记的说话人按原文发送（warning 已由
    :func:`derive_voice_bindings` 发出），未被识别成记号的花括号同样原样发送——不做剥除，
    作者能在成片里看见自己写坏的那一段。

    说话人位写下的形态与描述位同形：``@[张三/劲装]{台词}`` 渲染成 ``<张三/劲装>说 {台词}``，
    同一角色在一条 prompt 里只有一种主体记号（见 ``docs/adr/0072``）。声音仍绑本体——那是
    ``SpeechMark.speaker`` 的职责，与画面上呈现哪套外观无关。写下的衍生没登记时退回本体记号，
    与描述位「未登记的 mention 留原文」不同：说话人位的记号是渲染期重组出来的，没有原文可留。

    无归属旁白（裸 ``{台词}``）整段丢弃、不进 prompt：叙述旁白只经 TTS 与后期配音交付
    （ADR 0040 / 0061），会产音的视频模型拿到这段文本会连提示语一起念出、或让画面人物对着
    旁白对口型。同一行的画面描述照常渲染，只有记号本身消失。
    """
    lines: list[str] = []
    for line in text.splitlines():
        pieces: list[str | None] = []
        for part in split_speech_line(line):
            if isinstance(part, str):
                pieces.append(render_mentions_as_subjects(part, subjects))
            elif not part.speaker:
                pieces.append(None)
            elif part.speaker in characters:
                pieces.append(f"<{_speaker_subject(part, catalog)}>说 {{{part.text}}}")
            else:
                pieces.append(render_mentions_as_subjects(part.raw, subjects))
        lines.append(_join_line_pieces(pieces))
    return "\n".join(line for line in lines if line.strip())


def _speaker_subject(mark: SpeechMark, catalog: ReferenceCatalog) -> str:
    """Subject marker for the speaker slot: a written and registered derivative gives ``本体/衍生`` (base/derivative), otherwise the base look name.

    说话人位的主体记号：写下且已登记的衍生取 ``本体/衍生``，否则取本体名。"""
    reference = mark.speaker_reference
    if mark.derivative and catalog.lookup("character", reference) is not None:
        return reference
    return mark.speaker


def _join_line_pieces(pieces: list[str | None]) -> str:
    """Join the rendered pieces of a line; ``None`` is a dropped narration marker, and the extra separator punctuation and
    whitespace on its two sides are merged here.

    When the author writes narration between two description pieces (``推门，{夜风灌进来}，他按住剑``), the marker has a
    separator on each side, and joining directly would leave "，，". If the left side already has a separator, drop the
    right one; if both sides are only whitespace, merge them into one.

    When the marker is at line start or end, only one side has description, and the remaining separator has nothing
    to connect to: at line start (``{夜风灌进来}，他按住剑``) drop the separator and whitespace to the right of the
    marker; at line end (``推门，{夜风灌进来}``) drop the dangling connecting punctuation on the left; terminal
    punctuation at line end (``推门。{夜风灌进来}``) legitimately closes the sentence and is kept.

    拼接一行的渲染片段；``None`` 是被丢弃的旁白记号，其两侧多出的分隔标点与空白在此合并。

    作者把旁白写在两段描述之间时（``推门，{夜风灌进来}，他按住剑``），记号左右各有一个分隔
    标点，直接拼接会留下「，，」。左侧已有分隔时丢掉右侧的，两侧都只是空白时并成一个。

    记号写在行首或行尾时只有一侧有描述，留下的分隔标点没有另一头可接：行首（``{夜风灌进来}，
    他按住剑``）丢掉记号右侧的分隔与空白，行尾（``推门，{夜风灌进来}``）丢掉左侧悬空的连接
    标点；行尾的终止标点（``推门。{夜风灌进来}``）是合法收句，保留。
    """
    rendered = ""
    dropped = False
    for piece in pieces:
        if piece is None:
            dropped = True
            continue
        if dropped:
            dropped = False
            head = rendered.rstrip()
            if not head or head[-1] in _MARK_SEPARATORS:
                rendered = head
                piece = piece.lstrip(_MARK_SEPARATORS + _MARK_SPACES)
            elif rendered[-1:].isspace():
                # Both sides are only whitespace: merge into one; multiple spaces should not remain in the text just because the marker vanished.
                # 两侧都只是空白：并成一个，多个空白不该因为记号消失而留在正文里。
                rendered = head
                piece = piece.lstrip()
                if piece and piece[0] not in _MARK_SEPARATORS:
                    rendered += " "
        rendered += piece
    if dropped:
        rendered = rendered.rstrip(_MARK_JOINERS + _MARK_SPACES)
    return rendered


def resolve_reference_audio_paths(project: dict, project_path: Path) -> dict[str, Path]:
    """Map of characters in the project whose "reference audio is actually usable" → absolute path.

    Only entries whose field points inside ``characters/refs_audio`` and whose file actually exists are included
    (out-of-bounds paths are blocked by :func:`lib.speech.audio_utils.resolve_audio_ref_path` -- this field can be set
    to any in-project string via asset PATCH). The render layer decides binding from this, so the numbering and the
    number of audio clips actually sent are strictly the same length.

    Keys are normalized character names (already normalized by ``_character_bucket``): this map is passed as
    ``audio_ready`` to :func:`lib.script.reference_video.script_preview.derive_voice_bindings` and compared with
    speakers; only same-form sides avoid misjudging "audio actually usable" as "unusable" and silently not binding.

    项目内「参考音频确实可用」的角色 → 绝对路径映射。

    只收录字段指向 ``characters/refs_audio`` 内且文件确实存在的条目（越界路径由
    :func:`lib.speech.audio_utils.resolve_audio_ref_path` 挡下——该字段可经资产 PATCH 写成项目内
    任意字符串）。渲染层据此判定绑定，编号与实际发出的音频段数因此严格等长。

    key 是归一后的角色名（``_character_bucket`` 已归一）：本映射作为 ``audio_ready`` 传给
    :func:`lib.script.reference_video.script_preview.derive_voice_bindings` 与说话人判等，两侧同形
    才不会把「音频确实可用」误判成「不可用」而静默不绑。
    """
    audio_refs_dir = project_path / ASSET_AUDIO_SUBDIR
    resolved: dict[str, Path] = {}
    for name, item in _character_bucket(project).items():
        if not isinstance(item, dict):
            continue
        path = resolve_audio_ref_path(project_path, audio_refs_dir, item.get("reference_audio"))
        if path is not None and path.exists():
            resolved[name] = path
    return resolved
