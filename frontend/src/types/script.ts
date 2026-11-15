/**
 * Script / segment / scene type definitions.
 *
 * Maps to backend models in:
 * - lib/script/script_models.py (NarrationSegment, DramaScene, ImagePrompt, VideoPrompt, etc.)
 */

import type { DraftSoftViolation, ReferenceScriptPlanDraft, ReferenceUnitCapabilityMap, ScriptReviewQuarantine } from "./reference-video";

export const SHOT_TYPES = [
  "Extreme Close-up",
  "Close-up",
  "Medium Close-up",
  "Medium Shot",
  "Medium Long Shot",
  "Long Shot",
  "Extreme Long Shot",
  "Over-the-shoulder",
  "Point-of-view",
] as const;

export type ShotType = (typeof SHOT_TYPES)[number];

export const SHOT_TYPE_I18N_KEYS: Record<ShotType, string> = {
  "Extreme Close-up": "shot_type_extreme_close_up",
  "Close-up": "shot_type_close_up",
  "Medium Close-up": "shot_type_medium_close_up",
  "Medium Shot": "shot_type_medium_shot",
  "Medium Long Shot": "shot_type_medium_long_shot",
  "Long Shot": "shot_type_long_shot",
  "Extreme Long Shot": "shot_type_extreme_long_shot",
  "Over-the-shoulder": "shot_type_over_the_shoulder",
  "Point-of-view": "shot_type_point_of_view",
};

export const CAMERA_MOTIONS = [
  "Static",
  "Pan Left",
  "Pan Right",
  "Tilt Up",
  "Tilt Down",
  "Zoom In",
  "Zoom Out",
  "Push In",
  "Pull Out",
  "Truck Left",
  "Truck Right",
  "Pedestal Up",
  "Pedestal Down",
  "Orbit",
  "Tracking Shot",
  "Shake",
] as const;

export type CameraMotion = (typeof CAMERA_MOTIONS)[number];

export const CAMERA_MOTION_I18N_KEYS: Record<CameraMotion, string> = {
  Static: "camera_motion_static",
  "Pan Left": "camera_motion_pan_left",
  "Pan Right": "camera_motion_pan_right",
  "Tilt Up": "camera_motion_tilt_up",
  "Tilt Down": "camera_motion_tilt_down",
  "Zoom In": "camera_motion_zoom_in",
  "Zoom Out": "camera_motion_zoom_out",
  "Push In": "camera_motion_push_in",
  "Pull Out": "camera_motion_pull_out",
  "Truck Left": "camera_motion_truck_left",
  "Truck Right": "camera_motion_truck_right",
  "Pedestal Up": "camera_motion_pedestal_up",
  "Pedestal Down": "camera_motion_pedestal_down",
  Orbit: "camera_motion_orbit",
  "Tracking Shot": "camera_motion_tracking_shot",
  Shake: "camera_motion_shake",
};

export type DurationSeconds = number;
export type AssetStatus = "pending" | "storyboard_ready" | "completed";

export interface Dialogue {
  speaker: string;
  line: string;
}

export type UtteranceKind = "dialogue" | "voiceover";

/**
 * Drama shot-level ordered utterances, a discriminated union narrowed by kind that encodes the kind <-> speaker
 * constraint in the type: dialogue must carry a non-empty speaker, voiceover must not carry a speaker. Illegal combinations (dialogue without
 * speaker, voiceover with speaker) are rejected at compile time, consistent with the backend Utterance contract.
 *
 * Drama 分镜级有序发声条目，判别式联合（discriminated union）按 kind 收窄，把 kind ⇄ speaker
 * 约束编码进类型：dialogue 必带非空 speaker、voiceover 不得带 speaker。非法组合（dialogue 缺
 * speaker、voiceover 带 speaker）编译期即被拒，与后端 Utterance 契约一致。
 */
export interface DialogueUtterance {
  kind: "dialogue";
  /** Character dialogue lines must carry a non-empty speaker. / 角色台词必带非空说话人。 */
  speaker: string;
  text: string;
}

export interface VoiceoverUtterance {
  kind: "voiceover";
  /** Voiceover without speaker: speaker is always null or omitted. / 无说话人画外音：speaker 恒为 null 或缺省。 */
  speaker?: null;
  text: string;
}

