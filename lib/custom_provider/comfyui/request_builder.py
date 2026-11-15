"""Building the workflow actually sent: fill values into a deep copy of the base by node bindings, convert size and
frame count, set the seed, adjust the graph by image count.

Turning a generation request into a workflow ComfyUI can accept takes four conversions: the project only gives an
aspect ratio and resolution tier, the workflow wants pixels; the project only gives a duration, the workflow wants a
frame count; the seed is random or kept per the binding entry's policy; when there are fewer reference images than
slots reserved in the workflow, the extra image-loading nodes must be deleted together with their downstream rather
than repeating the last image. All four depend only on the inputs -- no config reads, no database, no requests;
material upload is done by the caller beforehand, and what is passed in are reference names the server already knows.

The base is never modified: ``definition["workflow"]`` is the single source of truth for this endpoint, and every
submission deep-copies it before filling. The output is returned together with ``workflow_sha256``; the same inputs
built twice give the same digest, so preview requests and real submissions can be compared by it.

实发 workflow 的构造：在底稿深拷贝上按节点绑定填值、换算尺寸与帧数、定种子、按张数改图。

一次生成请求要变成一份 ComfyUI 能提交的 workflow，中间隔着四件换算：项目只给比例与分辨率档，
workflow 要的是像素；项目只给时长，workflow 要的是帧数；种子按绑定条目的策略或随机或保留；参考
图张数少于 workflow 预留的格子时得把多余的读图节点连同它们的下游一起删掉，而不是把最后一张
重复填满。四件都只依赖入参，不读配置、不碰数据库、不发请求——上传素材由调用方先做完，传进来的
是服务端已认得的引用名。

底稿一律不动：``definition["workflow"]`` 是这个端点的唯一真相，每次提交都从它深拷贝一份再填。
产出连同 ``workflow_sha256`` 一起回传，同一份入参两次构造得到同一个摘要，预览请求与真实提交据此
可比。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from random import Random
from typing import Any

from arcreel_market_core.aspect_size import (
    DEFAULT_SHORT_EDGE,
    IMAGE_TIER_SHORT_EDGE,
    VIDEO_TIER_SHORT_EDGE,
    aspect_size,
)
from arcreel_market_core.aspect_size import resolution_to_short_edge as short_edge_of_resolution
from arcreel_market_core.comfyui.bindings import align_frames, step_of
from arcreel_market_core.comfyui.bindings import bound_fps as _bound_fps
from arcreel_market_core.comfyui.bindings import int_literal_of as _int_literal
from arcreel_market_core.comfyui.bindings import literal_of as _literal
from arcreel_market_core.comfyui.bindings import positive_number as _positive_number
from arcreel_market_core.comfyui.bindings import targets_of as _targets
from arcreel_market_core.comfyui.workflow import is_link, node_inputs
from lib.prompts.prompt_utils import append_avoid_text, split_avoid_lines

from .capabilities import keeps_its_own_frame_count, size_is_fixed
from .failures import IMAGE_DROP_UNSUPPORTED, ComfyuiError
from .inference_rules import InferenceRules, MergeNode, load_inference_rules

logger = logging.getLogger(__name__)

#: Upper bound (exclusive) of the random seed range: the seed input of ComfyUI samplers takes a 32-bit unsigned integer.
#: 随机种子的取值区间上界（不含）：ComfyUI 各采样器的 seed 输入按 32 位无符号整数收。
SEED_UPPER_BOUND = 2**32

#: Default policy for seed entries, same value as the schema's ``default``. / 种子条目缺省策略，与 schema 的 ``default`` 同值。
_DEFAULT_SEED_POLICY = "random"


@dataclass(frozen=True)
class MediaInputs:
    """Material reference names actually included in this generation: the ``subfolder/name`` from the server's upload
    response (just ``name`` without a subfolder), assembled by the caller after upload.

    ``None`` versus an empty sequence means "not given this time", unrelated to "this endpoint does not support it" --
    the latter is decided by the binding's three states.

    本次生成实际带上的素材引用名：服务端上传响应给出的 ``subfolder/name``（无子目录只写
    ``name``），由调用方在上传后拼好。

    ``None`` 与空序列的区别是「这次没给」，与「这个端点不支持」无关——后者看的是绑定三态。
    """

    start_image: str | None = None
    end_image: str | None = None
    reference_images: Sequence[str] = ()


@dataclass(frozen=True)
class BuiltWorkflow:
    """Output of one build: the workflow to send, its digest, and the resolved values of the three conversions.

    ``width`` / ``height`` / ``frames`` / ``seed`` being ``None`` means that dimension has no binding and the
    workflow's literal value applies. The endpoint test preview request explains the conversions from these items, and
    ``dropped_nodes`` tells which nodes this graph adjustment deleted, so they are returned with the workflow rather than
    only kept in logs.

    ``negative_prompt`` is the exclusion text split out of the prompt body itself, not the final value of the negative
    node -- the latter also carries the literal value the workflow author wrote there, and when no negative input is
    bound not a single character is written into it.

    一次构造的产出：实发 workflow、它的摘要，以及三项换算的落地值。

    ``width`` / ``height`` / ``frames`` / ``seed`` 为 ``None`` 表示该维度没有绑定、值以 workflow
    字面值为准。端点测试的预览请求按这几项给出换算说明，``dropped_nodes`` 则说明这次改图删掉了
    哪些节点，故它们与 workflow 一并回传而不是只留在日志里。

    ``negative_prompt`` 是从正文里拆出的排除项文本本身，不是负向节点最终的值——后者还带着
    workflow 作者写在那里的字面值，未绑定负向入口时更是一个字也没写进去。
    """

    workflow: dict[str, Any]
    workflow_sha256: str
    seed: int | None = None
    width: int | None = None
    height: int | None = None
    frames: int | None = None
    negative_prompt: str = ""
    dropped_nodes: tuple[str, ...] = ()


def build_workflow(
    definition: Mapping[str, Any],
    *,
    prompt: str,
    aspect_ratio: str,
    resolution: str | None = None,
    duration_seconds: float | None = None,
    media: MediaInputs | None = None,
    seed: int | None = None,
    rng: Random | None = None,
) -> BuiltWorkflow:
    """Build the workflow to send from a ComfyUI endpoint definition and one generation request.

    ``rng`` is the injection point for the random seed, defaulting in production to the module-level
    :class:`~random.Random`; ``seed`` is the request's own seed (image requests may specify one); it overrides random
    but not the ``keep`` policy -- when the author declares the literal value kept, a seed in the request should not
    silently change this workflow's intended behaviour.

    Adjust the graph first, then fill values: deleting nodes changes the shape of the graph, while filling only knows
    node ids and field names; filling on an incomplete graph would just write values into a node about to disappear.

    按一份 ComfyUI 端点定义与一次生成请求，构造实发 workflow。

    ``rng`` 是随机种子的注入点，生产默认用模块级 :class:`~random.Random`；``seed`` 是请求自带的
    种子（图像请求可指定），它顶掉随机但顶不掉 ``keep`` 策略——作者声明保留字面值时，请求里的种子
    不该悄悄改写这份 workflow 的既定行为。

    先改图后填值：删节点会改变图的形状，而填值只认节点 id 与字段名，在残缺的图上填值只会把值写进
    一个即将消失的节点里。
    """
    workflow = deepcopy(dict(definition["workflow"]))
    bindings: Mapping[str, Any] = definition.get("bindings") or {}
    media_type = "image" if definition.get("media_type") == "image" else "video"

    dropped = _apply_media(workflow, bindings, media or MediaInputs(), load_inference_rules(media_type))

    body, avoid_text = split_avoid_lines(prompt)
    _write_all(workflow, _targets(bindings.get("prompt")), body)
    _write_negative_prompt(workflow, bindings.get("negative_prompt"), avoid_text)
    width, height = _write_size(workflow, bindings, aspect_ratio=aspect_ratio, resolution=resolution, media=media_type)
    # When the graph sets its own clip length and that length cannot match a native duration option, leave the frame count
    # alone: the endpoint says exactly that "duration is not driven by ArcReel" (no options, read-only in the UI); the
    # seconds in the request are borrowed by the planning layer, not chosen by the user.
    # 图自己写着片长、而这份片长凑不出一档原生时长时不动帧数：端点对外说的正是「时长不由
    # ArcReel 驱动」（档位为空、界面只读），请求里那个秒数是规划层借的，不是用户选的。
    driven = None if keeps_its_own_frame_count(definition) else duration_seconds
    frames = _write_frames(workflow, bindings, duration_seconds=driven)
    actual_seed = _write_seed(workflow, bindings, requested=seed, rng=rng or Random())

    return BuiltWorkflow(
        workflow=workflow,
        workflow_sha256=workflow_sha256(workflow),
        seed=actual_seed,
        width=width,
        height=height,
        frames=frames,
        negative_prompt=avoid_text,
        dropped_nodes=dropped,
    )


def workflow_sha256(workflow: Mapping[str, Any]) -> str:
    """Content digest of a workflow.

    SHA-256 over canonical JSON (sorted keys, no extra whitespace, non-ASCII as is): the same content gives the same
    digest regardless of dict insertion order, so preview requests and real submissions, and two builds, are comparable.

    一份 workflow 的内容摘要。

    按规范化 JSON（键排序、无多余空白、非 ASCII 原样）取 SHA-256：同一份内容不论 dict 的插入序
    如何都得到同一个摘要，预览请求与真实提交、两次构造之间才可比。
    """
    canonical = json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- Value-filling primitives
# ---------------------------------------------------------------- 填值原语


def _mutable_inputs(node: Any) -> dict[str, Any]:
    """Get a node's writable ``inputs``. If ``inputs`` is missing or misshaped, add an empty table in place.

    取一个节点可写的 ``inputs``。缺 ``inputs`` 或形状不对时就地补一个空表。"""
    if not isinstance(node, dict):
        return {}
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
        node["inputs"] = inputs
    return inputs


def _write_one(workflow: dict[str, Any], target: Mapping[str, Any], value: object) -> None:
    """Write one value into one target. Skip if the target node has already disappeared in graph adjustment; never create nodes.

    把一个值写进一个目标。目标节点已在改图中消失时跳过，不新建节点。"""
    node_id = str(target["node"])
    name = target.get("input")
    if node_id not in workflow or not isinstance(name, str):
        logger.debug("跳过填值：节点 %s 的 %r 已不在实发 workflow 中", node_id, name)
        return
    _mutable_inputs(workflow[node_id])[name] = value


def _write_all(workflow: dict[str, Any], targets: Sequence[Mapping[str, Any]], value: object) -> None:
    """Write the same value into all targets of a semantic key.

    Takes an already normalised sequence of entries and does not normalise again internally: the caller already holds
    the result of :func:`_targets`, and if the function took the raw value again there would be ambiguity about which
    kind was passed in -- passing in a normalised result would then be judged misshaped and the whole batch dropped.

    把同一个值写进一个语义键的全部目标。

    收 已规范化 的条目序列，不在内部再规范一次：调用方手里本就有 :func:`_targets` 的结果，函数
    再收一次原始值就会出现「传进来的到底是哪一种」的歧义——传规范化过的结果进来反而被判成形状
    不对而整批丢掉。
    """
    for target in targets:
        _write_one(workflow, target, value)


# ---------------------------------------------------------------- Negative prompt
# ---------------------------------------------------------------- 负向提示词


def _write_negative_prompt(workflow: dict[str, Any], targets: object, avoid_text: str) -> None:
    """Append the exclusions split out of the prompt body after the negative node's literal value.

    When unbound (empty list or missing key) fill nothing: the exclusions were already removed from the body when
    splitting, this workflow has no negative input, and stuffing them back into the body would describe "what must not
    appear" to the model as "what should appear".

    把正文里拆出的排除项追加到负向节点的字面值之后。

    未绑定（空列表或键缺失）时什么也不填：排除项已在拆分时从正文里删掉，这份 workflow 没有负向
    入口，硬塞回正文只会把「不要出现的东西」当成「要出现的东西」描述给模型。
    """
    for target in _targets(targets):
        literal = _literal(workflow, target)
        _write_one(workflow, target, append_avoid_text(literal if isinstance(literal, str) else "", avoid_text))


# ---------------------------------------------------------------- Size
# ---------------------------------------------------------------- 尺寸


def _write_size(
    workflow: dict[str, Any],
    bindings: Mapping[str, Any],
    *,
    aspect_ratio: str,
    resolution: str | None,
    media: str,
) -> tuple[int | None, int | None]:
    """Derive width and height from the project aspect ratio and resolution tier, align down per entry by its step, then write.

    ``round_to`` takes the least common multiple of all steps on both width and height:
    :func:`~arcreel_market_core.aspect_size.aspect_size` produces width and height that are integer multiples of it, so
    each entry's step divides them naturally, with zero aspect error. Before writing, still align down once more by the
    entry's step -- alignment is a constraint of that workflow input itself, and whether it holds should not depend on how
    ``round_to`` happens to be chosen.

    When no resolution is selected, the short side takes the smaller of the workflow's literal width and height: the
    workflow's native size is the tier the author tuned; the aspect ratio still follows the project. If the literal
    values cannot be read as integers, fall back to the cross-backend default short side.

    Whether the size dimension can be driven goes through the single criterion
    :func:`~lib.custom_provider.comfyui.capabilities.size_is_fixed` -- the UI uses it to disable the resolution
    selector, filling uses it to decide whether to write; the two don't each have their own copy. When only one side is
    bound it counts as fixed: the derived size could only be written to the bound side, the other side stays the
    workflow's literal value, and the resulting aspect ratio would be neither native nor what the user wanted.

    按项目比例与分辨率档派生宽高，逐条目按步长向下对齐后写入。

    ``round_to`` 取宽高两侧全部步长的最小公倍数：:func:`~arcreel_market_core.aspect_size.aspect_size` 产出的宽高
    都是它的整数倍，于是每个条目各自的步长天然被整除，比例零偏差。写入前仍按条目步长再向下对齐
    一次——对齐是 workflow 那个输入自己的约束，它成立与否不该取决于 ``round_to`` 恰好怎么取。

    分辨率未选时短边取 workflow 字面宽高的较小者：这份 workflow 的原生尺寸就是作者调好的那一档，
    比例仍按项目走。字面值读不出整数时退到跨后端统一的兜底短边。

    尺寸这一维驱不驱动得了走 :func:`~lib.custom_provider.comfyui.capabilities.size_is_fixed` 这一份
    判据——界面据它禁用分辨率选择器，填值据它决定写不写，两处不各写一份。只绑一侧时它判为固定：
    派生出的宽高只写得进绑了的那一侧，另一侧仍是 workflow 的字面值，产出的比例既不是原生的也不是
    用户要的。
    """
    if size_is_fixed(bindings):
        return None, None
    width_targets = _targets(bindings.get("width"))
    height_targets = _targets(bindings.get("height"))

    round_to = math.lcm(*[step_of(target) for target in (*width_targets, *height_targets)])
    tier_map = IMAGE_TIER_SHORT_EDGE if media == "image" else VIDEO_TIER_SHORT_EDGE
    if resolution and resolution.strip():
        short_edge = short_edge_of_resolution(resolution, tier_map=tier_map)
    else:
        short_edge = _literal_short_edge(workflow, width_targets, height_targets)

    width, height = aspect_size(aspect_ratio, short_edge, round_to=round_to)
    written_width = _write_stepped(workflow, width_targets, width)
    written_height = _write_stepped(workflow, height_targets, height)
    return written_width, written_height


def _literal_short_edge(
    workflow: Mapping[str, Any],
    width_targets: Sequence[Mapping[str, Any]],
    height_targets: Sequence[Mapping[str, Any]],
) -> int:
    literals = [
        value
        for target in (*width_targets, *height_targets)
        if (value := _int_literal(workflow, target)) is not None and value > 0
    ]
    if not literals:
        logger.info("分辨率未选且 workflow 字面宽高读不出整数，短边取兜底 %d", DEFAULT_SHORT_EDGE)
        return DEFAULT_SHORT_EDGE
    return min(literals)


def _write_stepped(workflow: dict[str, Any], targets: Sequence[Mapping[str, Any]], value: int) -> int | None:
    """Write a derived size after aligning it down to each entry's step; return the last value written. / 把一个派生尺寸按各条目的步长向下对齐后写入，回传最后写出的值。"""
    written: int | None = None
    for target in targets:
        written = _align_down(value, step_of(target))
        _write_one(workflow, target, written)
    return written


