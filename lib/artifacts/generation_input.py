"""Generation input: the content inputs a generation step gathers for one target from the project's current state.

The executor, the target-state planner and the final prompt preview all take reference image assembly and gap
decisions from the same generation input (see ``GLOSSARY.md`` "generation input" and ``docs/adr/0073`` "execution
uses the same rule"):

- Entry functions always return a generation input: the input values present plus all gaps (``docs/adr/0098``).
  Gaps only block generation: the executor and preview take :attr:`ImageGenerationInput.refusal`, which is an
  :class:`InputRefused` when there are gaps, and the executor converts it to an error code on the server via the
  single translation function; the planner takes the expected basis as usual, with missing items recorded as
  absences in the basis.
- Entry functions are split by generation step: storyboard image :func:`storyboard_image_input`, asset sheet
  :func:`asset_sheet_input`, derivative asset sheet :func:`derivative_sheet_input`; valid generation inputs share
  one shape, each with typed semantics.
- The expected basis is obtained only via ``expected_basis()`` and the registered basis only via ``freeze()``; both
  go through the same private constructor :func:`_visual_basis`, so consistency is guaranteed by construction
  (``docs/adr/0062``). ``freeze()`` only accepts generation inputs without gaps; with complete inputs the expected
  basis is byte for byte the same as today.
- The reference image set is recorded completely in assembly order, not trimmed; the provider limit only affects
  what is actually sent and the "image N" numbering, not the basis.

This module reads the project's current state only through the single seam :class:`InputObservation`: it does not
import the server and does not call the currency resolver, so the planner calling it never recurses back into itself.

生成输入：一个生成环节为一个目标按项目现状备齐的内容输入。

执行器、目标态规划器与最终提示词预览从同一份生成输入取参考图装配与缺口判定（见
``GLOSSARY.md``「生成输入」与 ``docs/adr/0073``「执行期沿用同一口径」）：

- 入口函数总是返回生成输入：已有的输入值加上全部缺口（``docs/adr/0098``）。缺口只拦生成——
  执行器与预览取 :attr:`ImageGenerationInput.refusal`，有缺口时它是 :class:`InputRefused`，执行器
  在服务端经唯一的翻译函数转成错误码；规划器照常取期望依据，缺的项在依据里记为缺席。
- 入口函数按生成环节分：分镜图 :func:`storyboard_image_input`、资产图 :func:`asset_sheet_input`、
  衍生资产图 :func:`derivative_sheet_input`，成立的生成输入同形，各带类型化的 semantics。
- 期望依据只经 ``expected_basis()`` 取得，登记依据只经 ``freeze()`` 取得，两者走同一个私有
  构造器 :func:`_visual_basis`，一致由构造保证（``docs/adr/0062``）。``freeze()`` 只接受没有
  缺口的生成输入，输入完整时期望依据与今天逐字节相同。
- 参考图集按装配序完整记录、不裁剪；供应商上限只影响实发与「图N」编号，不影响依据。

本模块只经 :class:`InputObservation` 这一个 seam 读项目现状：不 import 服务端，也不调用
时效解析器——规划器调用它时不会递归回到自己。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from lib.artifacts.artifact_manifest import (
    ArtifactBasis,
    ArtifactInputClaim,
    ArtifactKey,
    ArtifactManifestAdapter,
    ArtifactManifestEntry,
    ProjectArtifactManifestAdapter,
)
from lib.artifacts.image_reference_snapshot import FrozenImageReferences, freeze_image_references
from lib.artifacts.input_gap import InputGap
from lib.artifacts.video_visual_provenance import resolve_video_aspect_ratio
from lib.artifacts.visual_artifact_provenance import (
    VisualReference,
    build_asset_sheet_visual_basis,
    build_storyboard_image_visual_basis,
    project_basis_records_asset_names,
)
from lib.project.asset_derivatives import (
    DERIVATIVE_ASSET_TYPE,
    DERIVATIVE_SOURCE_KIND,
    DERIVATIVE_SOURCE_ROLE,
    build_derivative_sheet_basis,
)
from lib.project.asset_types import ASSET_SPECS, DERIVATIVES_FIELD, AssetSpec, asset_name_comparison_key
from lib.prompts.prompt_builders import (
    build_character_derivative_prompt,
    build_character_prompt,
    build_product_prompt,
    build_prop_prompt,
    build_scene_prompt,
    render_storyboard_image_prompt,
)
from lib.prompts.reference_image_numbering import PREVIOUS_STORYBOARD_ROLE, clamp_reference_images
from lib.references.reference_admission import SHEET_MISSING_CODE, UNREGISTERED_REFERENCE_CODE
from lib.references.reference_catalog import ReferenceCatalog, build_reference_catalog
from lib.script.storyboard_sequence import find_storyboard_item, get_storyboard_items

#: An original image is declared but the file cannot be read. Separate from ``reference_asset_missing``: the fix is
#: to re-upload the original image or clear the original image field, not to generate the asset sheet.
#: 声明了原图但文件读不到。与 ``reference_asset_missing`` 分开：修法是重新上传原图或清掉原图字段，
#: 而不是去生成资产图。
ORIGINAL_MISSING_CODE = "asset_original_missing"

#: The shot's image prompt is pending generation: semantics not ready; reuses the same code as the generation entry.
#: 分镜的图片提示词待生成：语义未就绪，沿用生成入口的同一码。
PROMPT_PENDING_CODE = "script_prompt_pending"

#: The asset has no description yet: the asset sheet's semantics are not ready. / 资产还没有描述：资产图的语义未就绪。
ASSET_DESCRIPTION_REQUIRED_CODE = "asset_description_required"

#: The derivative has no appearance change description yet: the derivative asset sheet's semantics are not ready; reuses the same code as the derivative generation entry.
#: 衍生还没有外观变化描述：衍生资产图的语义未就绪，沿用衍生生成入口的同一码。
DERIVATIVE_DESCRIPTION_REQUIRED_CODE = "derivative_description_required"

#: The derivative's base asset sheet is unusable (not declared, file absent, or not registered in the manifest); reuses the same code as the derivative generation entry.
#: 衍生的本体资产图不可用（未声明、文件不在或清单未登记），沿用衍生生成入口的同一码。
DERIVATIVE_OWNER_SHEET_MISSING_CODE = "derivative_owner_sheet_missing"

#: Canvas aspect ratio for asset sheets and derivative asset sheets, shared by the executor and the planner. / 资产图与衍生资产图的画布比例，执行器与规划器共用。
ASSET_SHEET_CANVAS_RATIO = "16:9"


@dataclass(frozen=True, slots=True)
class ObservedFile:
    """A project file that is present.

    ``artifact_path`` is the canonical project-relative path (used for registration and claims); ``path`` is the
    absolute path for reading the current bytes; the two may point to different locations while a rename is pending
    commit. ``content_digest`` is provided by observers that need stability evidence; other observers leave it empty and
    it is computed when the basis is built.

    一个在场的项目文件。

    ``artifact_path`` 是规范的项目相对路径（登记与 claim 用它），``path`` 是读取当前字节的
    绝对路径；两者在待提交改名时可以指向不同位置。``content_digest`` 由需要稳定性证据的观测方
    给出，其余观测方留空、由依据构造时现算。
    """

    artifact_path: str
    path: Path
    content_digest: str | None = None


class InputObservation(Protocol):
    """The single seam through which generation inputs read the project's current state. / 生成输入读项目现状的唯一 seam。"""

    def observe(self, artifact_path: str) -> ObservedFile | None:
        """Return the observation when the file is present and its path is safe, otherwise ``None``. / 文件在场且路径安全时返回观测结果，否则返回 ``None``。"""
        ...

    def registered(self, key: ArtifactKey) -> ArtifactManifestEntry | None:
        """The registered entry for this key in the artifact manifest; ``None`` when not registered. / 该键在产物清单里的登记条目；未登记返回 ``None``。"""
        ...


