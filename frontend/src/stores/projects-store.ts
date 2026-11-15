import { create } from "zustand";
import type { ProjectData, ProjectSummary, EpisodeScript } from "@/types";
import { API } from "@/api";
import { isDemoProject } from "@/onboarding/demo-project";
import { useAppStore } from "./app-store";

/**
 * Settlement result of {@link ProjectsState.refreshProject}:
 * - `success`: the store has synced to the latest data
 * - `failed`: the request itself errored (network / server error); `onError` is called
 * - `cancelled`: not synced, but not an error: the project-switch cancel scope rotated, superseded by a later queued request, etc.
 *   Callers should handle it silently without an error toast
 *
 * {@link ProjectsState.refreshProject} 的结算结果：
 * - `success` — store 已同步为最新数据
 * - `failed` — 请求本身出错（网络 / 服务端错误），`onError` 会被调用
 * - `cancelled` — 未同步但不代表出错：项目切换取消域轮换、被更晚的排队请求取代等。
 *   调用方应静默处理，不弹错误提示
 */
export type RefreshProjectResult = "success" | "failed" | "cancelled";

/** Optional behavior of {@link ProjectsState.refreshProject}. / {@link ProjectsState.refreshProject} 的可选行为。 */
interface RefreshProjectOptions {
  /** Entity version keys to invalidate after a successful refresh (keeping the old variant semantics of StudioCanvasRouter). / 刷新成功后要失效的实体版本 key（沿用 StudioCanvasRouter 旧变体语义）。 */
  invalidateKeys?: string[];
  /** Called on every getProject failure (with the error object) so callers can show hints as needed; does not affect the keep-old semantics. / 每次 getProject 失败时回调（附错误对象），供调用方按需提示；不影响留旧语义。 */
  onError?: (err: unknown) => void;
}

interface ProjectsState {
  // List
  projects: ProjectSummary[];
  projectsLoading: boolean;

  // Current project detail
  currentProjectName: string | null;
  currentProjectData: ProjectData | null;
  currentScripts: Record<string, EpisodeScript>;
  projectDetailLoading: boolean;

  // Per-project increasing revision number of project.json + the script spec snapshot. / project.json + 剧本规范快照按项目递增的修订号。
  projectSnapshotRevisions: Record<string, number>;

  // Create modal
  showCreateModal: boolean;
  creatingProject: boolean;

  // Asset fingerprints (path → mtime_ns)
  assetFingerprints: Record<string, number>;

  /**
   * Internal bookkeeping: whether at least one real project has been established during the store lifecycle. Only used by
   * {@link refreshProject} to tell, when `currentProjectName` is `null`, whether "the store has never loaded
   * any project" or "a transitional state mid route switch"; not for UI consumption.
   *
   * 内部记账：store 生命周期内是否已经建立过至少一个真实项目。仅供
   * {@link refreshProject} 判断 `currentProjectName` 为 `null` 时是「store 从未加载
   * 过任何项目」还是「路由切换途中的过渡态」，不供 UI 消费。
   */
  hasLoadedAnyProject: boolean;

