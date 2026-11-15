/**
 * Render enqueue actions: final cut rendering and JianYing draft export. The JianYing draft takes only the narration version from the options.
 *
 * Render-lane tasks occupy no resource slot and do not appear in the usage overlay, so no optimistic occupancy mark is set here;
 * progress is polled by the render dialog using the returned task_id. When deduped=true (the same artifact already has a task in progress),
 * a unified info toast is shown and the caller tracks the returned task_id as usual. Failures are always thrown upward for the caller to report.
 *
 * 出片入队动作：成片渲染与剪映草稿导出。剪映草稿只取选项里的旁白版本。
 *
 * render 车道任务不占用任何资源槽，也不进用量悬浮层，因此这里不打乐观占用标记；
 * 进度由出片对话框按返回的 task_id 轮询。deduped=true（同一产物已有任务在处理）时
 * 弹统一 info 提示，调用方照常跟踪返回的 task_id。失败一律向上抛，由调用方提示。
 */
import { API } from "@/api";
import i18n from "@/i18n";
import { useAppStore } from "@/stores/app-store";
import type { RenderKind, RenderOptions, RenderSubmission } from "@/types";

export async function submitRender(
  projectName: string,
  timelineId: string,
  kind: RenderKind,
  options: RenderOptions,
): Promise<RenderSubmission> {
  const submission =
    kind === "final_cut"
      ? await API.renderFinalCut(projectName, timelineId, options)
      : await API.exportJianyingDraft(projectName, timelineId, { narration: options.narration });
  if (submission.deduped) {
    useAppStore.getState().pushToast(i18n.t("dashboard:enqueue_deduped_toast"), "info");
  }
  return submission;
}