def _align_down(value: int, step: int) -> int:
    """Align down to an integer multiple of ``step``, with a floor of one ``step`` -- submitting a zero size is sure to fail.

    向下对齐到 ``step`` 的整数倍，下限一个 ``step``——零尺寸提交上去必然报错。"""
    return max(step, value - value % step)


# ---------------------------------------------------------------- Frame count
# ---------------------------------------------------------------- 帧数


def _write_frames(
    workflow: dict[str, Any], bindings: Mapping[str, Any], *, duration_seconds: float | None
) -> int | None:
    """Convert duration and frame rate to a frame count and write it.

    ``frames = round(duration × fps) + 1``: ComfyUI video models count frames as "first frame + some intervals", so 1
    second at 24 fps is 25 frames, not 24. The frame rate preferably comes from the literal value read by the read-only
    ``fps`` binding -- that is the rate this workflow really uses; without that binding take the constant hand-filled on
    the ``frames`` entry. If neither exists don't write: changing the frame count based on a guessed frame rate is more
    likely to produce a wrong clip length than letting the workflow keep its own literal value.

    For frame counts, step means ``frames ≡ 1 (mod step)`` (4n+1 / 8n+1 and the like), aligned down with a floor of ``1 + step``.

    ``duration_seconds`` being ``None`` means "this dimension is not driven by this request"; the caller passes it when
    the endpoint cannot offer duration options: in that case the workflow's literal frame count is kept as is.

    按时长与帧率换算帧数并写入。

    ``frames = round(时长 × 帧率) + 1``：ComfyUI 的视频模型按「首帧 + 若干段间隔」计帧，24 帧
    每秒的 1 秒是 25 帧而不是 24。帧率优先取 ``fps`` 只读绑定读出的字面值——那是这份 workflow 真
    正在用的帧率；没有该绑定时取 ``frames`` 条目上手填的常量。两处都没有就不写：凭一个猜出来的
    帧率改帧数，比让 workflow 保持它自己的字面值更容易出片长不符。

    步长对帧数的含义是 ``frames ≡ 1 (mod step)``（4n+1 / 8n+1 这类），向下对齐、下限 ``1 + step``。

    ``duration_seconds`` 为 ``None`` 即「这一维不由本次请求驱动」，调用方在端点给不出档位时传的
    就是它：那种情形下 workflow 的字面帧数原样留着。
    """
    targets = _targets(bindings.get("frames"))
    if not targets or duration_seconds is None:
        return None

    bound_fps = _bound_fps(workflow, bindings)
    written: int | None = None
    for target in targets:
        fps = bound_fps if bound_fps is not None else _positive_number(target.get("fps"))
        if fps is None:
            logger.info("帧数未写：既无 fps 只读绑定，条目也未手填帧率")
            continue
        step = step_of(target)
        written = align_frames(round(duration_seconds * fps) + 1, step)
        _write_one(workflow, target, written)
    return written


