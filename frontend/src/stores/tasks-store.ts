import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import type { TaskItem, TaskStats, TaskStatus } from "@/types";

/** Maximum number of tasks fetched per refresh. / 单次刷新拉取的任务条数上限。 */
const TASKS_PAGE_SIZE = 200;

/**
 * All resource kinds the occupancy check cares about (excluding image_edit, which is classified into one of them by resource_type).
 *
 * The read and write sides of the occupancy set share this one source of truth: the write side {@link TasksState.beginOptimisticActive} marks,
 * the read side {@link selectActiveResourceIds} filters, and the kind spelling on both sides must match to hit the same slot.
 * Narrowing it to a union type moves the "misspelled means silently not occupied" failure mode to compile time.
 *
 * 占用判定关心的全量资源种类（不含 image_edit——它按 resource_type 归入其中之一）。
 *
 * 占用集的读写两侧共用这一个真相源：写侧 {@link TasksState.beginOptimisticActive} 打标、
 * 读侧 {@link selectActiveResourceIds} 过滤，两侧的 kind 拼写必须一致才能匹配上同一个槽。
 * 收紧为联合类型即为把「拼错就静默不占用」的失败模式提前到编译期。
 */
export type ResourceKind =
  | "character"
  | "character_derivative"
  | "scene"
  | "prop"
  | "product"
  | "storyboard"
  | "video"
  | "tts"
  | "reference_video"
  | "grid"
  | "text_episode_script"
  | "text_episode_plan"
  | "text_script_plan"
  | "text_draft_repair";

/** Resource kinds that support instruction editing; `image_edit` tasks are classified into the matching resource slot by this. / 可做指令式编辑的资源种类；`image_edit` 任务按此归入对应资源槽。 */
export type ImageEditResourceKind = Extract<
  ResourceKind,
  "character" | "character_derivative" | "scene" | "prop" | "product" | "storyboard"
>;

/**
 * Refresh scope: `projectName === null` means "do not filter by project" (fetch global tasks), and the whole scope being
 * `null` means "disabled": then {@link TasksState.refreshTasks} sends no request. The scope is registered at a single point by
 * `useTaskRefresh`; other entries (task terminal states pushed by the project event SSE) only call refreshTasks
 * without each deciding which project to fetch, so the global task list is not overwritten by project-filtered results.
 *
 * 刷新作用域：`projectName === null` 表示「不按项目过滤」（拉全局任务），整个 scope 为
 * `null` 表示「未启用」——此时 {@link TasksState.refreshTasks} 不发请求。作用域由
 * `useTaskRefresh` 单点登记，其余入口（项目事件 SSE 推来的任务终态）只调 refreshTasks、
 * 不各自决定拉哪个项目，避免全局任务列表被项目过滤结果覆盖。
 */
export interface TasksRefreshScope {
  projectName: string | null;
}

/**
 * Optimistic occupancy handle for one enqueue submit. Obtained by the enqueue action layer **before the request is sent**, and fulfilled one of two ways when the request ends:
 *
 * - `settle(taskIds)`: the backend created (or dedup-matched) task rows; the mark switches to waiting for these task_ids to land in the DB;
 *   an empty array means this time produced no task rows, equivalent to `rollback()` (otherwise the mark would wait forever for real rows).
 * - `rollback()`: the request failed; revoke the mark.
 *
 * Before fulfillment the mark is "in flight" and is not cleared by any poll write-back: this is exactly the request round-trip window,
 * which each caller used to need its own in-flight ref to cover.
 *
 * 一次入队提交的乐观占用句柄。由入队动作层在**请求发出前**取得，请求结束时二选一兑现：
 *
 * - `settle(taskIds)` —— 后端已建（或去重命中）任务行，标记转为等待这些 task_id 落库；
 *   传空数组表示本次没有产生任何任务行，等同 `rollback()`（否则标记永远等不到真实行）。
 * - `rollback()` —— 请求失败，撤销标记。
 *
 * 未兑现前标记处于「在途」态，不被任何轮询写回清除——这段正是请求往返窗口，
 * 各调用方过去要自备 in-flight ref 才能覆盖。
 */
export interface OptimisticHandle {
  settle: (taskIds: string[]) => void;
  rollback: () => void;
}

interface TasksState {
  tasks: TaskItem[];
  stats: TaskStats;
  connected: boolean;
  refreshScope: TasksRefreshScope | null;
  /** Optimistic occupancy marks; see the optimistic occupancy subsection of {@link selectActiveResourceIds} below. / 乐观占用标记，见下方 {@link selectActiveResourceIds} 的乐观占用小节。 */
  optimisticActive: Set<string>;
  /** Optimistic occupancy marks (scriptFile granularity); see the optimistic occupancy subsection of {@link selectHasActiveTaskForScriptFile} below. / 乐观占用标记（scriptFile 粒度），见下方 {@link selectHasActiveTaskForScriptFile} 的乐观占用小节。 */
  optimisticActiveScriptFile: Set<string>;

  // Actions
  setTasks: (tasks: TaskItem[] | ((prev: TaskItem[]) => TaskItem[])) => void;
  setStats: (stats: TaskStats) => void;
  setConnected: (connected: boolean) => void;
  setRefreshScope: (scope: TasksRefreshScope | null) => void;
  /**
   * Fetch the task list and stats of the current scope and write them back to the store.
   *
   * Shared by several entries (the polling timer + task terminal states pushed by the project event SSE), so consolidated into a single action that
   * merges in-flight work internally: when a refresh is already in flight, new requests merge into "run one more round afterwards", and each caller settles
   * its own round. Otherwise two entries refreshing at once would interleave write-backs, and an old result sent earlier but arriving later would overwrite the new one.
   *
   * 拉取当前作用域的任务列表与统计并写回 store。
   *
   * 多入口共享（轮询定时器 + 项目事件 SSE 推来的任务终态）故收敛为单个 action，并在
   * 内部做在途合并：已有刷新在途时新请求合并为「结束后再跑一轮」，各调用方分别结算
   * 自己那一轮。否则两个入口同时刷新会交错写回，先发后到的旧结果盖住新结果。
   */
  refreshTasks: () => Promise<void>;
  beginOptimisticActive: (
    projectName: string,
    resourceKind: ResourceKind,
    resourceId: string,
    pendingTaskType: string,
  ) => OptimisticHandle;
  beginOptimisticActiveForScriptFile: (
    projectName: string,
    taskType: string,
    scriptFile: string,
  ) => OptimisticHandle;
}

