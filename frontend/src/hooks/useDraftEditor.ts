import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API, ApiRequestError } from "@/api";
import { draftRepairResourceId, enqueueDraftRepair } from "@/actions/generation";
import type { DraftRepairTaskResult, SaveEpisodeDraftResult, ScriptReviewQuarantine } from "@/types";
import { useAppStore } from "@/stores/app-store";
import {
  isOccupyingStatus,
  isResourceBusy,
  useActiveResourceIds,
  useLatestTasksByResource,
  useTaskRowsByIds,
} from "@/stores/tasks-store";

interface DraftEditorOptions<T> {
  projectName: string;
  episode: number;
  /** The server's draft view (invalid draft); when null this hook holds no edits. / 服务端的草稿视图（待修复草稿）；为 null 时本 hook 不持有任何编辑。 */
  view: ScriptReviewQuarantine | null;
  /**
   * Narrow the draft body into a shape the panel can edit; when it can't (structure broken) return null and the panel shows a read-only explanation instead.
   * Must be a stable reference (module-level function or `useCallback`).
   *
   * 把草稿正文收窄成面板可编辑的形状；收不成（结构已损坏）时返回 null，面板改呈只读说明。
   * 须是稳定引用（模块级函数或 `useCallback`）。
   */
  narrow: (content: Record<string, unknown> | null) => T | null;
  /** After save, discard or draft repair settles, tell the caller to refresh its own server state (after adoption the final content has changed). / 保存、丢弃或 AI 修复落定后通知调用方刷新自己的服务端态（采用后正式内容已变）。 */
  onSettled: () => void;
}

interface Synced<T> {
  revision: string | null;
  base: T | null;
  edited: T | null;
}

export interface DraftEditorHandle<T> {
  /** The draft body currently being edited; null when the draft structure can't be narrowed into an editable shape. / 当前编辑中的草稿正文；草稿结构收不成可编辑形状时为 null。 */
  content: T | null;
  setContent: (update: (prev: T) => T) => void;
  dirty: boolean;
  /** The server draft was changed by someone else while there are unsaved local edits: saving will be rejected; local edits can be abandoned to load the latest. / 服务端草稿已被他方改过、而本地有未保存编辑：保存会被拒绝，可放弃本地编辑载入最新。 */
  outdated: boolean;
  saving: boolean;
  discarding: boolean;
  /** Draft repair of this draft is being submitted, queued or running. / 本份草稿的 AI 修复在提交、排队或执行中。 */
  repairing: boolean;
  /** Save and validate; adopted once violations reach zero. / 保存并校验；违约清零即采用。 */
  save: () => Promise<void>;
  /** Submit draft repair (repairing the saved draft); returns `true` when submitted or settled, so the caller can close the dialog. / 提交 AI 修复（修复已保存的草稿）；返回 `true` 表示已提交或已落定，供调用方收起弹窗。 */
  repair: (instructions: string) => Promise<boolean>;
  /** Discard the draft; returns whether it succeeded, so the caller decides whether to close the confirmation. / 丢弃草稿；返回是否成功，供调用方决定是否收起确认框。 */
  discard: () => Promise<boolean>;
  /** Abandon local edits and load the server's latest draft. / 放弃本地编辑，载入服务端最新草稿。 */
  reloadLatest: () => void;
}

function clone<T>(value: T | null): T | null {
  return value == null ? null : structuredClone(value);
}

function isDirty<T>(synced: Synced<T>): boolean {
  return JSON.stringify(synced.edited) !== JSON.stringify(synced.base);
}

