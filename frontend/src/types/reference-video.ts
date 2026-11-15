/**
 * Reference-to-video unit types — mirrors lib/script/script_models.py Pydantic models.
 *
 * One "unit" produces one rendered video clip. Its body (`text`) is the single
 * source of truth: reference images are resolved from the `@[名称]` mentions at
 * execution time and never persisted or transported.
 */

import type { CostBreakdown } from "./cost";
import type { DurationExclusionReason, VideoCapabilityProblem } from "./project";
import type { PlanNewAsset, RenderedPromptPreview } from "./script";
import type {
  AdmissionProblem,
  WorkflowAdmission,
} from "./workflow";

export type AssetKind = "product" | "character" | "scene" | "prop";

/** Project.json sheet field for each asset kind. Mirrors lib/project/asset_types.py SHEET_KEY. */
export const SHEET_FIELD: Record<AssetKind, "product_sheet" | "character_sheet" | "scene_sheet" | "prop_sheet"> = {
  product: "product_sheet",
  character: "character_sheet",
  scene: "scene_sheet",
  prop: "prop_sheet",
};

/**
 * Raw persisted status value returned by the backend in `generated_assets.status`.
 * Mirrors lib/script/script_models.py:GeneratedAssets.status Pydantic Literal exactly.
 * Note: "storyboard_ready" never appears for reference_video units — it's a legacy
 * storyboard-mode value retained in the shared GeneratedAssets model.
 */
export type UnitPersistedStatus = "pending" | "storyboard_ready" | "completed";

/**
 * UI-derived status shown in the UnitList status dot and preview panel.
 * Composed from (persisted status + task-queue state + error signals) by UI code.
 * Not sent to or received from the backend.
 */
export type UnitStatus = "pending" | "running" | "ready" | "failed";

export interface UnitGeneratedAssets {
  storyboard_image: string | null;
  storyboard_last_image: string | null;
  grid_id: string | null;
  grid_cell_index: number | null;
  video_clip: string | null;
  video_uri: string | null;
  video_thumbnail?: string | null;
  narration_audio?: string | null;
  /** Raw backend status — use `UnitStatus` for UI display. */
  status: UnitPersistedStatus;
  /** ISO8601 completion time; null is treated as "before any voice setting". */
  video_generated_at: string | null;
  /** Legacy migration history only; runtime never reads or creates it. */
  source_signature?: string | null;
}

export interface ReferenceVideoUnit {
  /** Format: "E{episode}U{index}" */
  unit_id: string;
  /** Unit body — free-form text carrying `@[名称]` mentions; the only persisted content truth. */
  text: string;
  /** Planning duration in seconds — provider request duration is resolved during precheck. */
  duration_seconds: number;
  note: string | null;
  /** Units that have never produced any artifact lack this section -- the backend writes it only on generation, so the read side always treats it as possibly absent. / 尚未生成过任何产物的单元不带这一节——后端只在生成时写入，故读侧一律按可能缺席处理。 */
  generated_assets?: UnitGeneratedAssets;
  /** Problem shell or mixed-speech marker; generation is blocked until repaired. */
  needs_replan?: boolean;
  /** Pending authoring: the unit's body has not been written by prompt authoring yet. Read-only. */
  pending_authoring?: boolean;
  /** Source text carried over from the script plan on content confirmation. Read-only; empty or absent for manually added units. */
  source_text?: string;
}

/** Task type bucket: i2v with no usable reference images, r2v otherwise. / 任务类型桶：无可用参考图落 i2v，有则落 r2v。 */
export type ReferenceVideoBucket = "i2v" | "r2v";

export interface ReferenceDeclaredResource {
  type: string;
  name: string;
}

