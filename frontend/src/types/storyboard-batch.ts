/** Batch generation of an episode's storyboard images / shot videos: preview (list, skipped items, estimated cost) and submit result. / 一集分镜图 / 分镜视频的批量生成：预览（名单、跳过项、预估费用）与提交结果。 */

import type { CostBreakdown } from "./cost";
import type { ReferenceBatchEnqueueFailure } from "./reference-video";
import type { WorkflowAdmission } from "./workflow";

/** `storyboards` batch-generates storyboard images, `videos` batch-generates shot videos. / `storyboards` 批量生成分镜图，`videos` 批量生成分镜视频。 */
export type StoryboardBatchKind = "storyboards" | "videos";

/** Why an item without a usable artifact is still left out of this batch. / 没有可用产物、却不进这一批的原因。 */
export type StoryboardBatchSkipReason =
  | "missing_prompt"
  | "missing_storyboard"
  | "generating"
  | "reference_unavailable"
  | "unavailable";

export interface StoryboardBatchSkip {
  unit_id: string;
  reason: StoryboardBatchSkipReason;
}

export interface StoryboardBatchPreview {
  targets: { unit_id: string }[];
  skipped: StoryboardBatchSkip[];
  /** Totals only the priced part; `null` when nothing is priced. / 只合计已定价的部分；一项都没有定价时为 `null`。 */
  estimated_cost: CostBreakdown | null;
  /** Number of items the price table cannot price, not included in `estimated_cost`. / 价目表算不出价、未计入 `estimated_cost` 的项数。 */
  unpriced_count: number;
  /** Shot videos only: whole-batch admission verdict; when `blocked` this batch cannot be submitted. / 仅分镜视频：整批准入结论；`blocked` 时这一批不能提交。 */
  admission?: WorkflowAdmission;
}

export interface StoryboardBatchSubmitted {
  /** `null` when whole-batch admission of shot videos failed; not a single task was created. / 分镜视频整批准入未通过时为 `null`，一个任务也没建。 */
  batch_id: string | null;
  task_ids_by_unit: Record<string, string>;
  skipped: StoryboardBatchSkip[];
  enqueue_failures: ReferenceBatchEnqueueFailure[];
  admission?: WorkflowAdmission;
}
