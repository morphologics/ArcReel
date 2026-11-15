import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API, ReadOnlyModeError, setApiReadOnly } from "@/api";
import { DEMO_PROJECT_NAME } from "@/onboarding/demo-project";

/**
 * Structural guarantee of the read-only gate: `withAuth()` is the single exit of all fetches and the gate sits there,
 * so "some button forgot to be disabled" cannot turn into a real write request.
 *
 * 只读闸门的结构性保证：`withAuth()` 是全部 fetch 的唯一出口，闸门落在那里，
 * 所以「哪个按钮忘了禁用」不会变成一次真实写请求。
 */

function okResponse(): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers(),
    json: vi.fn().mockResolvedValue({}),
    text: vi.fn().mockResolvedValue(""),
    blob: vi.fn().mockResolvedValue(new Blob()),
  } as unknown as Response;
}

describe("read-only demo mode", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(okResponse());
    vi.stubGlobal("fetch", fetchMock);
    setApiReadOnly(true);
  });

  afterEach(() => {
    setApiReadOnly(false);
    vi.unstubAllGlobals();
  });

  it("blocks writes before they reach the network", async () => {
    await expect(
      API.request("/projects", { method: "POST", body: "{}" }),
    ).rejects.toThrow(ReadOnlyModeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("blocks every non-GET method, not just POST", async () => {
    for (const method of ["POST", "PUT", "PATCH", "DELETE", "post"]) {
      await expect(
        API.request(`/projects/${DEMO_PROJECT_NAME}/x`, { method }),
      ).rejects.toThrow(ReadOnlyModeError);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("blocks writes with no project in the URL (global endpoints)", async () => {
    await expect(
      API.request("/assets", { method: "POST", body: "{}" }),
    ).rejects.toThrow(ReadOnlyModeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not block writes aimed at a different, real project", async () => {
    // For a multi-request write started by a real project, if navigation into the demo workbench happens between two requests,
    // the gate is then global state rather than judged by request target; but as long as the request itself does not write the demo project,
    // it should not be blocked by this fallback, or a partially completed real-project write would be left behind.
    // 真实项目发起的多请求写操作，若在两次请求之间导航进了演示工作台，
    // 闸门此时是全局态而非按请求目标判定——但只要请求本身写的不是演示项目，
    // 就不该被这个兜底拦下，否则会留下部分完成的真实项目写入。
    await API.request("/projects/real-project/characters/hero", {
      method: "PATCH",
      body: "{}",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("blocks the demo project under any casing, matching isDemoProject", async () => {
    // The gate and isDemoProject must use the same rule: the latter is case-insensitive; if the gate compared exactly,
    // a demo project name with different case would be judged "another real project" and the write request let through
    // 闸门与 isDemoProject 必须同一口径：后者大小写不敏感，闸门若做精确比较，
    // 大小写不同的演示项目名就会被判成「另一个真实项目」而放行写请求
    await expect(
      API.request(`/projects/${DEMO_PROJECT_NAME.toUpperCase()}/characters/hero`, {
        method: "PATCH",
        body: "{}",
      }),
    ).rejects.toThrow(ReadOnlyModeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not treat a project literally named 'import' as project-less", async () => {
    // ProjectManager.normalize_project_name allows "import" as a valid project name, and archive import
    // also adopts that name directly; only the exact /projects/import static archive endpoint itself should be excluded,
    // not also blocking write requests for a project that happens to be named import as unowned
    // ProjectManager.normalize_project_name 允许 "import" 作为合法项目名，归档导入
    // 也会直接采用该名——只有精确的 /projects/import 静态归档端点本身该被排除，
    // 不能连带把「项目名恰好叫 import」的写请求也判成无归属而拦下
    await API.request("/projects/import/characters/hero", {
      method: "PATCH",
      body: "{}",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("still blocks the static archive-import endpoint itself", async () => {
    await expect(
      API.request("/projects/import", { method: "POST", body: "{}" }),
    ).rejects.toThrow(ReadOnlyModeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("blocks task enqueueing", async () => {
    // Enqueue endpoints are all POST, so the gate covers them the same as ordinary writes
    // 入队端点一律是 POST，所以闸门对它们的覆盖和对普通写操作一样
    await expect(
      API.request(`/projects/${DEMO_PROJECT_NAME}/generate/storyboard/E1S1`, {
        method: "POST",
        body: JSON.stringify({ prompt: "p", script_file: "E1.json" }),
      }),
    ).rejects.toThrow(ReadOnlyModeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("still allows reads", async () => {
    await API.request("/projects");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("exempts the onboarding-seen system endpoint from the gate", async () => {
    // When the onboarding tour exits it writes this seen mark while still on the demo route (apiReadOnly not yet reset);
    // it writes no project data and should not be blocked along with the default rule "block when no project ownership"
    // 引导 tour 退出时会在仍处于演示路由（apiReadOnly 尚未复位）期间写这条已看过标记，
    // 它不写任何项目数据，不该被「无项目归属即拦截」的默认规则连带挡下
    await API.request("/onboarding/seen", { method: "POST" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("lets the tour mark itself seen from inside the demo workbench", async () => {
    // The workbench's four steps run in read-only state, and pressing "Skip" or "Done" there writes exactly this mark;
    // go through the real API method rather than a hand-written endpoint, so this fails too if the method's path changes.
    // 工作台四步就跑在只读态里，那里按「跳过」或「完成」写的正是这条标记——
    // 走真实的 API 方法而不是手写 endpoint，方法改路径时这条也跟着失败。
    await API.markOnboardingSeen();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("lifts the gate when the demo workbench is left", async () => {
    setApiReadOnly(false);
    await API.request("/projects", { method: "POST", body: "{}" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("getFileUrl", () => {
  it("passes inline placeholder URIs through untouched", () => {
    const dataUri = "data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E";
    expect(API.getFileUrl("onboarding_demo", dataUri, null)).toBe(dataUri);
    expect(API.getFileUrl("onboarding_demo", dataUri, 3, { width: 320 })).toBe(dataUri);
  });

  it("still builds a project-relative URL for real asset paths", () => {
    expect(API.getFileUrl("demo", "storyboards/E1S1.png", null)).toContain(
      "storyboards/E1S1.png",
    );
  });
});
