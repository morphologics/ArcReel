import type {
  CallType,
  EpisodeItemRef,
  TaskItem,
  TaskMediaType,
  UsageRecord,
  UsageRecordStatus,
} from "@/types";
import { parseIsoTimestamp } from "@/utils/date-format";

/**
 * View model of a record row. Finished calls and in-progress tasks that have no call row yet are both projected here,
 * so the records table, in-progress sub-block and header popover share one row component.
 *
 * 记录行的视图模型。已结束的调用与尚未产生调用行的进行中任务都投影到这里，
 * 让记录表、进行中子块与顶栏悬浮层共用同一个行组件。
 */
export interface UsageRecordView {
  /** React key; in-progress task rows have no record id, so the key is built from the task id. / React key；进行中的任务行没有记录 id，用任务 id 组键。 */
  key: string;
  /** Only rows that can open details have a record id; null for in-progress task rows. / 可打开详情的行才有记录 id；进行中的任务行为 null。 */
  recordId: number | null;
  /** Calls that served a task carry the task id; null for text calls without a task and endpoint trial runs. / 服务了任务的调用带任务 id；无任务的文本调用与端点试跑为 null。 */
  taskId: string | null;
  /** Empty string for endpoint trial-run records. / 端点试跑记录为空串。 */
  projectName: string;
  /** Project title; the UI falls back to the project name when absent. In-progress task rows have no title. / 项目标题；没有时界面回退到项目名。进行中的任务行没有标题。 */
  projectTitle: string | null;
  mediaType: CallType;
  provider: string | null;
  /** Null when a queued task's model is not resolved yet; the UI shows "To be resolved". / 排队中的任务模型尚未解析时为 null，界面显示「待解析」。 */
  model: string | null;
  status: UsageRecordStatus;
  purpose: string | null;
  segmentId: string | null;
  /** Title and episode order of the episode segmentId belongs to; the UI shows "Title · S01" from it. / segmentId 所属集的标题与播出位置，界面据此显示「标题 · S01」。 */
  segmentRef: EpisodeItemRef | null;
  errorCode: string | null;
  errorMessage: string | null;
  /** Start instant of a finished call; in-progress rows use it for sorting and timing. / 已结束调用的开始时刻；进行中行用它排序与计时。 */
  startedAt: string;
  finishedAt: string | null;
  durationMs: number | null;
  /** Null means unpriced; 0 means priced at zero. / null 表示未定价；0 是定价为零。 */
  costAmount: number | null;
  unpricedReason: string | null;
  currency: string;
}

export function usageRecordToView(record: UsageRecord): UsageRecordView {
  return {
    key: `record:${record.id}`,
    recordId: record.id,
    taskId: record.task_id,
    projectName: record.project_name,
    projectTitle: record.project_title ?? null,
    mediaType: record.media_type,
    provider: record.provider,
    model: record.model || null,
    status: record.status,
    purpose: record.purpose,
    segmentId: record.segment_id,
    segmentRef: record.segment_ref ?? null,
    errorCode: record.error_code,
    errorMessage: record.error_message,
    startedAt: record.started_at,
    finishedAt: record.finished_at,
    durationMs: record.duration_ms,
    costAmount: record.cost_amount,
    unpricedReason: record.unpriced_reason,
    currency: record.currency,
  };
}

/**
 * Backend resource type of a task. Billing always uses plural bucket names; the singular `resource_type` of `image_edit` is also
 * converted to the same bucket names at execution time (`edit_version_resource_type` in `server/services/tasks/image_edit_tasks.py`),
 * so both sources share this one table.
 *
 * 任务的后端资源类型。记账侧一律用复数桶名，`image_edit` 的单数 `resource_type` 亦在
 * 执行时转成同一套桶名（`server/services/tasks/image_edit_tasks.py` 的
 * `edit_version_resource_type`），故两条来源共用这一张表。
 */
const RESOURCE_TYPE_BY_KIND: Record<string, string> = {
  storyboard: "storyboards",
  grid: "grids",
  video: "videos",
  reference_video: "reference_videos",
  tts: "audio",
  voice_sample: "audio",
  character: "characters",
  character_derivative: "character_derivatives",
  scene: "scenes",
  prop: "props",
  product: "products",
};