  // Actions
  setProjects: (projects: ProjectSummary[]) => void;
  setProjectsLoading: (loading: boolean) => void;
  /**
   * Write the current project. When the name changes hands, also rotate the project-level cancel scope: the previous project's in-flight requests on the old scope are cancelled
   * on the spot, and their late responses cannot write back to the store (see refreshProject's "project-level cancel scope"). This is not a pure setter;
   * all switching paths (the route effect and its cleanup, demo workspace takeover) converge here.
   *
   * 写入当前项目。名称易主时同时轮换项目级取消域：前一个项目挂在旧域上的在途请求当场
   * 取消，其迟到响应写不回 store（见 refreshProject 的「项目级取消域」）。这不是纯 setter，
   * 全部切换路径（路由 effect 及其 cleanup、演示工作台接管）经此收口。
   */
  setCurrentProject: (
    name: string | null,
    data: ProjectData | null,
    scripts?: Record<string, EpisodeScript>,
    fingerprints?: Record<string, number>,
  ) => void;
  setProjectDetailLoading: (loading: boolean) => void;
  setShowCreateModal: (show: boolean) => void;
  setCreatingProject: (creating: boolean) => void;
  updateAssetFingerprints: (fps: Record<string, number>) => void;
  getAssetFingerprint: (path: string) => number | null;
  /**
   * Refresh the current project's data into the store, returning a discriminated union result (see {@link RefreshProjectResult}).
   *
   * A single entry point that consolidates refresh semantics previously scattered across call sites, eliminating the race where "with two entry points refreshing at once, the later finisher
   * overwrites the earlier one":
   * - **In-flight merging**: only one getProject may be in flight at a time; refresh requests arriving meanwhile
   *   merge into "run one more round afterwards", taking the latest request's name / invalidateKeys.
   * - **Keep old on failure**: when getProject fails, currentProjectData is not overwritten; `"failed"` is returned for
   *   the caller to decide whether to show a hint (onError is also called).
   * - **Project-level cancel scope**: requests hang on an AbortSignal that rotates with the current project; switching projects voids the previous
   *   project's in-flight and late refreshes, returning `"cancelled"`, not treated as failure (onError not triggered).
   *
   * 刷新当前项目数据到 store，返回结果判别式联合（见 {@link RefreshProjectResult}）。
   *
   * 单一入口收敛此前各调用点分散的刷新语义，消除「两入口同时刷新时后完成者盖住
   * 先完成者」的竞态：
   * - **在途合并**：同一时刻只允许一个 getProject 在途；在途期间到达的刷新请求
   *   合并为「结束后再跑一轮」，取最新一次请求的 name / invalidateKeys。
   * - **失败留旧**：getProject 失败时不覆盖 currentProjectData，返回 `"failed"` 交
   *   调用方决定是否提示（onError 亦会被调用）。
   * - **项目级取消域**：请求挂在随当前项目轮换的 AbortSignal 上，切换项目即作废前一个
   *   项目的在途与迟到刷新，返回 `"cancelled"`，不视为失败（不触发 onError）。
   */
  refreshProject: (name: string, options?: RefreshProjectOptions) => Promise<RefreshProjectResult>;
}

