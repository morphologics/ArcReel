import { create } from "zustand";
import type {
  PlaybackStartRequest,
  WorkspaceFocusTarget,
  WorkspaceFocusTargetInput,
  WorkspaceNotification,
  WorkspaceNotificationInput,
  WorkspaceNotificationTarget,
} from "@/types";

/** Action button on a toast, for "Undo" on reversible actions, or to open a just-finished result when the user has left (e.g. "Open project"); clicking runs the callback and closes the toast. / 提示上的操作按钮，用于可逆操作的「撤销」，或在用户已离开时打开刚完成的结果（如「打开项目」）；点击后执行回调并关闭这条提示。 */
interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface Toast {
  id: string;
  text: string;
  tone: "info" | "success" | "error" | "warning";
  action?: ToastAction;
}

const PLAYBACK_START_TTL_MS = 8000;
const ALL_ENTITIES_REVISION_KEY = "__all__";

export const ASSISTANT_PANEL_DEFAULT_WIDTH = 420;
export const ASSISTANT_PANEL_MIN_WIDTH = 320;
export const ASSISTANT_PANEL_MAX_WIDTH = 640;
const ASSISTANT_PANEL_WIDTH_STORAGE_KEY = "arcreel_assistant_panel_width";
const ASSISTANT_PANEL_OPEN_STORAGE_KEY = "arcreel_assistant_panel_open";

export function clampAssistantPanelWidth(value: number): number {
  if (!Number.isFinite(value)) return ASSISTANT_PANEL_DEFAULT_WIDTH;
  return Math.min(
    ASSISTANT_PANEL_MAX_WIDTH,
    Math.max(ASSISTANT_PANEL_MIN_WIDTH, Math.round(value)),
  );
}

function readPersistedAssistantPanelWidth(): number {
  if (typeof window === "undefined") return ASSISTANT_PANEL_DEFAULT_WIDTH;
  try {
    const raw = window.localStorage.getItem(ASSISTANT_PANEL_WIDTH_STORAGE_KEY);
    if (!raw) return ASSISTANT_PANEL_DEFAULT_WIDTH;
    const parsed = parseInt(raw, 10);
    if (Number.isNaN(parsed)) return ASSISTANT_PANEL_DEFAULT_WIDTH;
    return clampAssistantPanelWidth(parsed);
  } catch {
    return ASSISTANT_PANEL_DEFAULT_WIDTH;
  }
}

function readPersistedAssistantPanelOpen(): boolean | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(ASSISTANT_PANEL_OPEN_STORAGE_KEY);
    return raw === "true" ? true : raw === "false" ? false : null;
  } catch {
    return null;
  }
}

function persistAssistantPanelOpen(open: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ASSISTANT_PANEL_OPEN_STORAGE_KEY, String(open));
  } catch {
    // When localStorage is unavailable, keep only the in-memory value / localStorage 不可用时仅保留内存值
  }
}

interface AppState {
  // Scroll targeting (Agent-triggered)
  scrollTarget: WorkspaceFocusTarget | null;
  triggerScrollTo: (target: WorkspaceFocusTargetInput) => void;
  clearScrollTarget: (requestId?: string) => void;
  playbackStart: PlaybackStartRequest | null;
  requestPlaybackStart: (input: Omit<PlaybackStartRequest, "request_id">) => void;
  clearPlaybackStart: (requestId?: string) => void;
  assistantToolActivitySuppressed: boolean;
  setAssistantToolActivitySuppressed: (suppressed: boolean) => void;

  // Toast: the most recently sent toast; ToastOverlay subscribes to every write, so several sent in the same batch are all shown
  // Toast：最近一次发出的提示；ToastOverlay 订阅每次写入，同一批次连发的几条都会显示
  toast: Toast | null;
  pushToast: (text: string, tone?: Toast["tone"], options?: { action?: ToastAction }) => void;
  pushNotification: (
    text: string,
    tone?: Toast["tone"],
    options?: { target?: WorkspaceNotificationTarget | null },
  ) => void;
  workspaceNotifications: WorkspaceNotification[];
  pushWorkspaceNotification: (input: WorkspaceNotificationInput) => void;
  markWorkspaceNotificationRead: (id: string) => void;
  markAllWorkspaceNotificationsRead: () => void;
  removeWorkspaceNotification: (id: string) => void;
  clearWorkspaceNotifications: () => void;