export const defaultTaskStats: TaskStats = {
  queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0, total: 0,
};

// ---------------------------------------------------------------------------
// Encoding of optimistic occupancy marks and the yield check
// Marks are stored as string keys in a Set; the last two segments are the "mark sequence number" and the "fulfilled task_id list":
//   resource granularity   project \0 resourceKind \0 resourceId \0 pendingTaskType \0 seq \0 taskIds
//   scriptFile             project \0 taskType \0 scriptFile \0 seq \0 taskIds
// seq lets two concurrent submits on the same resource roll back independently; an empty taskIds means "in flight": the request was sent,
// the backend task id is unknown, and the mark cannot be cleared by any poll write-back. The two granularities differ in field count, but taskIds
// is always the last segment, so the yield check needs only one parsing rule, {@link markTaskIds}, and adding fields cannot misalign it.
// The yield check looks at task_id, not timestamps: only when **all** of this submit's own task rows appear in tasks does the mark
// hand over to real data; ids already in the DB are subtracted from the key round by round, so they need not appear in the same snapshot.
// This criterion closes two timing holes at once:
//   1. A terminal state arriving first no longer lingers forever: on a dedup match against an existing task the backend returns that existing row's task_id,
//      which is already (or will soon be) in tasks, so the mark yields right away; comparing timestamps would never yield because that row's updated_at
//      is not greater than the baseline when the mark was made.
//   2. A late old poll result no longer overwrites a newer mark: the old snapshot lacks this submit's task_id, so the check does not hold.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// 乐观占用标记的编码与让位判定
//
// 标记以字符串 key 存于 Set，尾部两段是「标记序号」与「已兑现的 task_id 列表」：
//
//   资源粒度   project \0 resourceKind \0 resourceId \0 pendingTaskType \0 seq \0 taskIds
//   scriptFile project \0 taskType \0 scriptFile \0 seq \0 taskIds
//
// seq 让同一资源上的两次并发提交各自可独立回滚；taskIds 为空串即「在途」——请求已发出、
// 后端任务 id 未知，此时标记不可被任何轮询写回清除。两种粒度的字段数不同，但 taskIds
// 一律是最后一段，故让位判定只需 {@link markTaskIds} 一个解析口径，加字段不会错位。
//
// 让位判定看 task_id 而非时间戳：只有本次提交自己的任务行**全部**出现在 tasks 里，标记
// 才交还给真实数据；已落库的 id 逐轮从 key 里扣除，故不要求它们在同一个快照里同时出现。
// 这条判据同时堵住两个时序洞：
//   1. 终态先到不再永久残留——去重命中既有任务时后端返回的是那条既有行的 task_id，
//      它已在（或即将在）tasks 里，标记随即让位；按时间戳比较则会因该行 updated_at
//      不大于标记发起时的基线而永不让位。
//   2. 晚到的旧轮询结果不再覆盖较新的标记——旧快照里没有本次的 task_id，判定不成立。
// ---------------------------------------------------------------------------

/** Separator between fulfilled task_ids; distinct from the field separator \0 for easy parsing. / 已兑现 task_id 之间的分隔符；与字段分隔符 \0 区分开，便于解析。 */
const TASK_ID_SEP = "\u0001";

let optimisticSeq = 0;

function encodeTaskIds(taskIds: readonly string[]): string {
  return taskIds.join(TASK_ID_SEP);
}

/** Get taskIds from the last segment of a mark key; an in-flight mark (empty last segment) returns an empty array. / 取标记 key 末段的 taskIds；在途标记（末段为空串）返回空数组。 */
function markTaskIds(key: string): string[] {
  const encoded = key.split("\0").at(-1);
  return encoded ? encoded.split(TASK_ID_SEP) : [];
}

function optimisticKey(
  projectName: string,
  resourceKind: ResourceKind,
  resourceId: string,
  pendingTaskType: string,
  seq: number,
  taskIds: readonly string[],
): string {
  return `${projectName}\0${resourceKind}\0${resourceId}\0${pendingTaskType}\0${seq}\0${encodeTaskIds(taskIds)}`;
}

function optimisticScriptFileKey(
  projectName: string,
  taskType: string,
  scriptFile: string,
  seq: number,
  taskIds: readonly string[],
): string {
  return `${projectName}\0${taskType}\0${stripScriptsPrefix(scriptFile)}\0${seq}\0${encodeTaskIds(taskIds)}`;
}

/** Replace the taskIds in the key's last segment with `taskIds`, keeping the other fields as is. / 把 key 末段的 taskIds 换成 `taskIds`，其余字段原样保留。 */
function withTaskIds(key: string, taskIds: readonly string[]): string {
  return key.slice(0, key.lastIndexOf("\0") + 1) + encodeTaskIds(taskIds);
}

/** task_ids already in the DB in this snapshot. / 本次快照里已落库的 task_id。 */
function landedTaskIds(tasks: TaskItem[]): Set<string> {
  return new Set(tasks.map((t) => t.task_id));
}

/**
 * In what form this mark should remain after this snapshot: fully subtracted (all awaited task_ids in the DB) returns null
 * meaning yield; otherwise returns the key after subtraction (returned as is when unchanged, to avoid pointless Set churn).
 *
 * In-flight marks (empty taskIds) remain as is and never yield: during the request round trip there is no basis to judge
 * the resource idle. When one submit creates several task rows (grid enqueues one per group), it yields only once **all** are in the DB:
 * handing back after just one would misjudge the resource as idle in the window where "the first is terminal and the rest are not yet in the snapshot".
 * Subtraction accumulates round by round and does not require all task_ids to appear in one snapshot: each poll only takes the latest
 * 200 rows, while one enqueue has no upper bound on task rows produced.
 *
 * 该标记经本次快照后应以什么形态留下：已扣完（等待的 task_id 全部落库）返回 null
 * 表示让位，否则返回扣除后的 key（无变化时原样返回，避免无谓的 Set 抖动）。
 *
 * 在途标记（taskIds 为空）原样留下、永不让位——请求往返期间没有任何依据可以判定
 * 资源已空闲。一次提交建多条任务行时（宫格按分组逐条入队），要**全部**落库才让位：
 * 只等到其中一条就交还，会在「首条已终态、其余尚未进入快照」的窗口里把资源误判为
 * 空闲。扣除逐轮累计，不要求所有 task_id 在同一个快照里同时出现——轮询每次只取最新
 * 200 行，而单次入队产生的任务行数没有上限。
 */
