/**
 * Shared API types: request payloads, query filters, and response structures reused across endpoints.
 * Re-exported via `@/api`; callers keep importing from `@/api`.
 *
 * API 共享类型：请求载荷、查询筛选与跨端点复用的响应结构。
 * 对外经 `@/api` 再导出，调用方继续从 `@/api` 导入。
 */

import type {
  AssistantSessionResumedPayload,
  EpisodeScript,
  NarrationDelivery,
  ProjectChangeBatchPayload,
  ProjectDeletedPayload,
  ProjectEventSnapshotPayload,
} from "@/types";
import type { GenerationRoute } from "@/utils/generation-mode";
import type { SseStreamError } from "@/utils/sse-stream";

/** The four asset types within a project (aligned with asset_type in backend ASSET_SPECS). / 项目内四类资产（与后端 ASSET_SPECS 的 asset_type 对齐）。 */
export type ProjectAssetType = "character" | "scene" | "prop" | "product";

/** Asset types that can be merged: types with aliases; products are excluded (aligned with backend MERGEABLE_ASSET_TYPES). / 可以合并的资产类型：带别名的类型，商品不开放（与后端 MERGEABLE_ASSET_TYPES 对齐）。 */
export type MergeableAssetType = Exclude<ProjectAssetType, "product">;

/** Impact report of a cascading asset rename (same structure for dry_run preview and execution). / 资产级联重命名的影响报告（dry_run 预览与执行同一结构）。 */
export interface AssetRenameResult {
  success: boolean;
  dry_run: boolean;
  old_name: string;
  new_name: string;
  episodes: number;
  references: number;
  files: number;
}

/** Impact of an asset merge on one episode: number of references rewritten in each place, and number of storyboard images and videos that will go stale. / 资产合并对一集的影响：各处改写的引用数，以及将过期的分镜图与视频数。 */
export interface AssetMergeEpisodeImpact {
  episode: number;
  script_plan: number;
  script: number;
  draft: number;
  prompt_text: number;
  speaker: number;
  storyboards: number;
  videos: number;
}

/** Impact report of an asset merge (same structure for dry_run preview and execution). / 资产合并的影响报告（dry_run 预览与执行同一结构）。 */
export interface AssetMergeResult {
  success: boolean;
  dry_run: boolean;
  source: string;
  target: string;
  as_derivative: boolean;
  aliases_added: string[];
  derivative_created: string | null;
  derivatives_moved: string[];
  derivatives_folded: string[];
  references: number;
  episodes: AssetMergeEpisodeImpact[];
}

/** Reference preview before deleting an asset (`DELETE …?dry_run=true`): same scan as rename, listed by ascending episode number. / 删除资产前的引用预览（`DELETE …?dry_run=true`）：与重命名同一套扫描，按集号升序列出。 */
export interface AssetDeletionPreview {
  success: boolean;
  dry_run: true;
  name: string;
  /** Total number of references; exceeds the sum of episodes when some references cannot be attributed to an episode. / 全部引用处数；个别引用归不到某一集时，会多于各集之和。 */
  references: number;
  episodes: { episode: number; references: number }[];
}

/** Login response from POST /auth/token (mirrors backend TokenResponse). */
export interface LoginResponse {
  access_token: string;
  token_type: string;
}

export type ScriptEditOperation =
  | { op: "update"; id: string; fields: Record<string, unknown> }
  | { op: "insert_after"; after_id: string | null; item: Record<string, unknown> }
  | { op: "move_after"; id: string; after_id: string | null }
  | { op: "remove"; id: string };

export interface ScriptEditCommand {
  script?: string;
  episode?: number;
  expected_revision: string;
  operations: ScriptEditOperation[];
}

export interface EpisodeScriptSnapshot {
  script: EpisodeScript;
  revision: string;
}

/** Version metadata returned by the versions API. */
export interface VersionInfo {
  version: number;
  filename: string;
  created_at: string;
  file_size: number;
  is_current: boolean;
  /** Whether this history record carries verified provenance for restore. */
  restorable?: boolean;
  /** Whether the shared presentation reader can preview/export this video version. */
  presentation_available?: boolean;
  file_url?: string;
  prompt?: string;
  restored_from?: number;
  /** Version source marker; "manual_upload" means uploaded manually by the user / 版本来源标记；"manual_upload" 表示用户手动上传 */
  source?: string;
  /** The generation call that produced this version (from the usage ledger); null for uploads, restores and
   * versions without a matching call. */
  generation?: VersionGeneration | null;
  duration_seconds?: number;
  aspect_ratio?: string;
  seed?: number | string;
  /** The shot's editable prompt (image_prompt / video_prompt) this version was generated from; absent on
   * versions made before it was recorded, uploads and restores. */
  script_prompt?: unknown;
  /** What an image version was generated from (an asset sheet's description, reference digests, style). */
  artifact_image_basis?: unknown;
}

