/**
 * Frontend projection of the workflow plan. Fields map one-to-one to backend `lib/workflow/workflow_plan.py` / `lib/workflow/workflow_state.py` /
 * `lib/generation/generation_result.py`; REST and MCP share the same serialization, with no top-level envelope.
 *
 * Three axes are separated at the type level, and the UI must not fold them into one status:
 * - Step progress `WorkflowStepState` -- which step the orchestration has reached
 * - Artifact freshness `ArtifactStatus` -- whether the thing on disk still represents the current content
 * - Task and provider acceptance -- how far this attempt has run, and whether the provider side has accepted the job
 *
 * 工作流计划的前端投影。字段与后端 `lib/workflow/workflow_plan.py` / `lib/workflow/workflow_state.py` /
 * `lib/generation/generation_result.py` 一一对应，REST 与 MCP 共用同一序列化，顶层无 envelope。
 *
 * 三条轴在类型层就分开，界面不得把它们折成一个状态：
 * - 步骤进度 `WorkflowStepState` —— 编排走到哪一步
 * - 产物时效 `ArtifactStatus` —— 磁盘上那份东西还能不能代表当前内容
 * - 任务与供应商收单 —— 这一次尝试跑到哪了，以及供应商侧是否已经收单
 */

/** Progress of the orchestration step itself, independent of the artifact and task axes. / 编排步骤自身的进度，与产物、任务两轴无关。 */
export type WorkflowStepState =
  | "completed"
  | "ready"
  | "active"
  | "blocked"
  | "pending"
  | "skipped";

/** Artifact freshness. stale is a "still usable warning", not missing, and does not authorize automatic regeneration. / 产物时效。stale 是「可继续使用的告警」，不是缺失，也不授权自动重生。 */
export type ArtifactStatus = "current" | "stale" | "missing" | "blocked";

/** Narration delivery mode: project config (docs/adr/0089). / 旁白交付方式：项目配置（docs/adr/0089）。 */
export type NarrationDelivery = "post_production" | "use_tts";

/**
 * Closed set of next-action identifiers given by the backend, one-to-one with `WorkflowActionType` in `lib/workflow/workflow_state.py`;
 * backend contract tests keep both sides in sync. It is listed as a runtime array rather than just a type so the translation coverage
 * check can iterate each one: a new action without copy turns the test red, instead of silently falling to the fallback statement.
 *
 * 后端给出的下一步动作标识的闭集，与 `lib/workflow/workflow_state.py` 的 `WorkflowActionType`
 * 一一对应，后端契约测试守住两侧同步。列成运行时数组而不只是类型，是为了让译文覆盖
 * 检查能逐个遍历：新增动作没配文案时测试直接红，而不是静默落到兜底陈述。
 */
export const WORKFLOW_ACTION_TYPES = [
  "none",
  "collect_project_input",
  "create_episode",
  "draft_selling_points",
  "plan_episodes",
  "reset_episode_planning",
  "resolve_draft",
  "prepare_script_plan",
  "start_blank_script",
  "provide_episode_source",
  "confirm_script_plan",
  "generate_script",
  "add_script_items",
  "author_prompts",
  "generate_asset_sheets",
  "generate_storyboards",
  "generate_grid",
  "repair_video_units",
  "generate_videos",
  "create_edit_timeline",
  "retry_project_migration",
  "patch_episode_script",
  "retry",
  "fix_input",
  "generate_dependency",
  "wait_for_task",
  "replan_unit",
  "confirm_request_duration",
  "configure_provider",
  "repair_artifact_state",
  "retry_artifact_download",
] as const;

export type WorkflowActionType = (typeof WORKFLOW_ACTION_TYPES)[number];

export interface WorkflowNextAction {
  type: WorkflowActionType;
  args: Record<string, unknown>;
  requested_ids: string[];
  requires_confirmation: boolean;
  reason: string;
}

/**
 * Blocks of the data-corruption kind. `path` points to the exact field location, `reason` is the technical detail --
 * the former goes into the summary, the latter into the collapsed area.
 *
 * 数据损坏一类的阻断。`path` 指出具体字段位置，`reason` 是技术细节——
 * 前者进摘要，后者进折叠区。
 */
export interface WorkflowBlocker {
  code: string;
  path: string;
  reason: string;
}

/**
 * A note in the plan. `code` is a stable machine key the UI localizes by, `params` are the copy parameters;
 * raw reason text is not sent. Each note appears only under its own step (project-level ones at the top level), never duplicated in two places.
 *
 * 计划里的一条提示。`code` 是稳定的机器 key，界面按它本地化，`params` 是文案参数；
 * 原始理由文本不下发。每条提示只出现在所属步骤（项目级的在顶层），不在两处重复。
 */