/**
 * Resource types that can serve as `segment_id`, matching the `segment_id_for` allowlist in backend `lib/generation/media_generator.py`;
 * audio has no allowlist and passes through unconditionally.
 *
 * 可作 `segment_id` 的资源类型，与后端 `lib/generation/media_generator.py` 的
 * `segment_id_for` 白名单同口径；audio 无白名单，无条件透传。
 */
const SEGMENT_RESOURCE_TYPES: Record<TaskMediaType, ReadonlySet<string> | null> = {
  image: new Set(["storyboards", "videos", "grids"]),
  video: new Set(["storyboards", "videos", "reference_videos"]),
  audio: null,
  render: new Set(),
};

/**
 * Shot label basis for in-progress task rows. Record rows stored after a task finishes get `segment_id` from backend `segment_id_for`;
 * in-progress rows use the same allowlist here, so character / prop / scene image tasks do not
 * show "Shot <name>" while in progress and then revert to the generic target type after finishing.
 *
 * 进行中任务行的分镜标签口径。任务结束后落库的记录行按后端 `segment_id_for` 判定
 * `segment_id`，进行中的行在这里用同一份白名单判定，免得角色 / 道具 / 场景图任务
 * 进行中显示「分镜 <名字>」、结束后又变回泛化的目标类型。
 */
export function taskSegmentId(task: TaskItem): string | null {
  if (!task.resource_id) return null;
  // Audio billing ignores resource_type, so whether the task's resource kind is registered on this branch does not affect the result.
  // audio 记账不看 resource_type，任务的资源种类有没有登记在这一支上都不影响结论。
  const allowed = SEGMENT_RESOURCE_TYPES[task.media_type];
  if (allowed === null) return task.resource_id;
  const kind = task.task_type === "image_edit" ? task.resource_type : task.task_type;
  const resourceType = kind === null ? undefined : RESOURCE_TYPE_BY_KIND[kind];
  if (resourceType === undefined) return null;
  return allowed.has(resourceType) ? task.resource_id : null;
}

/**
 * Project an in-progress task into a record row. The task side has no model or call row; the model is left empty and the UI shows "To be resolved";
 * timing starts at `started_at`, or `queued_at` if not started yet. Local render tasks (`render`) do not call
 * a provider or record usage and have no row, so return null.
 *
 * 进行中的任务投影成记录行。任务侧没有模型与调用行，模型留空由界面显示「待解析」；
 * 计时起点取 `started_at`，尚未开始时取 `queued_at`。本地渲染任务（`render`）不调用
 * 供应商、不记用量，没有对应的行，返回 null。
 */
export function taskToUsageRecordView(task: TaskItem): UsageRecordView | null {
  if (task.media_type === "render") return null;
  return {
    key: `task:${task.task_id}`,
    recordId: null,
    taskId: task.task_id,
    projectName: task.project_name,
    projectTitle: null,
    mediaType: task.media_type,
    provider: task.provider_id,
    model: task.model ?? null,
    status: "pending",
    purpose: "generation_task",
    segmentId: taskSegmentId(task),
    segmentRef: taskSegmentId(task) === null ? null : (task.resource_ref ?? null),
    errorCode: null,
    errorMessage: null,
    startedAt: task.started_at ?? task.queued_at,
    finishedAt: null,
    durationMs: null,
    costAmount: 0,
    unpricedReason: null,
    currency: "USD",
  };
}

/**
 * The in-progress area sorts by start time descending: most recently queued or started first. Timestamps from tasks and calls may not carry
 * the same time zone suffix, so compare parsed instants, not strings.
 *
 * 进行中区按开始时刻倒序：最近排队或启动的排在最前。任务与调用两侧的时间戳未必带
 * 同样的时区后缀，故按解析出的时刻比较，不按字符串。
 */
export function sortByStartedDesc(views: UsageRecordView[]): UsageRecordView[] {
  return [...views].sort(
    (a, b) =>
      parseIsoTimestamp(b.startedAt).getTime() - parseIsoTimestamp(a.startedAt).getTime(),
  );
}