/**
 * The server's bucket verdict for one unit -- mirrors the envelope of lib/script/reference_video/unit_capabilities.py.
 *
 * The bucket is decided by **usable reference images** (after hydration), the same criterion as the execution side; the frontend does not judge by "name registered" itself.
 * `problem` is a video request fact failure of the chosen bucket; `problems` are blocking issues where declared references and usable reference images diverge
 * (unregistered / missing image / bucket changed), and `unavailable_references` names the references missing images.
 *
 * 服务端对一个单元的定桶结论——镜像 lib/script/reference_video/unit_capabilities.py 的信封。
 *
 * 桶按**可用参考图**（水合后）判定，与执行侧同一判据；前端不按「名字已登记」自判。
 * `problem` 是所落桶的视频请求事实失败；`problems` 是声明引用与可用参考图分裂的阻断问题
 * （未登记 / 缺图 / 桶改变），`unavailable_references` 点名缺图的引用。
 */
export interface ReferenceUnitCapability {
  unit_id: string;
  declared_capability: ReferenceVideoBucket;
  hydrated_capability: ReferenceVideoBucket;
  declared_references: ReferenceDeclaredResource[];
  unavailable_references: ReferenceDeclaredResource[];
  unregistered_references: string[];
  /** Options of the chosen bucket after narrowing; null on fact failure. A legal empty set when the endpoint fixes it. / 所落桶收窄后的档位；事实失败时为 null。端点固定时为合法空集。 */
  allowed_durations: number[] | null;
  excluded_durations: Record<string, DurationExclusionReason> | null;
  duration_endpoint_fixed: boolean;
  duration_endpoint_fixed_reason: string | null;
  problem: VideoCapabilityProblem | null;
  problems: ReferenceProjectionProblem[];
}

/** Per-unit verdicts indexed by `unit_id`. / 按 `unit_id` 索引的逐单元结论。 */
export type ReferenceUnitCapabilityMap = Record<string, ReferenceUnitCapability>;

export interface ReferenceGenerationRequestOptions {
  /** Exact video tier accepted for this request; omitted when no cross-tier confirmation is needed. */
  confirmed_request_duration_seconds?: number | null;
}

export interface ReferenceProjectionLocation {
  path: (string | number)[];
  line: number | null;
}

export interface ReferenceProjectionProblem {
  code: string;
  blocking: boolean;
  unit_id: string;
  locations: ReferenceProjectionLocation[];
  params: Record<string, unknown>;
  reason?: string;
  action: string;
  message?: string;
}

export interface ReferenceProjectionAdmission {
  allowed: false;
  kind: "reference_request_projection";
  unit_id: string;
  problems: ReferenceProjectionProblem[];
}

/**
 * Duration tier pre-check result. `adjustment` describes the direction of the requested seconds relative to the tier input:
 * `exact` equal, `up` the clip is longer, `down` the clip is shorter. When capability metadata cannot be parsed the pre-check fails outright.
 *
 * 时长取档预检结果。`adjustment` 说明申请秒数相对取档输入的偏移方向：
 * `exact` 一致、`up` 成片更长、`down` 成片更短。能力元数据不可解析时预检直接失败。
 */
export interface ReferenceDurationPrecheck {
  /** True when the requested tier differs from the current visual tier (the script tier when there is no clip) / 请求档位与当前视觉档位（无成片时为剧本档位）不一致时为 true */
  needs_confirmation: boolean;
  /** Planned duration in the script (seconds) / 剧本编排时长（秒） */
  script_duration: number;
  /** Tier input, i.e. the planned duration in the script / 取档输入，即剧本编排时长 */
  duration_input: number;
  /** Tier seconds to be requested from the model / 将向模型申请的档位秒数 */
  request_duration: number;
  adjustment: "exact" | "up" | "down";
  declared_capability: "i2v" | "r2v";
  hydrated_capability: "i2v" | "r2v";
  provider_id: string | null;
  model_id: string | null;
  problems: ReferenceProjectionProblem[];
}

/**
 * A target that failed to enqueue. Tasks already created are not revoked and run as usual; the units listed here
 * got no task and no charge this time, and the next "generate missing" will pick them up exactly.
 *
 * 一个没能入队的目标。已创建的任务不因此被撤销，它们照常执行；这里列出的 unit
 * 本次没有任务、也没有计费，下次「缺失即生成」会正好补上它们。
 */