class ManifestInputObservation:
    """Observation used by the executor and preview: reads registered entries via the artifact manifest adapter and observes file presence, without computing content digests.

    One instance serves one assembly.

    执行器与预览用的观测：按产物清单 adapter 读登记条目、观测文件存在，不算内容摘要。

    一个实例服务一次装配。
    """

    def __init__(self, project_dir: Path, manifest: ArtifactManifestAdapter) -> None:
        self._project_dir = project_dir
        self._manifest = manifest
        self._entries: Mapping[ArtifactKey, ArtifactManifestEntry] | None = None

    def observe(self, artifact_path: str) -> ObservedFile | None:
        observation = self._manifest.inspect_artifact(artifact_path)
        if observation.blocker is not None or not observation.present:
            return None
        return ObservedFile(
            artifact_path=observation.artifact_path,
            path=self._project_dir.joinpath(*Path(observation.artifact_path).parts),
        )

    def registered(self, key: ArtifactKey) -> ArtifactManifestEntry | None:
        # One assembly reads one manifest snapshot, so availability conclusions for every reference come from the same moment.
        # 一次装配读同一份清单快照，各条引用的可用性结论出自同一时刻。
        if self._entries is None:
            self._entries = self._manifest.snapshot_entries()
        return self._entries.get(key)


def project_input_observation(project_dir: Path) -> ManifestInputObservation:
    """Observation based on the artifact manifest in the project directory. / 基于项目目录里产物清单的观测。"""

    resolved = Path(project_dir).resolve(strict=True)
    return ManifestInputObservation(resolved, ProjectArtifactManifestAdapter(resolved))


@dataclass(frozen=True, slots=True)
class InputRefused:
    """All gaps of this target: semantic gaps first, reference gaps in assembly order, deduplicated per item.

    该目标的全部缺口：语义缺口在前，引用缺口按装配序，逐项去重。"""

    reasons: tuple[InputGap, ...]

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("a refused generation input must carry at least one gap")

    @property
    def codes(self) -> tuple[str, ...]:
        """Gap codes deduplicated by first occurrence. / 按首次出现去重的缺口码。"""

        return tuple(dict.fromkeys(gap.code for gap in self.reasons))


@dataclass(frozen=True, slots=True)
class AssembledReference:
    """One reference image in the assembly order.

    ``visual`` carries the logical identity and the current read path; ``artifact_path`` is the canonical relative
    path; ``claim`` is the candidate input claim produced when it is judged usable; original images are not registered
    artifacts and have no claim.

    装配序里的一张参考图。

    ``visual`` 携带逻辑身份与当前读取路径，``artifact_path`` 是规范相对路径；``claim`` 是
    判定可用时同时产出的候选 input claim，原图不是登记产物，没有 claim。
    """

    visual: VisualReference
    artifact_path: str
    claim: ArtifactInputClaim | None = None


