import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { useAppStore } from "@/stores/app-store";
import { useTasksStore } from "@/stores/tasks-store";

const RENDER_READY_KEY: Record<string, string> = {
  render_final_cut: "render_ready_final_cut",
  render_jianying_draft: "render_ready_draft",
};

/**
 * A toast when a final cut or a Jianying draft finishes rendering (the render dialog may be closed by then);
 * failures are already notified by useTaskFailureNotifications. Tasks already finished when the list first
 * loads are not announced.
 */
export function useRenderReadyToasts(projectName?: string | null): void {
  const { t } = useTranslation("dashboard");
  const tasks = useTasksStore((s) => s.tasks);
  const connected = useTasksStore((s) => s.connected);
  const seen = useRef<Map<string, string> | null>(null);

  useEffect(() => {
    seen.current = null;
  }, [projectName]);

  useEffect(() => {
    if (!connected) return;
    const before = seen.current;
    const now = new Map<string, string>();
    for (const task of tasks) {
      if (task.project_name !== projectName || !(task.task_type in RENDER_READY_KEY)) continue;
      now.set(task.task_id, task.status);
      const previous = before?.get(task.task_id);
      if (before !== null && task.status === "succeeded" && previous !== undefined && previous !== "succeeded") {
        useAppStore.getState().pushToast(t(RENDER_READY_KEY[task.task_type]), "success");
      }
    }
    seen.current = now;
  }, [tasks, connected, projectName, t]);
}