function keepMark(key: string, landed: ReadonlySet<string>): string | null {
  const taskIds = markTaskIds(key);
  if (taskIds.length === 0) return key; // in-flight mark / 在途标记
  const remaining = taskIds.filter((id) => !landed.has(id));
  if (remaining.length === 0) return null;
  return remaining.length === taskIds.length ? key : withTaskIds(key, remaining);
}

// Trim optimistic occupancy marks by the current tasks (adds no marks, only subtracts task_ids in the DB and cleans up those that yielded).
// Besides the incidental cleanup at fulfillment, this must also run on every setTasks (poll write-back): real task rows enter the store via polling,
// and that is the only moment a mark can find its rows have landed.
// 按当前 tasks 修剪乐观占用标记（不新增标记，仅扣除已落库的 task_id 并清理已让位者）。
// 除兑现时机的顺带清理外，也要在每次 setTasks（轮询写回）时执行——真实任务行是经轮询
// 进入 store 的，标记只能在那一刻发现自己的行已落库。
function pruneSupersededOptimistic(tasks: TaskItem[], marks: ReadonlySet<string>): Set<string> {
  const landed = landedTaskIds(tasks);
  const next = new Set<string>();
  for (const key of marks) {
    const kept = keepMark(key, landed);
    if (kept !== null) next.add(kept);
  }
  return next;
}

