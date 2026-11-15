import { create } from "zustand";
import { API } from "@/api";
import type { WorkflowPlan } from "@/types/workflow";

/**
 * Settlement result of one plan refresh. `cancelled` is not a failure: the target project/episode changed hands mid-flight,
 * the late response was dropped, and callers can handle it silently.
 *
 * 一次计划刷新的结算结果。`cancelled` 不是失败：目标项目/剧集在途中易主，
 * 迟到响应被丢弃，调用方静默处理即可。
 */
export type RefreshPlanResult = "success" | "failed" | "cancelled";

/** Target the plan applies to. Requests for the same target merge, and a target change voids in-flight requests; UIs reading the plan check `planKey` against it. / 计划的作用目标。同一目标的请求合并，目标易主则作废在途请求；读计划的界面拿它核对 `planKey`。 */
export function workflowPlanKey(projectName: string, episode: number | null): string {
  return `${projectName}::${episode ?? ""}`;
}

interface WorkflowState {
  /** The most recently fetched plan. A failed refresh does not clear it. / 最近一次成功取回的计划。刷新失败不清空它。 */
  plan: WorkflowPlan | null;
  /** Target `plan` belongs to; when it differs from the current target the UI must not state status from it. / `plan` 所属的目标；与当前目标不符时界面不得据此陈述状态。 */
  planKey: string | null;
  loading: boolean;
  /** Error text of the most recent refresh; `plan` still keeps the previous result. / 最近一次刷新的错误文案；`plan` 仍保留上一次的结果。 */
  error: string | null;
  /** Requested duration options the user has confirmed, per unit; included in the re-solve after confirming. / 用户已确认的申请档位，按 unit 给；确认后重求解会带上。 */
  confirmedDurations: Record<string, number>;

  confirmDurations: (durations: Record<string, number>) => void;
  /**
   * Fetch the target's plan.
   *
   * - **In-flight merging**: only one request per target is in flight at a time; requests arriving meanwhile merge into "run one more round afterwards".
   * - **Keep old on failure**: when a request fails, the previous `plan` is kept and only `error` is written. Results already paid for and produced
   *   do not disappear from the UI because one refresh failed.
   * - **Target cancel scope**: the cancel scope rotates on project or episode switch, so late responses for the old target cannot write back to the store.
   *
   * 取回目标的计划。
   *
   * - **在途合并**：同一目标同时只有一个请求在途，期间到达的请求合并为「结束后再跑一轮」。
   * - **失败留旧**：请求失败时保留上一次的 `plan`，只写 `error`。已经付费产出的结果
   *   不因为一次刷新失败而从界面上消失。
   * - **目标取消域**：切项目或切集时轮换取消域，旧目标的迟到响应写不回 store。
   */
  refreshPlan: (projectName: string, episode: number | null) => Promise<RefreshPlanResult>;
  /** Clear the plan when the target changes hands: facts of the previous target cannot be used to state this target. / 目标易主时清空计划：上一个目标的事实不能拿来陈述这一个目标。 */
  resetTarget: () => void;
}

