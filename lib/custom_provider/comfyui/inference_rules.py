"""Rule tables for node binding inference: reading and shaping the packaged data files.

What inference has to recognise -- input name aliases, native node allowlists, steps, optional inputs, merge nodes,
external convention node families, artifact candidate order, audio track detection -- is all node knowledge of the
ComfyUI ecosystem, not ArcReel logic. It drifts over time as node packs evolve, so it lives in
``inference_rules.<media_type>.json``; changing a line of JSON does not touch a line of Python.

One file per media type: image endpoints have no first/end frames or timeline and a different artifact candidate
order; merging them into one would mean hanging a "only valid for video" switch on every rule. The sections on node
type facts (constant nodes, optional inputs, merge nodes, external node families, seed gates) do not depend on
whether the endpoint produces images or video; they are identical word for word in both files, kept from drifting by
tests.

Files are checked against ``inference_rules.schema.json`` on read, and the semantic keys they mention are checked
again against the roster of that media type: this is data shipped with the package, and if it is broken it should
blow up at startup rather than make some semantic key silently fail to infer.

节点绑定推断的规则表：随包数据文件的读入与形状化。

推断要认的东西——input 名别名、原生节点白名单、步长、可选入口、合并节点、外部约定节点族、
产物候选顺序、音轨判定——全是 ComfyUI 生态的节点知识，不是 ArcReel 的逻辑。它们随节点包的
迭代长期漂移，因此放在 ``inference_rules.<media_type>.json`` 里，改一行 JSON 不动一行 Python。

两种媒体类型各一份：图像端点没有首尾帧与时间轴，产物候选顺序也不同，合成一份就得在每条规则上
再挂一个「这条只对视频有效」的开关。节点类型的事实那几节（常量节点、可选入口、合并节点、外部
节点族、种子闸门）与端点产图还是产视频无关，两份里逐字相同，由测试守住不漂移。

文件在读入时即过 ``inference_rules.schema.json``，再核一遍它提到的语义键都在该媒体类型的名录
里：它是随包发布的数据，写坏了该在启动时炸，而不是变成某个语义键悄悄推断不出来。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from arcreel_market_core.comfyui.bindings import BINDING_KEYS_BY_MEDIA_TYPE

RULES_SCHEMA_PATH = Path(__file__).parent / "inference_rules.schema.json"

#: Media type -> rule file for that media type. / 媒体类型 → 该媒体类型的规则文件。
RULES_PATHS: Mapping[str, Path] = {
    "image": Path(__file__).parent / "inference_rules.image.json",
    "video": Path(__file__).parent / "inference_rules.video.json",
}


@dataclass(frozen=True)
class SemanticKeyRules:
    """Field-level clues for recognising one semantic key. / 认出一个语义键的字段级线索。"""

    #: Input name aliases for this semantic key. / 该语义键的 input 名别名。
    input_names: tuple[str, ...]
    #: Allowlist of native nodes that carry this semantic key. / 承载该语义键的原生节点白名单。
    class_types: frozenset[str]
    #: Conditional rule: an input name counts for this key only on these nodes. / 条件规则：某个 input 名只在这些节点上算本键。
    input_name_class_types: Mapping[str, frozenset[str]]
    #: Secondary clues in the title, case-insensitive substring match. / 标题里的次级线索，大小写不敏感子串匹配。
    title_keywords: tuple[str, ...]

    def accepts(self, input_name: str, class_type: str) -> bool:
        """Whether this ``(input name, carrier node)`` combination counts as a clue for this key. / 这个 ``(input 名, 承载节点)`` 组合算不算本键的线索。"""
        if input_name not in self.input_names:
            return False
        limited = self.input_name_class_types.get(input_name)
        return limited is None or class_type in limited

    def title_hit(self, title: str) -> bool:
        lowered = title.lower()
        return any(keyword in lowered for keyword in self.title_keywords)


@dataclass(frozen=True)
class NodeInput:
    """One input on one node. / 一个节点上的一个入口。"""

    class_type: str
    input: str


@dataclass(frozen=True)
class InputName:
    """Input name in a rule entry: an exact name, or a family of slots recognised by prefix like ComfyUI autogrow inputs.

    规则条目里的入口名：精确名，或 ComfyUI autogrow 口那样按前缀认的一族槽位。"""

    text: str
    #: When ``True``, ``text`` is a prefix: ``ref_images.ref_image_`` recognises every slot from ``ref_images.ref_image_0`` on.
    #: ``True`` 时 ``text`` 是前缀，``ref_images.ref_image_`` 认得出 ``ref_images.ref_image_0`` 起的每一格。
    prefix: bool = False

    def matches(self, input_name: str) -> bool:
        return input_name.startswith(self.text) if self.prefix else input_name == self.text


@dataclass(frozen=True)
class OptionalInput:
    """An optional input on a node: when the upstream image-loading node is removed, just drop the key and the node still runs.

    一个节点上的可选入口：上游的读图节点删掉后摘键即可，节点本身照跑。"""

    class_type: str
    input: InputName


@dataclass(frozen=True)
class ConsumerPort:
    """When an image-loading node's output is wired to this input, it carries the corresponding semantic key.

    读图节点的输出接到这个入口时，它承载的就是对应的语义键。"""

    input: InputName
    #: Restrict this input name to count only on these nodes; an empty set means no restriction. / 限定这个入口名只在这些节点上算数；空集即不限定。
    class_types: frozenset[str]

    def matches(self, input_name: str, class_type: str) -> bool:
        if not self.input.matches(input_name):
            return False
        return not self.class_types or class_type in self.class_types


@dataclass(frozen=True)
class OutputCandidate:
    """Artifact node candidate. / 产物节点候选。"""

    class_type: str
    #: When this input is false the node only previews and does not save. / 该入口取 false 时此节点只预览不落盘。
    requires_true: str | None


@dataclass(frozen=True)
class StepRules:
    """Step prefill for a semantic key. / 某个语义键的步长预填。"""

    default: int
    by_class_type: Mapping[str, int]

    def of(self, class_type: str) -> int:
        return self.by_class_type.get(class_type, self.default)


@dataclass(frozen=True)
class MergeNode:
    """Pairwise merge node: bypass that stage when one image is missing. / 两两合并节点：少一张图时对该级做 bypass。"""

    class_type: str
    inputs: tuple[str, ...]


@dataclass(frozen=True)
class ExternalFamily:
    """Node family of an external parameterisation convention. Recognised only by prefix, without full compatibility.

    外部参数化约定的节点族。只按前缀识别，不做完整兼容。"""

    name: str
    prefix: str
    #: Which input the parameter name is written on; ``None`` when written in the title. / 参数名写在哪个 input 上；写在标题上时为 ``None``。
    parameter_input: str | None
    #: Whether the parameter name is taken from ``_meta.title``. / 参数名是否取自 ``_meta.title``。
    parameter_from_title: bool
    #: Fields that carry the parameter name itself and are not bindable landings. / 承载参数名本身、不作为可绑落点的字段。
    ignored_inputs: frozenset[str]

    def parameter_of(self, inputs: Mapping[str, Any], title: str) -> str | None:
        """Read the parameter name this node declares. / 读出这个节点声明的参数名。"""
        if self.parameter_from_title:
            return title.strip() or None
        raw = inputs.get(self.parameter_input) if self.parameter_input else None
        return raw.strip() or None if isinstance(raw, str) else None


@dataclass(frozen=True)
class AudioTrackSource:
    """Audio track detection: if this input has a link, the final video has an audio track. / 音轨判定：这个入口有连线即成片带音轨。"""

    class_type: str
    audio_input: str
    #: When the audio input is not on the artifact node itself, first follow this input upstream to the final video node.
    #: 音频入口不在产物节点自身上时，先沿这个入口走到上游的成片节点。
    through_input: str | None
    through_class_types: frozenset[str]


@dataclass(frozen=True)
class SeedGate:
    """Seed gate: a sampler whose gate field is not ``enabled_value`` does not actually use the seed.

    种子闸门：闸门字段不取 ``enabled_value`` 的采样器不真正吃种子。"""

    class_type: str
    input: str
    enabled_value: str


@dataclass(frozen=True)
class ManualOnlyNode:
    """Known node shapes that cannot be inferred and can only be bound manually. / 已知推断不出、只能手动绑的节点形态。"""

    class_type: str
    binding_keys: tuple[str, ...]
    #: Inputs on this node that take images, but not reference images. / 这个节点上收图、但收的不是参考图的入口。
    blocked_inputs: frozenset[str]


@dataclass(frozen=True)
class InferenceRules:
    """All inference rules for one media type. / 一种媒体类型的全部推断规则。"""

    media_type: str
    semantic_keys: Mapping[str, SemanticKeyRules]
    image_loaders: tuple[NodeInput, ...]
    consumer_ports: Mapping[str, tuple[ConsumerPort, ...]]
    sampler_ports: Mapping[str, str]
    conditioning_slots: Mapping[int, str]
    conditioning_pass_through: frozenset[str]
    output_candidates: tuple[OutputCandidate, ...]
    excluded_output_class_types: frozenset[str]
    class_type_tiers: Mapping[str, tuple[frozenset[str], ...]]
    steps: Mapping[str, StepRules]
    constant_nodes: Mapping[str, str]
    optional_inputs: tuple[OptionalInput, ...]
    merge_nodes: tuple[MergeNode, ...]
    external_families: tuple[ExternalFamily, ...]
    audio_track_sources: tuple[AudioTrackSource, ...]
    seed_gates: tuple[SeedGate, ...]
    batch_size_inputs: tuple[NodeInput, ...]
    manual_only_class_types: tuple[ManualOnlyNode, ...]

    def port_of(self, binding_key: str) -> str | None:
        """Sampler conditioning port name for the semantic key. / 语义键对应的采样器条件端口名。"""
        for port, key in self.sampler_ports.items():
            if key == binding_key:
                return port
        return None

    def tier_of(self, binding_key: str, class_type: str) -> int:
        """Position of the carrier node in the semantic key's priority tiers: earlier tiers score higher, outside the tiers is 0.

        承载节点在该语义键的优先级分档里的位置：靠前的档位得分更高，档外为 0。"""
        tiers = self.class_type_tiers.get(binding_key, ())
        for index, tier in enumerate(tiers):
            if class_type in tier:
                return len(tiers) - index
        return 0

    def step_of(self, binding_key: str, class_type: str) -> int | None:
        rules = self.steps.get(binding_key)
        return None if rules is None else rules.of(class_type)

    def family_of(self, class_type: str) -> ExternalFamily | None:
        for family in self.external_families:
            if class_type.startswith(family.prefix):
                return family
        return None

    def loader_input(self, class_type: str) -> str | None:
        """Is this node an image-loading node; if so return the input holding the filename. / 这个节点是读图节点吗；是则回它承载文件名的入口。"""
        for loader in self.image_loaders:
            if loader.class_type == class_type:
                return loader.input
        return None

    def is_optional_input(self, class_type: str, input_name: str) -> bool:
        return any(item.class_type == class_type and item.input.matches(input_name) for item in self.optional_inputs)

    def merge_node(self, class_type: str) -> MergeNode | None:
        for node in self.merge_nodes:
            if node.class_type == class_type:
                return node
        return None

    def adjustable_input(self, class_type: str, input_name: str) -> bool:
        """When the graph has one image fewer, can this input be adjusted: drop the key for optional inputs, bypass pairwise
        merge nodes, anything else is not recognised.

        Inference and the actual request builder read the same criterion: inference uses it to note "this slot cannot
        adjust the graph when there are fewer images", the builder uses it to decide between adjusting the graph and
        repeating the last image. Once the two sides were implemented separately, the notes and the actual handling would
        disagree.

        图里少一张图时，这个入口改得动吗：可选入口摘键、两两合并节点 bypass，其余都不认识。

        推断与实发构造读的是同一条判据：推断据此提示「这个格子在张数变少时改不动图」，构造据此
        决定改图还是重复填充最后一张。两侧分开实现过一次，提示与实际处置就会各说各话。
        """
        if self.is_optional_input(class_type, input_name):
            return True
        merge = self.merge_node(class_type)
        return merge is not None and input_name in merge.inputs

    def seed_gate(self, class_type: str) -> SeedGate | None:
        for gate in self.seed_gates:
            if gate.class_type == class_type:
                return gate
        return None

    def blocks_image(self, class_type: str, input_name: str) -> bool:
        """The images this input takes are not reference images: control video, masks and the like; image-loading nodes flowing into it are not image candidates.

        这个入口收的图不是参考图：控制视频、掩码一类，流向它的读图节点不进图像类候选。"""
        return any(
            node.class_type == class_type and input_name in node.blocked_inputs for node in self.manual_only_class_types
        )

    def output_rank(self, class_type: str) -> int:
        """Priority of artifact candidates: earlier candidates score higher, 0 if not in the candidate table.

        产物候选的优先级：靠前的候选分更高，不在候选表里为 0。"""
        for index, candidate in enumerate(self.output_candidates):
            if candidate.class_type == class_type:
                return len(self.output_candidates) - index
        return 0

    def output_candidate(self, class_type: str) -> OutputCandidate | None:
        for candidate in self.output_candidates:
            if candidate.class_type == class_type:
                return candidate
        return None


@cache
def load_rules_schema() -> dict[str, Any]:
    """Read and cache the schema of the rule tables. Public, so tests and the docs site get the same contract.

    读入并缓存规则表的 schema。对外公开，供测试与文档站取同一份契约。"""
    return json.loads(RULES_SCHEMA_PATH.read_text(encoding="utf-8"))


@cache
def load_inference_rules(media_type: str) -> InferenceRules:
    """Read the rule table for a media type. An unregistered media type is the caller's error, a plain ``KeyError``.

    读入某种媒体类型的规则表。未登记的媒体类型是调用方的错，直接 ``KeyError``。"""
    document = json.loads(RULES_PATHS[media_type].read_text(encoding="utf-8"))
    _validator().validate(document)
    unknown = sorted(semantic_key_names(document) - set(BINDING_KEYS_BY_MEDIA_TYPE[media_type]))
    if unknown:
        raise ValueError(f"inference_rules.{media_type}.json 提到 {media_type} 端点没有的语义键：{', '.join(unknown)}")
    return _rules_from(document)


def semantic_key_names(document: Mapping[str, Any]) -> frozenset[str]:
    """All semantic key names mentioned in a rule table, whether in key position or value position.

    The schema cannot cover this level: both media types share one schema, but each has its own set of valid keys. A
    misspelled key name -- ``reference_images`` written as ``reference_image``, or ``frames`` put into the image table
    -- still passes the schema, with the result that nobody reads that rule section and the semantic key silently fails
    to infer.

    一份规则表里提到的全部语义键名，无论写在键位上还是值位上。

    schema 管不到这一层：两种媒体类型共用一份 schema，可用的键集却各有一份。写错一个键名——
    ``reference_images`` 写成 ``reference_image``、或者把 ``frames`` 写进图像表——schema 照样过，
    结果是那一节规则谁都读不到，那个语义键悄悄推断不出来。
    """
    keyed = {
        key
        for section in ("semantic_keys", "consumer_ports", "class_type_tiers", "steps")
        for key in _mapping(document.get(section))
    }
    valued = {
        str(key)
        for section in ("sampler_ports", "conditioning_slots")
        for key in _mapping(document.get(section)).values()
    }
    listed = {
        key for item in _items(document.get("manual_only_class_types")) for key in _strings(item.get("binding_keys"))
    }
    return frozenset(keyed | valued | listed)


@cache
def _validator() -> Draft202012Validator:
    schema = load_rules_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _rules_from(document: Mapping[str, Any]) -> InferenceRules:
    return InferenceRules(
        media_type=str(document["media_type"]),
        semantic_keys={
            key: SemanticKeyRules(
                input_names=_strings(rules.get("input_names")),
                class_types=frozenset(_strings(rules.get("class_types"))),
                input_name_class_types={
                    name: frozenset(_strings(values))
                    for name, values in _mapping(rules.get("input_name_class_types")).items()
                },
                title_keywords=_strings(rules.get("title_keywords")),
            )
            for key, rules in _mapping(document.get("semantic_keys")).items()
        },
        image_loaders=_node_inputs(document.get("image_loaders")),
        consumer_ports={
            key: tuple(ConsumerPort(_input_name(port), frozenset(_strings(port.get("class_types")))) for port in ports)
            for key, ports in _mapping(document.get("consumer_ports")).items()
        },
        sampler_ports={str(port): str(key) for port, key in _mapping(document.get("sampler_ports")).items()},
        conditioning_slots={int(slot): str(key) for slot, key in _mapping(document.get("conditioning_slots")).items()},
        conditioning_pass_through=frozenset(_strings(document.get("conditioning_pass_through"))),
        output_candidates=tuple(
            OutputCandidate(str(item["class_type"]), _optional_str(item.get("requires_true")))
            for item in _items(document.get("output_candidates"))
        ),
        excluded_output_class_types=frozenset(_strings(document.get("excluded_output_class_types"))),
        class_type_tiers={
            key: tuple(frozenset(_strings(tier)) for tier in tiers)
            for key, tiers in _mapping(document.get("class_type_tiers")).items()
        },
        steps={
            key: StepRules(
                default=int(rules["default"]),
                by_class_type={name: int(step) for name, step in _mapping(rules.get("by_class_type")).items()},
            )
            for key, rules in _mapping(document.get("steps")).items()
        },
        constant_nodes={str(name): str(field) for name, field in _mapping(document.get("constant_nodes")).items()},
        optional_inputs=tuple(
            OptionalInput(str(item["class_type"]), _input_name(item))
            for item in _items(document.get("optional_inputs"))
        ),
        merge_nodes=tuple(
            MergeNode(str(item["class_type"]), _strings(item.get("inputs")))
            for item in _items(document.get("merge_nodes"))
        ),
        external_families=tuple(_family_from(item) for item in _items(document.get("external_families"))),
        audio_track_sources=tuple(_audio_from(item) for item in _items(document.get("audio_track_sources"))),
        seed_gates=tuple(
            SeedGate(str(item["class_type"]), str(item["input"]), str(item["enabled_value"]))
            for item in _items(document.get("seed_gates"))
        ),
        batch_size_inputs=_node_inputs(document.get("batch_size_inputs")),
        manual_only_class_types=tuple(
            ManualOnlyNode(
                str(item["class_type"]),
                _strings(item.get("binding_keys")),
                frozenset(_strings(item.get("blocked_inputs"))),
            )
            for item in _items(document.get("manual_only_class_types"))
        ),
    )


def _family_from(item: Mapping[str, Any]) -> ExternalFamily:
    source = _mapping(item.get("parameter_from"))
    return ExternalFamily(
        name=str(item["name"]),
        prefix=str(item["prefix"]),
        parameter_input=_optional_str(source.get("input")),
        parameter_from_title=bool(source.get("title", False)),
        ignored_inputs=frozenset(_strings(item.get("ignored_inputs"))),
    )


def _audio_from(item: Mapping[str, Any]) -> AudioTrackSource:
    through = _mapping(item.get("through"))
    return AudioTrackSource(
        class_type=str(item["class_type"]),
        audio_input=str(item["audio_input"]),
        through_input=_optional_str(through.get("input")),
        through_class_types=frozenset(_strings(through.get("class_types"))),
    )


def _node_inputs(raw: object) -> tuple[NodeInput, ...]:
    return tuple(NodeInput(str(item["class_type"]), str(item["input"])) for item in _items(raw))


def _input_name(item: Mapping[str, Any]) -> InputName:
    """Exactly one of ``input`` and ``input_prefix`` is written, guaranteed by the schema on read.

    ``input`` 与 ``input_prefix`` 恰好写一个，由 schema 在读入时保证。"""
    if "input_prefix" in item:
        return InputName(str(item["input_prefix"]), prefix=True)
    return InputName(str(item["input"]))


def _items(raw: object) -> Iterable[Any]:
    """Only check whether the outer level is an array; element shapes are guaranteed by the schema on read.

    只认外层是不是数组；元素的形状由 schema 在读入时保证。"""
    return raw if isinstance(raw, list) else ()


def _mapping(raw: object) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def _strings(raw: object) -> tuple[str, ...]:
    return tuple(str(item) for item in raw) if isinstance(raw, list) else ()


def _optional_str(raw: object) -> str | None:
    return str(raw) if isinstance(raw, str) else None
