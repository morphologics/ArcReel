import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ContextMenu } from "@base-ui/react/context-menu";
import { Ban, Info } from "lucide-react";
import { API } from "@/api";
import { UsageRecordDetailModal } from "@/components/usage/UsageRecordDetailModal";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { useAppStore } from "@/stores/app-store";
import type { TaskItem } from "@/types";
import type { UsageRecordDetail } from "@/types/usage-record";
import { errMsg } from "@/utils/async";

function isActive(task: TaskItem): boolean {
  return task.status === "queued" || task.status === "running";
}

/**
 * Right-click menu on a queue item: Cancel while it waits (a running task can't be stopped), Show details once it
 * has finished (the generation call's usage record, or the task's own facts when there is none, e.g. a render).
 */
export function QueueItemMenu({
  task,
  cancelLabel,
  children,
}: {
  task: TaskItem;
  /** "Cancel generation" / "Cancel render". */
  cancelLabel: string;
  children: ReactNode;
}) {
  const { t } = useTranslation("dashboard");
  const [details, setDetails] = useState(false);
  const cancel = async () => {
    try {
      await API.cancelTask(task.task_id);
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    }
  };
  return (
    <>
      <ContextMenu.Root>
        <ContextMenu.Trigger render={<div />}>{children}</ContextMenu.Trigger>
        <DropdownMenuContent className="w-auto min-w-44">
          {isActive(task) ? (
            <DropdownMenuItem disabled={task.status === "running"} onClick={() => void cancel()}>
              <Ban aria-hidden />
              <span className="flex flex-col">
                {cancelLabel}
                {task.status === "running" && (
                  <span className="text-xs text-muted-foreground">{t("queue_running_not_cancellable")}</span>
                )}
              </span>
            </DropdownMenuItem>
          ) : (
            // opened after the menu has closed, or its closing focus move would dismiss the dialog at once
            <DropdownMenuItem onClick={() => window.setTimeout(() => setDetails(true), 0)}>
              <Info aria-hidden />
              {t("queue_show_details")}
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </ContextMenu.Root>
      {details && <TaskDetails task={task} onClose={() => setDetails(false)} />}
    </>
  );
}

/** The task's generation call (its latest usage record) in the usage detail view; the task's facts without one. */
function TaskDetails({ task, onClose }: { task: TaskItem; onClose: () => void }) {
  const [record, setRecord] = useState<{ id: number; detail: UsageRecordDetail | null } | null | undefined>(
    task.media_type === "render" ? null : undefined,
  );
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (task.media_type === "render") return;
    const controller = new AbortController();
    void (async () => {
      try {
        const page = await API.getUsageRecords(
          { projectName: task.project_name, segmentIds: [task.resource_id], since: task.queued_at, limit: 50 },
          { signal: controller.signal },
        );
        const latest = page.items.find((item) => item.task_id === task.task_id);
        if (!latest) {
          setRecord(null);
          return;
        }
        setRecord({ id: latest.id, detail: null });
        const detail = await API.getUsageRecord(latest.id, { signal: controller.signal });
        setRecord({ id: latest.id, detail });
      } catch {
        if (!controller.signal.aborted) {
          setFailed(true);
          setRecord(null);
        }
      }
    })();
    return () => controller.abort();
  }, [task]);

  if (record === undefined) return null;
  if (record !== null) {
    return (
      <UsageRecordDetailModal
        recordId={record.id}
        detail={record.detail}
        loading={record.detail === null && !failed}
        failed={failed}
        providerLabel={(provider) => provider ?? "—"}
        onClose={onClose}
      />
    );
  }
  return <TaskFactsDialog task={task} onClose={onClose} />;
}

function TaskFactsDialog({ task, onClose }: { task: TaskItem; onClose: () => void }) {
  const { t } = useTranslation(["dashboard", "common"]);
  const seconds =
    task.started_at && task.finished_at
      ? Math.round((new Date(task.finished_at).getTime() - new Date(task.started_at).getTime()) / 1000)
      : null;
  const rows: [string, ReactNode][] = [
    [t("queue_detail_type"), t(`task_type_${task.task_type}`, { defaultValue: task.task_type })],
    [t("queue_detail_item"), task.resource_ref ? `${task.resource_ref.episode_title} · ${task.resource_id}` : task.resource_id],
    [t("queue_detail_status"), task.status],
    [t("queue_detail_model"), task.model ?? task.provider_id ?? "—"],
    [t("queue_detail_queued"), new Date(task.queued_at).toLocaleString()],
    [t("queue_detail_finished"), task.finished_at ? new Date(task.finished_at).toLocaleString() : "—"],
    [t("queue_detail_run_time"), seconds !== null ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}` : "—"],
  ];
  if (task.error_message) rows.push([t("queue_detail_error"), task.error_message]);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("queue_show_details")}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="m-0 break-words">{value}</dd>
              </div>
            ))}
          </dl>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t("common:close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