export type Utterance = DialogueUtterance | VoiceoverUtterance;

/**
 * Structured intermediate state of script_plan (the reviewable / editable object of content review). Maps backend lib/script/script_models.py
 * DramaSceneContent / DramaNormalizedScript and NarrationScriptPlanSegment / NarrationScriptPlanDraft:
 * script_plan has fixed the content layer; prompt_authoring visual generation (image_prompt / video_prompt) is triggered only after user confirmation.
 *
 * script_plan 结构化中间态（内容确认的可审 / 可改对象）。映射后端 lib/script/script_models.py 的
 * DramaSceneContent / DramaNormalizedScript 与 NarrationScriptPlanSegment / NarrationScriptPlanDraft：
 * script_plan 已定内容层，prompt_authoring 视觉生成（image_prompt / video_prompt）由用户确认后才触发。
 */
export interface DramaSceneContent {
  scene_id: string;
  duration_seconds: number;
  segment_break: boolean;
  characters_in_scene: string[];
  scenes: string[];
  props: string[];
  /** Free text of the visual adaptation (for prompt_authoring to generate visuals, no spoken lines embedded). / 视觉改编自由文本（供 prompt_authoring 生成画面，不内嵌口播）。 */
  scene_description: string;
  /** Shot-level ordered utterance sequence: dialogue lines / voiceovers in time order (the rich editing object of content review). / 分镜级有序发声序列：台词 / 画外音按时序排列（内容确认的富编辑对象）。 */
  utterances: Utterance[];
  /** Verbatim source excerpt (traceability anchor; not read aloud, no sound). / 逐字原文摘录（追溯锚，不朗读、不出音）。 */
  source_text: string;
}

/** Kinds of new assets in the episode: only characters, scenes and props get added during planning. / 本集新增资产的类型：只有角色、场景、道具会在规划里新增。 */
export type NewAssetType = "character" | "scene" | "prop";

/** Handling decision for a new item: register as new asset / merge into existing asset / register as character derivative / do not register. / 新增项的处理决定：登记为新资产 / 归到已有资产 / 登记为角色衍生 / 不登记。 */
export type NewAssetDecision = "register" | "merge" | "derivative" | "skip";

/**
 * A new asset in the episode brought by the script plan. Maps backend PlanNewAsset in lib/script/script_models.py:
 * references in plan entries write `name`, and on confirmation they are rewritten and registered per `decision`.
 *
 * 脚本规划带出的一项本集新增资产。映射后端 lib/script/script_models.py 的 PlanNewAsset：
 * 规划条目里的引用写 `name`，确认时按 `decision` 改写并登记。
 */
export interface PlanNewAsset {
  type: NewAssetType;
  /** The name written in plan entries. / 规划条目里写的称呼。 */
  name: string;
  decision: NewAssetDecision;
  /** One-sentence rationale given by the AI. / AI 给出的一句依据。 */
  reason: string;
  /** register: appearance description; derivative: change description relative to the base look. / register：外观描述；derivative：相对本体的变化描述。 */
  description: string;
  /** register: other names in the source text. / register：原文中的其他称呼。 */
  aliases: string[];
  /** merge: name of the same-kind asset merged into, or the name of another new item in this episode; derivative: base character name. / merge：归入的同类资产名或本集另一新增项的称呼；derivative：本体角色名。 */
  target: string;
  /** register: registered name; derivative: derivative name; falls back to `name` when empty. / register：登记名；derivative：衍生名；为空时取 `name`。 */
  asset_name: string;
}

export interface DramaNormalizedScript {
  title: string;
  scenes: DramaSceneContent[];
  new_assets?: PlanNewAsset[];
}

export interface NarrationScriptPlanSegment {
  segment_id: string;
  /** Novel source text (kept verbatim, an editable object of content review). / 小说原文（逐字保留，内容确认的可编辑对象）。 */
  novel_text: string;
  duration_seconds: number;
  segment_break: boolean;
  characters_in_segment: string[];
  scenes: string[];
  props: string[];
}

export interface NarrationScriptPlanDraft {
  segments: NarrationScriptPlanSegment[];
  episode?: number;
  new_assets?: PlanNewAsset[];
}