@dataclass(frozen=True, slots=True)
class StoryboardImageSemantics:
    """Semantic inputs of a storyboard image. / 分镜图的语义输入。"""

    resource_id: str
    image_prompt: object
    style: str
    style_description: str
    #: Projects with schema < 19 keep the basis rule that records asset names (``project_basis_records_asset_names``).
    #: schema < 19 的项目沿用记资产名的依据口径（``project_basis_records_asset_names``）。
    asset_names: bool = False


@dataclass(frozen=True, slots=True)
class AssetSheetSemantics:
    """Semantic inputs of an asset sheet: the description is taken only from the entry stored in the project. ``name`` is in the comparison coordinate system.

    资产图的语义输入：描述只取项目里存储的条目。``name`` 在比对坐标系中。"""

    asset_type: str
    name: str
    description: str
    style: str
    style_description: str


@dataclass(frozen=True, slots=True)
class DerivativeSheetSemantics:
    """Semantic inputs of a derivative asset sheet: only the appearance change description relative to the base look; style is carried by the base asset sheet.

    衍生资产图的语义输入：只有相对本体的外观变化描述，画风由本体资产图承载。"""

    owner: str
    derivative: str
    description: str


type GenerationSemantics = StoryboardImageSemantics | AssetSheetSemantics | DerivativeSheetSemantics


@dataclass(frozen=True, slots=True)
class RenderedInput:
    """Prompt rendered after trimming to the provider limit. ``warnings`` has the same shape as the task result's ``{key, params}``.

    按供应商上限裁剪后渲染的提示词。``warnings`` 与任务结果的 ``{key, params}`` 同形。"""

    prompt: str
    sent_count: int
    warnings: tuple[dict[str, Any], ...] = ()


#: After freezing, rebind candidate claims by content digest: the inputs are the candidate claims and "canonical relative path → frozen byte digest".
#: 冻结后按内容摘要重新绑定候选 claim：入参是候选 claims 与「规范相对路径 → 冻结字节摘要」。
ClaimBinder = Callable[[Sequence[ArtifactInputClaim], Mapping[str, str]], Sequence[ArtifactInputClaim]]


class FrozenGenerationInput:
    """Frozen generation input: reference images actually sent, the rendered prompt, the registered basis and the bound claims.

    Used as a context manager; on exit it cleans up the task-private reference image snapshot.

    冻结后的生成输入：实发参考图、渲染好的提示词、登记依据与绑定后的 claims。

    作为上下文管理器使用，退出时清理任务私有的参考图快照。
    """

    __slots__ = ("basis", "claims", "prompt", "references", "warnings")

    def __init__(
        self,
        *,
        prompt: str,
        references: FrozenImageReferences,
        basis: ArtifactBasis,
        claims: tuple[ArtifactInputClaim, ...],
        warnings: tuple[dict[str, Any], ...],
    ) -> None:
        self.prompt = prompt
        #: The first N reference images sent to the provider with the request (view trimmed to the provider limit).
        #: 随请求发给供应商的前 N 张参考图（按供应商上限裁剪后的视图）。
        self.references = references
        #: Registered basis built from the complete frozen set. / 按完整冻结集构造的登记依据。
        self.basis = basis
        self.claims = claims
        self.warnings = warnings

    def __enter__(self) -> FrozenGenerationInput:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.references.cleanup()


def _refusal(gaps: tuple[InputGap, ...]) -> InputRefused | None:
    return InputRefused(reasons=gaps) if gaps else None


@dataclass(frozen=True)
class ImageGenerationInput[S: GenerationSemantics]:
    """Generation input: semantics, canvas aspect ratio, the complete reference image set in assembly order, and all gaps.

    With gaps, missing semantic values stay empty and the reference set lacks the missing images; it can only project
    the expected basis, not render or freeze.

    生成输入：语义、画布比例、按装配序排列的完整参考图集，以及全部缺口。

    有缺口时语义里缺的值留空、参考图集不含缺的那几张；它只能投影期望依据，不能渲染与冻结。
    """

    semantics: S
    canvas_ratio: str
    #: Complete assembly order, not trimmed. / 完整装配序，不裁剪。
    references: tuple[AssembledReference, ...]
    #: Semantic gaps first, reference gaps in assembly order, deduplicated per item. / 语义缺口在前，引用缺口按装配序，逐项去重。
    gaps: tuple[InputGap, ...] = ()

    @property
    def refusal(self) -> InputRefused | None:
        """All gaps that refuse generation; ``None`` when the input is valid. / 生成被拒的全部缺口；输入成立时为 ``None``。"""

        return _refusal(self.gaps)

    def expected_basis(self) -> ArtifactBasis:
        """Expected basis projected from the project's current state, for the target-state planner's currency checks; missing items are recorded as absences.

        按项目现状投影的期望依据，供目标态规划器判定时效；缺的项记为缺席。"""

        return _visual_basis(
            self.semantics,
            self.canvas_ratio,
            [reference.visual for reference in self.references],
            absent=self.gaps,
        )

    def render(self, *, max_reference_images: int, model: str) -> RenderedInput:
        """Render the prompt trimmed to the provider limit without freezing reference images; for preview, numbering consistent with :meth:`freeze`.

        按供应商上限裁剪后渲染提示词，不冻结参考图；预览用，编号与 :meth:`freeze` 一致。"""

        self._require_complete()
        visuals = [reference.visual for reference in self.references]
        return _render_clamped(self.semantics, visuals, max_reference_images=max_reference_images, model=model)

    def freeze(self, *, max_reference_images: int, model: str, bind_claims: ClaimBinder) -> FrozenGenerationInput:
        """Freeze the complete reference image set into a task-private snapshot, then bind claims, trim, render and build the registered basis.

        Basis and claims use the complete frozen set; the provider only gets the first N; when trimming happens the result
        carries a warning.

        把完整参考图集冻结到任务私有快照，再绑定 claim、裁剪、渲染并构造登记依据。

        依据与 claim 按完整冻结集，供应商只收前 N 张；裁剪发生时结果带一条 warning。
        """

        self._require_complete()
        return _freeze(
            self.semantics,
            self.canvas_ratio,
            self.references,
            max_reference_images=max_reference_images,
            model=model,
            bind_claims=bind_claims,
        )

    def _require_complete(self) -> None:
        if self.gaps:
            raise ValueError("generation input has gaps; it can only project an expected basis")