# ---------------------------------------------------------------- Seed
# ---------------------------------------------------------------- 种子


def _write_seed(
    workflow: dict[str, Any], bindings: Mapping[str, Any], *, requested: int | None, rng: Random
) -> int | None:
    """Determine the seed by each entry's policy and write it; return the seed actually in effect this time.

    ``random`` entries get the same value: multiple samplers in one workflow (MoE's dual sampler is a common shape) share
    one seed; randomising each sampler separately would make one generation irreproducible. ``keep`` entries are not
    touched by a single byte, and even the request's own seed does not override them -- the author declaring the
    literal value kept is this workflow's intended behaviour.

    The return value is "the seed actually in effect" rather than "the generated seed": when all are ``keep`` the literal
    value is returned, so what the version metadata records is the number really needed to rerun this version.

    按各条目的策略定种子并写入，回传本次实际生效的种子。

    ``random`` 条目写同一个值：一份 workflow 里的多个采样器（MoE 的双采样器是常见形态）共用一个
    种子，两个采样器各随机一次会让同一次生成不可复现。``keep`` 条目一个字节都不动，请求自带的
    种子也顶不掉它——作者声明保留字面值是这份 workflow 的既定行为。

    回传值是「实际生效的种子」而不是「生成的种子」：全是 ``keep`` 时回传字面值，版本元数据记下
    的才是重跑这一版真正要用的那个数。
    """
    targets = _targets(bindings.get("seed"))
    if not targets:
        return None
    rolling = [target for target in targets if target.get("policy", _DEFAULT_SEED_POLICY) == _DEFAULT_SEED_POLICY]
    if not rolling:
        return next((value for target in targets if (value := _int_literal(workflow, target)) is not None), None)

    value = requested if requested is not None else rng.randrange(SEED_UPPER_BOUND)
    for target in rolling:
        _write_one(workflow, target, value)
    return value