export interface WorkflowProblem {
  code: string;
  action: string;
  /** The unit with the problem; empty for project-level notes. / 出问题的单元；项目级提示为空。 */
  unit_id?: string | null;
  params: Record<string, unknown>;
}

/** Whether the provider side has accepted the job. Accepted means a retry may be billed twice, so it must be stated separately from task status. / 供应商侧是否已经收单。已收单意味着重试可能重复计费，必须与任务状态分开陈述。 */
export interface WorkflowProviderCheckpoint {
  submitted: boolean;
}

/** A queued or running task. It is progress, not a problem; recovering tasks stay on this axis and never count toward current artifacts. / 一次排队中或运行中的任务。它是进度而不是问题；恢复中的任务停在这条轴上，绝不计入 current 产物。 */
export interface WorkflowTaskObservation {
  unit_id: string;
  task_id: string;
  batch_id?: string | null;
  task_type: string;
  status: string;
  provider_checkpoint?: WorkflowProviderCheckpoint | null;
}

/**
 * Status words of artifact entries in the plan. Wider than {@link ArtifactStatus}: besides artifact freshness it also expresses
 * "not involved in this mode" and "only partly covered" (e.g. the asset inventory covered only part of the source).
 * Values evolve with the backend; the UI dispatches on known values and gives the rest a fallback statement, with no exhaustive assertions.
 *
 * 计划里产物条目的状态词。比 {@link ArtifactStatus} 宽：产物时效之外还要表达
 * 「本模式不涉及」与「只覆盖了部分范围」（如资产盘点只盘了一部分源文）。
 * 取值随后端演进，界面按已知值分派、其余走兜底陈述，不做穷举断言。
 */
export type WorkflowArtifactState = ArtifactStatus | "not_applicable" | "partial" | (string & {});

/**
 * The artifact set under a step. When the set is enumerable three id lists are given; when the container itself cannot be read
 * only `state: "blocked"` is given, and no id is guessed into current / stale / missing.
 * Single artifacts (script, script_plan, etc.) give only `state`.
 *
 * 一个步骤下的产物集合。集合可枚举时给三份 id 列表；容器本身读不了时
 * 只给 `state: "blocked"`，此时不猜任何 id 落进 current / stale / missing。
 * 单份产物（剧本、script_plan 等）只给 `state`。
 */
export interface WorkflowArtifactCollection {
  state?: WorkflowArtifactState;
  current_ids?: string[];
  stale_ids?: string[];
  missing_ids?: string[];
  path?: string;
  [key: string]: unknown;
}

export interface WorkflowStepContracts {
  script_edit?: "script_batch_edit/v1" | null;
  batch_admission?: "video_batch_admission/v1" | null;
}

export interface WorkflowPlanStep {
  id: string;
  state: WorkflowStepState;
  required: boolean;
  action?: WorkflowNextAction | null;
  requested_ids: string[];
  artifacts: WorkflowArtifactCollection;
  problems: WorkflowProblem[];
  tasks: WorkflowTaskObservation[];
  admission?: WorkflowAdmission | null;
  contracts: WorkflowStepContracts;
}

/**
 * Verdict of whole-batch admission. Both outcomes other than `admitted` **created not a single task**,
 * and the UI must say so clearly rather than just reporting a failure.
 *
 * 整批准入判定的结论。`admitted` 之外的两种结局都**一个任务也没建**，
 * 界面必须把这一点说清楚，而不是只报一句失败。
 */
export type BatchAdmissionDecision = "admitted" | "confirmation_required" | "blocked";

/**
 * A single gap in the admission verdict. Same origin as the generation contract's problems; the only difference is that
 * the batch endpoint renders the copy into `message` in the user's language -- the plan endpoint does not, so the UI falls back to the translation table.
 *
 * 准入结论里的单条缺口。与生成契约的问题同源，差别只在于
 * 批量端点会把文案按用户语言渲染进 `message`——计划端点不渲染，界面据此回退到译文表。
 */
export interface AdmissionProblem {
  code: string;
  detail?: string | null;
  action?: string | null;
  params?: Record<string, unknown>;
  /** Already localized to the user's language and can be shown directly; when absent, look up `code` in the translation table. / 已按用户语言本地化，可直接展示；缺省时按 `code` 查译文表。 */
  message?: string | null;
}