class StoryboardImageInput(ImageGenerationInput[StoryboardImageSemantics]):
    """Valid generation input for one regular storyboard image. / 一张普通分镜图成立的生成输入。"""


class AssetSheetInput(ImageGenerationInput[AssetSheetSemantics]):
    """Valid generation input for one asset sheet (character, scene, prop, product). / 一张资产图（角色、场景、道具、商品）成立的生成输入。"""


class DerivativeSheetInput(ImageGenerationInput[DerivativeSheetSemantics]):
    """Valid generation input for one derivative asset sheet: the only reference image is the base asset sheet.

    一张衍生资产图成立的生成输入：唯一参考图是本体资产图。"""


def storyboard_image_input(
    project: Mapping[str, Any],
    script: dict[str, Any],
    *,
    episode: int,
    resource_id: str,
    observation: InputObservation,
) -> StoryboardImageInput:
    """Gather the generation input for one storyboard image from the project's current state.

    Assembly order: products (each asset sheet first, then original images; products without an asset sheet use only
    original images) → character, scene, prop asset sheets (in field order) → previous storyboard image (when the index
    is greater than 0 and it is not a ``segment_break``); the whole list is deduplicated by path, keeping the first
    occurrence.

    Gaps: reference not registered; character, scene or prop without an asset sheet or with an unusable one; product
    declares an asset sheet that is unusable; original image declared but unreadable; prompt pending generation.
    Omitted: unusable previous storyboard image. A product with neither asset sheet nor original image uses text only
    and is not a gap.

    Raises ``ValueError`` when the entry does not exist or its field structure is broken.

    按项目现状为一张分镜图备齐生成输入。

    装配序：商品（每件先资产图、再原图；没有资产图的商品只用原图）→ 角色、场景、道具资产图
    （按字段序）→ 上一分镜图（序号大于 0 且不是 ``segment_break`` 时），整条按路径去重、保留
    首次出现。

    缺口：引用未登记；角色、场景、道具没有资产图或资产图不可用；商品声明了资产图但不可用；
    声明了原图但读不到；提示词待生成。略去：上一分镜图不可用。商品既没有资产图也没有原图时
    只用文字，不算缺口。

    条目不存在或字段结构损坏抛 ``ValueError``。
    """

    items, id_field, char_field, scene_field, prop_field = get_storyboard_items(script)
    resolved = find_storyboard_item(items, id_field, resource_id)
    if resolved is None:
        raise ValueError(f"scene/segment not found: {resource_id}")
    item, index = resolved

    style = project.get("style", "")
    style_description = project.get("style_description", "")
    if not isinstance(style, str) or not isinstance(style_description, str):
        raise ValueError("storyboard style and style description must be strings")
    canvas_ratio = resolve_video_aspect_ratio(project, "storyboards")

    catalog = build_reference_catalog(project)
    assembly = _Assembly(observation)
    image_prompt = item.get("image_prompt")
    if image_prompt is None:
        assembly.gap(InputGap(PROMPT_PENDING_CODE, name=resource_id))

    for name in _reference_names(item, "products_in_shot"):
        assembly.add_product(catalog, name)
    for asset_type, field in (("character", char_field), ("scene", scene_field), ("prop", prop_field)):
        for name in _reference_names(item, field):
            assembly.add_sheet(catalog, asset_type, name)

    if index > 0 and not item.get("segment_break"):
        assembly.add_previous_storyboard(items[index - 1], id_field=id_field, episode=episode)

    return StoryboardImageInput(
        semantics=StoryboardImageSemantics(
            resource_id=resource_id,
            image_prompt=image_prompt,
            style=style,
            style_description=style_description,
            asset_names=project_basis_records_asset_names(project),
        ),
        canvas_ratio=canvas_ratio,
        references=tuple(assembly.references),
        gaps=tuple(assembly.gaps),
    )


