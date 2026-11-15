import { useAssetSheetBatchNotifications } from "@/hooks/useAssetSheetBatchNotifications";
import { useRenderReadyToasts } from "@/hooks/useRenderReadyToasts";
import { useTaskFailureNotifications } from "@/hooks/useTaskFailureNotifications";

interface TaskFailureListenerProps {
  projectName?: string | null;
}

/**
 * UI-less listener component: isolates the tasks-store subscription for task-failure and asset-sheet-batch notifications in a leaf node,
 * so mounting it directly in StudioLayout does not re-render the whole layout (top bar/sidebar/panels) on every task list update.
 *
 * 无 UI 监听组件：把任务失败与资产图批次的通知对 tasks store 的订阅隔离在叶子节点，
 * 避免直接挂在 StudioLayout 时任务列表每次更新都触发整个布局（顶栏/侧栏/面板）重渲染。
 */
export function TaskFailureListener({ projectName }: TaskFailureListenerProps) {
  useTaskFailureNotifications(projectName);
  useAssetSheetBatchNotifications(projectName);
  useRenderReadyToasts(projectName);
  return null;
}