/** Exact quote from the provider side for this video request. / 供应商侧对这一次视频请求的精确报价。 */
export interface VideoRequestCostQuote {
  /** Null means the price table cannot price this request; reason in unpriced_reason. / null 表示价目表算不出这次请求，原因见 unpriced_reason。 */
  amount: number | null;
  currency: string | null;
  provider_id: string;
  model_id: string;
  request_duration_seconds: number;
  unpriced_reason?: string | null;
}

export interface BatchAdmissionUnit {
  unit_id: string;
  /** This unit's own verdict; in a blocked batch it may still be true (held back along with other units in the same batch). / 该单元自身的判定；受阻批次里它仍可能为 true（被同批其它单元连带扣下）。 */
  admitted: boolean;
  withheld?: boolean;
  request_duration_seconds?: number | null;
  request_cost?: VideoRequestCostQuote | null;
  problems: AdmissionProblem[];
  projection?: unknown;
}

export interface BatchAdmissionTier {
  request_duration_seconds: number | null;
  unit_count: number;
  unit_ids: string[];
  cost_amount?: number | null;
  cost_currency?: string | null;
  /** Number of units in this tier the price table cannot price, not included in `cost_amount`. / 这一档里价目表算不出价、未计入 `cost_amount` 的单元数。 */
  unpriced_count?: number;
}

export interface WorkflowAdmission {
  decision: BatchAdmissionDecision;
  operation: string;
  selection: "explicit" | "missing_only";
  units: BatchAdmissionUnit[];
  confirmation?: { tiers: BatchAdmissionTier[] } | null;
}

export interface WorkflowProject {
  content_mode: string;
  generation_mode: string;
  grid_storyboard: boolean;
}

export interface WorkflowTarget {
  episode: number;
  script: string;
  script_filename: string;
  source: string;
}

/** Structural admission of an AI operation right now; `reason` is a stable reason code, same origin as the operation entry's rejection reason. / 一个 AI 操作此刻的结构准入；`reason` 是稳定的理由码，与操作入口的拒绝理由同源。 */
export interface WorkflowOperation {
  state: "admitted" | "refused" | "not_applicable";
  reason?: string | null;
}

/** A draft present on the target episode; `needs_repair` distinguishes an invalid draft from the Agent's editable draft. / 目标集上在场的一份草稿；`needs_repair` 区分待修复草稿与 Agent 的可编辑草稿。 */
export interface WorkflowDraft {
  kind: string;
  path: string;
  needs_repair: boolean;
}

/** Content status. Episode-level fields have values only when there is a target episode; the whole thing is empty when the project as a whole is unavailable. / 内容现状。集级字段只在有目标集时有值；项目整体不可用时整份为空。 */
export interface WorkflowContent {
  episode_count: number;
  whole_source: "present" | "absent" | "not_applicable";
  source_remaining: boolean;
  ad_inputs: "present" | "absent" | "not_applicable";
  products_without_selling_points: string[];
  episode_source?: "present" | "absent" | "not_applicable" | null;
  episode_plan_stale: boolean;
  expected_stale_script_plan_revision?: string | null;
  drafts: WorkflowDraft[];
  formal_script?: "present" | "absent" | "invalid" | null;
  script_item_count?: number | null;
  pending_authoring_ids: string[];
  needs_replan_ids: string[];
  referenced_assets_without_sheet: string[];
  unregistered_references: string[];
  /** Assets referenced by the episode whose sheets are stale; stated only, not put into the suggested next step. / 本集引用、资产图过期的资产；只陈述，不进建议的下一步。 */
  referenced_asset_sheets_stale: string[];
  /** Assets referenced by the episode that lack descriptions and thus cannot generate sheets; stated only, not put into the suggested next step. / 本集引用、缺描述因而不能生成资产图的资产；只陈述，不进建议的下一步。 */
  referenced_assets_without_description: string[];
  /** The target episode is complete: videos are all present and there is at least one edit timeline. / 目标集已完成：视频齐全且至少有一条剪辑时间线。 */
  episode_complete?: boolean;
  /** True only in queries not specifying an episode: every episode is complete and nothing remains of the whole source. / 只在不指定集的查询里为 true：每集都完成，且整本源文没有剩余。 */
  project_complete?: boolean;
}

/** One artifact the last migration failed to bring in: the file stays in place, unregistered. / 上一次迁移没能带入的一件产物：文件原地保留，没有登记。 */
export interface WorkflowMigrationSkippedArtifact {
  kind: string;
  episode: number | null;
  resource_id: string;
  artifact_path: string;
  reason: string;
  /** Why it went into "not brought in": the file itself cannot be read, or old narration audio has no synthesis settings record. / 进「未能带入」的原因：文件本身读不出，或旧版旁白音频没有合成设置记录。 */
  category?: "unreadable" | "legacy_audio_basis" | null;
}