@dataclass(frozen=True, slots=True)
class GridReferences:
    """Reference image set and gaps of one grid. The grid's composite basis is built by the grid executor from the frozen reference images.

    一张宫格的参考图集与缺口。宫格的合成依据由宫格执行器按冻结后的参考图构造。"""

    #: Complete assembly order, not trimmed. / 完整装配序，不裁剪。
    references: tuple[AssembledReference, ...]
    #: Reference gaps in assembly order, deduplicated per item. / 引用缺口按装配序，逐项去重。
    gaps: tuple[InputGap, ...] = ()

    @property
    def refusal(self) -> InputRefused | None:
        """All gaps that refuse generation; ``None`` when the reference set is valid. / 生成被拒的全部缺口；参考图集成立时为 ``None``。"""

        return _refusal(self.gaps)


def grid_references(
    project: Mapping[str, Any],
    script: dict[str, Any],
    *,
    member_ids: Sequence[str],
    observation: InputObservation,
) -> GridReferences:
    """Gather the reference image set for one grid from the project's current state.

    Assembly order: in ``member_ids`` order, take each member shot's character, scene and prop asset sheets (in field
    order), union them and deduplicate by path, keeping the first occurrence; no products and no previous storyboard image.

    Gaps: reference not registered; character, scene or prop without an asset sheet or with an unusable one.

    Raises ``ValueError`` when a member does not exist or its field structure is broken.

    按项目现状为一张宫格备齐参考图集。

    装配序：按 ``member_ids`` 的顺序，逐个成员分镜取角色、场景、道具资产图（按字段序），
    取并集并按路径去重、保留首次出现；不含商品与上一分镜图。

    缺口：引用未登记；角色、场景、道具没有资产图或资产图不可用。

    成员不存在或字段结构损坏抛 ``ValueError``。
    """

    items, id_field, char_field, scene_field, prop_field = get_storyboard_items(script)
    catalog = build_reference_catalog(project)
    assembly = _Assembly(observation)
    for member_id in member_ids:
        resolved = find_storyboard_item(items, id_field, member_id)
        if resolved is None:
            raise ValueError(f"scene/segment not found: {member_id}")
        item, _index = resolved
        for asset_type, field in (("character", char_field), ("scene", scene_field), ("prop", prop_field)):
            for name in _reference_names(item, field):
                assembly.add_sheet(catalog, asset_type, name)

    return GridReferences(references=tuple(assembly.references), gaps=tuple(assembly.gaps))


def asset_sheet_input(
    project: Mapping[str, Any],
    *,
    asset_type: str,
    name: str,
    observation: InputObservation,
) -> AssetSheetInput:
    """Gather the generation input for one asset sheet from the project's current state.

    The description is taken only from the entry stored in the project. Reference images are the original images
    uploaded by the author: characters 0 to 1, props 0 to 1 (reference_image), products all, scenes none; original
    images are not registered artifacts and have no claim.

    Gaps: empty description; original image declared but unreadable.

    Raises ``ValueError`` when the asset does not exist or its field structure is broken.

    按项目现状为一张资产图备齐生成输入。

    描述只取项目里存储的条目。参考图是作者上传的原图：角色 0 到 1 张，道具 0 到 1 张（reference_image），商品全部，
    场景没有；原图不是登记产物，没有 claim。

    缺口：描述为空；声明了原图但读不到。

    资产不存在或字段结构损坏抛 ``ValueError``。
    """

    spec = ASSET_SPECS[asset_type]
    asset_name, asset = _asset_entry(project.get(spec.bucket_key), name, label=asset_type)
    style = project.get("style", "")
    style_description = project.get("style_description", "")
    if not isinstance(style, str) or not isinstance(style_description, str):
        raise ValueError("asset sheet style and style description must be strings")

    assembly = _Assembly(observation)
    description = _stored_description(asset)
    if not description:
        assembly.gap(InputGap(ASSET_DESCRIPTION_REQUIRED_CODE, asset_type, asset_name))
    for original in _asset_originals(asset, spec):
        assembly.add_original(asset_type, asset_name, original)

    return AssetSheetInput(
        semantics=AssetSheetSemantics(
            asset_type=asset_type,
            name=asset_name,
            description=description,
            style=style,
            style_description=style_description,
        ),
        canvas_ratio=ASSET_SHEET_CANVAS_RATIO,
        references=tuple(assembly.references),
        gaps=tuple(assembly.gaps),
    )