  // Agent panel: expanded by default; after the user opens/closes it manually (toggleAssistantPanel) the choice is remembered;
  // programmatic opening (setAssistantPanelOpen, e.g. prefilling an instruction into the input) only affects this time and does not override the user's choice.
  // Agent 面板：默认展开，用户手动开合（toggleAssistantPanel）后记住选择；
  // 程序打开（setAssistantPanelOpen，如把指令预填进输入框）只影响本次，不覆盖用户的选择。
  assistantPanelOpen: boolean;
  toggleAssistantPanel: () => void;
  setAssistantPanelOpen: (open: boolean) => void;
  /** Width when expanded; when squeezing the canvas, the actual width is also bounded by the canvas minimum width. / 展开时的宽度；挤压画布时实际宽度还受画布最小宽度约束。 */
  assistantPanelWidth: number;
  setAssistantPanelWidth: (width: number) => void;
  persistAssistantPanelWidth: () => void;
  /** Open state of the top bar's usage popover; the canvas side also opens the panel from here. / 顶栏用量悬浮层的开合；画布侧也从这里打开面板。 */
  usagePanelOpen: boolean;
  setUsagePanelOpen: (open: boolean) => void;

  // Grid list invalidation signal (incremented on grid_ready SSE events)
  gridsRevision: number;
  invalidateGrids: () => void;

  // Project trash invalidation signal: incremented when delete, restore or purge (including Agent-initiated ones) arrive via the project event SSE;
  // the files view refetches the directory tree and the trash section from it.
  // 项目回收站失效信号：删除、恢复、清除（含 Agent 发起的）经项目事件 SSE 推来时自增，
  // 文件视图据此重拉目录树与回收站分区。
  trashRevision: number;
  invalidateTrash: () => void;

  // Video unit invalidation signal: incremented when a reference-to-video task's terminal state arrives via the project event SSE; the reference-to-video
  // canvas refetches units from it, so the final result is visible after generation without a manual refresh.
  // 视频单元失效信号：参考生视频任务终态经项目事件 SSE 推来时自增，参考生
  // 视频画布据此重拉单元，生成完成后无需手动刷新即可看到成片。
  referenceVideoUnitsRevision: number;
  invalidateReferenceVideoUnits: () => void;

  // Entity-scoped invalidation signal for cache-busted asset URLs
  entityRevisions: Record<string, number>;
  invalidateEntities: (keys: string[]) => void;
  invalidateAllEntities: () => void;
  getEntityRevision: (key: string) => number;
}