/** One adjustment migration or import made to user data; `code` decides the displayed copy, `params` are the values in it. / 迁移或导入对用户数据做的一处调整；`code` 决定展示文案，`params` 是文案里的值。 */
export type WorkflowMigrationAdjustmentCode =
  | "draft_name_conflict_resolved"
  | "duplicate_episode_entry_removed"
  | "shared_script_binding_split"
  | "script_episode_corrected"
  | "script_binding_canonicalized"
  | "item_id_reassigned"
  | "duplicate_asset_removed"
  | "duplicate_asset_derivative_merged"
  | "duplicate_asset_references_rewritten"
  | "shared_reference_file_copied"
  | "placeholder_asset_added";

export interface WorkflowMigrationAdjustment {
  code: WorkflowMigrationAdjustmentCode;
  episode: number | null;
  file: string | null;
  /** Where the replaced copy is kept (project-relative path); never cleaned up automatically. / 被替下的那一份留存的位置（项目内相对路径）；不会被自动清理。 */
  backup: string | null;
  params: Record<string, string>;
}

/**
 * What the last migration adjusted (`adjusted`) and failed to bring in (`skipped`). Explanatory only, not involved in status and blocking;
 * workflow status gives it only when the report is non-empty and the user has not acknowledged it yet. Projects whose migration failed go through `project_migration_failed` in `blockers`.
 *
 * 上一次迁移调整了什么（`adjusted`）、没能带入什么（`skipped`）。只作说明，不参与状态与阻断；
 * 制作状态只在报告非空、用户尚未确认过时给出。迁移失败的项目走 `blockers` 里的 `project_migration_failed`。
 */
export interface WorkflowMigrationReport {
  schema_version: 2;
  migrated_at: string;
  from_schema_version: number;
  to_schema_version: number;
  complete: boolean;
  registered: Record<string, number>;
  adjusted: WorkflowMigrationAdjustment[];
  skipped: WorkflowMigrationSkippedArtifact[];
  acknowledged_at: string | null;
}

/**
 * Workflow status: content status (`content` / `artifacts`), admission of each AI operation (`operations`) and the suggested next step
 * (`next_action`, with parallel options at forks in `next_alternatives`). `blockers` only means the project as a whole is unavailable;
 * data problems in the content itself are in `issues`.
 *
 * 制作状态：内容现状（`content` / `artifacts`）、各 AI 操作的准入（`operations`）与建议的下一步
 * （`next_action`，分岔处的并列选项在 `next_alternatives`）。`blockers` 只表示项目整体不可用，
 * 内容本身的数据问题在 `issues`。
 */
export interface WorkflowStatus {
  schema_version: 2;
  project_revision: string;
  source_revision: string | null;
  project: WorkflowProject;
  target: WorkflowTarget | null;
  blockers: WorkflowBlocker[];
  issues: WorkflowBlocker[];
  content: WorkflowContent | null;
  operations: Record<string, WorkflowOperation>;
  gates: Record<string, Record<string, unknown>>;
  artifacts: Record<string, WorkflowArtifactCollection>;
  next_action: WorkflowNextAction;
  next_alternatives: WorkflowNextAction[];
  migration_report?: WorkflowMigrationReport | null;
}

/** Suggested next step for one episode in the ledger (`GET /projects/{name}/workflow-status/episodes`), in ledger order. / 账本中一集建议的下一步（`GET /projects/{name}/workflow-status/episodes`），按账本顺序排列。 */
export interface EpisodeNextStep {
  episode: number;
  /** The episode's episode plan is invalid (the source was replanned); the next step is `none`. / 该集的集规划已失效（原文已重新规划），下一步为 `none`。 */
  plan_stale: boolean;
  next_action: WorkflowNextAction;
}

export interface WorkflowPlan {
  schema_version: 2;
  status: WorkflowStatus;
  steps: WorkflowPlanStep[];
  blockers: WorkflowBlocker[];
  /** Project-level notes not belonging to any step (e.g. data upgrade failure). / 不属于任何步骤的项目级提示（如数据升级失败）。 */
  problems: WorkflowProblem[];
  next_action: WorkflowNextAction;
  next_alternatives: WorkflowNextAction[];
}

/** Request body of `POST /projects/{name}/workflow-plan`. / `POST /projects/{name}/workflow-plan` 的请求体。 */
export interface WorkflowPlanRequest {
  episode_id?: number | null;
  confirmed_request_durations?: Record<string, number>;
}
