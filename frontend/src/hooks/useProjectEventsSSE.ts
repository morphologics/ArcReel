import { startTransition, useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "wouter";
import { API } from "@/api";
import { useHasUnsavedChanges } from "@/components/shared/edit-unit/LeaveGuard";
import type { SseStreamHandle } from "@/utils/sse-stream";
import { useAppStore } from "@/stores/app-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { useProjectsStore, type RefreshProjectResult } from "@/stores/projects-store";
import { useCostStore } from "@/stores/cost-store";
import { SCRIPT_PLAN_TASK_TYPES, useTasksStore } from "@/stores/tasks-store";
import { useUsageHeaderStore } from "@/stores/usage-header-store";
import { errMsg } from "@/utils/async";
import {
  WORKSPACE_ROUTE_CHARACTERS,
  WORKSPACE_ROUTE_EPISODES,
  WORKSPACE_ROUTE_PRODUCTS,
  WORKSPACE_ROUTE_PROPS,
  WORKSPACE_ROUTE_SCENES,
} from "@/app-routes";
import type {
  ProjectChange,
  ProjectChangeBatchPayload,
  ProjectChangeFocus,
  ProjectChangePane,
  WorkspaceNotificationTarget,
} from "@/types";
import {
  buildEntityRevisionKey,
  COMPLETION_ACTIONS,
  formatGroupedDeferredText,
  formatGroupedNotificationText,
  groupChangesByType,
  type GroupedProjectChange,
} from "@/utils/project-changes";


/** Asset entities usable as video unit reference images: their create/update/delete changes a unit's available reference images and the bucket it lands in. / 可作视频单元参考图的资产实体：它们的增删改会改变单元的可用参考图与所落的桶。 */
const REFERENCE_ASSET_ENTITY_TYPES: ReadonlySet<ProjectChange["entity_type"]> = new Set([
  "character",
  "scene",
  "prop",
  "product",
]);

const CHANGE_PRIORITY: Record<string, number> = {
  "segment:updated": 0,
  // updated for drama/ad/reference-to-video skeleton entries has the same priority as narration shots, so the four skeletons sort notifications the same way.
  // drama/ad/参考生视频骨架条目的 updated 与 narration 分镜同优先级，四种骨架通知排序一致。
  "drama_scene:updated": 0,
  "shot:updated": 0,
  "reference_unit:updated": 0,
  "character:created": 1,
  "character:updated": 2,
  "scene:created": 3,
  "scene:updated": 3.5,
  "prop:created": 4,
  "prop:updated": 4.5,
  "product:created": 4.6,
  "product:updated": 4.8,
  "episode:created": 5,
  "episode:updated": 6,
  "draft:created": 6.5,
  storyboard_ready: 7,
  video_ready: 8,
  grid_ready: 9,
  grid_split_done: 9.5,
  reference_video_ready: 10,
  tts_ready: 11,
  voice_sample_ready: 12,
};

/** Task final-state change (a refresh signal, not a project entity change). / 任务终态变更（刷新信号，非项目实体变更）。 */
function isTaskChange(change: ProjectChange): boolean {
  return change.entity_type === "task";
}

/** Billing settlement change (a refresh signal, not a project entity change). / 记账结算变更（刷新信号，非项目实体变更）。 */
function isUsageRecordChange(change: ProjectChange): boolean {
  return change.entity_type === "usage_record";
}

/** Project trash change (a refresh signal, not a project entity change). / 项目回收站变化（刷新信号，非项目实体变更）。 */
function isTrashChange(change: ProjectChange): boolean {
  return change.entity_type === "trash";
}

function getChangePriority(change: ProjectChange): number {
  if (COMPLETION_ACTIONS.has(change.action)) {
    return CHANGE_PRIORITY[change.action] ?? Number.MAX_SAFE_INTEGER;
  }
  return CHANGE_PRIORITY[`${change.entity_type}:${change.action}`] ?? Number.MAX_SAFE_INTEGER;
}

function isNavigableChange(change: ProjectChange): boolean {
  if (COMPLETION_ACTIONS.has(change.action)) {
    return false;
  }
  return Boolean(change.focus?.anchor_type && change.focus?.anchor_id);
}

/** Focus pane -> workspace route, exhaustive over the pane union type; returns null when the event lacks info needed for routing and can't navigate. / 定位窗格 → 工作区路由，按窗格联合类型穷尽；返回 null 表示该事件缺少路由所需信息、不可导航。 */
const PANE_ROUTES: Record<ProjectChangePane, (focus: ProjectChangeFocus) => string | null> = {
  characters: () => `/${WORKSPACE_ROUTE_CHARACTERS}`,
  scenes: () => `/${WORKSPACE_ROUTE_SCENES}`,
  props: () => `/${WORKSPACE_ROUTE_PROPS}`,
  products: () => `/${WORKSPACE_ROUTE_PRODUCTS}`,
  episode: (focus) =>
    typeof focus.episode === "number" ? `/${WORKSPACE_ROUTE_EPISODES}/${focus.episode}` : null,
};

/** Entity ID suffix subscribed by draft views: `draft:episode_N_<kind>`. / 草稿视图订阅的实体 ID 后缀：`draft:episode_N_<kind>`。 */
const DRAFT_DOC_KINDS = ["script_plan", "prompt_authoring"] as const;

function buildNotificationTarget(change: ProjectChange): WorkspaceNotificationTarget | null {
  const focus = change.focus;
  if (!focus?.anchor_type || !focus.anchor_id) return null;

  const route = PANE_ROUTES[focus.pane]?.(focus);
  if (!route) return null;

  return {
    type: focus.anchor_type,
    id: focus.anchor_id,
    route,
    highlight_style: "flash",
  };
}

function getGroupPriority(group: GroupedProjectChange): number {
  return Math.min(
    ...group.changes.map((change) => getChangePriority(change)),
  );
}

function sortGroupedChanges(
  groups: GroupedProjectChange[],
): GroupedProjectChange[] {
  return [...groups].sort(
    (left, right) => getGroupPriority(left) - getGroupPriority(right),
  );
}

function hasImportantChanges(group: GroupedProjectChange): boolean {
  return group.changes.some((change) => change.important);
}

function getPrimaryGroupTarget(
  group: GroupedProjectChange,
): WorkspaceNotificationTarget | null {
  const primaryChange =
    group.changes.find((change) => isNavigableChange(change)) ?? null;
  return primaryChange ? buildNotificationTarget(primaryChange) : null;
}

/**
 * While the user is editing, automatic focus and navigation from Agent changes never run: focus is in an input control, a legacy card is marked editing,
 * or an edit unit holds unsaved changes (shot details, video unit body, etc. aren't judged by focus; switching away triggers the leave guard).
 *
 * 用户正在编辑时，Agent 改动带来的自动定位与跳转一律不执行：焦点在输入控件里、旧式卡片标了编辑中，
 * 或有编辑单元带着未保存修改（分镜详情、视频单元正文等不靠焦点判断，切走会触发离开拦截）。
 */
function isWorkspaceEditing(hasUnsavedChanges: () => boolean): boolean {
  if (hasUnsavedChanges()) return true;
  const active = document.activeElement;
  if (active instanceof HTMLElement) {
    const tagName = active.tagName.toLowerCase();
    if (tagName === "input" || tagName === "textarea" || tagName === "select") {
      return true;
    }
    if (active.isContentEditable) {
      return true;
    }
  }
  return Boolean(document.querySelector("[data-workspace-editing='true']"));
}

export function useProjectEventsSSE(projectName?: string | null): void {
  const { t } = useTranslation("dashboard");
  // Expose t to the callback via ref, so an i18n language switch doesn't rebuild refreshProject
  // -> the event stream effect reconnecting -> notifications/focus hints being lost.
  // 把 t 通过 ref 暴露给 callback，避免 i18n 切语言时 refreshProject
  // 重建 → 事件流 effect 跟着重连 → 通知/focus 提示丢失。
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);
  const { t: tEvents } = useTranslation("events");
  const tEventsRef = useRef(tEvents);
  useEffect(() => {
    tEventsRef.current = tEvents;
  }, [tEvents]);
  const [, setLocation] = useLocation();
  const invalidateEntities = useAppStore((s) => s.invalidateEntities);
  const triggerScrollTo = useAppStore((s) => s.triggerScrollTo);
  const clearScrollTarget = useAppStore((s) => s.clearScrollTarget);
  const pushNotification = useAppStore((s) => s.pushNotification);
  const pushWorkspaceNotification = useAppStore((s) => s.pushWorkspaceNotification);
  const clearWorkspaceNotifications = useAppStore((s) => s.clearWorkspaceNotifications);
  const setAssistantToolActivitySuppressed = useAppStore(
    (s) => s.setAssistantToolActivitySuppressed
  );

  const hasUnsavedChanges = useHasUnsavedChanges();

  const sourceRef = useRef<SseStreamHandle | null>(null);
  const lastFingerprintRef = useRef<string | null>(null);
  const queuedFocusRef = useRef<WorkspaceNotificationTarget | null>(null);

  const executeFocus = useCallback(
    (target: WorkspaceNotificationTarget) => {
      startTransition(() => {
        setLocation(target.route);
      });
      triggerScrollTo({
        type: target.type,
        id: target.id,
        route: target.route,
        highlight_style: target.highlight_style ?? "flash",
        expires_at: Date.now() + 3000,
      });
    },
    [setLocation, triggerScrollTo],
  );

  const flushQueuedFocus = useCallback(() => {
    const target = queuedFocusRef.current;
    if (!target) return;
    queuedFocusRef.current = null;
    if (isWorkspaceEditing(hasUnsavedChanges)) {
      return;
    }
    executeFocus(target);
  }, [executeFocus, hasUnsavedChanges]);

  const refreshProject = useCallback(async (): Promise<RefreshProjectResult> => {
    if (!projectName) return "cancelled";
    // The in-flight merge logic (single-flight + queue one more run + keep old on failure) has moved down into projects-store.refreshProject;
    // only the SSE-specific wrapper stays here: warn on failure, and consume the queued focus target once the refresh settles.
    // refreshProject now resolves per round (see projects-store), so when this call settles,
    // a later batch of onChanges may already have rewritten queuedFocusRef to its own target — the data for that new target
    // is only written to the store once its own getProject round finishes. Consuming the ref unconditionally would navigate/scroll early with
    // a target not yet persisted, and the ref would be cleared after consumption with no retry after that batch; this
    // risk isn't limited to "this call set a new target itself" — calls that set no new target (onSnapshot,
    // webui source, the draftHandled branch) may likewise have the ref rewritten by other calls while waiting to settle.
    // So instead snapshot the ref before issuing the request, and after settling consume it only if the ref still equals the snapshot — meaning during this
    // wait no other call rewrote it, so it can safely be treated as "matching this round"; if the ref changed, skip,
    // leaving it to the call that rewrote it to consume after its own round settles.
    // 在途合并逻辑（单飞 + 排队再跑一轮 + 失败留旧）已下沉到 projects-store.refreshProject；
    // 此处只保留 SSE 专属包装：失败时告警、刷新落定后消费排队的聚焦目标。
    //
    // refreshProject 现在按轮次各自 resolve（见 projects-store），因此本次调用落定时，
    // 可能已有更晚一批 onChanges 把 queuedFocusRef 改写为它自己的目标——那个新目标
    // 对应的数据要等它自己那一轮 getProject 完成才会写入 store。无条件消费 ref 会拿着
    // 尚未落库的目标提前导航/滚动，且消费后 ref 被清空，那一批之后也不会再重试；这个
    // 风险不局限于「本次调用自己设置了新目标」的场景——不设置新目标的调用（onSnapshot、
    // webui 来源、draftHandled 分支）同样可能在等待落定期间被别的调用改写 ref。
    //
    // 因此改为在发起请求前对 ref 拍快照，落定后只在 ref 仍等于快照时才消费——说明这段
    // 等待期间没有别的调用改写过它，可以放心视为「跟自己这一轮对应」；ref 已变则跳过，
    // 交由改写它的那次调用在自己对应轮次落定后消费。
    const focusSnapshot = queuedFocusRef.current;
    const result = await useProjectsStore.getState().refreshProject(projectName, {
      onError: (err) =>
        pushNotification(tRef.current("project_sync_failed", { message: errMsg(err) }), "warning"),
    });
    if (queuedFocusRef.current === focusSnapshot) {
      flushQueuedFocus();
    }
    return result;
  }, [flushQueuedFocus, projectName, pushNotification]);

  useEffect(() => {
    lastFingerprintRef.current = null;
    queuedFocusRef.current = null;
    clearScrollTarget();
    clearWorkspaceNotifications();
    return () => {
      queuedFocusRef.current = null;
      clearScrollTarget();
      clearWorkspaceNotifications();
    };
  }, [clearScrollTarget, clearWorkspaceNotifications, projectName]);

  useEffect(() => {
    if (!projectName) return;
    let disposed = false;
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }

    // Reconnection is handled by the streaming client; after reconnect the server resends the snapshot, and the project is refreshed only if the fingerprint changed.
    // 断线重建由流式客户端承担；重建后服务端重发 snapshot，fingerprint 变了才刷新项目。
    const source = API.openProjectEventStream({
      projectName,
      onSnapshot(payload) {
        if (disposed) return;
        const previousFingerprint = lastFingerprintRef.current;
        lastFingerprintRef.current = payload.fingerprint;
        if (previousFingerprint && previousFingerprint !== payload.fingerprint) {
          // Draft views fetch their own data and re-fetch only on draft events; draft changes missed during disconnection are all invalidated here.
          // 草稿视图各自取数、只随 draft 事件重拉；断线期间错过的草稿变化在这里一并作废。
          const episodes = useProjectsStore.getState().currentProjectData?.episodes ?? [];
          invalidateEntities(
            episodes.flatMap(({ episode }) =>
              DRAFT_DOC_KINDS.map((kind) => buildEntityRevisionKey("draft", `episode_${episode}_${kind}`)),
            ),
          );
          void refreshProject();
        }
        // A snapshot arrives on every connection. Every one other than the first means the line dropped once: settlement and
        // task final-state events during the disconnection were missed, and top-bar usage refreshes on events, so do a full re-fetch here.
        // 快照在每次建连时到达。首次之外的每一次都意味着断过一次线：断线期间的结算与
        // 任务终态事件已经错过，顶栏用量按事件刷新，故在此补一次全量重取。
        if (previousFingerprint) {
          void useUsageHeaderStore.getState().refresh();
          // The trash isn't in the snapshot fingerprint; additions and deletions during disconnection can likewise only be caught up here.
          // 回收站不在快照指纹里，断线期间的增删同样只能在这里补拉。
          useAppStore.getState().invalidateTrash();
        }
        // Session-resume notifications are pushed once; those sent before the subscription was established (including failed retries before the first connection) are missed;
        // the first snapshot must also check: the state read by the session hook's load may predate this resume.
        // 会话恢复通知只推一次，订阅建立之前（含首次建连前的失败重试）发出的已经错过；
        // 首次快照也要核对：会话 hook 的加载读到的状态可能早于这次恢复。
        useAssistantStore.getState().requestSessionResync(projectName);
      },
      onChanges(payload: ProjectChangeBatchPayload) {
        if (disposed) return;
        lastFingerprintRef.current = payload.fingerprint;
        setAssistantToolActivitySuppressed(true);

        // Task final states are refresh signals, not entity changes, so separate them from entity changes first: the entity invalidation, grouped notifications and
        // focus navigation below only target entity changes. Mixing in task changes does two real harms — every final-state task would leave
        // a never-consumed `task:<uuid>` key in `entityRevisions` (the table isn't cleared on project switch,
        // and is copied whole on every invalidation, growing unbounded in long sessions); and task batches have no navigable target, so reaching the focus logic
        // would reset `queuedFocusRef`, where a previous batch of entity changes queued a target waiting for refreshProject, to null, and the user
        // would lose the automatic navigation and highlight that should have happened.
        // 任务终态是刷新信号而非实体变更，先与实体变更分开：下方的实体失效、分组通知与
        // 聚焦跳转都只针对实体变更。混入任务变更会有两处实际损害——每个终态任务都会在
        // `entityRevisions` 里留下一个从未被消费的 `task:<uuid>` 键（该表不随切项目清空，
        // 且每次失效整表复制，长会话下无界增长）；而任务批次没有可导航目标，走到聚焦逻辑
        // 会把前一批实体变更排队等待 refreshProject 的 `queuedFocusRef` 清成 null，用户
        // 因此丢失本该发生的自动导航与高亮。
        const taskChanges = payload.changes.filter(isTaskChange);
        // Billing settlements, like task final states, are refresh signals: mixed into entity changes they would leave never-
        // consumed `usage_record:<id>` keys in entityRevisions and clear the focus target queued by a previous batch of entity changes.
        // 记账结算与任务终态同为刷新信号：混进实体变更会在 entityRevisions 里留下永不
        // 被消费的 `usage_record:<id>` 键，并把前一批实体变更排队的聚焦目标清空。
        const entityChanges = payload.changes.filter(
          (c) => !isTaskChange(c) && !isUsageRecordChange(c) && !isTrashChange(c),
        );
        if (payload.changes.some(isTrashChange)) {
          useAppStore.getState().invalidateTrash();
        }

        // Extract and update asset fingerprints (zero delay, written to the store immediately) / 提取并更新 asset fingerprints（零延迟，立即写入 store）
        const mergedFingerprints: Record<string, number> = {};
        for (const change of entityChanges) {
          if (change.asset_fingerprints) {
            Object.assign(mergedFingerprints, change.asset_fingerprints);
          }
        }
        if (Object.keys(mergedFingerprints).length > 0) {
          useProjectsStore.getState().updateAssetFingerprints(mergedFingerprints);
        }

        const invalidationKeys = entityChanges.map((change) =>
          buildEntityRevisionKey(change.entity_type, change.entity_id),
        );
        // The final script plan isn't in the project snapshot, so finishing a replan produces no entity change; invalidate each episode's
        // script plan view on task final states, and the content review page re-fetches accordingly. Task events carry no episode number, so invalidate per episode; only mounted views issue requests.
        // 正式脚本规划不在项目快照里，重新规划完成不会产生实体变更；按任务终态作废各集的
        // 脚本规划视图，内容确认页据此重拉。任务事件不带集号，逐集作废，只有挂载的视图会发请求。
        if (taskChanges.some((c) => c.action === "task_succeeded" && SCRIPT_PLAN_TASK_TYPES.has(c.task_type ?? ""))) {
          const episodes = useProjectsStore.getState().currentProjectData?.episodes ?? [];
          invalidationKeys.push(
            ...episodes.map(({ episode }) => buildEntityRevisionKey("draft", `episode_${episode}_script_plan`)),
          );
        }
        // Draft repair is written back to the draft or adopted as final content by the worker, also without entity changes; invalidate each episode's draft view on final states.
        // 草稿 AI 修复由 worker 写回草稿或采用为正式内容，同样没有实体变更；按终态作废各集的草稿视图。
        if (
          taskChanges.some(
            (c) =>
              (c.action === "task_succeeded" || c.action === "task_failed" || c.action === "task_cancelled") &&
              c.task_type === "text_draft_repair",
          )
        ) {
          const episodes = useProjectsStore.getState().currentProjectData?.episodes ?? [];
          invalidationKeys.push(
            ...episodes.flatMap(({ episode }) =>
              DRAFT_DOC_KINDS.map((kind) => buildEntityRevisionKey("draft", `episode_${episode}_${kind}`)),
            ),
          );
        }
        if (invalidationKeys.length > 0) {
          invalidateEntities(invalidationKeys);
        }

        const groupedChanges = sortGroupedChanges(
          groupChangesByType(entityChanges),
        );

        const notifyChanges = entityChanges.length > 0 && payload.source !== "webui";
        const workspaceNotifications: { group: (typeof groupedChanges)[number]; target: WorkspaceNotificationTarget }[] =
          [];

        if (notifyChanges) {
          // Draft event — auto-navigate to the episode's script plan tab / Draft 事件 — 自动导航到剧集脚本规划 Tab
          let draftHandled = false;
          for (const change of entityChanges) {
            if (
              change.entity_type === "draft" &&
              change.action === "created" &&
              typeof change.episode === "number" &&
              !isWorkspaceEditing(hasUnsavedChanges)
            ) {
              startTransition(() => {
                setLocation(`/episodes/${change.episode}`);
              });
              draftHandled = true;
              break;
            }
          }

          if (!draftHandled) {
            const nextFocusTarget =
              groupedChanges
                .map((group) => {
                  const target = getPrimaryGroupTarget(group);
                  if (!target) {
                    return null;
                  }
                  workspaceNotifications.push({ group, target });
                  return target;
                })
                .find(Boolean) ?? null;

            queuedFocusRef.current = isWorkspaceEditing(hasUnsavedChanges) ? null : nextFocusTarget;
          }
        }

        // Task final state: re-fetch the task list and stats immediately, without waiting for the fallback poll interval.
        // 任务终态：立即重拉任务列表与统计，不等兜底轮询的间隔。
        if (taskChanges.length > 0) {
          void useTasksStore.getState().refreshTasks();
        }

        // Unit create/update/delete may come from the Agent or another browser; generation completion changes the final cut; asset create/update/delete changes
        // the reference images a unit can use right now, and the server's per-unit buckets and duration options change with it. All three kinds of events must invalidate
        // reference-video-store's separate list cache, incremented only once per batch.
        // Unit 增删改可能来自 Agent 或另一浏览器；生成完成则改变成片；资产增删改会改变
        // 单元此刻可用的参考图，服务端逐单元的桶与档位随之变化。三类事件都要作废
        // reference-video-store 的独立列表缓存，同一批只自增一次。
        if (
          entityChanges.some(
            (c) => c.entity_type === "reference_unit" || REFERENCE_ASSET_ENTITY_TYPES.has(c.entity_type),
          ) ||
          taskChanges.some(
            (c) => c.action === "task_succeeded" && c.task_type === "reference_video",
          )
        ) {
          useAppStore.getState().invalidateReferenceVideoUnits();
        }

        // Re-fetch the project only when there are entity changes: task final states and billing settlements are refresh signals that don't change project snapshot content;
        // changes persisted during the reconnect window but not described by any in-flight batch are rescanned by the backend and broadcast separately.
        // Episode titles and order positions in notifications come from the refreshed ledger: episodes the Agent created, renamed or reordered aren't
        // in the local ledger yet when the event arrives, and wording from the old ledger would show an unnamed episode or the old title, unchangeable once pushed.
        // No wording when the refresh failed: the old script doesn't have newly created entries yet and would describe them as "deleted"; sync failure is warned about separately.
        // 只在含实体变更时重拉项目：任务终态与记账结算是刷新信号，不改项目快照里的内容；
        // 重建窗口内落盘、却未被任何在途批次描述的变更，后端会补扫后单独广播。
        // 通知里的集标题与播出位置取刷新后的账本：Agent 新建、改名或调序的集在事件到达时
        // 还不在本地账本里，按旧账本成文会显示成未命名集或旧标题，推出后无法再改。
        // 刷新没有成功时不成文：旧剧本里还没有新建的条目，会被写成「已删除」；同步失败已另行告警。
        if (entityChanges.length > 0) {
          void refreshProject().then((result) => {
            if (disposed || !notifyChanges || result !== "success") return;
            const { currentProjectData, currentScripts } = useProjectsStore.getState();
            const episodeLedger = currentProjectData?.episodes ?? [];
            for (const group of groupedChanges) {
              if (!hasImportantChanges(group)) {
                continue;
              }
              pushNotification(
                formatGroupedNotificationText(group, tEventsRef.current, episodeLedger, currentScripts),
                "success",
              );
            }
            for (const { group, target } of workspaceNotifications) {
              pushWorkspaceNotification({
                text: formatGroupedDeferredText(group, tEventsRef.current, episodeLedger, currentScripts),
                target,
              });
            }
          });
        }

        // Refresh cost data when generation completes
        const hasCompletionEvent = entityChanges.some((c) =>
          COMPLETION_ACTIONS.has(c.action),
        );
        // voice_sample billing happens after synthesis succeeds and before duration/size validation (or user cancellation):
        // failing validation lands failed, cancellation during execution lands cancelled; neither triggers the
        // voice_sample_ready above (emitted only in emit_generation_success_batch together with the executor's successful return),
        // but the provider cost has already been incurred — non-success final states must also refresh costs, otherwise the user sees
        // a task that really cost money while the cost panel isn't updated.
        // voice_sample 的计费发生在合成成功之后、时长/大小校验（或用户取消）之前：
        // 校验不通过落 failed、执行期间被取消落 cancelled，两种情形都不会触发上面的
        // voice_sample_ready（只在 emit_generation_success_batch 里跟着执行器成功返回
        // 一起发），但供应商费用已经产生——非成功终态同样要刷新成本，否则用户会看到
        // 一个已经真实花费但成本面板未更新的任务。
        const hasBilledVoiceSampleTerminal = taskChanges.some(
          (c) =>
            (c.action === "task_failed" || c.action === "task_cancelled") &&
            c.task_type === "voice_sample",
        );
        // The split itself isn't billed, but it writes each shot's generated_assets.grid_id back, so the grid's incurred
        // cost moves from the "unattributed" bucket into this episode's apportionment (ADR 0053). Attribution changed, so re-fetch, otherwise the cost panel
        // keeps showing the pre-split allocation; grid_split_done isn't in COMPLETION_ACTIONS (that is the completion notification
        // category, and a split isn't a generation), so it's listed separately here.
        // 切分本身不计费，但它把各分镜的 generated_assets.grid_id 写回，宫格已发生的
        // 成本随之从「未归属」桶转入本集均摊（ADR 0053）。归属变了就要重拉，否则成本面板
        // 一直显示切分前的分配；grid_split_done 不进 COMPLETION_ACTIONS（那是完成通知
        // 类别，切分不是一次生成），故在这里单列。
        const hasGridSplit = entityChanges.some((c) => c.action === "grid_split_done");
        // Persisting one provider call settlement (success/failure/cancel) is a cost change; task-less text calls and
        // assistant sessions have only this signal, with no task final state or completion notification to rely on.
        // 一次供应商调用结算落库（成功/失败/取消）就是一笔费用变动；无任务的文本调用与
        // 助手会话只有这一个信号，没有对应的任务终态或完成通知可依赖。
        const hasUsageRecord = payload.changes.some(isUsageRecordChange);
        // Top-bar usage recognizes only these two signals: billing settlements change amounts and the finished list, task final states change the in-progress list.
        // 顶栏用量只认这两个信号：记账结算改金额与已结束列表，任务终态改进行中列表。
        if (hasUsageRecord || taskChanges.length > 0) {
          void useUsageHeaderStore.getState().refresh();
        }
        if (
          (hasCompletionEvent || hasBilledVoiceSampleTerminal || hasGridSplit || hasUsageRecord) &&
          projectName
        ) {
          useCostStore.getState().debouncedFetch(projectName);
        }

        // Refresh grid list when a grid completes or gets split into cells
        if (entityChanges.some((c) => c.action === "grid_ready" || c.action === "grid_split_done")) {
          useAppStore.getState().invalidateGrids();
        }
      },
      onAssistantSessionResumed(payload) {
        if (disposed) return;
        useAssistantStore.getState().notifySessionResumed(payload.project_name, payload.session_id);
      },
      onProjectDeleted() {
        if (disposed) return;
        // The project directory was deleted: the backend closes the stream normally and the close handle stops auto-reconnect — no periodic requests for a deleted project.
        // 项目目录已被删除：后端正常关流，关闭句柄停止自动重建——不对已删项目周期性发起请求。
        if (sourceRef.current) {
          sourceRef.current.close();
          sourceRef.current = null;
        }
      },
    });
    sourceRef.current = source;

    return () => {
      disposed = true;
      if (sourceRef.current) {
        sourceRef.current.close();
        sourceRef.current = null;
      }
    };
  }, [
    clearWorkspaceNotifications,
    hasUnsavedChanges,
    invalidateEntities,
    projectName,
    pushNotification,
    pushWorkspaceNotification,
    refreshProject,
    setAssistantToolActivitySuppressed,
    setLocation,
  ]);
}
