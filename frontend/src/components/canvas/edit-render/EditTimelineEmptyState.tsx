import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Bot, Loader2, Plus, Scissors } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useEpisodeLedger } from "@/hooks/useEpisodeLedger";
import { useAppStore } from "@/stores/app-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { useWorkflowStore, workflowPlanKey } from "@/stores/workflow-store";
import { refusalReason } from "@/components/workflow/step-list";
import type { EditTimelineReadout } from "@/types/edit-timeline";
import { errMsg } from "@/utils/async";
import { episodeAgentRef } from "@/utils/episode-display";
import { createScriptEditTimeline } from "./create-script-timeline";

interface EditTimelineEmptyStateProps {
  projectName: string;
  episode: number;
  /** Callback after successful creation; the edit view switches to the new edit timeline accordingly. / 新建成功后回调，剪辑视图据此切到新的剪辑时间线。 */
  onCreated: (created: EditTimelineReadout) => void;
}

/**
 * Empty state of the edit view when this episode has no edit timeline yet.
 *
 * The two entry points are the same as the WorkflowPanel "Edit" row: "Hand to Agent to edit" only prefills the request into the chat input and opens the panel,
 * for the user to confirm and send; "New edit timeline" mechanically creates one from the script directly, bypassing the Agent. Rendering never creates an edit timeline automatically.
 *
 * 剪辑视图在本集还没有剪辑时间线时的空状态。
 *
 * 两个入口与 WorkflowPanel「剪辑」行同一套：「交给 Agent 剪辑」只把请求预填进对话输入框并打开面板，
 * 由用户确认发送；「新建剪辑时间线」直接按脚本机械新建，不经过 Agent。出片从不自动新建剪辑时间线。
 */
export function EditTimelineEmptyState({
  projectName,
  episode,
  onCreated,
}: EditTimelineEmptyStateProps) {
  const { t } = useTranslation(["dashboard", "workflow"]);
  const ledger = useEpisodeLedger();
  const [creating, setCreating] = useState(false);
  // Admission uses the same operation in the episode page workflow status (the episode has at least one usable video), the same source as the server's rejection on creation;
  // when workflow status is not yet fetched or belongs to another episode, do not disable; the server rechecks on submit.
  // 准入取集页制作状态里的同一操作（本集至少有一个可用视频），与服务端新建时的拒绝同源；
  // 制作状态尚未取回或属于别的集时不置灰，提交时由服务端复核。
  const operation = useWorkflowStore((s) =>
    s.planKey === workflowPlanKey(projectName, episode) ? s.plan?.status.operations.create_edit_timeline : undefined,
  );
  const blockedReason = refusalReason(t, operation) ?? undefined;
  const blocked = blockedReason !== undefined;

  const handleHandToAgent = useCallback(() => {
    // Only fill, do not send; with an existing session, do not switch or create a new one / 只填不发送，已有会话时不切换、不新建
    const episodeRef = episodeAgentRef(ledger, episode, t);
    useAssistantStore.getState().setInput(t("workflow:agent_prefill_create_edit_timeline", { episodeRef }));
    useAppStore.getState().setAssistantPanelOpen(true);
  }, [episode, ledger, t]);

  const handleCreate = async () => {
    if (creating || blocked) return;
    setCreating(true);
    try {
      // The new edit timeline immediately becomes the selected tab; no separate toast. / 新建的剪辑时间线随即成为选中的标签，不再另弹提示。
      onCreated((await createScriptEditTimeline(projectName, episode, t)).created);
    } catch (err) {
      useAppStore.getState().pushToast(t("edit_timeline_create_failed", { message: errMsg(err) }), "error");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-8">
      <div className="flex max-w-md flex-col items-center gap-4 text-center">
        <span
          aria-hidden="true"
          className="grid size-11 place-items-center rounded-xl border border-primary/25 bg-primary/10 text-primary"
        >
          <Scissors className="size-5" />
        </span>
        <div className="flex flex-col gap-1.5">
          <h2 className="text-base font-semibold text-foreground">{t("edit_view_empty_title")}</h2>
          <p className="text-sm text-muted-foreground">{t("edit_view_empty_description")}</p>
        </div>
        {/* A disabled button does not trigger hover, so the admission reason hangs on the outer container. / 禁用的 button 不触发悬停，准入原因挂在外层容器上。 */}
        <div className="flex flex-wrap items-center justify-center gap-2" title={blockedReason}>
          <Button size="sm" onClick={handleHandToAgent} disabled={blocked}>
            <Bot data-icon="inline-start" aria-hidden />
            {t("workflow:act_agent_edit")}
          </Button>
          <Button variant="outline" size="sm" onClick={() => void handleCreate()} disabled={blocked || creating}>
            {creating ? (
              <Loader2 data-icon="inline-start" className="animate-spin" aria-hidden />
            ) : (
              <Plus data-icon="inline-start" aria-hidden />
            )}
            {t("workflow:act_create_edit_timeline")}
          </Button>
        </div>
        {blocked && <p className="text-xs text-muted-foreground">{blockedReason}</p>}
      </div>
    </div>
  );
}