export type ScriptReviewStatus =
  | "not_applicable"
  | "no_script_plan"
  | "pending_review"
  | "confirmed";

/** Final script entries content review will overwrite, with the artifacts and ownership under them that cannot be restored within the project. / 内容确认将覆盖的正式脚本条目，及其名下无法在项目内恢复的产物与归属。 */
export interface ScriptOverwriteEntry {
  id: string;
  has_storyboard: boolean;
  has_video: boolean;
  has_narration_audio: boolean;
  has_end_frame: boolean;
  /** ID of the grid (grid composite) it belongs to; null when not in any grid. / 所属宫格（联合图）ID；不在任何宫格内为 null。 */
  grid_id: string | null;
}

/** Final script content review will overwrite: all old shots are removed, listing the unrecoverable artifacts under each shot. / 内容确认将覆盖的正式脚本：旧分镜全部移除，列出每条分镜名下无法恢复的产物。 */
export interface ScriptOverwrite {
  /** Version of the listed final script; sent back as is when approving the overwrite; if the final script changes afterwards, it is rejected again with the new list. / 被列出的这份正式脚本的版本；认可覆盖时原样回传，正式脚本之后又有变化则按新清单再次拒绝。 */
  revision: string;
  entries: ScriptOverwriteEntry[];
  storyboard_count: number;
  video_count: number;
  narration_audio_count: number;
  end_frame_count: number;
  grid_member_count: number;
  grid_count: number;
  /** Server-generated loss list text (already written in the request language); the confirm dialog shows it as is, and the Agent receives the same. / 服务端生成的丢失清单文本（已按请求语言成文）；确认框原样呈现，Agent 收到同一份。 */
  text: string;
}

/** script_plan -> prompt_authoring content review state (GET response of backend server/routers/script_review.py). / script_plan→prompt_authoring 内容确认状态（后端 server/routers/script_review.py 的 GET 响应）。 */
export interface ScriptReviewState {
  episode: number;
  content_mode: string | null;
  status: ScriptReviewStatus;
  fingerprint: string | null;
  confirmed_at: string | null;
  content: DramaNormalizedScript | NarrationScriptPlanDraft | ReferenceScriptPlanDraft | null;
  /** Non-null when a draft is present (possible on all three script_plan routes), otherwise null. / 草稿在场时非 null（三条 script_plan 路线都可能出现），否则 null。 */
  quarantine: ScriptReviewQuarantine | null;
  /** Per-unit degradation notes of the final reference plan; notes only, not blocking. / 正式参考规划的逐单元降级提示，只提示、不阻断。 */
  soft_violations?: DraftSoftViolation[];
  /**
   * Selectable unit duration options; non-null only for the reference_video variant (also null when the project has no video model configured and it cannot be resolved,
   * in which case the presentation layer falls back to read-only seconds). The same option table as the backend read-time migration uses -- the full structural range, without
   * resolution / reference-image coupling constraints.
   *
   * unit 时长可选档位，reference_video 变体才非 null（项目未配置视频型号而解析不到时也为
   * null，呈现层退回只读秒数）。与后端读时迁移收编所用的是同一份档位表——结构区间全集，不
   * 含分辨率 / 参考图联动约束。
   */
  supported_durations: number[] | null;
  /**
   * Effective per-unit options narrowed by "with reference images or not", the same yardstick as validation before prompt_authoring persists; null when the model
   * cannot be resolved.
   *
   * 按「是否带参考图」收窄后的逐 unit 生效档位，与 prompt_authoring 落盘前的校验同一把尺；无法解析型号
   * 时为 null。
   */
  duration_tiers: {
    with_references: number[];
    /** Server bucket verdict per unit (by usable reference images); the panel takes options and judges out-of-range from it, not by registered references. / 逐 unit 的服务端定桶结论（按可用参考图），面板据此取档与判越档，不按已登记引用自判。 */
    units: ReferenceUnitCapabilityMap;
  } | null;
  /**
   * Project-level "episode target duration" preference (seconds), null when unset. The review panel renders the "episode total / target" comparison from it;
   * exceeding the target only warns, not blocking confirmation and later generation.
   *
   * 项目级「单集目标时长」偏好（秒），未设时 null。审核面板据它渲染「本集合计 / 目标」对比；
   * 超出目标只提示，不阻断确认与后续生成。
   */
  episode_target_duration: number | null;
  /** Final script that confirmation will overwrite; null when the episode has no final script yet. When non-null, confirmation must carry its `revision` to approve the overwrite. / 确认将覆盖的正式脚本；该集尚无正式脚本时 null。非 null 时确认须带其 `revision` 认可覆盖。 */
  script_overwrite: ScriptOverwrite | null;
}

