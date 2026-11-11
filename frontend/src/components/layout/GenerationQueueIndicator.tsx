import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { CheckCircle2, Clock, Film, ImageIcon, Loader2, XCircle, type LucideIcon } from "lucide-react";
import { API } from "@/api";
import { ElapsedTime, WorkingBar } from "@/components/canvas/edit-render/ElapsedTime";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { useTasksStore } from "@/stores/tasks-store";
import type { TaskItem, WorkspaceNotificationTarget } from "@/types";
import { errMsg } from "@/utils/async";
import { formatRelativeTime, parseIsoTimestamp } from "@/utils/date-format";
import { buildTaskFailureTarget } from "@/utils/task-target";

export type GenerationQueueKind = "storyboards" | "videos";

const QUEUES: Record<GenerationQueueKind, { icon: LucideIcon; title: string; matches: (task: TaskItem) => boolean }> = {
  storyboards: {
    icon: ImageIcon,
    title: "gen_queue_storyboards",
    matches: (task) =>
      task.task_type === "storyboard" ||
      task.task_type === "grid" ||
      (task.task_type === "image_edit" && task.resource_type === "storyboard"),
  },
  videos: {
    icon: Film,
    title: "gen_queue_videos",
    matches: (task) => task.task_type === "video" || task.task_type === "reference_video",
  },
};

/** Finished tasks stay listed this long; the list shows the newest ones first, at most LIST_LIMIT. */
const RECENT_MS = 24 * 60 * 60 * 1000;
const LIST_LIMIT = 50;

function isActive(task: TaskItem): boolean {
  return task.status === "queued" || task.status === "running";
}

/**
 * Top-bar entry for one generation queue of the project (storyboard images, or videos): shows while one of its
 * tasks is queued or running, or finished in the last 24 h. The badge reads running/active; the popover lists each
 * task with its shot (thumbnail, episode, position, opening words), model and running time or outcome, Cancel while
 * queued, and opens the shot on click.
 */
export function GenerationQueueIndicator({
  kind,
  onNavigate,
}: {
  kind: GenerationQueueKind;
  onNavigate: (target: WorkspaceNotificationTarget) => void;
}) {
  const { t, i18n } = useTranslation("dashboard");
  const queue = QUEUES[kind];
  const projectName = useProjectsStore((s) => s.currentProjectName);
  const projectData = useProjectsStore((s) => s.currentProjectData);
  const tasks = useTasksStore((s) => s.tasks);
  const [open, setOpen] = useState(false);
  // re-evaluates the 24 h window now and then, not only when tasks change
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const listed = useMemo(
    () =>
      tasks
        .filter((task) => task.project_name === projectName && queue.matches(task))
        .filter(
          (task) =>
            isActive(task) || now - parseIsoTimestamp(task.finished_at ?? task.updated_at).getTime() < RECENT_MS,
        )
        .sort((a, b) => Number(isActive(b)) - Number(isActive(a)) || b.queued_at.localeCompare(a.queued_at)),
    [tasks, projectName, queue, now],
  );
  const active = listed.filter(isActive);
  const running = active.filter((task) => task.status === "running");
  const failed = listed.filter((task) => task.status === "failed").length;

  if (!projectName || listed.length === 0) return null;

  const cancel = async (task: TaskItem) => {
    try {
      await API.cancelTask(task.task_id);
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    }
  };

  const Icon = queue.icon;
  const title = t(queue.title);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="sm"
            aria-label={title}
            title={
              active.length > 0
                ? `${title}: ${t("gen_queue_summary", { running: running.length, queued: active.length - running.length })}`
                : title
            }
          />
        }
      >
        {active.length > 0 ? (
          <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
        ) : (
          <Icon aria-hidden data-icon="inline-start" className={failed > 0 ? "text-destructive" : undefined} />
        )}
        <span className="num text-xs">{active.length > 0 ? `${running.length}/${active.length}` : listed.length}</span>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[26rem]">
        <div className="flex items-baseline justify-between gap-2">
          <PopoverTitle>{title}</PopoverTitle>
          <span className="num text-xs text-muted-foreground">
            {t("gen_queue_counts", { running: running.length, queued: active.length - running.length, failed })}
          </span>
        </div>
        <ul className="m-0 flex max-h-[28rem] list-none flex-col gap-1 overflow-y-auto p-0">
          {listed.slice(0, LIST_LIMIT).map((task) => {
            const ref = task.resource_ref;
            const target = buildTaskFailureTarget(task, projectData);
            const thumb = ref?.item_thumbnail ? API.getFileUrl(projectName, ref.item_thumbnail, null, { width: 160 }) : null;
            return (
              <li key={task.task_id} className="flex items-center gap-2 rounded-md p-1.5 hover:bg-muted/60">
                <button
                  type="button"
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  disabled={!target}
                  onClick={() => {
                    if (!target) return;
                    setOpen(false);
                    onNavigate(target);
                  }}
                >
                  <span className="relative h-9 w-16 shrink-0 overflow-hidden rounded bg-muted">
                    {thumb && <img src={thumb} alt="" className="size-full object-cover" loading="lazy" />}
                    <span className="absolute right-0.5 bottom-0.5 rounded-full bg-background/90 p-px">
                      {task.status === "succeeded" ? (
                        <CheckCircle2 aria-hidden className="size-3.5 text-good" />
                      ) : task.status === "failed" ? (
                        <XCircle aria-hidden className="size-3.5 text-destructive" />
                      ) : task.status === "cancelled" ? (
                        <XCircle aria-hidden className="size-3.5 text-muted-foreground" />
                      ) : task.status === "running" ? (
                        <Loader2 aria-hidden className="size-3.5 animate-spin text-muted-foreground" />
                      ) : (
                        <Clock aria-hidden className="size-3.5 text-muted-foreground" />
                      )}
                    </span>
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className="truncate text-sm">
                      {ref
                        ? `${ref.episode_title} · ${ref.item_position !== null ? `#${ref.item_position}` : ref.item_id}${ref.item_excerpt ? ` ${ref.item_excerpt}` : ""}`
                        : task.resource_id}
                    </span>
                    <span className="truncate text-xs text-muted-foreground">
                      {task.status === "running" && task.started_at ? (
                        <>
                          {t("gen_queue_running")} · <ElapsedTime since={task.started_at} />
                        </>
                      ) : task.status === "queued" ? (
                        <>
                          {t("render_queue_queued")} · <ElapsedTime since={task.queued_at} />
                        </>
                      ) : task.status === "succeeded" ? (
                        t("render_queue_done", { time: formatRelativeTime(task.finished_at, i18n.language) ?? "" })
                      ) : task.status === "failed" ? (
                        t("render_queue_failed", { message: task.error_message ?? "" })
                      ) : (
                        t("render_queue_cancelled")
                      )}
                      {task.model && ` · ${task.model}`}
                    </span>
                    {task.status === "running" && <WorkingBar />}
                  </span>
                </button>
                {task.status === "queued" && (
                  <Button variant="ghost" size="xs" onClick={() => void cancel(task)}>
                    {t("downloads_cancel")}
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
        {listed.length > LIST_LIMIT && (
          <p className="m-0 text-xs text-muted-foreground">{t("gen_queue_more", { count: listed.length - LIST_LIMIT })}</p>
        )}
      </PopoverContent>
    </Popover>
  );
}