export const useTasksStore = create<TasksState>((set, get) => {
  // Coordination state for refresh in-flight merging (non-reactive singleton, not in store state, to avoid triggering subscriber re-renders).
  // Isomorphic to projects-store.refreshProject; created with the store and not left over across store instances.
  // 刷新的在途合并协调状态（非响应式单例，不进 store state，避免触发订阅重渲染）。
  // 与 projects-store.refreshProject 同构，随 store 一同创建，不跨 store 实例残留。
  let refreshRunning = false;
  let refreshQueued = false;
  // Each queued-round caller's own resolve: each caller is only answered by the result of the round that actually served it.
  // 排队轮调用方各自的 resolve：每个调用方只对自己实际服务的那一轮结果负责。
  let queuedResolvers: Array<() => void> = [];
  // Scope whose task baseline has been established; null means the next round is this scope's first, which only builds the baseline without terminal-state judging.
  // 已建立任务基线的作用域；null 表示下一轮是该作用域的第一轮，只建基线不做终态判定。
  let baselineScopeKey: string | null = null;

  /**
   * Whether in this fetch round any task that writes back to a reference unit just went from non-terminal to succeeded.
   *
   * The reference-to-video canvas refetches final cuts and narration paths via `referenceVideoUnitsRevision`, whose main increment channel is the project
   * event SSE: when the SSE disconnects, silently stalls or drops this event, task status can still recover via the poll fallback, but the unit
   * snapshot never updates until the canvas remounts. Here a reference_video / tts poll success also triggers the same
   * invalidation, filling the gap in the fallback path; non-reference-to-video projects do not mount that canvas, so an extra revision increment triggers no request.
   *
   * The criterion is "succeeded this round, not succeeded last round", where "not seen last round" also counts as not succeeded: when a task's enqueue and completion
   * fall entirely between two polls (the idle tier interval is long, and a provider cache hit may return within seconds), it first appears as
   * succeeded, and requiring it to have been seen last round would miss this kind. In the next round it is already succeeded in the old snapshot,
   * so it does not invalidate twice.
   *
   * A scope's first round only builds the baseline without judging: that round's old snapshot is either empty or belongs to the previous project, and treating historical
   * successful tasks as just completed is meaningless. The baseline is marked by `baselineScopeKey` and reset on project switch or disable.
   *
   * 本轮拉取里是否有会写回 reference unit 的任务刚由非终态转成功。
   *
   * 参考生视频画布靠 `referenceVideoUnitsRevision` 重拉成片和旁白路径，而自增主通道是项目
   * 事件 SSE：SSE 断线、静默失速或丢掉这一条事件时，任务状态还能靠轮询兜底恢复，unit
   * 快照却一直不更新，直到画布重新挂载。这里让 reference_video / tts 的轮询成功也走同一次
   * 失效，补上兜底路径的缺口；非参考生视频没有挂载该画布，多一次 revision 自增不触发请求。
   *
   * 判据是「这一轮成功、上一轮不成功」，其中「上一轮没见过」也算不成功——任务的入队与完成
   * 整个落在两次轮询之间时（空闲档间隔较长，provider 命中缓存时可能秒回），它是首次以
   * succeeded 出现的，要求上一轮见过就会漏掉这一类。下一轮它在旧快照里已是 succeeded，
   * 因此不会重复失效。
   *
   * 作用域的第一轮只建基线、不判定：那一轮的旧快照要么是空的、要么属于上一个项目，把历史
   * 成功任务全当成刚完成没有意义。基线由 `baselineScopeKey` 标记，切项目或停用后重置。
   */
  const hasNewlySucceededReferenceUnitAsset = (
    prev: readonly TaskItem[],
    next: readonly TaskItem[],
  ): boolean => {
    const prevStatus = new Map(prev.map((t) => [t.task_id, t.status]));
    return next.some(
      (t) =>
        (t.task_type === "reference_video" || t.task_type === "tts") &&
        t.status === "succeeded" &&
        prevStatus.get(t.task_id) !== "succeeded",
    );
  };

  // One fetch round; does not throw itself, a failure only sets connected false and keeps old data.
  // 单轮拉取；自身不抛错，失败只把 connected 置 false 并留旧数据。
  const runTasksRefresh = async (): Promise<void> => {
    const scope = get().refreshScope;
    if (!scope) return;
    const { projectName } = scope;

    // By the time it settles the scope may have been rewritten by a project switch/disable: a late response is not written back, otherwise the old project's task list would overwrite
    // data the taker just wrote (or, when disabled, overwrite a store that should be empty).
    // 落定时作用域可能已被切项目/停用改写：迟到响应不写回，否则旧项目的任务列表会盖住
    // 接管方刚写入的数据（停用时则会盖住本该清空的 store）。
    const scopeUnchanged = () => {
      const current = get().refreshScope;
      return current !== null && current.projectName === projectName;
    };

    try {
      const [tasksRes, statsRes] = await Promise.all([
        API.listTasks({ projectName: projectName ?? undefined, pageSize: TASKS_PAGE_SIZE }),
        API.getTaskStats(projectName ?? null),
      ]);
      if (!scopeUnchanged()) return;
      const scopeKey = projectName ?? "";
      const unitsStale =
        baselineScopeKey === scopeKey &&
        hasNewlySucceededReferenceUnitAsset(get().tasks, tasksRes.items);
      baselineScopeKey = scopeKey;
      get().setTasks(tasksRes.items);
      get().setStats(statsRes.stats);
      get().setConnected(true);
      // Staying inside try is intentional: invalidation is just one zustand set with no throw path; moving it outside try would make
      // this function no longer satisfy "does not throw itself": the project event SSE path calls refreshTasks as a bare void call,
      // so a throw would become an unhandled rejection, and callers queued in the in-flight merge would never be settled.
      // 留在 try 内是有意的：失效只是一次 zustand set，没有抛出路径；而把它挪到 try 外会让
      // 本函数不再满足「自身不抛错」——项目事件 SSE 那一路是裸 void 调用 refreshTasks，
      // 抛出会变成未处理的 rejection，且在途合并里排队的调用方永远等不到结算。
      if (unitsStale) {
        useAppStore.getState().invalidateReferenceVideoUnits();
      }
    } catch {
      if (!scopeUnchanged()) return;
      get().setConnected(false);
    }
  };

  /**
   * Common skeleton of the begin/settle/rollback three states: the two granularities differ only in key encoding and which Set they land in.
   * `field` picks the Set, and `keyOf(taskIds)` encodes the key from fulfilled task_ids (an empty array gives the in-flight key).
   *
   * begin/settle/rollback 三态的公共骨架：两种粒度只在 key 编码与落到哪个 Set 上不同。
   * `field` 选定 Set，`keyOf(taskIds)` 按已兑现的 task_id 编码出 key（空数组即在途 key）。
   */
  function beginMark(
    field: "optimisticActive" | "optimisticActiveScriptFile",
    keyOf: (taskIds: readonly string[]) => string,
  ): OptimisticHandle {
    const pendingKey = keyOf([]);
    set((s) => {
      // Incidentally clean up old marks that have yielded, so the Set does not grow unboundedly over the session.
      // 顺带清理已让位的旧标记，避免 Set 在会话周期内无界增长。
      const next = pruneSupersededOptimistic(s.tasks, s[field]);
      next.add(pendingKey);
      return { [field]: next };
    });

    const drop = (replacement: string | null): void => {
      if (!get()[field].has(pendingKey)) return; // already rolled back or reset by the store / 已被回滚或被 store 重置
      set((s) => {
        const next = new Set(s[field]);
        next.delete(pendingKey);
        if (replacement !== null) {
          // On a dedup match against an existing task the row is often already in the store, so subtract it right now without waiting for the next poll;
          // once fully subtracted, yield on the spot.
          // 去重命中既有任务时该行往往已在 store 里，此刻即扣除，不必等下一轮轮询；
          // 全部扣完即当场让位。
          const kept = keepMark(replacement, landedTaskIds(s.tasks));
          if (kept !== null) next.add(kept);
        }
        return { [field]: next };
      });
    };

    return {
      settle: (taskIds) => drop(taskIds.length > 0 ? keyOf(taskIds) : null),
      rollback: () => drop(null),
    };
  }

  return {
    tasks: [],
    stats: defaultTaskStats,
    connected: false,
    refreshScope: null,
    optimisticActive: new Set(),
    optimisticActiveScriptFile: new Set(),

    setRefreshScope: (scope) => {
      // Project switch and disable both clear/replace the task snapshot, so the old baseline is no longer comparable: rebuild the baseline next round.
      // 切项目与停用都会清空/替换任务快照，旧基线不再可比：下一轮重新建基线。
      baselineScopeKey = null;
      set({ refreshScope: scope });
    },
    refreshTasks: async () => {
      if (refreshRunning) {
        refreshQueued = true;
        return new Promise<void>((resolve) => {
          queuedResolvers.push(resolve);
        });
      }
      refreshRunning = true;
      try {
        await runTasksRefresh();
        while (refreshQueued) {
          refreshQueued = false;
          const resolvers = queuedResolvers;
          queuedResolvers = [];
          await runTasksRefresh();
          for (const resolve of resolvers) resolve();
        }
      } finally {
        refreshRunning = false;
      }
    },

    setTasks: (tasks) =>
      set((s) => {
        // When a function is passed, it is evaluated against the snapshot at write time: reading getState() first and then passing an array would wholly overwrite
        // SSE updates arriving between those two steps.
        // 传函数时按写入那一刻的快照求值：先读 getState() 再传数组，会把这两步之间到达的
        // SSE 更新整份覆盖掉。
        const next = typeof tasks === "function" ? tasks(s.tasks) : tasks;
        return {
          tasks: next,
          optimisticActive: pruneSupersededOptimistic(next, s.optimisticActive),
          optimisticActiveScriptFile: pruneSupersededOptimistic(next, s.optimisticActiveScriptFile),
        };
      }),
    setStats: (stats) => set({ stats }),
    setConnected: (connected) => set({ connected }),
    beginOptimisticActive: (projectName, resourceKind, resourceId, pendingTaskType) => {
      const seq = ++optimisticSeq;
      return beginMark("optimisticActive", (taskIds) =>
        optimisticKey(projectName, resourceKind, resourceId, pendingTaskType, seq, taskIds),
      );
    },
    beginOptimisticActiveForScriptFile: (projectName, taskType, scriptFile) => {
      const seq = ++optimisticSeq;
      return beginMark("optimisticActiveScriptFile", (taskIds) =>
        optimisticScriptFileKey(projectName, taskType, scriptFile, seq, taskIds),
      );
    },
  };
});