# ---------------------------------------------------------------- Graph adjustment
# ---------------------------------------------------------------- 改图


def _apply_media(
    workflow: dict[str, Any], bindings: Mapping[str, Any], media: MediaInputs, rules: InferenceRules
) -> tuple[str, ...]:
    """Fill in the materials provided this time, and delete image-loading nodes with no material to fill, together with their downstream.

    First/end frames and reference images all require graph adjustment when "bound but no value given this time": an
    image-loading node left in the graph would read an image from last time by the workflow's literal filename, the
    model would generate from it, and the user would see a first frame they never chose.

    填入本次带上的素材，并把没有素材可填的读图节点连同下游一并删掉。

    首尾帧与参考图都是「绑定了但这次没给值」就要改图：读图节点留在图里会按 workflow 的字面
    文件名去读一张上次的图，模型照着它出片，用户看到的是一张自己没选过的首帧。
    """
    doomed: list[str] = []
    for key, value in (("start_image", media.start_image), ("end_image", media.end_image)):
        targets = _targets(bindings.get(key))
        if value is not None:
            _write_all(workflow, targets, value)
        else:
            doomed.extend(str(target["node"]) for target in targets)
    doomed.extend(
        _apply_reference_images(workflow, _targets(bindings.get("reference_images")), media.reference_images, rules)
    )
    if not doomed:
        return ()
    return _drop_nodes(workflow, doomed, output_nodes=_output_nodes(bindings), rules=rules)


