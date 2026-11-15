import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { DEMO_PROJECT_NAME } from "@/onboarding/demo-project";
import type { ProjectData } from "@/types";
import { createDeferred } from "@/test/deferred";

type GetProjectResult = Awaited<ReturnType<typeof API.getProject>>;

function makeProject(title: string): ProjectData {
  return {
    title,
    content_mode: "narration",
    style: "Anime",
    episodes: [],
    characters: {},
    scenes: {},
    props: {},
  };
}

function makeResult(title: string, fingerprints: Record<string, number> = {}): GetProjectResult {
  return { project: makeProject(title), scripts: {}, asset_fingerprints: fingerprints };
}

// Flush the microtask + timer queues so the in-flight refresh's continuation advances to the next await.
// 冲刷 microtask + timer 队列，让在途刷新的续跑推进到下一次 await。
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("projects-store refreshProject", () => {
  beforeEach(() => {
    useProjectsStore.setState(useProjectsStore.getInitialState(), true);
    useAppStore.setState(useAppStore.getInitialState(), true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("空 name 直接返回 cancelled，不发请求", async () => {
    const spy = vi.spyOn(API, "getProject");
    const result = await useProjectsStore.getState().refreshProject("");
    expect(result).toBe("cancelled");
    expect(spy).not.toHaveBeenCalled();
  });

  it("成功时写入 currentProject 并返回 success", async () => {
    vi.spyOn(API, "getProject").mockResolvedValue(makeResult("Demo", { "a.png": 1 }));
    const result = await useProjectsStore.getState().refreshProject("demo");
    expect(result).toBe("success");
    const s = useProjectsStore.getState();
    expect(s.currentProjectName).toBe("demo");
    expect(s.currentProjectData?.title).toBe("Demo");
    expect(s.getAssetFingerprint("a.png")).toBe(1);
    expect(s.projectSnapshotRevisions.demo).toBe(1);
  });

  it("同一项目每次完整快照落定时推进其独立修订", () => {
    const store = useProjectsStore.getState();
    store.setCurrentProject("A", null);
    expect(useProjectsStore.getState().projectSnapshotRevisions).toEqual({});

    store.setCurrentProject("A", makeProject("A-1"), {}, {});
    store.setCurrentProject("A", makeProject("A-2"), {}, {});
    store.setCurrentProject("B", null);
    store.setCurrentProject("B", makeProject("B-1"), {}, {});

    expect(useProjectsStore.getState().projectSnapshotRevisions).toEqual({ A: 2, B: 1 });
  });

  it("成功后按 invalidateKeys 失效实体版本", async () => {
    vi.spyOn(API, "getProject").mockResolvedValue(makeResult("Demo"));
    const result = await useProjectsStore
      .getState()
      .refreshProject("demo", { invalidateKeys: ["segment:S1", "character:hero"] });
    expect(result).toBe("success");
    const app = useAppStore.getState();
    expect(app.getEntityRevision("segment:S1")).toBe(1);
    expect(app.getEntityRevision("character:hero")).toBe(1);
  });

  it("失败留旧：不覆盖 currentProjectData，返回 failed，onError 收到错误", async () => {
    useProjectsStore.getState().setCurrentProject("demo", makeProject("旧"), {}, {});
    const err = new Error("boom");
    vi.spyOn(API, "getProject").mockRejectedValue(err);
    const onError = vi.fn();
    const result = await useProjectsStore.getState().refreshProject("demo", { onError });
    expect(result).toBe("failed");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("旧");
    expect(onError).toHaveBeenCalledWith(err);
  });

  it("在途合并：在途期间的多次请求只多触发一次 getProject，最终反映最新一轮", async () => {
    const d1 = createDeferred<GetProjectResult>();
    const d2 = createDeferred<GetProjectResult>();
    const spy = vi
      .spyOn(API, "getProject")
      .mockReturnValueOnce(d1.promise)
      .mockReturnValueOnce(d2.promise);

    const store = useProjectsStore.getState();
    const p1 = store.refreshProject("demo"); // owner: starts the first round / owner：发起第一轮
    const p2 = store.refreshProject("demo"); // in flight -> merged / 在途 → 合并
    const p3 = store.refreshProject("demo"); // in flight -> merged / 在途 → 合并
    // Only the first round's request was sent during merging
    // 合并期间只发起了第一轮请求
    expect(spy).toHaveBeenCalledTimes(1);

    d1.resolve(makeResult("R1"));
    await flush();
    // Queued requests converge into "run one more round afterwards", and the second round has now started
    // 排队请求收敛为「结束后再跑一轮」，此刻第二轮已发起
    expect(spy).toHaveBeenCalledTimes(2);

    d2.resolve(makeResult("R2"));
    const [r1, r2, r3] = await Promise.all([p1, p2, p3]);
    expect([r1, r2, r3]).toEqual(["success", "success", "success"]);
    // 3 refresh intents merged into 2 requests; the store settles on the latest round
    // 3 个刷新意图合并为 2 次请求，store 落定在最新一轮
    expect(spy).toHaveBeenCalledTimes(2);
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("R2");
  });

  it("首轮失败、排队轮成功时用新值替换旧值；各调用方返回自己那一轮的结果", async () => {
    useProjectsStore.getState().setCurrentProject("demo", makeProject("旧"), {}, {});
    const d1 = createDeferred<GetProjectResult>();
    const d2 = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise);

    const store = useProjectsStore.getState();
    const p1 = store.refreshProject("demo");
    const p2 = store.refreshProject("demo"); // merged -> one more round afterwards / 合并 → 结束后再跑一轮

    d1.reject(new Error("first fail"));
    await flush();
    // First round fails: keep the old data
    // 第一轮失败：留旧
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("旧");

    d2.resolve(makeResult("新"));
    const [r1, r2] = await Promise.all([p1, p2]);
    // The first-round caller gets its own round's real result (failure), not overwritten by the queued round's later success;
    // the queued-round caller gets its own round's result (success).
    // 首轮调用方拿到自己那一轮的真实结果（失败），不因排队轮后续成功被覆盖；
    // 排队轮调用方拿到自己那一轮的结果（成功）。
    expect(r1).toBe("failed");
    expect(r2).toBe("success");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("新");
  });

  it("首轮成功、排队轮失败时，首轮调用方仍返回 success（不被无关的后续轮次拖累）", async () => {
    const d1 = createDeferred<GetProjectResult>();
    const d2 = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise);

    const store = useProjectsStore.getState();
    // E.g. handleMoveShot: starts a refresh after reordering and relies on the return value to advance the selection.
    // 例如 handleMoveShot：重排后发起刷新，依赖返回值推进选中态。
    const pMoveShot = store.refreshProject("demo");
    // Another intent arriving during merging (e.g. an SSE refresh), which then fails.
    // 合并期间到达的另一意图（如 SSE 刷新），随后失败。
    const pSse = store.refreshProject("demo");

    d1.resolve(makeResult("重排后"));
    await flush();
    // The first round has written successfully
    // 首轮已成功写入
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("重排后");

    d2.reject(new Error("sse round fail"));
    const [okMoveShot, okSse] = await Promise.all([pMoveShot, pSse]);
    // The first-round caller gets its own round's real result (success), not overwritten by the queued round's later failure.
    // 首轮调用方拿到自己那一轮的真实结果（成功），不因排队轮后续失败被覆盖。
    expect(okMoveShot).toBe("success");
    expect(okSse).toBe("failed");
    // Keep old on failure: the store still holds the first round's data, not cleared or rolled back by the queued round's failure.
    // 失败留旧：store 仍保留首轮写入的数据，不因排队轮失败被清空或回滚。
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("重排后");
  });

  it("排队期间被更晚的不同项目请求取代：被取代的调用方立即收到 cancelled，不与新项目的结果混同", async () => {
    const dA = createDeferred<GetProjectResult>();
    const dC = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(dA.promise).mockReturnValueOnce(dC.promise);

    const store = useProjectsStore.getState();
    const pA = store.refreshProject("A"); // in progress / 发起中
    const pB = store.refreshProject("B"); // queued -> queuedName = B / 排队 → queuedName = B
    const pC = store.refreshProject("C"); // a different project arrives while queued: B is superseded by C / 排队期间到达不同项目：B 被 C 取代

    // B's caller need not wait for A / C to settle; it receives cancelled the moment it is superseded;
    // the project it asked for was never actually fetched and must not be merged into C's result.
    // B 的调用方无需等 A / C 落定，在被取代的一刻就立即收到 cancelled——
    // 它请求的项目从未被真正拉取过，不能被并入 C 的结果。
    const okB = await pB;
    expect(okB).toBe("cancelled");
    // Only A's request was sent; B had not had its turn when superseded and produced no request.
    // 只发起了 A 的请求；B 被取代时尚未轮到它，不产生任何请求。
    expect(API.getProject).toHaveBeenCalledTimes(1);

    dA.resolve(makeResult("A-数据"));
    await flush();
    // A's response arrives but the queued target is already C: no commit (existing behavior), and C's request has been sent.
    // A 的响应到达，但排队目标已是 C：不提交（既有行为），且已发起 C 的请求。
    expect(useProjectsStore.getState().currentProjectData?.title).not.toBe("A-数据");
    expect(API.getProject).toHaveBeenCalledTimes(2);

    dC.resolve(makeResult("C-数据"));
    const [okA, okC] = await Promise.all([pA, pC]);
    expect(okA).toBe("cancelled");
    expect(okC).toBe("success");
    expect(useProjectsStore.getState().currentProjectName).toBe("C");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("C-数据");
  });

  it("跨项目合并：A 在途时排队刷新 B，A 的响应不写入 store（避免覆盖排队中的 B）", async () => {
    useProjectsStore.getState().setCurrentProject("B", makeProject("B-旧"), {}, {});
    const dA = createDeferred<GetProjectResult>();
    const dB = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(dA.promise).mockReturnValueOnce(dB.promise);

    const store = useProjectsStore.getState();
    const pA = store.refreshProject("A");
    const pB = store.refreshProject("B"); // merged -> queued with a different name / 合并 → 排队到不同名称

    dA.resolve(makeResult("A-数据"));
    await flush();
    // A's response arrives but the queued target is already a different project B: no commit, the store still has B's old data
    // A 的响应到达，但排队目标已是不同项目 B：不提交，store 仍是 B 的旧数据
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-旧");

    dB.reject(new Error("B failed"));
    const [okA, okB] = await Promise.all([pA, pB]);
    expect(okA).toBe("cancelled");
    expect(okB).toBe("failed");
    // B's round fails: keep old, still B's old data, and it must never become A's data
    // B 轮失败：留旧，仍是 B 的旧数据，绝不能变成 A 的数据
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-旧");
  });

  it("排队轮 onError：首轮无回调、排队轮有回调且失败时通知排队轮回调", async () => {
    const d1 = createDeferred<GetProjectResult>();
    const d2 = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise);
    const onError2 = vi.fn();

    const store = useProjectsStore.getState();
    const p1 = store.refreshProject("demo"); // first round has no onError / 首轮无 onError
    const p2 = store.refreshProject("demo", { onError: onError2 }); // queued round carries onError / 排队轮带 onError

    d1.resolve(makeResult("R1"));
    await flush();
    const err = new Error("round2 fail");
    d2.reject(err);
    await Promise.all([p1, p2]);
    expect(onError2).toHaveBeenCalledWith(err);
  });

  it("合并期间累积 invalidateKeys：排队轮成功后一并失效", async () => {
    const d1 = createDeferred<GetProjectResult>();
    const d2 = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise);

    const store = useProjectsStore.getState();
    const p1 = store.refreshProject("demo", { invalidateKeys: ["segment:S1"] });
    const p2 = store.refreshProject("demo", { invalidateKeys: ["segment:S2"] });

    d1.resolve(makeResult("R1"));
    await flush();
    d2.resolve(makeResult("R2"));
    await Promise.all([p1, p2]);

    const app = useAppStore.getState();
    // The first round invalidates S1; the queued round brings S2 along (S1 is not counted again in the queued round)
    // 第一轮失效 S1；排队轮把 S2 带上（S1 不重复计入排队轮）
    expect(app.getEntityRevision("segment:S1")).toBe(1);
    expect(app.getEntityRevision("segment:S2")).toBe(1);
  });

  it("真实项目的刷新在只读演示项目接管当前视图后落定：不覆盖回真实项目", async () => {
    // Simulated scenario: SSE onChanges triggered refreshProject for a real project, and while the request was in flight the user
    // navigated into the read-only demo workbench (the demo project does not go through refreshProject, but calls
    // setCurrentProject directly with frontend constants); when the late real-project response settles it should not
    // write currentProjectName back to the real project, or the read-only banner/demo data would be quietly displaced.
    // 模拟场景：SSE onChanges 触发了真实项目的 refreshProject，请求在途期间用户
    // 导航进了只读演示工作台（演示项目不经过 refreshProject，直接调用
    // setCurrentProject 写入前端常量）——迟到的真实项目响应落定时不该把
    // currentProjectName 写回真实项目，否则只读横幅/演示数据会被悄悄顶掉。
    const d = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(d.promise);

    const store = useProjectsStore.getState();
    const p = store.refreshProject("real-project");

    store.setCurrentProject(DEMO_PROJECT_NAME, makeProject("演示"), {}, {});

    d.resolve(makeResult("迟到的真实数据"));
    const ok = await p;

    expect(ok).toBe("cancelled");
    expect(useProjectsStore.getState().currentProjectName).toBe(DEMO_PROJECT_NAME);
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("演示");
  });

  it("项目切换后 A 的迟到响应落定：不覆盖已接管的 B", async () => {
    // Project switches at the route layer do not go through refreshProject (they have their own AbortController + setCurrentProject),
    // so queue dedupe cannot see them; if a refresh triggered by SSE / writes on page A settles only after switching to B,
    // it should not write currentProjectName back to A.
    // 路由层的项目切换不经过 refreshProject（自带 AbortController + setCurrentProject），
    // 因此排队去重看不到它——A 页面上 SSE / 写操作触发的刷新若在切到 B 之后才落定，
    // 不该把 currentProjectName 写回 A。
    useProjectsStore.getState().setCurrentProject("A", makeProject("A-数据"), {}, {});
    const dA = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(dA.promise);

    const store = useProjectsStore.getState();
    const pA = store.refreshProject("A");

    // The route switches to B: cleanup clears the current project, then writes B's data.
    // 路由切到 B：cleanup 清空当前项目，随后写入 B 的数据。
    store.setCurrentProject(null, null);
    store.setCurrentProject("B", makeProject("B-数据"), {}, {});

    dA.resolve(makeResult("A-迟到数据"));
    const ok = await pA;

    expect(ok).toBe("cancelled");
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-数据");
  });

  it("null 过渡窗口内发起的旧项目刷新：不豁免为「无当前项目」，不写回", async () => {
    // Scenario raised by Codex: router cleanup first clears currentProjectName to null, and there is an async window before B's own
    // getProject settles and writes currentProjectName. If an uncancelled write in A happens to call refreshProject("A") in that window,
    // it gets the active, non-aborted domain after clearing, and currentProjectName is exactly null; it must not be exempted
    // by "no current project, let it through", or A's data would be written back before B lands.
    // Codex 提出的场景：router cleanup 先把 currentProjectName 清为 null，B 自己的
    // getProject 落定、写入 currentProjectName 之前有一段异步窗口。若 A 中未取消的
    // 写操作恰好在这段窗口里发起 refreshProject("A")，此时取到的是清空后现役、未 abort
    // 的域，且 currentProjectName 恰为 null——不能被「无当前项目，放行」豁免，否则
    // A 的数据会在 B 落地前抢先写回。
    useProjectsStore.getState().setCurrentProject("A", makeProject("A-数据"), {}, {});
    useProjectsStore.getState().setCurrentProject(null, null); // route cleanup: cleared but B not loaded yet / 路由 cleanup：清空但尚未加载 B

    const dA = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(dA.promise);

    const pA = useProjectsStore.getState().refreshProject("A"); // started within the null window / 在 null 窗口内发起
    dA.resolve(makeResult("A-迟到数据"));
    const ok = await pA;

    expect(ok).toBe("cancelled");
    expect(useProjectsStore.getState().currentProjectName).toBe(null);
    expect(useProjectsStore.getState().currentProjectData).toBe(null);

    // B lands afterwards: unaffected by the A write blocked above.
    // B 随后落地：不受上面被拦截的 A 写入影响。
    useProjectsStore.getState().setCurrentProject("B", makeProject("B-数据"), {}, {});
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
  });

  it("跨两次切换的更早项目迟到写入（C→A→B）：null 窗口不因换了新名字而放行", async () => {
    // Multi-hop scenario Codex pointed out after the previous fix: an uncancelled write on page C calls refreshProject("C") only during the null window
    // of the A->B switch. If we only remembered "the most recently cleared name" (A at that time),
    // curName="C" would not equal it and be misjudged as "no current project, let through" and written back. The real
    // criterion is "whether the store has ever established any real project", regardless of which one.
    // Codex 在上一条修复后指出的多跳场景：C 页面未取消的写操作直到 A→B 切换期间
    // 的 null 窗口才发起 refreshProject("C")。若只记「最近一次被清空的名字」（当时是
    // A），curName="C" 不等于它，会被误判成「无当前项目，可放行」重新写回。真正的
    // 判据应是「store 是否已经建立过任何真实项目」，不分具体是哪一个。
    const store = useProjectsStore.getState();
    store.setCurrentProject("C", makeProject("C-数据"), {}, {}); // establish C / 建立 C
    store.setCurrentProject(null, null); // route cleanup: leave C / 路由 cleanup：离开 C
    store.setCurrentProject("A", makeProject("A-数据"), {}, {}); // A lands / A 落地
    store.setCurrentProject(null, null); // route cleanup: leave A, B not loaded yet / 路由 cleanup：离开 A，尚未加载 B

    const dC = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(dC.promise);

    const pC = useProjectsStore.getState().refreshProject("C"); // started within the second null window / 在第二次 null 窗口内发起
    dC.resolve(makeResult("C-迟到数据"));
    const ok = await pC;

    expect(ok).toBe("cancelled");
    expect(useProjectsStore.getState().currentProjectName).toBe(null);

    useProjectsStore.getState().setCurrentProject("B", makeProject("B-数据"), {}, {});
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-数据");
  });

  it("项目已切到 B 后才发起的 A 刷新：不写回 store（现役域未 abort，靠当前项目名拦截）", async () => {
    // Unlike the previous case: here the switch happens before the refreshProject("A") call completes; e.g. a callback after a write
    // captured the pre-switch old project name, and by the time it actually sends the request the project is already B. It then gets B's
    // active, non-aborted domain; neither signal.aborted nor queue dedupe can stop it, so it must be checked against the current project name.
    // 与上一条不同：这里切换先于 refreshProject("A") 调用完成——例如写操作完成后的
    // 回调捕获了切换前的旧项目名，等它真正发起请求时项目已经是 B。此时拿到的是 B
    // 现役、未 abort 的域，signal.aborted 与排队去重都拦不住，必须靠当前项目名核对。
    useProjectsStore.getState().setCurrentProject("A", makeProject("A-数据"), {}, {});
    useProjectsStore.getState().setCurrentProject("B", makeProject("B-数据"), {}, {});

    const dA = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(dA.promise);

    const pA = useProjectsStore.getState().refreshProject("A");
    dA.resolve(makeResult("A-迟到数据"));
    const ok = await pA;

    expect(ok).toBe("cancelled");
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-数据");
  });

  it("A 在途时排队同项目再刷一轮，期间切到 B：排队轮响应不写回（同名不豁免当前项目核对）", async () => {
    // Scenario raised by Codex: A's first round is in flight and another A refresh queues and merges (queuedName === curName,
    // not triggering supersededByOtherProject); then the user switches to B. The first round aborts due to the cancellation domain rotation,
    // but the queued round keeps running in the while loop, takes B's active, non-aborted domain, and sends a
    // second request for queuedName=A. When the response settles the current project is already B, so it must be blocked by checking the current project name, not by a same-name check.
    // Codex 提出的场景：A 首轮在途、又有一次 A 刷新排队合并（queuedName === curName，
    // 不触发 supersededByOtherProject）；随后用户切到 B。首轮因取消域轮换而 abort，
    // 但排队轮沿用 while 循环继续跑，取的是 B 现役、未 abort 的域，对 queuedName=A 发起
    // 第二次请求。响应落定时当前项目已是 B，须靠当前项目名核对拦截，而非同名判断。
    useProjectsStore.getState().setCurrentProject("A", makeProject("A-旧"), {}, {});
    const d1 = createDeferred<GetProjectResult>();
    const d2 = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise);

    const store = useProjectsStore.getState();
    const p1 = store.refreshProject("A");
    const p2 = store.refreshProject("A"); // in flight -> merged and queued / 在途 → 合并排队

    store.setCurrentProject("B", makeProject("B-数据"), {}, {});
    d1.resolve(makeResult("A-首轮迟到"));
    await flush();
    // The first round aborts, and the queued round has sent the second request
    // 首轮 abort，排队轮已发起第二次请求
    d2.resolve(makeResult("A-排队轮迟到"));

    const [ok1, ok2] = await Promise.all([p1, p2]);
    expect(ok1).toBe("cancelled");
    expect(ok2).toBe("cancelled");
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-数据");
  });

  it("项目切换 abort 在途请求：请求被取消，且不按刷新失败提示", async () => {
    useProjectsStore.getState().setCurrentProject("A", makeProject("A-数据"), {}, {});
    let abortedSignal: AbortSignal | undefined;
    vi.spyOn(API, "getProject").mockImplementation(
      (_name, options) =>
        new Promise<GetProjectResult>((_resolve, reject) => {
          abortedSignal = options?.signal;
          options?.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );

    const onError = vi.fn();
    const store = useProjectsStore.getState();
    const pA = store.refreshProject("A", { onError });

    store.setCurrentProject("B", makeProject("B-数据"), {}, {});

    const ok = await pA;
    expect(abortedSignal?.aborted).toBe(true);
    expect(ok).toBe("cancelled");
    // abort is the normal result of a project switch, not a refresh failure: no "project sync failed" should pop up.
    // abort 是项目切换的正常结果，不是刷新失败：不该弹「项目同步失败」。
    expect(onError).not.toHaveBeenCalled();
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-数据");
  });

  it("切换项目后新项目的刷新照常生效（取消域轮换不会长期作废后续刷新）", async () => {
    useProjectsStore.getState().setCurrentProject("A", makeProject("A-数据"), {}, {});
    const dA = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject")
      .mockReturnValueOnce(dA.promise)
      .mockResolvedValue(makeResult("B-新数据"));

    const store = useProjectsStore.getState();
    const pA = store.refreshProject("A");

    store.setCurrentProject("B", makeProject("B-数据"), {}, {});
    dA.resolve(makeResult("A-迟到数据"));
    expect(await pA).toBe("cancelled");

    const okB = await useProjectsStore.getState().refreshProject("B");
    expect(okB).toBe("success");
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-新数据");
  });

  it("在途轮是当前项目、排队的却是旧项目名：在途轮照常写入，不为排队者让位", async () => {
    // After first-screen loading was folded in, "another project in the queue" no longer necessarily means this round is about to be superseded: a route switch
    // sets the name first and then refreshes, so the project that would really supersede this round is already the current project; whereas callers holding the old project name
    // (refreshes after writes, SSE) queue in while the current project has not changed hands. If this round still yielded to them, this round
    // is the current project's own first-screen data, and once dropped nobody would load it again.
    // 首屏加载收编进来之后，「排队里有别的项目」不再必然意味着本轮即将被取代：路由切换
    // 会先落名再刷新，因此真正要取代本轮的项目此刻已是当前项目；而持旧项目名的调用方
    // （写操作完成后的刷新、SSE）排进来时，当前项目并未易主。此时若仍为它让位，本轮
    // 就是当前项目自己的首屏数据，丢掉后没有任何人会再加载它。
    useProjectsStore.getState().setCurrentProject("B", null);
    const dB = createDeferred<GetProjectResult>();
    const dStale = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockImplementation((name) =>
      name === "B" ? dB.promise : dStale.promise,
    );

    const store = useProjectsStore.getState();
    const pB = store.refreshProject("B"); // the current project B's first-screen round, in flight / 当前项目 B 的首屏轮，在途
    const pStale = store.refreshProject("A"); // a caller holding the old project name queues / 持旧项目名的调用方排队

    dB.resolve(makeResult("B-数据"));
    expect(await pB).toBe("success");
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-数据");

    // The queued round runs the old project A: it could not write anyway (not the current project), so settling as cancelled is enough.
    // 排队轮跑的是旧项目 A：它本就写不进去（非当前项目），结算为 cancelled 即可。
    dStale.resolve(makeResult("A-迟到数据"));
    expect(await pStale).toBe("cancelled");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-数据");
  });

  it("当前项目的刷新在排队时，持旧项目名的调用方不夺走唯一的排队名额", async () => {
    // The queue has only one slot and by design "takes the latest name". After folding in first-screen loading, the one squeezed out may be exactly
    // the current project's own first-screen refresh, while the old project name squeezing in is bound to fail to write the store, so the current project's data
    // would not be fetched even once, and projectDetailLoading would never fall back.
    // 排队只有一个名额，且设计上「取最新 name」。收编首屏加载后，被挤掉的那个可能正是
    // 当前项目自己的首屏刷新——而挤进来的旧项目名注定写不进 store，于是当前项目的数据
    // 一轮都不会被拉取，projectDetailLoading 也永远回落不了。
    useProjectsStore.getState().setCurrentProject("A", makeProject("A-数据"), {}, {});
    const dSseA = createDeferred<GetProjectResult>();
    const dB = createDeferred<GetProjectResult>();
    vi.spyOn(API, "getProject").mockImplementation((name) =>
      name === "B" ? dB.promise : dSseA.promise,
    );

    const store = useProjectsStore.getState();
    const pSseA = store.refreshProject("A"); // SSE refresh on page A, in flight / A 页面上的 SSE 刷新，在途
    store.setCurrentProject("B", null); // route switches to B: name set first, cancellation domain rotates with it / 路由切到 B：先落名，取消域随之轮换
    const pFirstScreenB = store.refreshProject("B"); // B's first-screen refresh -> queued / B 的首屏刷新 → 排队
    const pStaleA = store.refreshProject("A"); // a caller holding the old project name, must not squeeze out B / 持旧项目名的调用方，不该挤掉 B

    dSseA.resolve(makeResult("A-迟到数据"));
    await flush();
    dB.resolve(makeResult("B-数据"));

    expect(await pFirstScreenB).toBe("success");
    expect(useProjectsStore.getState().currentProjectName).toBe("B");
    expect(useProjectsStore.getState().currentProjectData?.title).toBe("B-数据");
    expect(await pSseA).toBe("cancelled");
    expect(await pStaleA).toBe("cancelled");
  });
});