def derivative_sheet_input(
    project: Mapping[str, Any],
    *,
    owner: str,
    derivative: str,
    observation: InputObservation,
) -> DerivativeSheetInput:
    """Gather the generation input for one derivative asset sheet from the project's current state.

    The base asset sheet is the only reference image (see ``docs/adr/0072``); its usability is judged the same way as
    asset sheets referenced by storyboard images.

    Gaps: derivative lacks an appearance change description; base asset sheet not declared or unusable.

    Raises ``ValueError`` when the base look or derivative does not exist or the field structure is broken.

    按项目现状为一张衍生资产图备齐生成输入。

    本体资产图是唯一参考图（见 ``docs/adr/0072``），可用性与分镜图引用的资产图同一判定。

    缺口：衍生缺外观变化描述；本体资产图未声明或不可用。

    本体或衍生不存在、字段结构损坏抛 ``ValueError``。
    """

    spec = ASSET_SPECS[DERIVATIVE_ASSET_TYPE]
    owner_name, owner_asset = _asset_entry(project.get(spec.bucket_key), owner, label=DERIVATIVE_ASSET_TYPE)
    derivative_name, derivative_asset = _asset_entry(
        owner_asset.get(DERIVATIVES_FIELD), derivative, label=f"derivative of {owner_name}"
    )

    assembly = _Assembly(observation)
    description = _stored_description(derivative_asset)
    if not description:
        assembly.gap(InputGap(DERIVATIVE_DESCRIPTION_REQUIRED_CODE, DERIVATIVE_ASSET_TYPE, derivative_name))
    assembly.add_owner_sheet(spec, owner_name, owner_asset)

    return DerivativeSheetInput(
        semantics=DerivativeSheetSemantics(owner=owner_name, derivative=derivative_name, description=description),
        canvas_ratio=ASSET_SHEET_CANVAS_RATIO,
        references=tuple(assembly.references),
        gaps=tuple(assembly.gaps),
    )


def _asset_entry(bucket: object, name: str, *, label: str) -> tuple[str, Mapping[str, Any]]:
    """Look up an entry in the asset table by asset identity (stripped of surrounding whitespace, NFC), returning the identity name and the entry.

    Same coordinates as the identity the target-state planner registers asset sheets under: existing keys with
    surrounding whitespace are also addressed by the stripped name. Among several keys of the same identity, the one
    written later wins. Raises ``ValueError`` when it does not exist or the entry is not an object.

    按资产身份（去两端空白、NFC）在资产表里取条目，返回身份名与条目。

    与目标态规划器登记资产图的身份同一坐标：存量里两端带空白的键也按去空白后的名字寻址。
    同一身份的多个键后写入的胜出。不存在或条目不是对象抛 ``ValueError``。
    """

    identity = asset_name_comparison_key(name)
    found: object = None
    if isinstance(bucket, Mapping):
        for key, entry in bucket.items():
            if isinstance(key, str) and asset_name_comparison_key(key) == identity:
                found = entry
    if not identity or not isinstance(found, Mapping):
        raise ValueError(f"{label} not found: {name}")
    return identity, found


def _stored_description(asset: Mapping[str, Any]) -> str:
    """Description stored in the entry, stripped of surrounding whitespace; empty string when not filled in, ``ValueError`` when the type is wrong.

    条目里存储的描述，去两端空白；未填写返回空串，类型不对抛 ``ValueError``。"""

    description = asset.get("description")
    if description is None:
        return ""
    if not isinstance(description, str):
        raise ValueError("asset description must be a string")
    return description.strip()


def _asset_originals(asset: Mapping[str, Any], spec: AssetSpec) -> list[str]:
    """Original image paths registered on the asset, in field declaration order; list fields are read as lists per the schema, others as single values.

    资产登记的原图路径，按字段声明序；列表字段按 schema 取列表，其余取单值。"""

    paths: list[str] = []
    for field in spec.original_image_fields:
        if field in spec.extra_list_fields:
            paths.extend(_declared_originals(asset, field))
        elif (path := _declared_path(asset, field)) is not None:
            paths.append(path)
    return paths


def _reference_names(item: Mapping[str, Any], field: str | None) -> list[str]:
    if field is None:
        return []
    values = item.get(field)
    if values is None:
        return []
    if not isinstance(values, list | tuple) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"storyboard reference field {field} must be a list of names")
    return [value for value in values if value]