/** Per image model ("provider/model"): the price of one storyboard image of the shot, or why the model can't make
 * it; `lane` is t2i, or i2i when the shot has reference images. */
export interface ImageModelQuotes {
  current: string | null;
  lane: "t2i" | "i2i";
  quotes: Record<
    string,
    { unavailable: string } | { cost: { amount: number; currency: string } | null }
  >;
}

/** Per model ("provider/model"): the price of the shot's video request, or why the model can't run it. */
export interface VideoModelQuotes {
  /** The configured video model; null when none resolves. */
  current: string | null;
  quotes: Record<
    string,
    | { unavailable: string }
    | {
        cost: { amount: number | null; currency: string | null; unpriced_reason: string | null } | null;
        duration_supported: boolean;
        supported_durations: number[];
      }
  >;
}

export interface VersionGeneration {
  provider: string;
  model: string;
  cost_amount: number | null;
  currency: string;
  /** YouReel: how long the generation took (ms); null / absent when not recorded (older studios, older calls). */
  duration_ms?: number | null;
}

/** Unified response for shot / video unit media uploads. / 分镜/视频单元媒体上传的统一响应。 */
export interface ShotUploadResult {
  success: boolean;
  path: string;
  version: number;
  asset_fingerprints: Record<string, number>;
}

export interface ProjectEventStreamOptions {
  projectName: string;
  /** The server always sends a snapshot first after every connection (including reconnects after a drop). / 每次建连（含断线重建）后服务端都会先发一次 snapshot。 */
  onSnapshot?: (payload: ProjectEventSnapshotPayload) => void;
  onChanges?: (payload: ProjectChangeBatchPayload) => void;
  /** Received once after the project directory is deleted, then the server closes the stream normally; subscribers should close the handle here to stop auto-reconnect. / 项目目录被删除后收到一次，随后服务端正常关流；订阅方应在此关闭句柄以停止自动重建。 */
  onProjectDeleted?: (payload: ProjectDeletedPayload) => void;
  /** An Agent session under the project went back to running on its own; the current session receives this turn via its resident entry stream, this notice is for other views such as the session list. / 项目下某个 Agent 会话自主回到 running；当前会话经常驻 entry 流收到这一轮，此通知供会话列表等其他视图使用。 */
  onAssistantSessionResumed?: (payload: AssistantSessionResumedPayload) => void;
  /** Connection failed or interrupted; when `retryable` is true the client reconnects automatically afterwards. / 连接失败或中断；`retryable` 为 true 时客户端随后自动重建。 */
  onError?: (error: SseStreamError) => void;
}

export interface AssistantEntriesStreamOptions {
  projectName: string;
  sessionId: string;
  /** Cold subscription cursor (seq); resuming after a reconnect is handled by `Last-Event-ID`. / 冷订阅游标（seq）；断线重建的续传由 `Last-Event-ID` 承担。 */
  after?: number;
  onEvent: (event: string, payload: Record<string, unknown>) => void;
  onError?: (error: SseStreamError) => void;
}

/** Filters for {@link API.listTasks} and {@link API.listProjectTasks}. */
export interface TaskListFilters {
  projectName?: string;
  status?: string;
  taskType?: string;
  source?: string;
  page?: number;
  pageSize?: number;
}

/** Filters for {@link API.getUsageRecords}; array dimensions are comma-separated in the query string. / {@link API.getUsageRecords} 的筛选；数组维度在查询串里逗号分隔。 */
export interface UsageRecordsQuery {
  /** Empty string filters endpoint test-run records; `undefined` means no project filter. / 空串筛选端点试跑记录，`undefined` 表示不按项目筛。 */
  projectName?: string;
  providers?: readonly string[];
  models?: readonly string[];
  mediaTypes?: readonly string[];
  statuses?: readonly string[];
  segmentIds?: readonly string[];
  /** ISO 8601 instants, half-open interval [since, until), applied to started_at. / ISO 8601 时刻，半开区间 [since, until)，作用于 started_at。 */
  since?: string;
  until?: string;
  limit?: number;
  /** Opaque cursor returned by the previous page. / 上一页返回的不透明游标。 */
  cursor?: string;
  /** View; defaults to "generation calls", excluding Agent records. / 视图；缺省为「生成调用」，不含 Agent 记录。 */
  view?: "generation" | "agent";
}