// ---------------------------------------------------------------------------
// Derived selectors: the single source of truth for the task queue's two invariants
// Consumers (canvas loading derivation, video unit status, etc.) must follow these two contracts:
//   1. "What counts as active": occupancy and display are two predicates: the occupancy check (isOccupyingStatus) aligns with the backend
//      dedupe index's ACTIVE_TASK_STATUSES; the display check (isActiveStatus) decides whether
//      to show as in progress. Both currently contain only queued / running.
//   2. "Latest row wins": the same resource may have several task rows: a retry after failure has a new task_id,
//      and tasks is written wholesale from the server list with no guaranteed order, so the check must take the row with the latest updated_at,
//      so a retry's new row is not hidden by an old failed row (selectLatestTaskByResource).
// The pure-function versions consolidate both invariants here and are directly testable with vitest; the hook versions use useShallow to compare
// Set/Map contents, keeping references stable when contents do not change, to avoid returning new collections on every render and triggering re-renders.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// 派生 selector —— 任务队列两条不变量的单一真相源
//
// 消费点（画布 loading 派生、视频单元状态等）必须遵守以下两条契约：
//   1.「什么算活跃」——占用与显示是两个谓词：占用判定（isOccupyingStatus）与后端
//      dedupe 索引的 ACTIVE_TASK_STATUSES 对齐；显示判定（isActiveStatus）决定是否
//      显示为进行中。两者当前都只含 queued / running。
//   2.「最新行胜出」——同一 resource 可能有多条任务行：失败后重试是新的 task_id，
//      tasks 由服务端列表整体写入、顺序不保证，故判定时须取 updated_at 最新的一行，
//      重试的新行不被旧失败行遮挡（selectLatestTaskByResource）。
//
// 纯函数版把两条不变量收敛于此、可直接用 vitest 测试；hook 版用 useShallow 比较
// Set/Map 内容，保证内容不变时引用稳定，避免每次渲染返回新集合触发重渲染。
// ---------------------------------------------------------------------------

/** Display predicate: queued or running tasks are shown as in progress. / 显示谓词：排队或运行中的任务显示为进行中。 */
export function isActiveStatus(status: TaskStatus): boolean {
  return status === "queued" || status === "running";
}

/**
 * Whether the mark set has entries belonging to the current refresh scope. Keys of both kinds of optimistic marks start with `projectName`,
 * so judge uniformly by prefix; when the scope is "do not filter by project" (`projectName === null`), count them all.
 *
 * Filter by scope rather than looking at `size` directly: marks are only trimmed when real task rows of the same project land; after switching to another project
 * the original project's leftover marks are never trimmed, and counting them would pin the fallback polling permanently at the high-frequency tier.
 *
 * 标记集中是否有属于当前刷新作用域的条目。两类乐观标记的 key 均以 `projectName` 打头，
 * 故统一按前缀判定；作用域为「不按项目过滤」（`projectName === null`）时全部计入。
 *
 * 按作用域筛而非直接看 `size`：标记只在同项目的真实任务行落地时才被修剪，切到别的项目
 * 后原项目的残留标记永远等不到修剪，若计入判据会把兜底轮询永久钉在高频档。
 */
function hasOptimisticInScope(
  marks: ReadonlySet<string>,
  scopeProjectName: string | null,
): boolean {
  if (marks.size === 0) return false;
  if (scopeProjectName === null) return true;
  const prefix = `${scopeProjectName}\0`;
  for (const key of marks) {
    if (key.startsWith(prefix)) return true;
  }
  return false;
}

/**
 * Whether the fallback polling needs to stay at the high-frequency tier. Three cases count as "needed":
 *
 * - **Some task has not reached a terminal state**. stats is the main criterion: it covers the whole scope, while tasks is only the first N after paging.
 *   Intermediate states (queued->running) have no event push and only polling sees them. tasks is counted too because the two are read by two
 *   concurrent requests and are not the same snapshot: when something elsewhere (another client or a Skill) enqueues during this refresh,
 *   stats may read zero active while tasks already contains that queued row; looking only at stats would judge this round idle, and that task's
 *   intermediate state would only become visible after a full idle interval.
 * - **There are optimistic occupancy marks in the current scope**. There is a gap between a successful enqueue and the task appearing in stats;
 *   judging idle then would make the new task appear in the UI only after a whole idle interval.
 * - **The previous fetch failed** (`connected === false`). stats is then stale data, and judging idle from it would delay
 *   recovery by a whole idle interval; after a failure it should retry soon anyway.
 *
 * 兜底轮询是否需要留在高频档。三种情形都算「需要」：
 *
 * - **有任务未落终态**。以 stats 为主判据：它覆盖整个作用域，而 tasks 只是分页后的前 N 条。
 *   中间态（queued→running）没有事件推送，只有轮询能看见。tasks 一并计入是因为二者由两个
 *   并发请求分别读出、彼此不是同一快照：别处（另一客户端或 Skill）恰在本轮刷新期间入队时，
 *   stats 可能读到零活跃而 tasks 已含那条 queued 行，只看 stats 会把这一轮判成空闲，该任务
 *   的中间态要等满一个空闲间隔才可见。
 * - **当前作用域内有乐观占用标记**。入队成功到该任务出现在 stats 之间还有一段空窗，
 *   此时若判为空闲，新任务要等一整个空闲间隔才在界面上出现。
 * - **上一轮拉取失败**（`connected === false`）。此时 stats 是过期数据，据它判空闲会把
 *   恢复推迟一整个空闲间隔；失败后本就该尽快重试。
 */
export function selectNeedsFastPolling(s: {
  tasks: readonly TaskItem[];
  stats: TaskStats;
  connected: boolean;
  refreshScope: TasksRefreshScope | null;
  optimisticActive: ReadonlySet<string>;
  optimisticActiveScriptFile: ReadonlySet<string>;
}): boolean {
  // When the scope is disabled refreshTasks sends no request, so marks cannot be reconciled whatever project they belong to; none are counted.
  // 作用域未启用时 refreshTasks 不发请求，标记落在哪个项目都无从对账，一律不计入。
  const scope = s.refreshScope;
  return (
    !s.connected ||
    s.stats.queued + s.stats.running > 0 ||
    s.tasks.some((t) => isOccupyingStatus(t.status)) ||
    (scope !== null &&
      (hasOptimisticInScope(s.optimisticActive, scope.projectName) ||
        hasOptimisticInScope(s.optimisticActiveScriptFile, scope.projectName)))
  );
}

/**
 * Occupancy predicate: tasks in this status still occupy their resource (button disabled, occupancy set check). Aligned with the backend dedupe
 * index's ACTIVE_TASK_STATUSES: the backend dedups repeat submits on the same resource onto the existing task;
 * if the frontend judged idle at that point, it would falsely report "button clickable, but no new task after submitting".
 *
 * 占用谓词：该状态的任务仍占用其 resource（按钮禁用、占用集判定）。与后端 dedupe
 * 索引的 ACTIVE_TASK_STATUSES 对齐——后端会把同资源的重复提交去重到既有任务上；
 * 若前端此时判定空闲，会出现「按钮可点、提交后没有新任务」的谎报。
 */