export interface Composition {
  shot_type: ShotType;
  lighting: string;
  ambiance: string;
}

export interface ImagePrompt {
  scene: string;
  composition: Composition;
}

export interface VideoPrompt {
  action: string;
  camera_motion: CameraMotion;
  ambiance_audio: string;
  dialogue: Dialogue[];
}

export interface GeneratedAssets {
  storyboard_image: string | null;
  storyboard_last_image: string | null;  // grid mode last frame
  grid_id: string | null;                // source grid ID
  grid_cell_index: number | null;        // cell index in source grid
  video_clip: string | null;
  video_thumbnail: string | null;
  video_uri: string | null;
  narration_audio?: string | null;       // narration audio file path
  status: AssetStatus;
}

export interface NarrationSegment {
  segment_id: string;
  episode: number;
  duration_seconds: DurationSeconds;
  segment_break: boolean;
  novel_text: string;
  characters_in_segment: string[];
  scenes?: string[];
  props?: string[];
  /** `null` = pending authoring: a final script produced by content review has only the content layer; prompts are not written yet. / `null` = 待编写：内容确认转出的正式脚本只有内容层，提示词尚未编写。 */
  image_prompt: ImagePrompt | string | null;
  video_prompt: VideoPrompt | string | null;
  note?: string;
  /**
   * End-frame snapshot path (project-relative). The video opens on the storyboard image and transitions to end on this image.
   * Written only by the /end-frame set/clear endpoints -- the generic script PATCH deliberately does not accept this field,
   * to avoid bypassing the snapshot copy and writing dangling references (see server/routers/end_frames.py).
   *
   * 尾帧快照路径（项目内相对路径）。视频从分镜图开场、过渡到这张图收尾。
   * 只由 /end-frame 的设置/清除端点写入——通用剧本 PATCH 刻意不接受该字段，
   * 避免绕过快照复制写出悬空引用（见 server/routers/end_frames.py）。
   */
  end_frame_image?: string | null;
  generated_assets?: GeneratedAssets;
  /** Pending authoring: the visual layer has not been filled in by prompt authoring yet. Set when the entry is added, cleared after prompt authoring writes back; read-only. / 待编写：视觉层尚未由提示词编写补出。新增条目时置位、提示词编写写回后清除，只读。 */
  pending_authoring?: boolean;
}

export interface DramaScene {
  scene_id: string;
  duration_seconds: DurationSeconds;
  segment_break: boolean;
  characters_in_scene: string[];
  scenes?: string[];
  props?: string[];
  /** `null` = pending authoring: a final script produced by content review has only the content layer; prompts are not written yet. / `null` = 待编写：内容确认转出的正式脚本只有内容层，提示词尚未编写。 */
  image_prompt: ImagePrompt | string | null;
  video_prompt: VideoPrompt | string | null;
  /**
   * Shot-level ordered utterance sequence: character dialogue lines and voiceovers in time order. New structure (drama);
   * legacy drama goes through backend read-time migration, so this field may be omitted when read by the frontend.
   *
   * 分镜级有序发声序列：角色台词与画外音按时序排列。新结构（drama）；
   * 存量 drama 走后端读时迁移，前端读到时此字段可能缺省。
   */
  utterances?: Utterance[];
  /** Visual description: passed through from the script plan on content review; empty or omitted for manually added shots. / 画面描述：内容确认时从脚本规划透传；手动新增的分镜为空或缺省。 */
  scene_description?: string;
  /** Source text: passed through from the script plan on content review, read-only on the timeline; empty or omitted for manually added shots. / 对应原文：内容确认时从脚本规划透传，时间线只读；手动新增的分镜为空或缺省。 */
  source_text?: string;
  note?: string;
  /**
   * End-frame snapshot path (project-relative). The video opens on the storyboard image and transitions to end on this image.
   * Written only by the /end-frame set/clear endpoints -- the generic script PATCH deliberately does not accept this field,
   * to avoid bypassing the snapshot copy and writing dangling references (see server/routers/end_frames.py).
   *
   * 尾帧快照路径（项目内相对路径）。视频从分镜图开场、过渡到这张图收尾。
   * 只由 /end-frame 的设置/清除端点写入——通用剧本 PATCH 刻意不接受该字段，
   * 避免绕过快照复制写出悬空引用（见 server/routers/end_frames.py）。
   */
  end_frame_image?: string | null;
  generated_assets?: GeneratedAssets;
  /** Pending authoring: the visual layer has not been filled in by prompt authoring yet. Set when the entry is added, cleared after prompt authoring writes back; read-only. / 待编写：视觉层尚未由提示词编写补出。新增条目时置位、提示词编写写回后清除，只读。 */
  pending_authoring?: boolean;
}