/** Filters for {@link API.getUsageSessions}: same basis as the record list, without status, media type and shot. / {@link API.getUsageSessions} 的筛选：与记录列表同口径，不含状态、媒体类型与分镜。 */
export interface UsageSessionsQuery {
  /** Empty string filters endpoint test-run records; `undefined` means no project filter. / 空串筛选端点试跑记录，`undefined` 表示不按项目筛。 */
  projectName?: string;
  providers?: readonly string[];
  models?: readonly string[];
  since?: string;
  until?: string;
  limit?: number;
  cursor?: string;
}

/** Filters for {@link API.getUsageSummary}; no status, pending is not aggregated. / {@link API.getUsageSummary} 的筛选；不收状态，pending 不进聚合。 */
export interface UsageSummaryQuery {
  /** Empty string filters endpoint test-run records; `undefined` means no project filter. / 空串筛选端点试跑记录，`undefined` 表示不按项目筛。 */
  projectName?: string;
  provider?: string;
  model?: string;
  mediaType?: string;
  since?: string;
  until?: string;
  /** IANA time zone name used to split days; defaults to UTC. / IANA 时区名，按此切天；缺省 UTC。 */
  tz?: string;
  /** Decides which view's projects, providers and models are listed as filter candidates; totals are not split by view. / 决定筛选候选值列哪个视图的项目、供应商与模型；合计不分视图。 */
  view?: "generation" | "agent";
}

/** Generic success response used by many endpoints. */
export interface SuccessResponse {
  success: boolean;
  message?: string;
}

/** Result of creating a project asset; when the project trash holds a deletion record for an asset of the same type and name, carries the ID of the latest one. / 新建项目资产的结果；项目回收站里有同类型同名资产的删除记录时带上最新那条的 ID。 */
export interface AssetCreateResult extends SuccessResponse {
  trash_record_id?: string | null;
}

export interface AgentProfileStatus {
  customized: boolean;
  customized_files: string[];
}

/** Payload for {@link API.createProject}. */
export interface CreateProjectPayload {
  title: string;
  name?: string;
  content_mode?: "narration" | "drama" | "ad";
  aspect_ratio?: "9:16" | "16:9";
  /** Generation mode, required at creation as one of two, no default (backend returns 422 if missing). / 生成模式，创建时必填二选一、无默认值（后端缺失即 422）。 */
  generation_mode: GenerationRoute;
  /** Multi-grid storyboard assembly switch, can be written at creation; only meaningful for storyboard-to-video. / 多宫格分镜装配开关，可随创建写入；仅分镜图生视频有意义。 */
  grid_storyboard?: boolean;
  /** Voiceover speech rate estimate (reading units / second); leave empty to estimate with the project language's default rate. / 口播语速估算（阅读单位 / 秒）；留空即按项目语言的默认速度估算。 */
  speech_rate_units_per_second?: number | null;
  default_duration?: number | null;
  /** Target duration per episode (seconds); not sent when unset. The server rejects this field for ad projects. / 单集目标时长（秒）；未设即不传。ad 项目服务端拒绝该字段。 */
  episode_target_duration?: number | null;
  /** ad only: target total duration (seconds), four UI options 15/30/60/90. / 仅 ad：目标总时长（秒），UI 四档 15/30/60/90。 */
  target_duration?: number;
  /** ad only: short text of the creative brief (may be empty). / 仅 ad：创作诉求短文本（可空）。 */
  brief?: string | null;
  style_template_id?: string | null;
  video_backend?: string | null;
  image_backend?: string | null;
  /** Project default image model. The creation wizard exposes only the default layer (docs/adr/0054); task-type buckets are left to the project settings page. / 项目默认图片模型。创建向导只暴露默认层（docs/adr/0054），任务类型桶留给项目设置页。 */
  default_image_backend?: string | null;
  text_backend_simple?: string | null;
  text_backend_complex?: string | null;
  default_text_backend?: string | null;
  model_settings?: Record<string, { resolution?: string | null }>;
  /** Narration delivery; server defaults to post-production dubbing. / 旁白交付方式；服务端缺省后期配音。 */
  narration_delivery?: NarrationDelivery;
  /** TTS snapshot (use_tts only): omitted fields are prefilled by the server with global defaults. / TTS 快照（仅 use_tts）：省略的字段由服务端以全局默认预填。 */
  audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
}

export interface VideoCapabilitiesQuery {
  signal?: AbortSignal;
  videoBackend?: string;
  /** undefined = server uses the project's saved option; null = explicit "Auto" (sends empty string); string = use that option. / undefined = 服务端按项目已保存档位；null = 显式「自动」（发空串）；字符串 = 按该档位。 */
  resolution?: string | null;
  usesReferenceImages?: boolean;
}