export const useWorkflowStore = create<WorkflowState>((set, get) => {
  // Non-reactive coordination state: not in store state, so subscribers do not re-render for internal bookkeeping.
  // 非响应式协调状态：不进 store state，避免订阅方为内部记账重渲染。
  let running = false;
  let queued = false;
  let queuedTarget: { projectName: string; episode: number | null } | null = null;
  let queuedResolvers: Array<(result: RefreshPlanResult) => void> = [];
  // Current request target: unlike `planKey`, it is registered when the request is sent, not updated only on success.
  // A target switch during the first load (`planKey` still null) must be recognized as a target change:
  // looking only at `planKey` would let this switch through because it is also null at that point, letting the old target's
  // late response write back to the store.
  // 当前请求目标：与 `planKey` 不同，它在请求发出时就登记，不等成功才更新。
  // 首次加载（`planKey` 仍是 null）期间切换目标必须能被识别为目标易主——
  // 只看 `planKey` 会因为它此时同样是 null 而放过这次切换，任由旧目标的
  // 迟到响应写回 store。
  let currentTarget: { projectName: string; episode: number | null } | null = null;

  let scope = new AbortController();
  const rotateScope = () => {
    scope.abort();
    scope = new AbortController();
  };

  const runRefresh = async (
    projectName: string,
    episode: number | null,
    resolvers: Array<(result: RefreshPlanResult) => void>,
  ): Promise<void> => {
    let curProject = projectName;
    let curEpisode = episode;
    let curResolvers = resolvers;
    let again = true;
    // Bind the cancel scope this iteration actually uses: `resetTarget` already explicitly reset `running` once when rotating the scope;
    // if this instance cleared the flag unconditionally on finishing without checking it still holds the current scope,
    // it would knock out the taker's (the instance running for the new target) in-flight flag, causing concurrent requests for the same target
    // and inverted write-back order. The merge loop rebinds every round, so an instance that took over a queued target can still, when it finishes naturally,
    // correctly clear the (possibly rotated) cancel scope it actually holds.
    // 绑定本轮迭代实际使用的取消域：`resetTarget` 轮换取消域时已经显式复位过
    // `running` 一次，若此实例结束时不核对自己仍持有当前取消域就无条件清标志，
    // 会把接管者（新目标下正在跑的实例）的在途标志顶掉，引发同目标并发请求、
    // 写回顺序倒挂。合并循环每轮重新绑定，让接手了排队目标的实例在自然完结时
    // 仍能正确清掉它实际持有的（可能是轮换后的）取消域。
    let ownScope = scope;
    while (again) {
      again = false;
      ownScope = scope;
      let result: RefreshPlanResult;
      const signal = ownScope.signal;
      const key = workflowPlanKey(curProject, curEpisode);
      try {
        const plan = await API.getWorkflowPlan(
          curProject,
          {
            episode_id: curEpisode,
            confirmed_request_durations: get().confirmedDurations,
          },
          { signal },
        );
        if (signal.aborted) {
          result = "cancelled";
        } else {
          set({ plan, planKey: key, error: null, loading: false });
          result = "success";
        }
      } catch (err) {
        if (signal.aborted) {
          result = "cancelled";
        } else {
          // Keep old: only write error, leave plan / planKey untouched. / 留旧：只写 error，不动 plan / planKey。
          set({ error: err instanceof Error ? err.message : String(err), loading: false });
          result = "failed";
        }
      }
      for (const resolve of curResolvers) resolve(result);
      // An old instance voided by `resetTarget` must not consume queued requests here either: `queued` is
      // a global flag; without checking the cancel scope, a late old instance would grab the queued request that the instance really taking over
      // the target under the new scope should serve, rebind to the current `scope` and send a second real request for the same
      // target, concurrent with the taker's own in-flight request, so the write-back order
      // is no longer guaranteed. Let the taker consume the queue itself.
      // 被 `resetTarget` 作废的旧实例到这里也不得消费排队请求：`queued` 是
      // 全局标志，若不核对取消域，姗姗来迟的旧实例会把新取消域下真正接管
      // 该目标的实例应该服务的排队请求抢走，重新绑定到当前 `scope` 后对同一
      // 目标发出第二个真实请求——与接管实例自己在途的请求并发，写回顺序
      // 不再有保证。让接管实例自己去消费队列。
      if (scope !== ownScope) return;
      if (queued && queuedTarget) {
        again = true;
        curProject = queuedTarget.projectName;
        curEpisode = queuedTarget.episode;
        curResolvers = queuedResolvers;
        queued = false;
        queuedTarget = null;
        queuedResolvers = [];
      }
    }
    // Only the instance still holding the last round's cancel scope clears running; an old instance voided by `resetTarget`
    // reads a `scope` here that is already the new cancel scope and skips: it is not the new instance's taker.
    // 只有仍持有最后一轮取消域的实例才清 running；被 `resetTarget` 作废的旧实例
    // 在这里读到的 `scope` 已经是新取消域，跳过——它不是新实例的接管者。
    if (scope === ownScope) running = false;
  };

  return {
    plan: null,
    planKey: null,
    loading: false,
    error: null,
    confirmedDurations: {},

    confirmDurations: (durations) =>
      set((s) => ({ confirmedDurations: { ...s.confirmedDurations, ...durations } })),

    resetTarget: () => {
      rotateScope();
      running = false;
      queued = false;
      queuedTarget = null;
      currentTarget = null;
      const resolvers = queuedResolvers;
      queuedResolvers = [];
      for (const resolve of resolvers) resolve("cancelled");
      set({
        plan: null,
        planKey: null,
        error: null,
        loading: false,
        confirmedDurations: {},
      });
    },

    refreshPlan: (projectName, episode) => {
      // Target change: judge by "the target registered for this request", not `planKey`: `planKey`
      // only updates after a request succeeds and is always null during the first load; using it as the criterion, a target switch
      // during the first load would skip resetTarget because "both reads are null", and the old target's
      // response would still be written into the store when it comes back.
      // 目标易主：按「本次请求登记的目标」判断，不用 `planKey`——`planKey`
      // 只在请求成功后才更新，首次加载期间它恒为 null，若拿它做判据，首次
      // 加载途中切换目标会因为「两次都读到 null」而放过 resetTarget，旧目标
      // 的响应回来后仍会写进 store。
      if (
        currentTarget !== null
        && (currentTarget.projectName !== projectName || currentTarget.episode !== episode)
      ) {
        get().resetTarget();
      }
      currentTarget = { projectName, episode };
      set({ loading: true });
      return new Promise<RefreshPlanResult>((resolve) => {
        if (running) {
          queued = true;
          queuedTarget = { projectName, episode };
          queuedResolvers.push(resolve);
          return;
        }
        running = true;
        void runRefresh(projectName, episode, [resolve]);
      });
    },
  };
});