export interface ReferenceBatchEnqueueFailure {
  unit_id: string;
  problem: AdmissionProblem;
}

export interface ReferenceBatchAdmission extends WorkflowAdmission {
  skipped_unit_ids: string[];
  /** Non-empty only when admitted / 仅 admitted 时非空 */
  task_ids: string[];
  /** Per-unit task rows, so each caller can settle its own optimistic occupancy marker. / 逐 unit 的任务行，供调用方各自兑现自己的乐观占用标记。 */
  task_ids_by_unit: Record<string, string>;
  /** Units not reached when enqueueing was interrupted; empty array when the whole batch enqueued successfully. / 入队中断时没轮到的 unit；整批入队成功时为空数组。 */
  enqueue_failures: ReferenceBatchEnqueueFailure[];
  deduped: boolean;
}

/** Batch preview: the same admission verdict as submit, plus the estimated cost of the priced part; creates no task at all. / 批量预览：与提交同一份准入结论，外加已定价部分的预估费用；一个任务也不建。 */
export interface ReferenceBatchPreview extends WorkflowAdmission {
  /** Units that already have a clip and are skipped this time. / 已有成片、本次跳过的 unit。 */
  skipped_unit_ids: string[];
  /** Totals only quoted units, separated by currency; `null` when none are quoted. / 只合计已报价的单元，按币种分开；一个都没报价时为 `null`。 */
  estimated_cost: CostBreakdown | null;
  /** Number of units without a quote, not included in `estimated_cost`. / 没有报价、未计入 `estimated_cost` 的单元数。 */
  unpriced_count: number;
}

/** Batch endpoint request body: omitting unit_ids means "generate missing"; an empty array is rejected by the backend. / 批量端点请求体：省略 unit_ids 表示「缺失即生成」，空数组会被后端拒绝。 */
export interface ReferenceBatchGenerateRequest {
  unit_ids?: string[];
  /** Requested tiers the user confirmed, per unit / 用户已确认的申请档位，按 unit 给 */
  confirmed_request_durations?: Record<string, number>;
}

/**
 * Read-time derivation of a video unit body -- the content source of the editor's parse preview panel.
 *
 * The body is the sole truth: utterances and reference images are mechanical derivations, not persisted.
 * `warnings` are already rendered as text in the request language (`key` is kept for tests and telemetry lookup).
 *
 * 视频单元正文的读时派生结果——编辑器解析预览面板的内容源。
 *
 * 正文是唯一真相：utterances 与参考图都是机械派生物，不落盘。
 * `warnings` 已按请求语言渲染成文本（`key` 保留供测试与埋点定位）。
 */
/** `index` is the 1-based utterance number, in order of appearance in the body. / `index` 是 1-based 的 utterance 序号，按正文出现顺序编号。 */
export type ScriptPreviewUtterance =
  | { index: number; kind: "dialogue"; speaker: string; text: string }
  | { index: number; kind: "voiceover"; speaker: null; text: string };

export interface ScriptPreviewWarning {
  key: string;
  message: string;
}

export interface ScriptPreview {
  utterances: ScriptPreviewUtterance[];
  warnings: ScriptPreviewWarning[];
}

/**
 * Structured intermediate state of the reference_video script_plan (the reviewable / editable object of content review). Maps backend
 * ReferenceScriptPlanUnit / ReferenceScriptPlanDraft in lib/script/script_models.py: script_plan fixes the content layer
 * (unit boundaries + unit durations + unit body), and prompt_authoring visual arrangement is triggered only after user confirmation.
 *
 * reference_video script_plan 结构化中间态（内容确认的可审 / 可改对象）。映射后端
 * lib/script/script_models.py 的 ReferenceScriptPlanUnit / ReferenceScriptPlanDraft：script_plan 定内容层
 * （unit 边界 + unit 时长 + 单元正文），prompt_authoring 视觉编排由用户确认后才触发。
 */