export function isOccupyingStatus(status: TaskStatus): boolean {
  return status === "queued" || status === "running";
}

/** Script planning has three task types by project mode; a project uses only one of them. / 脚本规划按项目模式分三种任务类型，一个项目只用其中一种。 */
export const SCRIPT_PLAN_TASK_TYPES: ReadonlySet<string> = new Set([
  "text_drama_script_plan",
  "text_narration_script_plan",
  "text_reference_script_plan",
]);

/**
 * The "resource kind" a task occupies. Except for image_edit, the task_type itself is the resource kind; image_edit shares one task_type across
 * character/scene/prop/product/storyboard and the real kind is in resource_type,
 * so it is slotted by resource_type: edit tasks and generation tasks of the same resource fall into the same occupancy set and are mutually exclusive.
 * `voice_sample` (character TTS preview sample) likewise goes into `character`: it shares one occupancy slot with that character's asset sheet generation/
 * editing/upload (see {@link enqueueCharacterVoiceSample}); the optimistic mark is made with the `character` kind before the request
 * is sent, so once the real task row lands it must map to the same kind to yield without a gap.
 * The three script planning task types ({@link SCRIPT_PLAN_TASK_TYPES}) all go into `text_script_plan`,
 * so entries need not pick the occupancy slot by project mode.
 *
 * The two assertions are a data boundary: {@link TaskItem} fields come from the backend and TS cannot control their value range. If the backend emits a kind not
 * registered in {@link ResourceKind}, that row matches no occupancy slot (degrading to not taking part in the occupancy check),
 * consistent with the behavior before narrowing; what the union type really blocks is frontend callers misspelling kind themselves.
 *
 * 任务占用的「资源种类」。除 image_edit 外，task_type 本身即资源种类；image_edit 跨
 * character/scene/prop/product/storyboard 共用一个 task_type，真正的种类在 resource_type，
 * 故按 resource_type 归槽——编辑任务与同资源的生成任务落入同一占用集、彼此互斥。
 * `voice_sample`（角色 TTS 试听样本）同理归入 `character`——它与该角色的资产图生成/
 * 编辑/上传共用同一占用槽（见 {@link enqueueCharacterVoiceSample}），乐观标记在请求
 * 发出前就用 `character` kind 打标，真实任务行落库后须归到同一 kind 才能让位不断档。
 * 脚本规划的三种任务类型（{@link SCRIPT_PLAN_TASK_TYPES}）统一归入 `text_script_plan`，
 * 入口不必按项目模式选择占用槽。
 *
 * 两处断言是数据边界：{@link TaskItem} 的字段来自后端、TS 管不到其值域。后端若出现未在
 * {@link ResourceKind} 登记的种类，该行匹配不上任何占用槽（退化为不参与占用判定），
 * 与收紧前的行为一致；真正被联合类型堵住的是前端调用方自己拼错 kind。
 */
export function taskResourceKind(task: TaskItem): ResourceKind | "" {
  if (task.task_type === "image_edit") {
    return (task.resource_type as ResourceKind | null) ?? "";
  }
  if (task.task_type === "voice_sample") {
    return "character";
  }
  if (SCRIPT_PLAN_TASK_TYPES.has(task.task_type)) {
    return "text_script_plan";
  }
  return task.task_type as ResourceKind;
}

/**
 * Invariant 2: group tasks by resource_id, taking the row with the latest updated_at when a resource has several rows.
 * Optional prefilter by projectName / taskType; the store does not guarantee order, so updated_at is compared explicitly.
 *
 * 不变量 2：按 resource_id 归并任务，同一 resource 多行时取 updated_at 最新的一行。
 * 可选按 projectName / taskType 预筛；store 不保证顺序，故显式比较 updated_at。
 */
export function selectLatestTaskByResource(
  tasks: TaskItem[],
  filter: { projectName?: string; taskType?: string } = {},
): Map<string, TaskItem> {
  const latest = new Map<string, TaskItem>();
  for (const task of tasks) {
    if (filter.projectName !== undefined && task.project_name !== filter.projectName) continue;
    if (filter.taskType !== undefined && taskResourceKind(task) !== filter.taskType) continue;
    const prev = latest.get(task.resource_id);
    if (!prev || task.updated_at > prev.updated_at) latest.set(task.resource_id, task);
  }
  return latest;
}

/**
 * Set of resource_ids matching taskType + projectName whose "latest row" is active.
 * "Latest row wins" groups separately by the two-level key (resource_id, task.task_type): within the same native task_type,
 * a retry's new running/queued row is not covered by an old failed row of the same resource; but image_edit and
 * the generation task of its target resource are two independent task_types (sharing one occupancy slot only via taskResourceKind),
 * with no mutual exclusion guaranteed by the backend, so both may really coexist. Judging by a single "latest row" would let a newer landed edit terminal state
 * (success/failure) mask a still-running generation task (or vice versa), misjudging the resource as idle. So take the latest row per
 * task_type separately, and count as occupied when the latest row of any task_type is active.
 *
 * Optimistic occupancy: between sending the enqueue request and the next refresh writing the new task row into the store, the resource has
 * no matching task row in the store yet and is judged idle. This gap spans the request round trip and one polling interval (the mark itself pulls fallback
 * polling back to the busy tier, see {@link selectNeedsFastPolling}, so the bound is one busy-tier interval rather than an idle-tier
 * interval); meanwhile other entries for the same resource would misjudge it as submittable; image_edit sharing an occupancy slot with its target resource is the first
 * scenario that submits concurrently with "another task_type of this resource" in this gap (concurrent submits of the same task_type are already blocked by the backend
 * dedupe index, see `idx_tasks_dedupe_active`, but that index uses task_type as part of its key
 * and does not block concurrency across task_types).
 *
 * `optimisticActive` is marked by the enqueue action layer via {@link OptimisticHandle} **before the request is sent**,
 * and rolled back if the request fails, so the whole gap is covered; here it is filtered by (projectName, resourceKind) and merged into
 * the occupancy set until the real task rows the mark waits for appear (the comparison ignores status; appearing means yielding to real data).
 * Marks claim their real rows by task_id; for the criterion and residual risks see "Encoding of optimistic occupancy marks and
 * the yield check" at the top of the file.
 *
 * 命中 taskType + projectName 且「最新行」处于活跃态的 resource_id 集合。
 * 「最新行胜出」按 (resource_id, task.task_type) 二级键分别归并——同一原生 task_type
 * 内，重试的新 running/queued 行不被同 resource 的旧 failed 行盖住；但 image_edit 与
 * 其目标资源的生成任务是两个独立 task_type（仅通过 taskResourceKind 共享同一占用槽），
 * 后端并无互斥保证，二者可能真实并存。若仍按单一「最新行」判定，较新落地的编辑终态
 * （成功/失败）会掩盖仍在运行的生成任务（或反之），导致资源被误判为空闲。故按各自
 * task_type 分别取最新行，再在任一 task_type 的最新行活跃时即计入占用。
 *
 * 乐观占用：从入队请求发出到下一次刷新把新任务行写进 store 之间，该 resource 在 store
 * 里还没有对应任务行、判定为空闲。这个空窗跨越请求往返与一个轮询间隔（标记本身会把兜底
 * 轮询拉回忙碌档，见 {@link selectNeedsFastPolling}，故上限是一个忙碌档间隔而非空闲档
 * 间隔）；期间同资源的其它入口会误判可提交——image_edit 与其目标资源共用占用槽，是第一个
 * 会在此空窗内与「本资源另一 task_type」并发提交的场景（同 task_type 的并发提交已被后端
 * dedupe 索引拦下，见 `idx_tasks_dedupe_active`，但该索引以 task_type 为键的一部分，
 * 不拦跨 task_type 并发）。
 *
 * `optimisticActive` 由入队动作层在**请求发出前**用 {@link OptimisticHandle} 标记、
 * 请求失败时回滚，故整个空窗都被覆盖；此处按 (projectName, resourceKind) 过滤后并入
 * 占用集，直到标记所等待的真实任务行出现为止（比对不看状态，出现即让位给真实数据）。
 * 标记按 task_id 认领自己的真实行，判据与残留风险见文件上方「乐观占用标记的编码与
 * 让位判定」。
 */
