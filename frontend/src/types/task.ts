import type { EpisodeItemRef } from "./project";

/**
 * Task queue type definitions.
 *
 * Maps to backend models in:
 * - lib/generation/generation_queue.py (GenerationQueue task schema, get_task_stats)
 * - webui/server/routers/tasks.py (API responses)
 */

export type TaskStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";
/** `render` is the local render lane (final cuts etc.), producing no usage records. / `render` 是本地渲染车道（成片等），不产生用量记录。 */
export type TaskMediaType = "image" | "video" | "audio" | "render";

export interface TaskItem {
  task_id: string;
  project_name: string;
  task_type: string;
  media_type: TaskMediaType;
  resource_id: string;
  /** Title and episode order of the episode resource_id belongs to; null when the ID has no episode prefix or the episode is no longer in the ledger. / resource_id 所属集的标题与播出位置；ID 不带集前缀或集已不在账本里时为 null。 */
  resource_ref?: EpisodeItemRef | null;
  /**
   * Resource kind. Only written by image_edit tasks (character/scene/prop/product/storyboard) --
   * other task types are already distinguished by resource kind via task_type itself, so null. Occupancy matching uses it to put edit tasks
   * into the matching resource slot (see taskResourceKind in tasks-store).
   *
   * 资源种类。仅 image_edit 任务写入（character/scene/prop/product/storyboard）——
   * 其余任务类型 task_type 本身已按资源种类区分，故为 null。占用匹配据此把编辑任务
   * 归入对应资源槽（见 tasks-store 的 taskResourceKind）。
   */
  resource_type: string | null;
  script_file: string | null;
  /** Parsed from payload_json in the SQLite row */
  payload: Record<string, unknown>;
  status: TaskStatus;
  result: Record<string, unknown> | null;
  error_message: string | null;
  /** Present when error_message was stored as a machine-encoded task failure. */
  error_code?: string;
  error_params?: Record<string, unknown>;
  /** Server original text; appears only when error_message was replaced by localized copy; for unknown errors the original is in error_message itself. / 服务端原文；只在 error_message 已换成本地化文案时出现，未知错误的原文就在 error_message 里。 */
  error_detail?: string;
  cancelled_by: "user" | "cascade" | "interrupted" | null;
  provider_id: string | null;
  /** Model of the most recent call of a running task; null when queued or no call sent yet. Returned only by the task list API. / 运行中任务最近一次调用的模型；排队中或尚未发出调用时为 null。仅任务列表接口返回。 */
  model?: string | null;
  provider_job_id: string | null;
  source: "webui" | "agent";
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export interface TaskStats {
  queued: number;
  running: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  total: number;
}

/** Terminal state: end of the task lifecycle, no longer occupying the resource. / 终态：任务生命周期末端，不再占用 resource。 */
export function isTerminalStatus(status: TaskStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}