export function diagnosticCode(err: unknown): string | null {
  if (!(err instanceof ApiRequestError)) return null;
  const diagnostic = err.diagnostic;
  if (diagnostic == null || typeof diagnostic !== "object") return null;
  const code = (diagnostic as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

/**
 * In-place manual fixing of an invalid draft: local editing, save and validate (adopted once violations reach zero), draft repair, discard.
 *
 * When the server draft changes (Agent write, a save elsewhere) and there are no unsaved local edits, adopt the new draft directly; with unsaved edits
 * keep them and mark `outdated`; saving will be rejected as a concurrency conflict, at which point load the latest draft.
 *
 * Draft repair runs as a queued text task: `repairing` is true while it occupies this draft's task slot. When the repair submitted from this UI, or one seen running,
 * reaches a final state, report the result and tell the caller to refresh; after adoption the draft view disappears, hence tracking by task ID.
 *
 * 待修复草稿的就地手修：本地编辑、保存并校验（违约清零即采用）、AI 修复、丢弃。
 *
 * 服务端草稿变了（Agent 写入、另一处保存）而本地没有未保存编辑时，直接采用新的草稿；有未保存编辑时
 * 保留编辑并标记 `outdated`，保存会以并发冲突被拒，届时载入最新草稿。
 *
 * AI 修复以排队文本任务执行：占用本份草稿的任务槽期间 `repairing` 为真。本界面提交的、或看到在跑的
 * 那次修复到达终态时，提示结果并通知调用方刷新；采用后草稿视图会消失，因此按任务 ID 跟踪。
 */
export function useDraftEditor<T>({ projectName, episode, view, narrow, onSettled }: DraftEditorOptions<T>): DraftEditorHandle<T> {
  const { t } = useTranslation("dashboard");
  const pushToast = useAppStore((s) => s.pushToast);
  const [synced, setSynced] = useState<Synced<T> | null>(null);
  const [saving, setSaving] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [watchedRepairId, setWatchedRepairId] = useState<string | null>(null);

  const repairResourceId = view != null ? draftRepairResourceId(episode, view.doc_type) : null;
  const activeRepairs = useActiveResourceIds("text_draft_repair", projectName);
  const repairing = repairResourceId != null && activeRepairs.has(repairResourceId);
  const latestRepair = useLatestTasksByResource(projectName, "text_draft_repair").get(repairResourceId ?? "");
  // Also track a repair seen running on this draft (submitted elsewhere, or the page reopened). / 看到本份草稿有在跑的修复（别处提交、或页面重新打开）时同样跟踪它。
  if (latestRepair != null && isOccupyingStatus(latestRepair.status) && watchedRepairId !== latestRepair.task_id) {
    setWatchedRepairId(latestRepair.task_id);
  }
  const watchedRepair = useTaskRowsByIds(watchedRepairId != null ? [watchedRepairId] : []).get(watchedRepairId ?? "");

  const incomingRevision = view?.revision ?? null;
  const adoptView = useCallback(
    (next: ScriptReviewQuarantine) => {
      const base = narrow(next.content);
      setSynced({ revision: next.revision, base, edited: clone(base) });
    },
    [narrow],
  );

  // Derived state when the server draft changes (synchronous during render): adopt only when there are no unsaved edits.
  // 服务端草稿变化时的派生状态（render 期同步）：无未保存编辑才采用。
  if (view != null && (synced == null || (synced.revision !== incomingRevision && !isDirty(synced)))) {
    adoptView(view);
  }
  if (view == null && synced != null) {
    setSynced(null);
  }

  const current = view != null ? synced : null;
  const dirty = current != null && isDirty(current);
  const outdated = current != null && current.revision !== incomingRevision;

  const setContent = useCallback((update: (prev: T) => T) => {
    setSynced((prev) => (prev?.edited == null ? prev : { ...prev, edited: update(prev.edited) }));
  }, []);

  const reloadLatest = useCallback(() => {
    if (view != null) adoptView(view);
  }, [view, adoptView]);

  const save = useCallback(async () => {
    if (view == null || current?.edited == null) return;
    setSaving(true);
    try {
      const result: SaveEpisodeDraftResult = await API.saveEpisodeDraft(
        projectName,
        episode,
        view.doc_type,
        current.edited,
        current.revision ?? "",
      );
      if (result.adopted) {
        pushToast(t("draft_adopted_toast"), "success");
      } else if (result.draft != null) {
        adoptView(result.draft);
        pushToast(t("draft_saved_with_violations_toast", { count: result.draft.violations.length }), "warning");
      }
      onSettled();
    } catch (err) {
      if (diagnosticCode(err) === "revision_conflict") {
        adoptView(view);
        pushToast(t("draft_conflict_toast"), "warning");
        onSettled();
      } else {
        pushToast(err instanceof Error && err.message ? err.message : t("draft_save_failed_toast"), "error");
      }
    } finally {
      setSaving(false);
    }
  }, [view, current, projectName, episode, adoptView, onSettled, pushToast, t]);

  const repair = useCallback(
    async (instructions: string): Promise<boolean> => {
      if (view == null) return false;
      if (isResourceBusy("text_draft_repair", projectName, draftRepairResourceId(episode, view.doc_type))) {
        pushToast(t("draft_repair_busy"), "error");
        return false;
      }
      try {
        const { taskIds } = await enqueueDraftRepair(
          projectName,
          episode,
          view.doc_type,
          current?.revision ?? view.revision ?? "",
          instructions.trim() || null,
        );
        if (taskIds[0] != null) setWatchedRepairId(taskIds[0]);
        return true;
      } catch (err) {
        if (diagnosticCode(err) === "revision_conflict") {
          adoptView(view);
          pushToast(t("draft_conflict_toast"), "warning");
          onSettled();
          return true;
        }
        pushToast(err instanceof Error && err.message ? err.message : t("draft_repair_failed_toast"), "error");
        return false;
      }
    },
    [view, current, projectName, episode, adoptView, onSettled, pushToast, t],
  );

  // The tracked repair reached a final state: report the result and have the caller re-fetch (after adoption the final content changed; otherwise the draft was written back). Reported once per task.
  // 跟踪的修复到达终态：提示结果，并让调用方重拉（采用后正式内容已变，否则草稿已写回）。每次任务只回报一次。
  const reportedRepairId = useRef<string | null>(null);
  useEffect(() => {
    if (watchedRepair == null || isOccupyingStatus(watchedRepair.status)) return;
    if (reportedRepairId.current === watchedRepair.task_id) return;
    reportedRepairId.current = watchedRepair.task_id;
    if (watchedRepair.status === "succeeded") {
      const result = (watchedRepair.result ?? {}) as Partial<DraftRepairTaskResult>;
      if (result.adopted) {
        pushToast(t("draft_adopted_toast"), "success");
      } else {
        pushToast(t("draft_repaired_with_violations_toast", { count: result.violation_count ?? 0 }), "warning");
      }
    } else if (watchedRepair.status === "failed") {
      pushToast(watchedRepair.error_message || t("draft_repair_failed_toast"), "error");
    }
    onSettled();
  }, [watchedRepair, onSettled, pushToast, t]);

  /** Discard the draft version shown in the UI; returns `true` when the confirmation can close. If the draft was updated elsewhere it isn't discarded; the latest version is loaded for the user to judge again. / 丢弃界面上呈现的那一版草稿；返回 `true` 表示确认框可关闭。草稿已在别处更新时不丢弃，载入最新版本供用户重新判断。 */
  const discard = useCallback(async (): Promise<boolean> => {
    if (view == null) return false;
    setDiscarding(true);
    try {
      await API.discardEpisodeDraft(projectName, episode, view.doc_type, current?.revision ?? view.revision);
      pushToast(t("draft_discarded_toast"), "success");
      onSettled();
      return true;
    } catch (err) {
      if (diagnosticCode(err) === "revision_conflict") {
        adoptView(view);
        pushToast(t("draft_conflict_toast"), "warning");
        onSettled();
        return true;
      }
      pushToast(err instanceof Error && err.message ? err.message : t("draft_discard_failed_toast"), "error");
      onSettled();
      return false;
    } finally {
      setDiscarding(false);
    }
  }, [view, current, projectName, episode, adoptView, onSettled, pushToast, t]);

  return {
    content: current?.edited ?? null,
    setContent,
    dirty,
    outdated,
    saving,
    discarding,
    repairing,
    save,
    repair,
    discard,
    reloadLatest,
  };
}