def _apply_reference_images(
    workflow: dict[str, Any],
    targets: Sequence[Mapping[str, Any]],
    values: Sequence[str],
    rules: InferenceRules,
) -> list[str]:
    """Fill reference images by count; return the image-loading nodes to delete.

    When there are fewer images than slots, delete the extra slots rather than repeating the last image -- a repeated
    image makes the model treat it as a subject emphasised twice. The precondition is recognising the input the slot is
    wired to: ``consumer`` is the landing recorded when the binding was saved, and the graph can only be adjusted when
    it lands on an optional input or a pairwise merge node. No recorded ``consumer``, or one landing outside these two
    tables, counts as unrecognised -- then fall back to repeating the last image, preferring a skewed composition over
    removing a required input on a guess and having ComfyUI report ``node_errors`` on submission. Inference already gave
    a note by the same criterion when this binding was saved. When no image is given at all there is nothing to repeat,
    and the image-loading nodes keep the base's literal values.

    按张数填参考图，回传要删的读图节点。

    张数少于格子数时删多余的格子，而不是把最后一张重复填满——重复一张会让模型把它当成被强调了
    两次的主体。前提是认得这个格子接到的那个入口：``consumer`` 是保存绑定时记下的落点，它落在
    可选入口或两两合并节点上才改得动图。没记下 ``consumer``、或它落在这两张表之外，都算不认识
    ——此时退回重复最后一张，宁可构图偏了也不凭猜测把一个必需输入摘掉、让 ComfyUI 在提交时报
    ``node_errors``。保存这份绑定时推断已按同一条判据给过提示。一张都没给时连可重复的都没有，
    读图节点保持底稿字面值。
    """
    filled = min(len(targets), len(values))
    for index in range(filled):
        _write_one(workflow, targets[index], values[index])
    spare = targets[filled:]
    if not spare:
        return []
    if any(not _adjustable(target, rules) for target in spare):
        logger.info("参考图格子 %d 个、本次 %d 张，但有格子的 consumer 改不动图，改图跳过", len(targets), len(values))
        if values:
            _write_all(workflow, spare, values[-1])
        return []
    return [str(target["node"]) for target in spare]


