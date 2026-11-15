import { describe, it, expect, vi, beforeEach } from "vitest";
import { API } from "@/api";
import { useWorkflowStore } from "./workflow-store";
import { makePlan } from "@/test/factories";

beforeEach(() => {
  useWorkflowStore.getState().resetTarget();
});

describe("workflow-store", () => {
  it("同一目标的并发刷新合并为两轮，不各自发一串请求", async () => {
    const spy = vi.spyOn(API, "getWorkflowPlan").mockResolvedValue(makePlan());
    const store = useWorkflowStore.getState();
    const results = await Promise.all([
      store.refreshPlan("proj", 1),
      store.refreshPlan("proj", 1),
      store.refreshPlan("proj", 1),
    ]);
    expect(results).toEqual(["success", "success", "success"]);
    // The first round + one merged catch-up round, not three
    // 首轮 + 合并后的一轮补跑，而不是三轮
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("刷新失败保留上一次的计划，只写错误", async () => {
    const plan = makePlan();
    const spy = vi.spyOn(API, "getWorkflowPlan").mockResolvedValue(plan);
    await useWorkflowStore.getState().refreshPlan("proj", 1);
    expect(useWorkflowStore.getState().plan).toEqual(plan);

    spy.mockRejectedValueOnce(new Error("offline"));
    expect(await useWorkflowStore.getState().refreshPlan("proj", 1)).toBe("failed");
    expect(useWorkflowStore.getState().plan).toEqual(plan);
    expect(useWorkflowStore.getState().error).toBe("offline");
  });

  it("换目标时清掉上一个目标的计划与本次请求选择", async () => {
    vi.spyOn(API, "getWorkflowPlan").mockResolvedValue(makePlan());
    const store = useWorkflowStore.getState();
    await store.refreshPlan("proj", 1);
    store.confirmDurations({ E1U1: 8 });
    expect(useWorkflowStore.getState().planKey).toBe("proj::1");

    await useWorkflowStore.getState().refreshPlan("proj", 2);
    expect(useWorkflowStore.getState().planKey).toBe("proj::2");
    // The confirmed option is the choice for "this request"; changing target voids it, and it does not follow to the next episode
    // 已确认档位是「本次请求」的选择，换目标即作废，不跟着走到下一集
    expect(useWorkflowStore.getState().confirmedDurations).toEqual({});
  });

  it("首次加载期间切换目标：旧目标的迟到响应作废，不写进 store", async () => {
    // planKey starts as null and is updated only after success. If the target-switch criterion looked only at planKey,
    // switching to B during the first load (A has not succeeded yet) would be judged as no change because "both reads give null",
    // and A's late response would be written into plan/planKey as facts of the current target.
    // planKey 起始为 null，且只在成功后才更新。若目标切换判据只看 planKey，
    // 首次加载（A 还没成功过）期间切到 B 会因为「两次都读到 null」被判定为
    // 未易主，A 的迟到响应会被当成当前目标的事实写进 plan/planKey。
    const planA = makePlan();
    const planB = makePlan();
    let resolveA!: (plan: ReturnType<typeof makePlan>) => void;
    let resolveB!: (plan: ReturnType<typeof makePlan>) => void;
    vi.spyOn(API, "getWorkflowPlan").mockImplementation((project) =>
      project === "A"
        ? new Promise((resolve) => { resolveA = resolve; })
        : new Promise((resolve) => { resolveB = resolve; }),
    );
    const store = useWorkflowStore.getState();

    // A::1 is the first request sent in this session: planKey is still null.
    // A::1 是这次会话里第一次发出的请求：planKey 仍是 null。
    const pA = store.refreshPlan("A", 1);
    await Promise.resolve();

    // Switch to B before A lands; exactly the path of quickly switching episodes during the first load.
    // 还没等 A 落地就切到 B——这正是首次加载途中快速切集的路径。
    const pB = store.refreshPlan("B", 1);
    await Promise.resolve();

    // A's response arrives late: it must be judged cancelled and must not overwrite the store.
    // A 的响应姗姗来迟：必须判定为 cancelled，且不得覆盖 store。
    resolveA(planA);
    expect(await pA).toBe("cancelled");
    await Promise.resolve();
    expect(useWorkflowStore.getState().planKey).not.toBe("A::1");
    expect(useWorkflowStore.getState().plan).not.toEqual(planA);

    resolveB(planB);
    expect(await pB).toBe("success");
    expect(useWorkflowStore.getState().planKey).toBe("B::1");
    expect(useWorkflowStore.getState().plan).toEqual(planB);
  });

  it("running 标志绑定取消域，旧实例的迟到结算不清掉新目标的在途标志（不产生同目标并发请求）", async () => {
    // Repro path: A::1 already has one successful plan; refresh the same target again (no resetTarget,
    // running=true, bound to the current cancellation domain); then immediately switch to B (resetTarget rotates the cancellation domain,
    // explicitly resets running, and starts an instance bound to the new domain); then the response of A's old instance arrives
    // late and is judged cancelled by abort; if it cleared running unconditionally on exit, then in the window where B has not
    // succeeded yet and store.planKey is still the old "A::1", the next refresh of A would misread
    // running===false and send a second real request, concurrent with the still in-flight B request.
    // 复现路径：A::1 已有一次成功计划；同目标再刷新一次（不触发 resetTarget，
    // running=true，绑定当前取消域）；紧接着切到 B（resetTarget 轮换取消域、
    // 显式复位 running，再起跑绑定新取消域的实例）；随后 A 那个旧实例的响应姗姗
    // 来迟，被 abort 判定为 cancelled——它退出时若无条件清 running，会在 B 仍未
    // 成功、store.planKey 仍是旧值 "A::1" 的窗口里，让下一次对 A 的刷新误判
    // running===false 而发出第二个真实请求，与仍在途的 B 请求并发。
    const planA = makePlan();
    const planB = makePlan();
    const pendingByProject: Record<string, Array<(plan: ReturnType<typeof makePlan>) => void>> = {
      A: [],
      B: [],
    };
    let inFlight = 0;
    let maxInFlight = 0;
    const spy = vi.spyOn(API, "getWorkflowPlan").mockImplementation((project: string) => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      return new Promise((resolve) => {
        pendingByProject[project as "A" | "B"].push((plan) => {
          inFlight -= 1;
          resolve(plan);
        });
      });
    });
    const settle = (project: "A" | "B", plan: ReturnType<typeof makePlan>) => {
      const fn = pendingByProject[project].shift();
      fn?.(plan);
    };
    const flush = async (times = 3) => {
      for (let i = 0; i < times; i += 1) await Promise.resolve();
    };

    const store = useWorkflowStore.getState();

    // 1) Establish the starting point: A::1 already has one successful plan.
    // 1) 建立起点：A::1 已有一次成功的计划。
    const initial = store.refreshPlan("A", 1);
    await flush();
    settle("A", planA);
    expect(await initial).toBe("success");
    expect(useWorkflowStore.getState().planKey).toBe("A::1");

    // 2) Refresh the same target again: the key equals the stored planKey, so resetTarget is not triggered;
    //    running=true, and the instance is bound to the current cancellation domain.
    // 2) 同目标再次刷新：key 与已存的 planKey 相同，不触发 resetTarget；
    //    running=true，实例绑定当前取消域。
    const p1 = store.refreshPlan("A", 1);
    await flush();

    // 3) The target switches to B: resetTarget rotates the cancellation domain, resets running, and starts an instance bound to the new domain.
    // 3) 目标切到 B：resetTarget 轮换取消域、复位 running，起跑绑定新取消域的实例。
    const p2 = store.refreshPlan("B", 1);
    await flush();

    // 4) The old instance from step 2 settles late: the cancellation domain it is bound to has been voided by resetTarget,
    //    so it should be judged cancelled; on exit it must not knock out the running of step 3's still in-flight instance.
    // 4) 步骤 2 的旧实例姗姗来迟结算：它绑定的取消域已被 resetTarget 作废，
    //    应判定为 cancelled；退出时不得顶掉步骤 3 那个仍在途的实例的 running。
    settle("A", planA);
    expect(await p1).toBe("cancelled");
    await flush();

    // 5) The target is now B (the currentTarget registered in step 3); refreshing B again takes the
    //    "same-target merge" branch rather than resetTarget. If running were wrongly cleared by step 4, this
    //    would immediately send a second real request for B, concurrent with step 3's still in-flight request; if running
    //    was not wrongly cleared, this request should merge and queue, sent only after step 3's round finishes.
    // 5) 目标此时是 B（步骤 3 登记的 currentTarget），再次刷新 B 走的是
    //    「同目标合并」分支而非 resetTarget。running 若被步骤 4 误清，这里
    //    会立刻对 B 发出第二个真实请求，与仍在途的步骤 3 请求并发；running
    //    未被误清时，这次请求应合并排队，等步骤 3 那一轮跑完才发出。
    const p3 = store.refreshPlan("B", 1);
    await flush();
    // Key assertion: before step 3's request settles, no extra real request should have been sent.
    // 关键断言：步骤 3 的请求尚未结算前，不应该已经多发出一次真实请求。
    expect(spy).toHaveBeenCalledTimes(3);
    expect(pendingByProject.B).toHaveLength(1);

    settle("B", planB);
    await flush();
    // Only after step 3's request finishes does the queued B request get a catch-up round; that is the 4th real request.
    // 步骤 3 的请求完结后才为排队的 B 请求补跑一轮，这才是第 4 次真实请求。
    expect(spy).toHaveBeenCalledTimes(4);
    settle("B", planB);

    const [r2, r3] = await Promise.all([p2, p3]);
    expect(r2).toBe("success");
    expect(r3).toBe("success");
    // At most one real network request is in flight at any time; in the step 2/3 handover window the old instance's
    // request is aborted but its response has not landed yet, which is the normal wind-down window of the network request itself,
    // not the concurrent duplicate request this invariant guards against.
    // 任意时刻最多只有一个真实网络请求在途——步骤 2/3 交接窗口里旧实例的
    // 请求虽已被 abort 但响应尚未落地，那是网络请求本身的正常收尾窗口，
    // 不是本条不变量要防的并发重复请求。
    expect(maxInFlight).toBe(2);
  });

  it("被作废的旧实例不消费新取消域下的排队请求（不产生同目标并发请求）", async () => {
    // Repro path: while A is in flight the target switches to B (resetTarget rotates the cancellation domain and starts a takeover instance);
    // then another refresh is sent for B; since the takeover instance is still running this request queues; then
    // A's old instance settles late. If the old instance consumed the queued request without checking whether it still holds the current cancellation domain,
    // it would steal the B request that the takeover instance should serve, rebind it to the current
    // scope and send a second real request for B, concurrent with the takeover instance's own in-flight request.
    // 复现路径：A 在途时目标切到 B（resetTarget 轮换取消域，起跑接管实例）；
    // 紧接着对 B 再发一次刷新——由于接管实例仍在跑，这次请求排队等待；随后
    // A 的旧实例姗姗来迟结算。若旧实例不核对自己是否仍持有当前取消域就去
    // 消费排队请求，会把本该由接管实例服务的 B 请求抢走，重新绑定到当前
    // scope 后对 B 发出第二个真实请求——与接管实例自己在途的请求并发。
    const planA = makePlan();
    const planB1 = makePlan();
    const planB2 = makePlan();
    const pendingByProject: Record<string, Array<(plan: ReturnType<typeof makePlan>) => void>> = {
      A: [],
      B: [],
    };
    let inFlight = 0;
    let maxInFlight = 0;
    const spy = vi.spyOn(API, "getWorkflowPlan").mockImplementation((project: string) => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      return new Promise((resolve) => {
        pendingByProject[project as "A" | "B"].push((plan) => {
          inFlight -= 1;
          resolve(plan);
        });
      });
    });
    const settle = (project: "A" | "B", plan: ReturnType<typeof makePlan>) => {
      const fn = pendingByProject[project].shift();
      fn?.(plan);
    };
    const flush = async (times = 3) => {
      for (let i = 0; i < times; i += 1) await Promise.resolve();
    };

    const store = useWorkflowStore.getState();

    // 1) A starts, in flight (first load, currentTarget=A).
    // 1) A 起跑，在途（首次加载，currentTarget=A）。
    const pA = store.refreshPlan("A", 1);
    await flush();

    // 2) The target switches to B: resetTarget voids A's cancellation domain and starts the takeover instance (2nd real request).
    // 2) 目标切到 B：resetTarget 作废 A 的取消域，起跑接管实例（第 2 次真实请求）。
    const pB1 = store.refreshPlan("B", 1);
    await flush();

    // 3) Refresh B again right away: the takeover instance is still running, so it queues and does not send a 3rd real request.
    // 3) 紧接着再刷新一次 B：接管实例仍在跑，排队等待，不发第 3 次真实请求。
    const pB2 = store.refreshPlan("B", 1);
    await flush();
    expect(spy).toHaveBeenCalledTimes(2);

    // 4) A's old instance settles late: it should be judged cancelled and must not steal the queue from step 3.
    // 4) A 的旧实例姗姗来迟结算：应判定为 cancelled，且不得抢走步骤 3 排的队。
    settle("A", planA);
    expect(await pA).toBe("cancelled");
    await flush();
    // Key assertion: after the old instance settles, no extra real request should have been sent; the queued B request
    // can be sent only by the takeover instance (the one serving step 2) after its own request finishes.
    // 关键断言：旧实例结算后，不应该已经多发出一次真实请求——排队的 B 请求
    // 只能由接管实例（服务步骤 2 的那个）在它自己的请求完结后才去发出。
    expect(spy).toHaveBeenCalledTimes(2);
    expect(maxInFlight).toBe(2);

    // 5) Only after the takeover instance's own request finishes does the queued B request get a catch-up round (3rd real request).
    // 5) 接管实例自己的请求完结，才为排队的 B 请求补跑一轮（第 3 次真实请求）。
    settle("B", planB1);
    await flush();
    expect(spy).toHaveBeenCalledTimes(3);
    settle("B", planB2);

    const [rB1, rB2] = await Promise.all([pB1, pB2]);
    expect(rB1).toBe("success");
    expect(rB2).toBe("success");
    expect(useWorkflowStore.getState().plan).toEqual(planB2);
    expect(maxInFlight).toBe(2);
  });

  it("求解带上本次请求的已确认档位", async () => {
    const spy = vi.spyOn(API, "getWorkflowPlan").mockResolvedValue(makePlan());
    const store = useWorkflowStore.getState();
    store.confirmDurations({ E1U1: 8 });
    await useWorkflowStore.getState().refreshPlan("proj", 1);
    expect(spy).toHaveBeenCalledWith(
      "proj",
      { episode_id: 1, confirmed_request_durations: { E1U1: 8 } },
      expect.anything(),
    );
  });
});