/** Novel source information (present in both episode script types). */
export interface NovelInfo {
  title: string;
  chapter: string;
}

export interface NarrationEpisodeScript {
  episode: number;
  title: string;
  content_mode: "narration";
  schema_version?: number;
  novel: NovelInfo;
  segments: NarrationSegment[];
}

export interface DramaEpisodeScript {
  episode: number;
  title: string;
  content_mode: "drama";
  schema_version?: number;
  novel: NovelInfo;
  scenes: DramaScene[];
}

/** Eight guiding values for the sales-framework section (same wording as the backend's approved ratio table; not a hard enum, custom values allowed). / 带货框架 section 八值引导（与后端审定配比表用词一致；不硬枚举，允许自定义值）。 */
export const AD_SECTION_VALUES = [
  "hook",
  "pain_point",
  "product_reveal",
  "selling_point",
  "demo",
  "trust",
  "price_promo",
  "cta",
] as const;

/** Ad/short shot (flat shots[], spoken copy is first-class). / 广告/短片分镜（平铺 shots[]，口播文案一等）。 */
export interface AdShot {
  shot_id: string;
  /** Sales-framework paragraph label (hook/pain_point/... eight guiding values, not a hard enum). / 带货框架段落标签（hook/pain_point/... 八值引导，不硬枚举）。 */
  section: string;
  duration_seconds: DurationSeconds;
  /** Spoken copy, the sole source for subtitle export and later voiceover. / 口播文案，字幕导出与后续配音的唯一来源。 */
  voiceover_text: string;
  characters_in_shot?: string[];
  scenes?: string[];
  props?: string[];
  /** Product name reference; non-empty means a product shot. / 商品名称引用，非空即商品分镜。 */
  products_in_shot?: string[];
  /** Null for pending-authoring shots (manually added), filled in by prompt authoring. / 待编写分镜（手动新增）为 null，由提示词编写补出。 */
  image_prompt: ImagePrompt | string | null;
  video_prompt: VideoPrompt | string | null;
  note?: string;
  /**
   * End-frame snapshot path (project-relative). The video opens on the storyboard image and transitions to end on this image.
   * Written only by the /end-frame set/clear endpoints -- the generic script PATCH deliberately does not accept this field,
   * to avoid bypassing the snapshot copy and writing dangling references (see server/routers/end_frames.py).
   *
   * 尾帧快照路径（项目内相对路径）。视频从分镜图开场、过渡到这张图收尾。
   * 只由 /end-frame 的设置/清除端点写入——通用剧本 PATCH 刻意不接受该字段，
   * 避免绕过快照复制写出悬空引用（见 server/routers/end_frames.py）。
   */
  end_frame_image?: string | null;
  generated_assets?: GeneratedAssets;
  /** Pending authoring: the visual layer has not been filled in by prompt authoring yet. Set when the entry is added, cleared after prompt authoring writes back; read-only. / 待编写：视觉层尚未由提示词编写补出。新增条目时置位、提示词编写写回后清除，只读。 */
  pending_authoring?: boolean;
}

