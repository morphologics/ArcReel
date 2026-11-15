import { isActiveStatus } from "@/stores/tasks-store";
import type { ReferenceVideoUnit, TaskItem, UnitStatus } from "@/types";

export interface UnitStatusConf {
  i18nKey: string;
  /** Badge text and background color. / 徽标的文字与底色。 */
  badgeClass: string;
  dotClass: string;
}

export const STATUS_CONF: Record<UnitStatus, UnitStatusConf> = {
  pending: {
    i18nKey: "reference_status_pending",
    badgeClass: "bg-muted text-muted-foreground",
    dotClass: "bg-muted-foreground",
  },
  running: {
    i18nKey: "reference_status_running",
    badgeClass: "bg-warn/15 text-warn",
    dotClass: "animate-breath bg-warn",
  },
  ready: {
    i18nKey: "reference_status_ready",
    badgeClass: "bg-good/15 text-good",
    dotClass: "bg-good",
  },
  failed: {
    i18nKey: "reference_status_failed",
    badgeClass: "bg-destructive/10 text-destructive",
    dotClass: "bg-destructive",
  },
};

/** Get this unit's status from the statusMap passed down by the canvas; when not provided, degrades to ready/pending by whether a final cut exists. / 从画布传下来的 statusMap 取该单元状态；未提供时按有无成片退化为 ready/pending。 */
export function resolveUnitStatus(
  unit: ReferenceVideoUnit,
  statusMap?: Record<string, UnitStatus>,
): UnitStatus {
  return statusMap?.[unit.unit_id] ?? (unit.generated_assets?.video_clip ? "ready" : "pending");
}

export interface UnitStatusInput {
  /** Whether the unit already has a final cut asset. / 该单元是否已有成片资产。 */
  hasClip: boolean;
  /** The unit's latest task row; "latest row wins" is guaranteed by selectLatestTaskByResource. / 该单元的最新任务行；「最新行胜出」由 selectLatestTaskByResource 保证。 */
  queueRow: TaskItem | undefined;
  /** Occupied set hit: occupied by a real task row or by the enqueue action layer's optimistic marker. / 占用集命中——真实任务行占用或入队动作层的乐观标记。 */
  busy: boolean;
  /** The unit is uploading a final cut. / 该单元正在上传成片。 */
  uploading?: boolean;
  /**
   * Whether this canvas offers a manual final cut upload entry point. If so, the final cut may come from an upload rather than generation, and historical failed rows should not
   * override an existing playable asset; if not, the final cut can only come from generation, so a failed latest row means this attempt failed and must
   * be shown as failed rather than masked as ready by an old final cut, otherwise the failure reason is nowhere to be seen.
   *
   * 本画布是否提供手动上传成片入口。提供时成片可能来自上传而非生成，历史失败行不该
   * 覆盖已有的可播放资产；不提供时成片只能来自生成，最新行失败即本次尝试失败，必须
   * 展示失败而不能被旧成片压成 ready，否则失败原因无处可见。
   */
  supportsManualUpload?: boolean;
}

/**
 * Display status of a video unit: the single derivation shared by both reference-to-video canvases.
 *
 * Decided by priority in order: uploading -> active queue row -> failed queue row -> optimistic occupied window.
 * Display semantics use isActiveStatus; control disabling looks at the occupied set `busy`, wired separately from this function's return value.
 *
 * The optimistic branch requires `!queueRow`: when a task row exists, status is always decided by that row. On the retry and regenerate paths
 * the task row is always present, so disabling within the optimistic window cannot rely only on this function's return value; see the note on `busy`.
 *
 * 视频单元的展示状态：两个参考生视频画布共用的单一推导口径。
 *
 * 判定按优先级依次落位——上传中 → 队列行活跃 → 队列行失败 → 乐观占用窗口。
 * 展示语义用 isActiveStatus；控件禁用看占用集 `busy`，独立于本函数的返回值另行接线。
 *
 * 乐观分支要求 `!queueRow`：有任务行时状态一律由该行决定。重试与重新生成这两条路径上
 * 任务行始终在，乐观窗口内的禁用因此不能只看本函数返回值——见 `busy` 的说明。
 */
export function deriveUnitStatus({
  hasClip,
  queueRow,
  busy,
  uploading = false,
  supportsManualUpload = false,
}: UnitStatusInput): UnitStatus {
  // Treat uploading as running: batch generation picks pending targets by status, otherwise the unit would be enqueued again during upload
  // and write the same final cut file as the generation write-back.
  // 上传中视为 running：批量生成按状态挑 pending 目标，否则上传期间会被再次入队，
  // 与生成回写同一个成片文件。
  if (uploading) return "running";
  if (queueRow && isActiveStatus(queueRow.status)) return "running";
  // Failed task rows are persisted in the DB and never expire, so whether a final cut overrides a failure depends on whether final cuts can come from uploads.
  // 失败任务行 DB 持久化、不会过期，故成片是否压过失败取决于成片能否来自上传。
  if (queueRow?.status === "failed" && !(supportsManualUpload && hasClip)) return "failed";
  if (!queueRow && busy) return "running";
  return hasClip ? "ready" : "pending";
}