export function selectActiveResourceIds(
  tasks: TaskItem[],
  taskType: ResourceKind,
  projectName: string,
  optimisticActive: ReadonlySet<string> = EMPTY_OPTIMISTIC,
): Set<string> {
  const latestByResourceAndTaskType = new Map<string, TaskItem>();
  for (const task of tasks) {
    if (task.project_name !== projectName) continue;
    if (taskResourceKind(task) !== taskType) continue;
    const key = `${task.resource_id}\0${task.task_type}`;
    const prev = latestByResourceAndTaskType.get(key);
    if (!prev || task.updated_at > prev.updated_at) latestByResourceAndTaskType.set(key, task);
  }
  const ids = new Set<string>();
  for (const task of latestByResourceAndTaskType.values()) {
    if (isOccupyingStatus(task.status)) ids.add(task.resource_id);
  }
  // Trimming happens in setTasks, and the selector may see a new snapshot before it, so compute by the same criterion here.
  // 修剪发生在 setTasks 时，selector 可能先于它看到新快照，故这里按同一判据现算。
  const landed = landedTaskIds(tasks);
  for (const key of optimisticActive) {
    const [kProject, kResourceKind, kResourceId] = key.split("\0");
    if (kProject !== projectName || kResourceKind !== taskType) continue;
    if (keepMark(key, landed) !== null) ids.add(kResourceId);
  }
  return ids;
}

const EMPTY_OPTIMISTIC: ReadonlySet<string> = new Set();

/**
 * Unified entry for re-checking occupancy at submit time: wraps the `getState()` fresh read + {@link selectActiveResourceIds} +
 * key assembly, so each write control can judge directly at the moment of submit without repeating these three steps.
 *
 * 提交时刻复核占用态的统一入口：封装 `getState()` 新鲜读 + {@link selectActiveResourceIds} +
 * key 拼装，供各写入控件在提交那一刻直接判定，不必各自重复这三步。
 */
export function isResourceBusy(kind: ResourceKind, projectName: string, resourceId: string): boolean {
  const { tasks, optimisticActive } = useTasksStore.getState();
  return selectActiveResourceIds(tasks, kind, projectName, optimisticActive).has(resourceId);
}