export interface ReferenceScriptPlanUnit {
  unit_id: string;
  /** Unit body, referencing registered assets with the `@` mention syntax. / 单元正文，用 `@[名称]` 引用已登记资产。 */
  text: string;
  /** Unit duration in seconds — one generation call, one duration. */
  duration_seconds: number;
  /** Verbatim source excerpt (traceability anchor); may be an empty string in legacy drafts. / 逐字原文摘录（追溯锚）；存量草稿可能为空串。 */
  source_text: string;
}

export interface ReferenceScriptPlanDraft {
  units: ReferenceScriptPlanUnit[];
  new_assets?: PlanNewAsset[];
}

/**
 * Flat draft structure of script_plan (this is what the draft holds, not the persisted `ReferenceScriptPlanDraft`):
 * `unit_id` is machine-derived and exists only before persisting -- the draft has only duration + source anchor + one body in writing syntax.
 * Mirrors lib/script/script_models.py ReferenceScriptPlanFlatUnit.
 *
 * script_plan 的扁平草稿结构（草稿装的是这个，不是落盘的 `ReferenceScriptPlanDraft`）：
 * `unit_id` 机器派生，落盘前才有——草稿中只有时长 + 原文锚 + 一段引用语法正文。
 * Mirrors lib/script/script_models.py ReferenceScriptPlanFlatUnit。
 */
export interface ReferenceScriptPlanFlatUnit {
  duration_seconds: number;
  source_text: string;
  text: string;
}

/**
 * Draft violation entry. Mirrors lib/script/draft_quarantine.py::violation_entries.
 * `label` is a locator prefix like `"unit E1U02"` (reference-to-video, array index = derived unit number - 1) or
 * `"segment E1S03"` (narration, matching `segment_id`); episode-level violations have no locator and are an empty string.
 * `line` is the 0-based raw line number within the unit body (same coordinate system as `sourceLine` in `useUnitPromptHighlight.ts`),
 * only for syntax violations; unit-level violations (no natural line) are null, and the presentation layer puts them in the in-card aggregate area.
 *
 * 草稿违约条目。Mirrors lib/script/draft_quarantine.py::violation_entries。
 * `label` 是定位前缀，形如 `"unit E1U02"`（参考生视频，数组下标 = 派生 unit 序号 - 1）或
 * `"segment E1S03"`（narration，与 `segment_id` 对应）；集级违约无定位、为空串。
 * `line` 是该单元正文内 0-based 原始行号（与 `useUnitPromptHighlight.ts` 的 `sourceLine` 同
 * 坐标系），仅语法类违约才有；单元级违约（无自然行归属）为 null，呈现层落卡内聚合区。
 */
export interface ScriptReviewViolation {
  code: string;
  label: string;
  message: string;
  line: number | null;
  locations?: Array<{ path: Array<string | number>; line: number | null }>;
  reason?: string;
  action?: string;
  /** Index of the violating entry in the draft body's entry array; missing together with `item_id` means an episode-level violation. / 违约所在条目在草稿正文条目数组里的下标；与 `item_id` 同缺即整集层面的违约。 */
  item_index?: number | null;
  item_id?: string | null;
  /** Render params of a video model config problem: `code` is the problem code, `message` already localized by the server per code. / 视频模型配置问题的渲染参数：`code` 即问题码，`message` 已由服务端按问题码本地化。 */
  params?: Record<string, unknown>;
}

/** Degradation note: does not block adoption, shown with the entry. `message` is written by the server in the request language. / 降级提示：不阻断采用，随条目呈现。`message` 由服务端按请求语言成文。 */
export interface DraftSoftViolation {
  code: string;
  params: Record<string, unknown>;
  item_index: number;
  item_id: string;
  message: string;
}