/**
 * Notification system division of labor:
 *
 * - pushToast(text, tone, { action? })
 *     For: immediate feedback on user-initiated actions. Reversible actions attach an "Undo" action; irreversible ones confirm with AlertDialog first.
 *     Typical: form save/validation, import/delete/switch/upload success, scroll target not found,
 *          background task submitted receipt (task_submitted), synchronous enqueue request failure
 *          (the user is present and can retry right away, not put in the drawer), lightweight error hints.
 *
 * - pushWorkspaceNotification({ text, tone, target })
 *     For: events produced asynchronously in the background, when the user may not be on the current page.
 *     Typical: a trace of a single SSE event (e.g. agent_update_scene).
 *
 * - pushNotification(text, tone, options?)
 *     For: important results the user needs to review later.
 *     Typical: background task (worker) failures: storyboard/video/character/scene/prop/grid/
 *          reference-to-video turning failed, watched centrally by useTaskFailureNotifications with
 *          a clickable jump-back target; JianYing/ZIP export failure, project regenerate failure;
 *          SSE grouped_notification. Note: a synchronous enqueue request failure is immediate feedback, use a toast.
 *
 * Rule of thumb:
 *   1. User not present now -> needs to persist
 *   2. A background task "failure" needs a trace for troubleshooting -> needs to persist
 *   3. Everything else -> toast only
 *
 * pushToast accepts no escape hatch like { persist: true }; call sites must pick one of the three, making intent explicit.
 *
 * 通知系统分工规则：
 *
 * - pushToast(text, tone, { action? })
 *     用于：用户主动操作的即时反馈。可逆操作附「撤销」action；不可逆操作先用 AlertDialog 确认。
 *     典型：表单保存/校验、导入/删除/切换/上传成功、scroll target 未找到、
 *          后台任务提交成功回执（task_submitted）、入队请求同步失败
 *          （用户在场可立即重试，不进 drawer）、轻量错误提示。
 *
 * - pushWorkspaceNotification({ text, tone, target })
 *     用于：后台异步产生的事件，用户可能不在当前页。
 *     典型：SSE 单条事件留痕（如 agent_update_scene）。
 *
 * - pushNotification(text, tone, options?)
 *     用于：用户需要后续回看的重要结果。
 *     典型：后台任务（worker）失败——storyboard/video/character/scene/prop/grid/
 *          参考生视频转入 failed，由 useTaskFailureNotifications 统一监听并附
 *          可点击回跳 target；剪映/ZIP 导出失败、项目 regenerate 失败；
 *          SSE grouped_notification。注意：入队请求同步失败属即时反馈，用 toast。
 *
 * 判断口诀：
 *   1. 用户现在不在场 → 需要持久
 *   2. 后台任务"失败"需要留痕排查 → 需要持久
 *   3. 其余 → 仅 toast
 *
 * pushToast 不接受 { persist: true } 之类逃生门，强制调用点三选一，意图显式。
 */
const MAX_WORKSPACE_NOTIFICATIONS = 40;

function buildWorkspaceNotification(
  input: WorkspaceNotificationInput,
): WorkspaceNotification {
  return {
    id: `${Date.now()}-${Math.random()}`,
    text: input.text,
    tone: input.tone ?? "info",
    created_at: Date.now(),
    read: input.read ?? false,
    target: input.target ?? null,
  };
}