class _Assembly:
    """Accumulator for one assembly: reference images deduplicated by path, gaps by content, both order-preserving.

    一次装配的累加器：参考图按路径去重，缺口按内容去重，两者都保序。"""

    def __init__(self, observation: InputObservation) -> None:
        self._observation = observation
        self.references: list[AssembledReference] = []
        self.gaps: list[InputGap] = []
        self._seen_paths: set[str] = set()

    def gap(self, gap: InputGap) -> None:
        if gap not in self.gaps:
            self.gaps.append(gap)

    def _append(self, reference: AssembledReference) -> None:
        if reference.artifact_path in self._seen_paths:
            return
        self._seen_paths.add(reference.artifact_path)
        self.references.append(reference)

    def _available(self, key: ArtifactKey, declared_path: str) -> tuple[ObservedFile, ArtifactInputClaim] | None:
        """The single definition of "usable": registered at this path in the manifest and the file present; when true, also produces a candidate claim.

        「可用」的唯一判定：清单登记在该路径且文件在场；成立时同时产出候选 claim。"""

        entry = self._observation.registered(key)
        if entry is None:
            return None
        observed = self._observation.observe(declared_path)
        if observed is None or observed.artifact_path != entry.artifact_path:
            return None
        return observed, ArtifactInputClaim(key=key, artifact_path=observed.artifact_path)

    def add_sheet(self, catalog: ReferenceCatalog, asset_type: str, name: str) -> None:
        entry = catalog.lookup(asset_type, name)
        if entry is None:
            self.gap(InputGap(UNREGISTERED_REFERENCE_CODE, asset_type, name))
            return
        name = entry.name
        sheet = _declared_path(entry.asset, entry.spec.sheet_field)
        available = (
            self._available(ArtifactKey.asset_sheet(asset_type, entry.name), sheet) if sheet is not None else None
        )
        if available is None:
            self.gap(InputGap(SHEET_MISSING_CODE, asset_type, name))
            return
        observed, claim = available
        self._append(
            AssembledReference(
                visual=_visual(observed, role="asset_sheet", logical_type=asset_type, logical_id=name, kind="sheet"),
                artifact_path=observed.artifact_path,
                claim=claim,
            )
        )

    def add_product(self, catalog: ReferenceCatalog, name: str) -> None:
        entry = catalog.lookup("product", name)
        if entry is None:
            self.gap(InputGap(UNREGISTERED_REFERENCE_CODE, "product", name))
            return
        name = entry.name
        sheet = _declared_path(entry.asset, entry.spec.sheet_field)
        if sheet is not None:
            available = self._available(ArtifactKey.asset_sheet("product", entry.name), sheet)
            if available is None:
                self.gap(InputGap(SHEET_MISSING_CODE, "product", name))
            else:
                observed, claim = available
                self._append(
                    AssembledReference(
                        visual=_visual(
                            observed, role="asset_sheet", logical_type="product", logical_id=name, kind="sheet"
                        ),
                        artifact_path=observed.artifact_path,
                        claim=claim,
                    )
                )
        for original in _declared_originals(entry.asset, "reference_images"):
            self.add_original("product", name, original)

    def add_original(self, asset_type: str, name: str, original: str) -> None:
        """One original image of an asset sheet: declared but unreadable is a gap. / 资产图的一张原图：声明了却读不到即缺口。"""

        observed = self._observation.observe(original)
        if observed is None:
            self.gap(InputGap(ORIGINAL_MISSING_CODE, asset_type, name))
            return
        self._append(
            AssembledReference(
                visual=_visual(observed, role="source", logical_type=asset_type, logical_id=name, kind="original"),
                artifact_path=observed.artifact_path,
            )
        )

    def add_owner_sheet(self, spec: AssetSpec, owner: str, asset: Mapping[str, Any]) -> None:
        """Source of a derivative asset sheet: base asset sheet not declared or unusable is a gap. / 衍生资产图的来源：本体资产图未声明或不可用即缺口。"""

        sheet = _declared_path(asset, spec.sheet_field)
        available = self._available(ArtifactKey.asset_sheet(spec.asset_type, owner), sheet) if sheet else None
        if available is None:
            self.gap(InputGap(DERIVATIVE_OWNER_SHEET_MISSING_CODE, spec.asset_type, owner))
            return
        observed, claim = available
        self._append(
            AssembledReference(
                visual=_visual(
                    observed,
                    role=DERIVATIVE_SOURCE_ROLE,
                    logical_type=spec.asset_type,
                    logical_id=owner,
                    kind=DERIVATIVE_SOURCE_KIND,
                ),
                artifact_path=observed.artifact_path,
                claim=claim,
            )
        )

    def add_previous_storyboard(self, previous_item: object, *, id_field: str, episode: int) -> None:
        """The previous storyboard image is an optional input derived by the system: when unusable it is just omitted, no gap.

        上一分镜图是系统推导的可选输入：不可用只是略去，不产生缺口。"""

        if not isinstance(previous_item, Mapping):
            return
        previous_id = str(previous_item.get(id_field) or "").strip()
        assets = previous_item.get("generated_assets")
        if not previous_id or not isinstance(assets, Mapping):
            return
        declared = _declared_path(assets, "storyboard_image")
        if declared is None:
            return
        available = self._available(ArtifactKey.episode_storyboard(episode, previous_id), declared)
        if available is None:
            return
        observed, claim = available
        self._append(
            AssembledReference(
                visual=_visual(
                    observed, role=PREVIOUS_STORYBOARD_ROLE, logical_type="storyboard", logical_id=previous_id
                ),
                artifact_path=observed.artifact_path,
                claim=claim,
            )
        )


def _declared_path(container: object, field: str) -> str | None:
    """Path declared in the entry; ``None`` when not declared (missing field, ``None``, empty string), ``ValueError`` when the type is wrong.

    条目里声明的路径；未声明（缺字段、``None``、空串）返回 ``None``，类型不对抛 ``ValueError``。"""

    if not isinstance(container, Mapping):
        return None
    value = container.get(field)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a project-relative path")
    return value


def _declared_originals(container: object, field: str) -> list[str]:
    if not isinstance(container, Mapping):
        return []
    values = container.get(field)
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{field} must be a list of project-relative paths")
    return [value for value in values if value]


def _visual(
    observed: ObservedFile,
    *,
    role: str,
    logical_type: str,
    logical_id: str,
    kind: str | None = None,
) -> VisualReference:
    return VisualReference(
        path=observed.path,
        role=role,
        logical_type=logical_type,
        logical_id=logical_id,
        kind=kind,
        content_digest=observed.content_digest,
    )


