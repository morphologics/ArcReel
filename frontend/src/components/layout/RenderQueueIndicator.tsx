import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { CheckCircle2, Clapperboard, Clock, Loader2, XCircle } from "lucide-react";
import { API } from "@/api";
import { ElapsedTime, WorkingBar } from "@/components/canvas/edit-render/ElapsedTime";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { useTasksStore } from "@/stores/tasks-store";
import type { TaskItem } from "@/types";
import { errMsg } from "@/utils/async";
import { formatRelativeTime, parseIsoTimestamp } from "@/utils/date-format";
import { episodeDisplayName } from "@/utils/episode-display";

const RENDER_TASK_TYPES = new Set(["render_final_cut", "render_jianying_draft"]);
/** Finished renders stay listed this long. */
const RECENT_MS = 24 * 60 * 60 * 1000;

interface TimelineLabel {
  name: string;
  episode: number;
}

function isActive(task: TaskItem): boolean {
  return task.status === "queued" || task.status === "running";
}

/**
 * Top-bar entry for the project's render queue (final cuts and Jianying drafts): shows while a render is queued or
 * running, or one finished in the last 24 h. The badge counts active renders; the popover lists each with its
 * timeline, running time or outcome, Cancel while queued (a running render can't be stopped), and opens the
 * episode's Edit View on click.
 */
export function RenderQueueIndicator({ onOpenEpisode }: { onOpenEpisode: (episode: number) => void }) {
  const { t, i18n } = useTranslation("dashboard");
  const projectName = useProjectsStore((s) => s.currentProjectName);
  const episodes = useProjectsStore((s) => s.currentProjectData?.episodes);
  const tasks = useTasksStore((s) => s.tasks);
  const [open, setOpen] = useState(false);
  const [labels, setLabels] = useState<Map<string, TimelineLabel>>(new Map());
  // re-evaluates the 24 h window now and then, not only when tasks change
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const renders = useMemo(
    () =>
      tasks
        .filter((task) => task.project_name === projectName && RENDER_TASK_TYPES.has(task.task_type))
        .filter(
          (task) =>
            isActive(task) ||
            now - parseIsoTimestamp(task.finished_at ?? task.updated_at).getTime() < RECENT_MS,
        )
        .sort((a, b) => b.queued_at.localeCompare(a.queued_at)),
    [tasks, projectName, now],
  );
  const active = renders.filter(isActive);
  const running = active.filter((task) => task.status === "running");

  // timeline names for the list: render tasks carry only the timeline ID, so look them up per episode when opened
  const timelineIds = renders.map((task) => String(task.payload.timeline_id ?? "")).join(",");
  useEffect(() => {
    if (!open || !projectName || !episodes?.length) return;
    if (timelineIds.split(",").every((id) => !id || labels.has(id))) return;
    const controller = new AbortController();
    void Promise.all(
      episodes.map((ep) =>
        API.listEditTimelines(projectName, ep.episode, { signal: controller.signal })
          .then((res) => res.timelines)
          .catch(() => []),
      ),
    ).then((lists) => {
      if (controller.signal.aborted) return;
      setLabels(new Map(lists.flat().map((tl) => [tl.id, { name: tl.name, episode: tl.episode }])));
    });
    return () => controller.abort();
  }, [open, projectName, episodes, timelineIds, labels]);

  if (!projectName || renders.length === 0) return null;

  const cancel = async (task: TaskItem) => {
    try {
      await API.cancelTask(task.task_id);
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="sm"
            aria-label={t("render_queue_title")}
            title={
              active.length > 0
                ? t("render_queue_summary", { running: running.length, queued: active.length - running.length })
                : t("render_queue_title")
            }
          />
        }
      >
        {active.length > 0 ? (
          <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
        ) : (
          <Clapperboard aria-hidden data-icon="inline-start" />
        )}
        <span className="num text-xs">{active.length > 0 ? `${running.length}/${active.length}` : renders.length}</span>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-96">
        <PopoverTitle>{t("render_queue_title")}</PopoverTitle>
        <ul className="m-0 flex max-h-96 list-none flex-col gap-1 overflow-y-auto p-0">
          {renders.map((task) => {
            const label = labels.get(String(task.payload.timeline_id ?? ""));
            const kind = t(task.task_type === "render_final_cut" ? "render_queue_final_cut" : "render_queue_draft");
            const variant = [
              task.payload.narration === "with_narration" ? t("edit_render_narration_with") : t("edit_render_narration_without"),
              task.payload.subtitles === true ? t("render_queue_subtitles") : null,
            ]
              .filter(Boolean)
              .join(" · ");
            return (
              <li key={task.task_id} className="flex items-start gap-2 rounded-md p-1.5 hover:bg-muted/60">
                <span className="mt-0.5 shrink-0">
                  {task.status === "succeeded" ? (
                    <CheckCircle2 aria-hidden className="size-4 text-good" />
                  ) : task.status === "failed" ? (
                    <XCircle aria-hidden className="size-4 text-destructive" />
                  ) : task.status === "cancelled" ? (
                    <XCircle aria-hidden className="size-4 text-muted-foreground" />
                  ) : task.status === "running" ? (
                    <Loader2 aria-hidden className="size-4 animate-spin text-muted-foreground" />
                  ) : (
                    <Clock aria-hidden className="size-4 text-muted-foreground" />
                  )}
                </span>
                <button
                  type="button"
                  className="flex min-w-0 flex-1 flex-col gap-1 text-left"
                  disabled={!label}
                  onClick={() => {
                    if (!label) return;
                    setOpen(false);
                    onOpenEpisode(label.episode);
                  }}
                >
                  <span className="truncate text-sm">
                    {kind}
                    {label && ` · ${episodeDisplayName(episodes ?? [], label.episode, t)} · ${label.name}`}
                  </span>
                  <span className="truncate text-xs text-muted-foreground">
                    {task.status === "running" && task.started_at ? (
                      <>
                        {t("render_queue_running")} · <ElapsedTime since={task.started_at} />
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
                    {variant && ` · ${variant}`}
                  </span>
                  {task.status === "running" && <WorkingBar />}
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
      </PopoverContent>
    </Popover>
  );
}