export const useProjectsStore = create<ProjectsState>((set, get) => {
  // Coordination state for refresh in-flight merging (non-reactive singleton, not in store state, to avoid triggering subscriber re-renders).
  // 刷新的在途合并协调状态（非响应式单例，不进 store state，避免触发订阅重渲染）。
  let refreshRunning = false;
  let refreshQueued = false;
  let queuedName: string | null = null;
  let queuedKeys: string[] = [];
  let queuedOnErrors: Array<(err: unknown) => void> = [];
  // Each queued-round caller's own resolve, accumulated separately from curResolvers, so "queued into the same batch"
  // is not misread as "sharing the same final result": each caller is only answered by the result of the round that actually served it.
  // 排队轮调用方各自的 resolve——与 curResolvers 分开累积，避免"排队进同一批"
  // 被误解为"共享同一个最终结果"：每个调用方只对自己实际服务的那一轮结果负责。
  let queuedResolvers: Array<(result: RefreshProjectResult) => void> = [];

  // Project-level cancel scope: all data requests of the current project hang on this controller. Rotated on project switch (abort the old one,
  // install a new one), so the previous project's in-flight requests are cancelled on the spot and late responses cannot write back to the store. After rotation the active
  // controller is always un-aborted, so it never voids later refreshes for long.
  // A refresh's own successful write (name going from null to this project) also triggers a rotation, aborting the scope this round just used:
  // every later round fetches the active scope again, so there is no impact; if other project-level requests hang on it in future, re-check this.
  // 项目级取消域：当前项目的数据请求都挂在这个 controller 上。切换项目时轮换（abort 旧的、
  // 换上新的），使前一个项目的在途请求当场取消、迟到响应无法写回 store。轮换后现役
  // controller 恒为未 abort 状态，因此不会长期作废后续刷新。
  // 刷新自己写入成功时（名称由 null 变为该项目）也会走一次轮换，abort 的是本轮刚用完的域：
  // 后续每一轮都重新取现役域，故无影响；日后若有别的项目级请求挂上来，需要复核这一点。
  let projectScope = new AbortController();
  const rotateProjectScope = () => {
    projectScope.abort();
    projectScope = new AbortController();
  };

  // Run the refresh loop: a while loop of queued reruns replaces recursion, and the failure path also consumes queued requests, until nothing new is queued.
  // Resolve each round's callers (curResolvers) as soon as the round ends, without waiting for later queued rounds; otherwise earlier
  // callers would get their return value overwritten by later, unrelated rounds (e.g. a reorder refresh that already wrote successfully dragged
  // down to false by a subsequently queued, failed SSE refresh, so the UI cannot advance the selection from it).
  // 执行刷新循环：while 排队重跑替代递归，失败路径也消费排队请求，直至无新排队为止。
  // 每轮结束立刻 resolve 该轮的调用方（curResolvers），不等后续排队轮跑完——否则先到
  // 的调用方会被后到、且与自己无关的轮次结果覆盖返回值（如已成功写入的重排刷新，被
  // 随后排队、失败的 SSE 刷新拖累成 false，UI 无法据此推进选中态）。
  const runRefresh = async (
    name: string,
    keys: string[],
    onError: ((err: unknown) => void) | undefined,
    resolvers: Array<(result: RefreshProjectResult) => void>,
  ): Promise<void> => {
    let curName = name;
    let curKeys = keys;
    let curOnErrors = onError ? [onError] : [];
    let curResolvers = resolvers;
    let again = true;
    while (again) {
      again = false;
      let result: RefreshProjectResult = "cancelled";
      // Fetch the active cancel scope once per round: a queued round must hang on the scope from the moment it started, not reuse the previous round's.
      // 每轮取一次现役取消域：排队轮要挂在它自己发起时刻的域上，不复用上一轮的。
      const signal = projectScope.signal;
      try {
        const res = await API.getProject(curName, { signal });
        // When currentProjectName is null, only let it through if the store has never established any real
        // project in its whole lifecycle: that is refreshProject's job of "establishing the first project's data in the store",
        // when there is no "current project" to compare against. Once any project has been established, any later null can only be the route
        // cleanup's transition between clearing the old project and asynchronously loading the new one: in this window no write for any name is allowed
        // (including old projects earlier than the one just cleared that only settle now; see the comment on hasLoadedAnyProject),
        // otherwise old data would be written back before the new project's own data lands.
        // currentProjectName 为 null 时，只有 store 整个生命周期内还从未建立过任何真实
        // 项目才放行——那是 refreshProject 承担的「把首个项目数据建立进 store」职责，
        // 届时没有「当前项目」可比较。一旦建立过任意项目，后续任何 null 都只会是路由
        // cleanup 清空旧项目、异步加载新项目之间的过渡态：这段窗口里不放行任何名字的
        // 写入（包括比刚清空的项目更早、直到现在才落定的旧项目，见 hasLoadedAnyProject
        // 的注释），否则旧数据会在新项目自己的数据落地前抢先写回。
        const { currentProjectName, hasLoadedAnyProject } = get();
        // If during flight another project "more entitled to write" has been queued, this round's response targets an old project about to be replaced:
        // skip the write so the new project the queued round is about to load is not overwritten (neither data nor name is committed).
        // "More entitled" has two cases: the queued project is already the current project (a route switch sets the name first, then starts the refresh,
        // so the switch is already complete at this moment), or the store has no current project yet (no project established, and the latest queued
        // intent is the target). Conversely, if the queued caller holds the old pre-switch project name (refresh after a write operation,
        // SSE), it is bound to fail the current-project check below and cannot write a single byte into the store, so this round
        // must not yield to it: this round is the current project's own data, and if dropped nobody would load it again.
        // 在途期间若已排队到「更有资格写入」的另一个项目，本轮响应针对的就是即将被取代
        // 的旧项目——跳过写入，避免覆盖排队轮即将加载的新项目（数据 / 名称均不提交）。
        // 「更有资格」有两种：排队的那个项目已经是当前项目（路由切换先落名再发起刷新，
        // 切换此刻已经完成），或 store 还没有当前项目（尚未建立任何项目，排队里的最新
        // 意图就是目标）。反过来，排队者若是持切走前旧项目名的调用方（写操作完成后的
        // 刷新、SSE），它注定过不了下面的当前项目核对、一个字节也写不进 store，本轮
        // 不能为它让位——本轮才是当前项目自己的数据，丢掉后没有任何人会再加载它。
        const supersededByOtherProject =
          refreshQueued &&
          queuedName !== null &&
          queuedName !== curName &&
          (currentProjectName === null || queuedName === currentProjectName);
        // The cancel scope rotated: the current project was replaced while the request was in flight (route switched to another project, demo workspace takeover,
        // leaving the workbench cleared it). None of these paths go through refreshProject, so queue dedup cannot see them; only
        // the cancel scope can identify them. abort and response settling are both async; if the response arrives first and abort later, the network layer does not reject,
        // so aborted must be re-checked before writing.
        // Both checks above only catch the case where "the project had been switched away when the request started": if the caller (e.g. a refresh after a write
        // operation) captured the old pre-switch project name and this call started only after the switch completed,
        // it gets the new project's active, un-aborted scope; so also check before writing whether curName is still
        // the current project, preventing old project data from overwriting the new project already switched in.
        // 取消域已轮换：当前项目在请求在途期间被换掉了（路由切到别的项目、演示工作台接管、
        // 离开工作台清空）。这些路径都不经过 refreshProject，排队去重看不到它们，只能靠
        // 取消域识别。abort 与响应落定同为异步，响应先到、abort 后到时网络层不会 reject，
        // 因此写入前还要复核一次 aborted。
        // 上述两项都只能拦住「请求发起时项目已切走」的情形：若调用方（如写操作完成后
        // 的刷新）捕获的是切走前的旧项目名，且这次调用是在切换已经完成之后才发起的，
        // 拿到的会是新项目现役、未 abort 的域——因此还要在写入前核对 curName 是否仍是
        // 当前项目，防止旧项目的数据覆盖已经切入的新项目。
        const isCurrentProject =
          currentProjectName === curName || (currentProjectName === null && !hasLoadedAnyProject);
        if (!supersededByOtherProject && !signal.aborted && isCurrentProject) {
          get().setCurrentProject(curName, res.project, res.scripts ?? {}, res.asset_fingerprints);
          if (curKeys.length > 0) {
            useAppStore.getState().invalidateEntities(curKeys);
          }
          result = "success";
        }
        // Not written but not an error either (superseded / aborted / not the current project): settle as "cancelled",
        // so callers stay silent and it is not confused with a real request failure.
        // 未写入但也未出错（superseded / aborted / 非当前项目）：结算为 "cancelled"，
        // 调用方据此静默，不与真正的请求失败混淆。
      } catch (err) {
        // An abort caused by cancel-scope rotation is the normal result of a project switch, not a refresh failure: settle as
        // "cancelled" without triggering onError, otherwise every project switch would show a sync-failure toast.
        // Keep old on failure: neither result overwrites currentProjectData.
        // 取消域轮换导致的 abort 是项目切换的正常结果，不是刷新失败：结算为
        // "cancelled"，不触发 onError，否则每次切项目都会弹一条同步失败提示。
        // 失败留旧：两种结果都不覆盖 currentProjectData。
        if (signal.aborted) {
          result = "cancelled";
        } else {
          result = "failed";
          curOnErrors.forEach((cb) => {
            try {
              cb(err);
            } catch (callbackErr) {
              console.error("refreshProject onError 回调失败", callbackErr);
            }
          });
        }
      }
      curResolvers.forEach((resolve) => resolve(result));
      if (refreshQueued) {
        refreshQueued = false;
        curName = queuedName ?? curName;
        curKeys = queuedKeys;
        curOnErrors = queuedOnErrors;
        curResolvers = queuedResolvers;
        queuedName = null;
        queuedKeys = [];
        queuedOnErrors = [];
        queuedResolvers = [];
        again = true;
      }
    }
    refreshRunning = false;
  };

  return {
    projects: [],
    projectsLoading: false,
    currentProjectName: null,
    currentProjectData: null,
    currentScripts: {},
    projectDetailLoading: false,
    projectSnapshotRevisions: {},
    showCreateModal: false,
    creatingProject: false,
    assetFingerprints: {},
    hasLoadedAnyProject: false,

    setProjects: (projects) => set({ projects }),
    setProjectsLoading: (loading) => set({ projectsLoading: loading }),
    setCurrentProject: (name, data, scripts, fingerprints) => {
      // Rotate the cancel scope whenever the current project changes hands. This is the mandatory pass of all switching paths (the route effect and its cleanup, demo workspace
      // takeover), so callers need not each propagate cancellation; they are protected as they are.
      // 当前项目易主即轮换取消域。这是全部切换路径（路由 effect 及其 cleanup、演示工作台
      // 接管）的必经之处，因此不必让各调用方各自传播取消——它们保持原样即受此保护。
      if (name !== get().currentProjectName) rotateProjectScope();
      set((s) => ({
        currentProjectName: name,
        currentProjectData: data,
        currentScripts: scripts ?? {},
        projectSnapshotRevisions:
          name === null || data === null
            ? s.projectSnapshotRevisions
            : {
                ...s.projectSnapshotRevisions,
                [name]: (s.projectSnapshotRevisions[name] ?? 0) + 1,
              },
        assetFingerprints: fingerprints ?? s.assetFingerprints,
        hasLoadedAnyProject: s.hasLoadedAnyProject || name !== null,
      }));
    },
    setProjectDetailLoading: (loading) => set({ projectDetailLoading: loading }),
    setShowCreateModal: (show) => set({ showCreateModal: show }),
    setCreatingProject: (creating) => set({ creatingProject: creating }),
    updateAssetFingerprints: (fps) =>
      set((s) => ({ assetFingerprints: { ...s.assetFingerprints, ...fps } })),
    getAssetFingerprint: (path) => get().assetFingerprints[path] ?? null,

    refreshProject: (name, options) => {
      if (!name) return Promise.resolve("cancelled");
      // The guided demo project's data comes from frontend constants; there is no server state to refresh / 引导演示项目的数据来自前端常量，没有可刷新的服务端状态
      if (isDemoProject(name)) return Promise.resolve("success");
      const invalidateKeys = options?.invalidateKeys ?? [];
      return new Promise<RefreshProjectResult>((resolve) => {
        if (refreshRunning) {
          // A refresh is already in flight: merge into "run one more round afterwards", take the latest name, accumulate invalidateKeys /
          // onError / resolve; when the queued round ends, every merged caller must be notified individually,
          // rather than sharing the single result of the first or last round.
          // If a request arriving while queued points to a different project than the current queued target, the previous batch of queued requests
          // is about to be overridden by this one: by design only one slot, "run one more round afterwards", is kept, taking the latest
          // name, and no extra round runs for the overridden project. So the previous batch of queued callers is bound never
          // to be hit by the round that actually runs next; without settling early they would be merged in and share this new project's
          // result even though they never requested this project (e.g. B queued then C overrides, and B's callers
          // would wrongly receive true after C succeeds while the store never synced B's data). Settle immediately as "not synced",
          // "cancelled", not treated as a request error, so onError is not triggered.
          // 已有刷新在途：合并为「结束后再跑一轮」，取最新 name，累积 invalidateKeys /
          // onError / resolve——排队轮次结束时需各自通知全部合并进来的调用方，
          // 而非共享首轮或末轮的单一结果。
          //
          // 排队期间到达的请求若指向和当前排队目标不同的项目，说明上一批排队请求
          // 即将被本次覆盖——设计上只保留「结束后再跑一轮」这一个名额，取最新
          // name，不会再为被覆盖的项目单独多跑一轮。因此上一批排队的调用方注定不会
          // 被接下来实际执行的那一轮命中；不提前结算就会被并入并共享本次这个新项目
          // 的结果，即便它们请求的从来不是这个项目（例如 B 排队后 C 覆盖，B 的调用方
          // 会在 C 成功后错误地收到 true，但 store 从未同步过 B 的数据）。按「未同步」
          // 立即结算为 "cancelled"，不视为请求出错，因此不触发 onError。
          if (refreshQueued && queuedName !== null && queuedName !== name) {
            // The slot already holds a refresh for the current project (usually the first-screen load after a route switch), while this one brings
            // another project name: the caller holds the old pre-switch project name. Even if this round ran, it would fail
            // the current-project check and write nothing: settle immediately as "cancelled" without taking the slot. Otherwise
            // the current project's own refresh would be pushed out, and since by design only one slot is kept with no rerun for it, the current
            // project's data would never be fetched in any round (and the first screen would stay loading forever).
            // 名额上已经是当前项目的刷新（通常就是路由切换后的首屏加载），而本次带来的
            // 是别的项目名——调用方持有的是切走前的旧项目名。这一轮就算跑起来也过不了
            // 当前项目核对，写不进任何东西：立即结算为 "cancelled"，不占用名额。否则
            // 当前项目自己的刷新会被挤掉，而设计上只保留一个名额、不会为它补跑，当前
            // 项目的数据一轮都不会被拉取（首屏则永远停在加载态）。
            const { currentProjectName } = get();
            if (currentProjectName !== null && queuedName === currentProjectName && name !== currentProjectName) {
              resolve("cancelled");
              return;
            }
            const supersededResolvers = queuedResolvers;
            queuedResolvers = [];
            queuedKeys = [];
            queuedOnErrors = [];
            supersededResolvers.forEach((r) => r("cancelled"));
          }
          refreshQueued = true;
          queuedName = name;
          queuedKeys = [...queuedKeys, ...invalidateKeys];
          if (options?.onError) {
            queuedOnErrors = [...queuedOnErrors, options.onError];
          }
          queuedResolvers = [...queuedResolvers, resolve];
          return;
        }
        refreshRunning = true;
        void runRefresh(name, invalidateKeys, options?.onError, [resolve]);
      });
    },
  };
});