export interface AdEpisodeScript {
  episode: number;
  title: string;
  content_mode: "ad";
  schema_version?: number;
  novel: NovelInfo;
  shots: AdShot[];
}

export type EpisodeScript = NarrationEpisodeScript | DramaEpisodeScript | AdEpisodeScript;

/** Prompt authoring request: scope (all pending when omitted), whether to explicitly rewrite, instructions and the overwrite token. / 提示词编写的请求：范围（省略为全部待编写）、是否显式重写、附加指令与覆盖令牌。 */
export interface AuthorPromptsRequest {
  entry_ids?: string[] | null;
  rewrite?: boolean;
  instructions?: string | null;
  overwrite_revision?: string | null;
}

/** Existing visual-layer content an explicit rewrite will overwrite; `text` is the server-rendered loss list, `revision` the approval token. / 显式重写将覆盖的已有视觉层内容；`text` 是服务端渲染的丢失清单，`revision` 是认可令牌。 */
export interface PromptOverwrite {
  revision: string | null;
  entries: { id: string; fields: string[] }[];
  text: string;
}

export interface AuthorPromptsResponse {
  batch: { batch_id: string; members: { unit_id: string; task_id: string | null; deduped?: boolean }[] };
}

/** AI script planning request: instructions (saved per episode with the request). / AI 规划脚本的请求：附加指令（随请求按集保存）。 */
export interface PlanScriptRequest {
  instructions?: string | null;
}

/** Generation batch returned right after submit, same shape as prompt authoring. / 提交后立即返回的生成批次，形状同提示词编写。 */
export type PlanScriptResponse = AuthorPromptsResponse;

/** Ad/short "AI generate script" request: instructions go only with this submission; a full redo carries the token approving the overwrite. / 广告/短片「AI 生成脚本」的请求：附加指令只随本次提交；整份重做时带上认可覆盖的令牌。 */
export interface GenerateAdScriptRequest {
  instructions?: string | null;
  regenerate?: boolean;
  overwrite_revision?: string | null;
}

/** Result of a successful ad/short whole-script generation task: new assets registered this run (derivative names written as "base/derivative"). / 广告/短片整份生成任务成功时的结果：本次登记的新资产（衍生名写作「本体/衍生」）。 */
export interface AdScriptTaskResult {
  message?: string;
  new_assets?: { type: "character" | "scene" | "prop"; name: string }[];
}

/** Draft repair of an invalid draft: generation batch returned right after submit, same shape as prompt authoring. / AI 修复待修复草稿：提交后立即返回的生成批次，形状同提示词编写。 */
export type DraftRepairResponse = AuthorPromptsResponse;

/** Result of a successful draft repair task: adopted once violations reach zero; when not adopted, the number of violations left in the draft. / AI 修复任务成功时的结果：违约清零即已采用；未采用时草稿里还剩的违约数。 */
export interface DraftRepairTaskResult {
  adopted: boolean;
  violation_count: number;
}

/**
 * Final rendering of one side's prompt. Exactly one of `text` and `unavailable` is non-null;
 * `unavailable` is already finished copy rendered by the backend in the request language; the frontend does not translate again.
 *
 * 一侧提示词的最终渲染结果。`text` 与 `unavailable` 恰有一个非 null；
 * `unavailable` 已是后端按请求语言渲染的成品文案，前端不再二次翻译。
 */
export interface RenderedPromptPreview {
  text: string | null;
  unavailable: string | null;
  /** This side of the entry's prompt is currently in text form. / 该条目的这一侧提示词当前是文本形态。 */
  is_text_form: boolean;
  /** Notes produced when rendering this text (e.g. reference images trimmed beyond the backend cap), already rendered by the backend as finished copy in the request language. / 渲染这份文本时产生的提示（如参考图超出后端上限被裁剪），已由后端按请求语言渲染成成品文案。 */
  warnings: string[];
}

/** Final prompt preview of an entry: the same rendering exit as execution time, verbatim equal to the text actually sent to the model. / 条目最终提示词预览：与执行期同一渲染出口，逐字等于实际发给模型的文本。 */
export interface ItemPromptPreview {
  item_id: string;
  content_mode: "narration" | "drama" | "ad";
  storyboard_image: RenderedPromptPreview;
  video: RenderedPromptPreview;
}