def _visual_basis(
    semantics: GenerationSemantics,
    canvas_ratio: str,
    references: Sequence[VisualReference],
    *,
    absent: Sequence[InputGap] = (),
) -> ArtifactBasis:
    """Constructor shared by the expected basis and the registered basis, dispatching by semantic type to the matching visual basis constructor.

    ``absent`` comes only from the expected basis: the registered basis comes from freezing, and freezing only accepts
    generation inputs without gaps.

    期望依据与登记依据共用的构造器，按语义类型分派到对应的视觉依据构造器。

    ``absent`` 只来自期望依据：登记依据出自冻结，冻结只接受没有缺口的生成输入。
    """

    match semantics:
        case StoryboardImageSemantics():
            return build_storyboard_image_visual_basis(
                resource_id=semantics.resource_id,
                image_prompt=semantics.image_prompt,
                style=semantics.style,
                style_description=semantics.style_description,
                aspect_ratio=canvas_ratio,
                references=references,
                absent=absent,
                asset_names=semantics.asset_names,
            )
        case AssetSheetSemantics():
            return build_asset_sheet_visual_basis(
                asset_type=semantics.asset_type,
                asset_id=semantics.name,
                description=semantics.description,
                style=semantics.style,
                style_description=semantics.style_description,
                aspect_ratio=canvas_ratio,
                references=references,
                absent=absent,
            )
        case DerivativeSheetSemantics():
            # When the base asset sheet is unusable there is no source; the gap is recorded in absent. / 本体资产图不可用时没有来源，缺口记在 absent 里。
            [*sources] = references
            if len(sources) > 1:
                raise ValueError("a derivative sheet has at most one source image")
            return build_derivative_sheet_basis(
                owner_name=semantics.owner,
                derivative_name=semantics.derivative,
                description=semantics.description,
                aspect_ratio=canvas_ratio,
                source=sources[0] if sources else None,
                absent=absent,
            )


#: Render outlets of asset sheet prompts by asset type. / 资产图提示词按资产类型的渲染出口。
_ASSET_SHEET_PROMPT_BUILDERS: dict[str, Callable[[str, str, str, str], str]] = {
    "character": build_character_prompt,
    "scene": build_scene_prompt,
    "prop": build_prop_prompt,
    "product": build_product_prompt,
}


def _render_prompt(semantics: GenerationSemantics, sent: Sequence[VisualReference]) -> str:
    match semantics:
        case StoryboardImageSemantics():
            return render_storyboard_image_prompt(
                semantics.image_prompt,
                style=semantics.style,
                style_description=semantics.style_description,
                references=sent,
            )
        case AssetSheetSemantics():
            builder = _ASSET_SHEET_PROMPT_BUILDERS[semantics.asset_type]
            return builder(semantics.name, semantics.description, semantics.style, semantics.style_description)
        case DerivativeSheetSemantics():
            return build_character_derivative_prompt(semantics.description)


def _render_clamped(
    semantics: GenerationSemantics,
    visuals: Sequence[VisualReference],
    *,
    max_reference_images: int,
    model: str,
) -> RenderedInput:
    clamp = clamp_reference_images(visuals, max_reference_images, model=model)
    warning = clamp.warning()
    return RenderedInput(
        prompt=_render_prompt(semantics, visuals[: clamp.kept]),
        sent_count=clamp.kept,
        warnings=(warning,) if warning is not None else (),
    )


def _freeze(
    semantics: GenerationSemantics,
    canvas_ratio: str,
    references: Sequence[AssembledReference],
    *,
    max_reference_images: int,
    model: str,
    bind_claims: ClaimBinder,
) -> FrozenGenerationInput:
    frozen = freeze_image_references(
        [{"image": reference.visual.path} for reference in references] or None,
        [reference.visual for reference in references],
    )
    try:
        content_digests: dict[str, str] = {}
        for reference, frozen_visual in zip(references, frozen.visual_references, strict=True):
            if frozen_visual.content_digest is None:
                raise ValueError("frozen visual reference has no content digest")
            content_digests[reference.artifact_path] = frozen_visual.content_digest
        claims = tuple(
            bind_claims([reference.claim for reference in references if reference.claim is not None], content_digests)
        )
        basis = _visual_basis(semantics, canvas_ratio, frozen.visual_references)
        rendered = _render_clamped(
            semantics,
            frozen.visual_references,
            max_reference_images=max_reference_images,
            model=model,
        )
    except BaseException:
        frozen.cleanup()
        raise
    return FrozenGenerationInput(
        prompt=rendered.prompt,
        references=frozen.sent(rendered.sent_count),
        basis=basis,
        claims=claims,
        warnings=rendered.warnings,
    )


__all__ = [
    "ASSET_DESCRIPTION_REQUIRED_CODE",
    "ASSET_SHEET_CANVAS_RATIO",
    "DERIVATIVE_DESCRIPTION_REQUIRED_CODE",
    "DERIVATIVE_OWNER_SHEET_MISSING_CODE",
    "ORIGINAL_MISSING_CODE",
    "PROMPT_PENDING_CODE",
    "AssembledReference",
    "AssetSheetInput",
    "AssetSheetSemantics",
    "ClaimBinder",
    "DerivativeSheetInput",
    "DerivativeSheetSemantics",
    "FrozenGenerationInput",
    "GenerationSemantics",
    "GridReferences",
    "ImageGenerationInput",
    "InputGap",
    "InputObservation",
    "InputRefused",
    "ManifestInputObservation",
    "ObservedFile",
    "RenderedInput",
    "StoryboardImageInput",
    "StoryboardImageSemantics",
    "asset_sheet_input",
    "derivative_sheet_input",
    "grid_references",
    "project_input_observation",
    "storyboard_image_input",
]