// Same normalization rule as stripScriptsPrefix in task-target.ts: script_file in episode metadata
// always carries the `scripts/` prefix (see ProjectManager._apply_episode_sync), but a task row's script_file
// is passed by each enqueue caller: the router passes the webui form value directly, while Agent tools force a bare filename via validate_script_filename,
// so the two formats are not guaranteed to match. Do not rely on callers trimming beforehand; normalize here before comparing.
// 与 task-target.ts 的 stripScriptsPrefix 同一归一化规则：episode 元数据的 script_file
// 固定带 `scripts/` 前缀（见 ProjectManager._apply_episode_sync），但任务行的 script_file
// 由各入队调用方各自传入——router 直传 webui 表单值，Agent 工具经 validate_script_filename
// 强制裸文件名，两者格式不保证一致。此处不依赖调用方预先裁剪，自行归一化后再比较。
function stripScriptsPrefix(path: string): string {
  return path.replace(/^scripts\//, "");
}

/**
 * Whether an active task of type taskType exists under the given scriptFile. No "latest row wins" grouping:
 * existence counts, for a coarse episode-level occupancy check: a grid task's resource_id is the grid_id, not a
 * shot segment_id, so it cannot go into selectActiveResourceIds' per-resource check; but the grid split phase
 * overwrites storyboard files of several shots in this episode, so judge by scriptFile "whether this episode has a grid task
 * running", used to disable shot editing entries when grid assembly is on, avoiding editing and splitting writing the same file concurrently.
 *
 * Optimistic occupancy: between sending the grid enqueue request and the next poll writing the new grid task row into the store there is a gap, during which
 * this episode has no matching grid task row in the store, and shot editing entries would misjudge it as idle and, with the following split phase,
 * write the same storyboard current image concurrently. Same semantics as the optimistic occupancy subsection of {@link selectActiveResourceIds},
 * but the grouping key becomes scriptFile (grid tasks cannot be grouped by resource_id, see above).
 *
 * 是否存在指定 scriptFile 下、taskType 类型的活跃任务。不做「最新行胜出」归并——
 * 存在即算，用于粗粒度剧集级占用判定：grid 任务的 resource_id 是 grid_id 而非
 * 分镜 segment_id，无法归入 selectActiveResourceIds 的按资源判定；但 grid 切割阶段
 * 会覆写本集内多个分镜的 storyboard 文件，故按 scriptFile 判定「本集是否有宫格任务
 * 在跑」，用于在启用宫格装配时禁用分镜编辑入口，避免编辑与切割并发写同一文件。
 *
 * 乐观占用：宫格入队请求发出到下一次轮询把新 grid 任务行写进 store 之间是空窗，期间
 * 本集在 store 里尚无对应 grid 任务行，分镜编辑入口会误判为空闲、与随后的切割阶段
 * 并发写同一张 storyboard current 图。语义与 {@link selectActiveResourceIds} 的乐观占用
 * 小节一致，但归组键换成 scriptFile（grid 任务无法按 resource_id 归组，见上）。
 */
export function selectHasActiveTaskForScriptFile(
  tasks: TaskItem[],
  taskType: string,
  scriptFile: string,
  projectName: string,
  optimisticActiveScriptFile: ReadonlySet<string> = EMPTY_OPTIMISTIC,
): boolean {
  const normalized = stripScriptsPrefix(scriptFile);
  const hasRealActive = tasks.some(
    (task) =>
      task.project_name === projectName &&
      task.task_type === taskType &&
      task.script_file != null &&
      stripScriptsPrefix(task.script_file) === normalized &&
      isOccupyingStatus(task.status),
  );
  if (hasRealActive) return true;

  const landed = landedTaskIds(tasks);
  for (const key of optimisticActiveScriptFile) {
    const [kProject, kTaskType, kScriptFile] = key.split("\0");
    if (kProject !== projectName || kTaskType !== taskType || kScriptFile !== normalized) continue;
    if (keepMark(key, landed) !== null) return true;
  }
  return false;
}

/**
 * scriptFile-granularity submit-time re-check entry, isomorphic to {@link isResourceBusy} (`getState()` fresh read).
 *
 * The reason it exists is the same as the resource-granularity one: a prop at render time reflects the last render, and between a store update and the re-render
 * being committed the user may still click. The granularity is scriptFile because a grid task's resource_id is the grid_id,
 * which cannot go into the per-shot resource_id check (see {@link selectHasActiveTaskForScriptFile}).
 * Returns false when scriptFile/projectName is missing, on the same basis as the hook version.
 *
 * scriptFile 粒度的提交时刻复核入口，与 {@link isResourceBusy} 同构（`getState()` 新鲜读）。
 *
 * 存在的理由与 resource 粒度那条相同——渲染时刻的 prop 反映的是上次渲染，store 更新到重渲染
 * 提交之间用户仍可能点下去。粒度换成 scriptFile 是因为 grid 任务的 resource_id 是 grid_id、
 * 归不进按分镜 resource_id 的判定（见 {@link selectHasActiveTaskForScriptFile}）。
 * scriptFile/projectName 缺失时返回 false，与 hook 版同口径。
 */
export function isScriptFileBusy(
  taskType: string,
  scriptFile: string | undefined | null,
  projectName: string | undefined | null,
): boolean {
  if (!scriptFile || !projectName) return false;
  const { tasks, optimisticActiveScriptFile } = useTasksStore.getState();
  return selectHasActiveTaskForScriptFile(
    tasks,
    taskType,
    scriptFile,
    projectName,
    optimisticActiveScriptFile,
  );
}

/** Hook version of {@link selectHasActiveTaskForScriptFile}; returns false when scriptFile/projectName is missing. / hook 版 {@link selectHasActiveTaskForScriptFile}；scriptFile/projectName 缺失时返回 false。 */
export function useHasActiveTaskForScriptFile(
  taskType: string,
  scriptFile: string | undefined | null,
  projectName: string | undefined | null,
): boolean {
  return useTasksStore((s) =>
    scriptFile && projectName
      ? selectHasActiveTaskForScriptFile(
          s.tasks,
          taskType,
          scriptFile,
          projectName,
          s.optimisticActiveScriptFile,
        )
      : false,
  );
}

// Reuse the same empty set when projectName is missing, keeping the hook reference stable. / projectName 缺失时复用同一空集，保证 hook 引用稳定。
const EMPTY_ACTIVE_IDS: Set<string> = new Set();

/** Hook version of {@link selectActiveResourceIds}; returns a stable empty set when projectName is missing. / hook 版 {@link selectActiveResourceIds}；projectName 缺失时返回稳定空集。 */
export function useActiveResourceIds(
  taskType: ResourceKind,
  projectName: string | undefined | null,
): Set<string> {
  return useTasksStore(
    useShallow((s) =>
      projectName
        ? selectActiveResourceIds(s.tasks, taskType, projectName, s.optimisticActive)
        : EMPTY_ACTIVE_IDS,
    ),
  );
}

/** Hook version of {@link selectLatestTaskByResource}, prefiltered by project + type. / hook 版 {@link selectLatestTaskByResource}，按 project + type 预筛。 */
export function useLatestTasksByResource(
  projectName: string,
  taskType: string,
): Map<string, TaskItem> {
  return useTasksStore(
    useShallow((s) => selectLatestTaskByResource(s.tasks, { projectName, taskType })),
  );
}

/**
 * Get these tasks' queue rows by task_id.
 *
 * Task observations in the workflow panel come from the workflow status API, which has no timestamps; the source of truth for durations is the task queue,
 * so look up by task_id rather than having the workflow API carry another copy of timestamps: the same task carrying a timestamp on each of two paths,
 * refreshed at different rhythms, would sooner or later contradict each other. When the queue has no matching row the id is not put in the Map,
 * and the caller does not render that block at all. Return the original objects in the store rather than trimmed subsets, so shallow comparison hits when the task list
 * refreshes but contents are unchanged.
 *
 * 按 task_id 取这些任务的队列行。
 *
 * 工作流面板的任务观测由工作流状态接口给出，其中不含时间戳；时长的真相源是任务队列，
 * 故按 task_id 回查而不是让工作流接口多带一份时间戳——同一任务在两条链路上各带一份
 * 时间戳，两者刷新节奏不同，迟早会互相矛盾。队列里没有对应行时该 id 不进 Map，
 * 由调用方整块不渲染。返回 store 里的原对象而非裁剪出的子集，浅比较才能在任务列表
 * 刷新但内容未变时命中。
 */
export function useTaskRowsByIds(taskIds: readonly string[]): Map<string, TaskItem> {
  const wanted = new Set(taskIds);
  return useTasksStore(
    useShallow((s) => {
      const rows = new Map<string, TaskItem>();
      for (const task of s.tasks) {
        if (wanted.has(task.task_id)) rows.set(task.task_id, task);
      }
      return rows;
    }),
  );
}