/** Document the draft corresponds to, same values as the Agent draft tool's `doc_type`. / 草稿对应的文档，取值同 Agent 草稿工具的 `doc_type`。 */
export type DraftDocType =
  | "drama_script_plan"
  | "narration_script_plan"
  | "reference_script_plan"
  | "reference_prompt_authoring";

/** Who handles the draft: `user` for an invalid draft (the creator can edit directly), `agent` for the Agent's editable draft (status only). / 草稿的处置方：`user` 为待修复草稿（创作者可直接改），`agent` 为 Agent 的可编辑草稿（只展示状态）。 */
export type DraftOwner = "user" | "agent";

/**
 * Presentation view of a draft (shared by `ScriptReviewState.quarantine` and the draft endpoint): non-null only when a draft is present.
 * `content` is the draft body as is, for creators to edit in place; `violations` of script plan drafts are recomputed on read by the same validator,
 * while prompt authoring drafts take the report from the latest generation / save.
 *
 * `content`'s shape varies by route (reference-to-video script plan `{ units }`, prompt authoring `{ title, units }`, drama
 * `{ title, scenes }`, narration `{ segments }`), and drafts may have been hand-edited by the Agent -- fields may be missing or mistyped.
 * So it is declared only as "an object"; each panel narrows field by field for its own route before rendering, not trusting the declaration.
 *
 * 草稿的呈现视图（`ScriptReviewState.quarantine` 与草稿端点共用）：草稿在场时才非 null。
 * `content` 是草稿正文原样，供创作者就地修改；脚本规划草稿的 `violations` 是读时按同一校验器重算的
 * 结果，提示词编写草稿取最近一次生成 / 保存时的报告。
 *
 * `content` 的形状随路线不同（参考生视频脚本规划 `{ units }`、提示词编写 `{ title, units }`、drama
 * `{ title, scenes }`、narration `{ segments }`），且草稿可能被 Agent 手改过——字段可能缺失或类型不对。
 * 故这里只声明到「一个对象」，各面板按自己那条路线逐项收窄后渲染，不信任声明。
 */
export interface ScriptReviewQuarantine {
  doc_type: DraftDocType;
  /** Concurrency token for save and discard; null when the draft file is corrupted. / 保存与丢弃的并发令牌；草稿文件已损坏时为 null。 */
  revision: string | null;
  editable_by: DraftOwner;
  /** Null when the draft file is corrupted, or for the Agent's editable draft. / 草稿文件已损坏、或是 Agent 的可编辑草稿时为 null。 */
  content: Record<string, unknown> | null;
  violations: ScriptReviewViolation[];
  soft_violations: DraftSoftViolation[];
  /** Whether there is final content to return to after discarding. / 丢弃后是否有正式内容可回。 */
  formal_exists: boolean;
}

/** View returned by the draft endpoint: `item_ids` are the final-script unit IDs corresponding to each entry of the prompt authoring draft. / 草稿端点返回的视图：`item_ids` 为提示词编写草稿各条目对应的正式剧本单元 ID。 */
export interface EpisodeDraftView extends ScriptReviewQuarantine {
  episode: number;
  item_ids: string[] | null;
}

export interface EpisodeDraftSummary {
  doc_type: DraftDocType;
  editable_by: DraftOwner;
  violation_count: number;
}

/** Result of saving manual fixes: adopted once violations reach zero (`draft` is null), otherwise returns the refreshed draft view. / 手修保存的结果：违约清零即采用（`draft` 为 null），否则带回刷新后的草稿视图。 */
export interface SaveEpisodeDraftResult {
  episode: number;
  doc_type: DraftDocType;
  adopted: boolean;
  draft: EpisodeDraftView | null;
}


/** Final text and images actually sent after projecting the current draft by model capability; image order matches the image numbers in the prompt. / 当前草稿按模型能力投影后的最终文本与实发图片，图片顺序对应提示词中的图号。 */
export interface ReferenceUnitPromptPreview extends RenderedPromptPreview {
  references: { type: AssetKind; name: string; path: string }[];
}