def _adjustable(target: Mapping[str, Any], rules: InferenceRules) -> bool:
    """Can this reference image slot adjust the graph when there are fewer images. / 这个参考图格子在张数变少时改得动图吗。"""
    consumer = target.get("consumer")
    if not isinstance(consumer, Mapping):
        return False
    class_type = consumer.get("class_type")
    input_name = consumer.get("input")
    if not isinstance(class_type, str) or not isinstance(input_name, str):
        return False
    return rules.adjustable_input(class_type, input_name)


def _output_nodes(bindings: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(str(target["node"]) for target in _targets(bindings.get("output")))


def _drop_nodes(
    workflow: dict[str, Any], seeds: Sequence[str], *, output_nodes: frozenset[str], rules: InferenceRules
) -> tuple[str, ...]:
    """Delete the given nodes, cascading forward to downstream and cleaning up backward the upstream that only fed them.

    Forward handling has three cases by node type (criteria in the optional input and merge node sections of the
    inference rule table): drop the key for inputs allowed to be absent, bypass pairwise merge nodes, otherwise delete it
    too and keep cascading. Reaching the artifact node in the cascade is a failure: the final video chain itself depends
    on this image, and submitting a workflow missing it would only buy a remote error plus a wait.

    删掉给定的节点，前向级联到下游、后向清理只喂给它们的上游。

    前向三种处置按节点类型分（判据在推断规则表的可选入口与合并节点两节）：允许缺席的输入摘键、
    两两合并节点 bypass、其余连它一起删并继续级联。级联触到产物节点即失败：成片链路本身依赖这张
    图，提交一份缺了它的 workflow 只会换来一次远端报错加一次等待。
    """
    deleted: list[str] = []
    orphan_candidates: set[str] = set()
    pending = list(seeds)
    while pending:
        node_id = pending.pop()
        if node_id not in workflow:
            continue
        if node_id in output_nodes:
            raise ComfyuiError(IMAGE_DROP_UNSUPPORTED, node=node_id)
        orphan_candidates.update(_link_sources(workflow[node_id]))
        del workflow[node_id]
        deleted.append(node_id)
        pending.extend(_detach_consumers(workflow, node_id, output_nodes, deleted, orphan_candidates, rules))
    _prune_orphans(workflow, orphan_candidates, pinned=output_nodes, deleted=deleted)
    return tuple(deleted)


def _detach_consumers(
    workflow: dict[str, Any],
    node_id: str,
    output_nodes: frozenset[str],
    deleted: list[str],
    orphan_candidates: set[str],
    rules: InferenceRules,
) -> list[str]:
    """Handle all downstream nodes referencing the node just deleted; return those that must be deleted as well.

    处置引用了刚删掉那个节点的全部下游，回传其中必须一并删除的。"""
    doomed: list[str] = []
    for consumer_id in list(workflow):
        consumer = workflow.get(consumer_id)
        if consumer is None:
            continue
        inputs = _mutable_inputs(consumer)
        names = [name for name, raw in inputs.items() if _refers_to(raw, node_id)]
        if not names:
            continue
        class_type = str(consumer.get("class_type") or "")
        merge = rules.merge_node(class_type)
        for name in names:
            if rules.is_optional_input(class_type, name):
                del inputs[name]
            elif merge is not None and name in merge.inputs and consumer_id not in output_nodes:
                if _bypass_merge(workflow, consumer_id, merge, name, deleted, orphan_candidates):
                    break
                doomed.append(consumer_id)
            else:
                doomed.append(consumer_id)
    return doomed


def _bypass_merge(
    workflow: dict[str, Any],
    merge_id: str,
    merge: MergeNode,
    dropped: str,
    deleted: list[str],
    orphan_candidates: set[str],
) -> bool:
    """Remove a pairwise merge node from the graph and rewire its downstream to the remaining branch.

    It can only be removed if the remaining branch is a link still connected to a live node. Two cases cannot be
    removed, and the node must be deleted too and the cascade continued: with a literal value there is no "upstream" to
    rewire to; when both inputs are wired to the same image-loading node, "the remaining branch" points exactly at the
    node just deleted, and rewiring to it would leave the downstream with a dangling link while this workflow still gets
    submitted.

    No new nodes are added; only downstream references change.

    把一个两两合并节点从图里摘掉，它的下游改接剩下那一路。

    剩下那一路必须是条还连着活节点的连线才摘得掉。两种情况摘不掉，都只能把这个节点也删掉、继续
    级联：换成字面值就没有「上游」可以改接；两个入口接的是同一个读图节点时，「剩下那一路」指的
    正是刚刚删掉的那个节点，照它改接只会给下游留一条悬空连线，而这份 workflow 照样会提交出去。

    不新增节点，只改下游的引用。
    """
    inputs = _mutable_inputs(workflow[merge_id])
    first, second = merge.inputs
    survivor = _as_link(inputs.get(second if dropped == first else first))
    if survivor is None or str(survivor[0]) not in workflow:
        return False
    for other_id in list(workflow):
        if other_id == merge_id:
            continue
        other_inputs = _mutable_inputs(workflow[other_id])
        for name, raw in list(other_inputs.items()):
            if _refers_to(raw, merge_id):
                other_inputs[name] = list(survivor)
    orphan_candidates.update(_link_sources(workflow[merge_id]))
    del workflow[merge_id]
    deleted.append(merge_id)
    return True


def _prune_orphans(
    workflow: dict[str, Any], candidates: set[str], *, pinned: frozenset[str], deleted: list[str]
) -> None:
    """Backward cleanup: upstream nodes that only fed deleted nodes are deleted along with them.

    Candidates only come from the linked upstream of deleted nodes, not "every node in the graph without downstream" --
    preview nodes and the like have no downstream anyway, and sweeping them too would be rewriting this workflow for the
    user. Nodes carrying bindings are not specially kept: once all their downstream is gone they are dead nodes, and
    values filled into them would never be executed by ComfyUI.

    后向清理：只喂给已删节点的上游随之删除。

    候选只来自被删节点的连线上游，不是「图里所有没有下游的节点」——预览节点之类本来就没有下游，
    一并扫掉等于替用户改写这份 workflow。承载绑定的节点不额外留下：下游全没了之后它已是死节点，
    往里填的值 ComfyUI 也不会执行到。
    """
    pending = set(candidates)
    while pending:
        node_id = pending.pop()
        if node_id not in workflow or node_id in pinned or _has_consumer(workflow, node_id):
            continue
        pending.update(_link_sources(workflow[node_id]))
        del workflow[node_id]
        deleted.append(node_id)


def _has_consumer(workflow: Mapping[str, Any], node_id: str) -> bool:
    return any(
        _refers_to(raw, node_id)
        for other_id, other in workflow.items()
        if other_id != node_id
        for raw in node_inputs(other).values()
    )


def _as_link(raw: object) -> list[Any] | None:
    """Return the field itself when it is a link, otherwise ``None``. The wrapped form of a literal array does not count as a link.

    该字段是一条连线时回传它本身，否则 ``None``。字面数组的包装形态不算连线。"""
    return raw if isinstance(raw, list) and is_link(raw) else None


def _refers_to(raw: object, node_id: str) -> bool:
    """Whether the field is a link pointing to ``node_id``. / 该字段是否是一条指向 ``node_id`` 的连线。"""
    link = _as_link(raw)
    return link is not None and str(link[0]) == node_id


def _link_sources(node: Any) -> set[str]:
    """Upstream node ids of all linked inputs of a node. / 一个节点全部连线输入的上游节点 id。"""
    return {str(link[0]) for raw in node_inputs(node).values() if (link := _as_link(raw)) is not None}
