import { create } from "zustand";
import type { AssetSheetBatchMember } from "@/types";

/** An asset sheet batch still tracked after one submit: when all member tasks reach a terminal state, summarize into one notification. / 一次提交后仍在跟踪的资产图批次：成员任务全部落到终态时汇总成一条通知。 */
export interface TrackedAssetSheetBatch {
  batchId: string;
  projectName: string;
  /** Includes members whose submit failed before enqueue; members stopped by prechecks such as a missing description are not tracked. / 含未入队的提交失败成员；缺描述等预检拦下的成员不跟踪。 */
  members: AssetSheetBatchMember[];
}

interface AssetSheetBatchState {
  batches: Record<string, TrackedAssetSheetBatch>;
  /** All tracked tasks: kept even after the batch summary, so per-task failure notifications do not report them again. / 跟踪过的全部任务：整批汇总之后仍保留，逐任务失败通知据此不再重复报。 */
  trackedTaskIds: Set<string>;
  track: (batch: TrackedAssetSheetBatch) => void;
  settle: (batchId: string) => void;
}

export const useAssetSheetBatchStore = create<AssetSheetBatchState>((set) => ({
  batches: {},
  trackedTaskIds: new Set(),
  track: (batch) =>
    set((s) => {
      const trackedTaskIds = new Set(s.trackedTaskIds);
      for (const member of batch.members) {
        if (member.task_id) trackedTaskIds.add(member.task_id);
      }
      return { batches: { ...s.batches, [batch.batchId]: batch }, trackedTaskIds };
    }),
  settle: (batchId) =>
    set((s) => {
      const { [batchId]: _settled, ...batches } = s.batches;
      return { batches };
    }),
}));

/** Whether this task belongs to some asset sheet batch: if so, its failure is reported by the batch summary. / 这个任务是否属于某个资产图批次：属于时它的失败由整批汇总报告。 */
export function isAssetSheetBatchTask(taskId: string): boolean {
  return useAssetSheetBatchStore.getState().trackedTaskIds.has(taskId);
}