export const useAppStore = create<AppState>((set, get) => ({
  scrollTarget: null,
  triggerScrollTo: (target) =>
    set({
      scrollTarget: {
        request_id: target.request_id ?? `${Date.now()}-${Math.random()}`,
        type: target.type,
        id: target.id,
        route: target.route ?? "",
        highlight: true,
        highlight_style: target.highlight_style ?? "flash",
        expires_at: target.expires_at ?? Date.now() + 3000,
      },
    }),
  clearScrollTarget: (requestId) =>
    set((s) => {
      if (!requestId || s.scrollTarget?.request_id === requestId) {
        return { scrollTarget: null };
      }
      return s;
    }),
  playbackStart: null,
  requestPlaybackStart: (input) => {
    const requestId = `${Date.now()}-${Math.random()}`;
    set({ playbackStart: { ...input, request_id: requestId } });
    // When the unit has no playable video the player does not appear; void it once expired so opening the unit later does not suddenly start playback.
    // 单元没有可播放的视频时播放器不会出现，到期后作废，免得日后打开该单元时突然开播。
    setTimeout(() => get().clearPlaybackStart(requestId), PLAYBACK_START_TTL_MS);
  },
  clearPlaybackStart: (requestId) =>
    set((s) => {
      if (!requestId || s.playbackStart?.request_id === requestId) {
        return { playbackStart: null };
      }
      return s;
    }),
  assistantToolActivitySuppressed: false,
  setAssistantToolActivitySuppressed: (suppressed) =>
    set({ assistantToolActivitySuppressed: suppressed }),

  toast: null,
  pushToast: (text, tone = "info", options) =>
    set({
      toast: { id: `${Date.now()}-${Math.random()}`, text, tone, action: options?.action },
    }),
  pushNotification: (text, tone = "info", options) =>
    set((s) => ({
      toast: { id: `${Date.now()}-${Math.random()}`, text, tone },
      workspaceNotifications: [
        buildWorkspaceNotification({ text, tone, target: options?.target ?? null }),
        ...s.workspaceNotifications,
      ].slice(0, MAX_WORKSPACE_NOTIFICATIONS),
    })),
  workspaceNotifications: [],
  pushWorkspaceNotification: (input) =>
    set((s) => ({
      workspaceNotifications: [
        buildWorkspaceNotification(input),
        ...s.workspaceNotifications,
      ].slice(0, MAX_WORKSPACE_NOTIFICATIONS),
    })),
  markWorkspaceNotificationRead: (id) =>
    set((s) => ({
      workspaceNotifications: s.workspaceNotifications.map((item) =>
        item.id === id ? { ...item, read: true } : item
      ),
    })),
  markAllWorkspaceNotificationsRead: () =>
    set((s) => ({
      workspaceNotifications: s.workspaceNotifications.map((item) =>
        item.read ? item : { ...item, read: true }
      ),
    })),
  removeWorkspaceNotification: (id) =>
    set((s) => ({
      workspaceNotifications: s.workspaceNotifications.filter((item) => item.id !== id),
    })),
  clearWorkspaceNotifications: () => set({ workspaceNotifications: [] }),

  assistantPanelOpen: readPersistedAssistantPanelOpen() ?? true,
  toggleAssistantPanel: () =>
    set((s) => {
      const open = !s.assistantPanelOpen;
      persistAssistantPanelOpen(open);
      return { assistantPanelOpen: open };
    }),
  setAssistantPanelOpen: (open) => set({ assistantPanelOpen: open }),
  assistantPanelWidth: readPersistedAssistantPanelWidth(),
  setAssistantPanelWidth: (width) =>
    set({ assistantPanelWidth: clampAssistantPanelWidth(width) }),
  persistAssistantPanelWidth: () => {
    if (typeof window === "undefined") return;
    const width = clampAssistantPanelWidth(get().assistantPanelWidth);
    try {
      window.localStorage.setItem(
        ASSISTANT_PANEL_WIDTH_STORAGE_KEY,
        String(width),
      );
    } catch {
      // localStorage unavailable (private mode / quota exceeded) fails silently; the in-memory value still applies
      // localStorage 不可用（隐私模式 / quota exceeded）静默失败，内存值仍生效
    }
  },
  usagePanelOpen: false,
  setUsagePanelOpen: (open) => set({ usagePanelOpen: open }),

  gridsRevision: 0,
  invalidateGrids: () => set((s) => ({ gridsRevision: s.gridsRevision + 1 })),

  trashRevision: 0,
  invalidateTrash: () => set((s) => ({ trashRevision: s.trashRevision + 1 })),

  referenceVideoUnitsRevision: 0,
  invalidateReferenceVideoUnits: () =>
    set((s) => ({ referenceVideoUnitsRevision: s.referenceVideoUnitsRevision + 1 })),

  entityRevisions: {},
  invalidateEntities: (keys) =>
    set((s) => {
      const normalizedKeys = [...new Set(keys.filter(Boolean))];
      if (normalizedKeys.length === 0) {
        return s;
      }

      const entityRevisions = { ...s.entityRevisions };
      for (const key of normalizedKeys) {
        entityRevisions[key] = (entityRevisions[key] ?? 0) + 1;
      }
      return { entityRevisions };
    }),
  invalidateAllEntities: () =>
    set((s) => ({
      entityRevisions: {
        ...s.entityRevisions,
        [ALL_ENTITIES_REVISION_KEY]:
          (s.entityRevisions[ALL_ENTITIES_REVISION_KEY] ?? 0) + 1,
      },
    })),
  getEntityRevision: (key) => {
    const entityRevisions = get().entityRevisions;
    return (
      (entityRevisions[key] ?? 0) +
      (entityRevisions[ALL_ENTITIES_REVISION_KEY] ?? 0)
    );
  },
}));
