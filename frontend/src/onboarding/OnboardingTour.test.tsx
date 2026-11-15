import { act, render, waitFor } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import i18n from "@/i18n";
import { API } from "@/api";
import { ROUTE_APP_ASSETS } from "@/app-routes";
import { useAuthStore } from "@/stores/auth-store";
import { useOnboardingStore } from "@/stores/onboarding-store";
import { ONBOARDING_ANCHORS, type OnboardingAnchor } from "./anchors";
import { DEMO_PROJECT_NAME, DEMO_SCRIPTED_EPISODE } from "./demo-project";
import { OnboardingTour } from "./OnboardingTour";

function renderAt(path: string) {
  const { hook } = memoryLocation({ path });
  return render(
    <Router hook={hook}>
      <OnboardingTour />
    </Router>,
  );
}

function renderWithNavigation(path: string) {
  const { hook, navigate } = memoryLocation({ path });
  const view = render(
    <Router hook={hook}>
      <OnboardingTour />
    </Router>,
  );
  return { ...view, navigate };
}

function popoverTitle(): string | null {
  return document.querySelector(".driver-popover-title")?.textContent ?? null;
}

describe("OnboardingTour", () => {
  beforeEach(() => {
    useOnboardingStore.setState(useOnboardingStore.getInitialState(), true);
    useAuthStore.setState({ isAuthenticated: true });
    vi.spyOn(API, "markOnboardingSeen").mockResolvedValue({ seen: true });
  });

  afterEach(async () => {
    await i18n.changeLanguage("zh");
  });

  it("opens the tour on the first visit to the main interface", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/app/projects");

    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));
  });

  it("stays out of the way once the tour has been seen", async () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: true });

    renderAt("/app/projects");

    await waitFor(() => expect(status).toHaveBeenCalled());
    expect(popoverTitle()).toBeNull();
  });

  it("does not run before the user reaches the main interface", () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    useAuthStore.setState({ isAuthenticated: false });

    renderAt("/app/projects");

    expect(status).not.toHaveBeenCalled();
    expect(popoverTitle()).toBeNull();
  });

  it("does not run on the login page", () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/login");

    expect(status).not.toHaveBeenCalled();
  });

  it("does not run on an unmatched route (404)", () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/some/unknown/path");

    expect(status).not.toHaveBeenCalled();
    expect(popoverTitle()).toBeNull();
  });

  it("does not run on an unmatched route that merely shares the /app prefix", () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/application");

    expect(status).not.toHaveBeenCalled();
    expect(popoverTitle()).toBeNull();
  });

  it("does not run on an unregistered sub-path under a single-page app route", () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/app/settings/unknown");

    expect(status).not.toHaveBeenCalled();
    expect(popoverTitle()).toBeNull();
  });

  it("does not run on an unregistered sub-path inside a project workspace", () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/app/projects/my-novel/unknown");

    expect(status).not.toHaveBeenCalled();
    expect(popoverTitle()).toBeNull();
  });

  it("runs on a registered sub-path inside a project workspace", async () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/app/projects/my-novel/characters");

    await waitFor(() => expect(status).toHaveBeenCalled());
  });

  it("runs on the project-level episodes view, which has no episode segment", async () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/app/projects/my-novel/episodes");

    await waitFor(() => expect(status).toHaveBeenCalled());
  });

  it("runs on the project settings page", async () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/app/projects/my-novel/settings");

    await waitFor(() => expect(status).toHaveBeenCalled());
  });

  it("runs on a case-variant URL that wouter itself matches case-insensitively", async () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/APP/Projects/my-novel/Characters");

    await waitFor(() => expect(status).toHaveBeenCalled());
  });

  it("runs on a URL with a trailing slash that wouter itself matches", async () => {
    const status = vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/app/settings/");

    await waitFor(() => expect(status).toHaveBeenCalled());
  });

  it("marks the tour as seen when it is closed, and does not reopen it", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    renderAt("/app/projects");
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    document.querySelector<HTMLElement>(".driver-popover-close-btn")?.click();

    await waitFor(() => expect(API.markOnboardingSeen).toHaveBeenCalledTimes(1));
    expect(popoverTitle()).toBeNull();
    expect(useOnboardingStore.getState().seen).toBe(true);
  });

  it("replays the tour on request without writing the flag again", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: true });

    renderAt("/app/projects");
    await waitFor(() => expect(API.getOnboardingStatus).toHaveBeenCalled());

    useOnboardingStore.getState().start();

    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));
    document.querySelector<HTMLElement>(".driver-popover-close-btn")?.click();

    await waitFor(() => expect(popoverTitle()).toBeNull());
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();
  });

  it("keeps its place when the interface language changes mid-tour", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    // Step 2 points to the lobby's "New project" button. Here only the tour mount point renders, with no lobby, so add an anchor
    // element; what is tested is position keeping, not degradation when the anchor is absent.
    // 第 2 步指向大厅的「新建项目」按钮。这里只渲染引导挂载点、没有大厅，补一个锚点
    // 元素，测的才是保位而不是锚点缺席时的降级。
    const anchor = document.createElement("button");
    anchor.setAttribute("data-onboarding", ONBOARDING_ANCHORS.lobbyCreateProject);
    document.body.appendChild(anchor);

    renderAt("/app/projects");
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));
    document.querySelector<HTMLElement>(".driver-popover-next-btn")?.click();
    expect(popoverTitle()).toBe("新建项目");

    await act(async () => {
      await i18n.changeLanguage("en");
    });

    await waitFor(() => expect(popoverTitle()).toBe("Create a project"));
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();
    anchor.remove();
  });

  it("navigates to the lobby before running the tour when replayed from another main-UI route", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: true });

    // The welcome step itself has anchor null and shows centered, appearing whether or not navigation happened; the bubble title alone
    // cannot tell whether navigation really happened, so assert the landing directly with the navigation history recorded by `record: true`.
    // 欢迎步骤本身 anchor 为 null、居中显示，跳没跳转它都会出现——单看气泡标题测
    // 不出跳转是否真的发生了，需要 `record: true` 记录的导航历史直接断言落点。
    const { hook, history } = memoryLocation({ path: "/app/settings", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(API.getOnboardingStatus).toHaveBeenCalled());
    expect(popoverTitle()).toBeNull();

    act(() => useOnboardingStore.getState().start());

    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));
    expect(history.at(-1)).toBe("/app/projects");
  });

  /**
   * Add anchor elements by hand. There is no real page to render; what is tested is cross-page chaining, not anchor degradation; anchor existence
   * in the real UI is covered separately by `anchors.test.tsx`.
   *
   * 手动补上锚点元素。没有真实页面可渲染，这里测的是跳页串联而不是锚点降级——锚点在真实
   * 界面上的存在性由 `anchors.test.tsx` 单独兜。
   */
  function mountAnchors(...names: OnboardingAnchor[]): HTMLElement[] {
    return names.map((name) => {
      const el = document.createElement("button");
      el.setAttribute("data-onboarding", name);
      document.body.appendChild(el);
      return el;
    });
  }

  /** Anchors for the lobby's three steps. / 大厅三步的锚点。 */
  function mountLobbyAnchors(): HTMLElement[] {
    return mountAnchors(
      ONBOARDING_ANCHORS.lobbyCreateProject,
      ONBOARDING_ANCHORS.lobbyDemoCard,
      ONBOARDING_ANCHORS.lobbySettings,
    );
  }

  /** Anchors for the settings page's two steps. / 设置页两步的锚点。 */
  function mountSettingsAnchors(): HTMLElement[] {
    return mountAnchors(ONBOARDING_ANCHORS.settingsProviders, ONBOARDING_ANCHORS.settingsAgent);
  }

  function click(selector: string): void {
    document.querySelector<HTMLElement>(selector)?.click();
  }

  it("crosses from the lobby into settings when the tour reaches the provider step", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const lobbyAnchors = mountLobbyAnchors();
    const settingsAnchor = document.createElement("button");
    settingsAnchor.setAttribute("data-onboarding", ONBOARDING_ANCHORS.settingsProviders);
    document.body.appendChild(settingsAnchor);

    const { hook, history } = memoryLocation({ path: "/app/projects", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    click(".driver-popover-next-btn"); // -> New project entry / → 新建项目入口
    click(".driver-popover-next-btn"); // -> Settings entry (still in the lobby) / → 设置入口（仍在大厅）
    click(".driver-popover-next-btn"); // -> Providers (cross-page to settings) / → 供应商（跨页到设置）

    await waitFor(() => expect(popoverTitle()).toBe("配置供应商"));
    // Carry the section query param; the settings content area relies on it to land on the providers section.
    // 带上 section 查询参数——设置页内容区靠它落到供应商一节。
    expect(history.at(-1)).toBe("/app/settings?section=providers");
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();
    [...lobbyAnchors, settingsAnchor].forEach((el) => el.remove());
  });

  it("crosses back into the lobby when the tour steps backwards out of settings", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const lobbyAnchors = mountLobbyAnchors();
    const settingsAnchor = document.createElement("button");
    settingsAnchor.setAttribute("data-onboarding", ONBOARDING_ANCHORS.settingsProviders);
    document.body.appendChild(settingsAnchor);

    const { hook, history } = memoryLocation({ path: "/app/projects", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    click(".driver-popover-next-btn");
    click(".driver-popover-next-btn");
    click(".driver-popover-next-btn"); // -> Providers (cross-page to settings) / → 供应商（跨页到设置）
    await waitFor(() => expect(popoverTitle()).toBe("配置供应商"));
    expect(history.at(-1)).toBe("/app/settings?section=providers");

    click(".driver-popover-prev-btn"); // <- Settings entry (cross-page back to the lobby) / ← 设置入口（跨页回大厅）

    await waitFor(() => expect(popoverTitle()).toBe("设置"));
    expect(history.at(-1)).toBe("/app/projects");
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();
    [...lobbyAnchors, settingsAnchor].forEach((el) => el.remove());
  });

  it("pulls back to the lobby if another tour route is reached mid-way through the interactive demo-card step", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const anchors = [...mountLobbyAnchors(), ...mountSettingsAnchors()];

    const { hook, history, navigate } = memoryLocation({ path: "/app/projects", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    // -> Demo card (interactive step, anchor in the lobby)
    for (let i = 0; i < 5; i++) click(".driver-popover-next-btn"); // → 演示卡（interactive 步，锚点在大厅）
    await waitFor(() => expect(popoverTitle()).toBe("演示项目"));

    // The demo card step is interactive, letting the user click the card itself to leave the lobby for the workbench; but if the landing is another route
    // covered by the tour (such as "Settings" in the top bar), this exemption should not let it through too; otherwise the driver stays on
    // the demo card step without finding the anchor and degrades into a centered bubble that does not match the settings page content.
    // 演示卡步是 interactive，允许用户点卡片本身离开大厅进工作台；但如果落点是引导
    // 覆盖的另一个路由（如顶栏「设置」），不该被这条豁免一并放过——否则 driver 停在
    // 演示卡步却找不到锚点，会降级成与设置页内容不符的居中气泡。
    act(() => navigate("/app/settings"));

    await waitFor(() => expect(history.at(-1)).toBe("/app/projects"));
    expect(popoverTitle()).toBe("演示项目");

    anchors.forEach((el) => el.remove());
  }, 20_000);

  it("pulls back to the lobby if a route outside the tour is reached mid-way through the interactive demo-card step", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const anchors = [...mountLobbyAnchors(), ...mountSettingsAnchors()];

    const { hook, history, navigate } = memoryLocation({ path: "/app/projects", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    for (let i = 0; i < 5; i++) click(".driver-popover-next-btn"); // -> Demo card (interactive step) / → 演示卡（interactive 步）
    await waitFor(() => expect(popoverTitle()).toBe("演示项目"));

    // Any destination other than the landing is pulled back, not let through just because it is outside the tour's coverage: the asset library is a main UI route but
    // is not in `tourRoutes`, and this step's exemption recognizes only its own declared `interactiveTarget`.
    // 落点之外的去处一律拽回，不因为它在引导覆盖范围之外就放过：资产库是主界面路由但
    // 不在 `tourRoutes` 里，这一步的豁免只认它自己声明的 `interactiveTarget`。
    act(() => navigate(ROUTE_APP_ASSETS));

    await waitFor(() => expect(history.at(-1)).toBe("/app/projects"));
    expect(popoverTitle()).toBe("演示项目");
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();

    anchors.forEach((el) => el.remove());
  }, 20_000);

  it("switches the settings pane back to providers when stepping backwards from the agent step", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const anchors = [...mountLobbyAnchors(), ...mountSettingsAnchors()];

    const { hook, history } = memoryLocation({ path: "/app/projects", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    for (let i = 0; i < 4; i++) click(".driver-popover-next-btn"); // -> Configure Agent / → 配置 Agent
    await waitFor(() => expect(popoverTitle()).toBe("配置 YouReel Studio Agent"));
    expect(history.at(-1)).toBe("/app/settings?section=arcreel-agent");

    // Both steps are on /app/settings, so going back leaves pathname unchanged; the content area must switch back to providers via the section param,
    // or the Agent would still be shown on the right while explaining providers (likewise forward).
    // 两步同在 /app/settings，退回时 pathname 不变——内容区必须靠 section 参数切回
    // 供应商，否则讲供应商时右边还摆着 Agent（正向同理）。
    click(".driver-popover-prev-btn");

    await waitFor(() => expect(popoverTitle()).toBe("配置供应商"));
    expect(history.at(-1)).toBe("/app/settings?section=providers");
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();

    anchors.forEach((el) => el.remove());
  });

  it("degrades to a centered popover when the settings-step anchor never mounts", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const lobbyAnchors = mountLobbyAnchors();

    const { hook } = memoryLocation({ path: "/app/projects" });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    click(".driver-popover-next-btn");
    click(".driver-popover-next-btn");
    // The settings page is not rendered and the anchor does not exist; after the wait times out it should degrade to a centered bubble rather than get stuck
    click(".driver-popover-next-btn"); // 没有渲染设置页，锚点不存在——等待超时后应当降级为居中气泡而不是卡住

    await waitFor(() => expect(popoverTitle()).toBe("配置供应商"), { timeout: 3000 });
    // The walkthrough continues as usual, only the highlight is lost: the driver substitutes its own placeholder element and the bubble returns to the screen center.
    // 讲解照常进行，丢的只是高亮——driver 顶上自己的占位元素，气泡回到屏幕中央。
    expect(document.getElementById("driver-dummy-element")?.classList.contains("driver-active-element")).toBe(
      true,
    );
    expect(warn).toHaveBeenCalledWith(expect.stringContaining(ONBOARDING_ANCHORS.settingsProviders));

    warn.mockRestore();
    lobbyAnchors.forEach((el) => el.remove());
  });

  it("still marks the tour as seen when skipped mid-way through the settings steps", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const lobbyAnchors = mountLobbyAnchors();
    const settingsAnchor = document.createElement("button");
    settingsAnchor.setAttribute("data-onboarding", ONBOARDING_ANCHORS.settingsProviders);
    document.body.appendChild(settingsAnchor);

    const { hook } = memoryLocation({ path: "/app/projects" });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    click(".driver-popover-next-btn");
    click(".driver-popover-next-btn");
    click(".driver-popover-next-btn");
    await waitFor(() => expect(popoverTitle()).toBe("配置供应商"));

    click(".arc-tour-skip-btn");

    await waitFor(() => expect(API.markOnboardingSeen).toHaveBeenCalledTimes(1));
    expect(popoverTitle()).toBeNull();
    expect(useOnboardingStore.getState().seen).toBe(true);
    [...lobbyAnchors, settingsAnchor].forEach((el) => el.remove());
  });

  /** Anchors for the demo workbench's five steps. / 演示工作台五步的锚点。 */
  function mountWorkbenchAnchors(): HTMLElement[] {
    return mountAnchors(
      ONBOARDING_ANCHORS.workbenchOverview,
      ONBOARDING_ANCHORS.workbenchAgent,
      ONBOARDING_ANCHORS.workbenchLorebook,
      ONBOARDING_ANCHORS.workbenchTimeline,
      ONBOARDING_ANCHORS.workbenchExport,
    );
  }

  /** Anchors for all 12 steps, mounted at once for cases running the full chain. / 全程 12 步的锚点，供跑完整串联的用例一次挂齐。 */
  function mountAllAnchors(): HTMLElement[] {
    return [...mountLobbyAnchors(), ...mountSettingsAnchors(), ...mountWorkbenchAnchors()];
  }

  const DEMO_WORKBENCH = `/app/projects/${DEMO_PROJECT_NAME}`;
  const DEMO_EPISODE = `${DEMO_WORKBENCH}/episodes/${DEMO_SCRIPTED_EPISODE}`;

  it("walks all twelve steps from the lobby through the demo workbench and back to the lobby", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const anchors = mountAllAnchors();

    const { hook, history } = memoryLocation({ path: "/app/projects", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    // Each step: title + the route after the step is done. Landings of cross-page steps are driven by route.
    // 每一步：标题 + 该步走完后当前所在路由。跨页步骤的落点由 route 驱动。
    const expected: [string, string][] = [
      ["新建项目", "/app/projects"],
      ["设置", "/app/projects"],
      ["配置供应商", "/app/settings?section=providers"],
      ["配置 YouReel Studio Agent", "/app/settings?section=arcreel-agent"],
      ["演示项目", "/app/projects"],
      ["Agent", DEMO_WORKBENCH],
      ["项目概览", DEMO_WORKBENCH],
      ["角色、场景与道具", `${DEMO_WORKBENCH}/characters`],
      ["分镜", `${DEMO_EPISODE}?view=board`],
      ["导出项目", `${DEMO_EPISODE}?view=board`],
      ["开始你的第一个项目", "/app/projects"],
    ];

    for (const [title, route] of expected) {
      click(".driver-popover-next-btn");
      await waitFor(() => expect(popoverTitle()).toBe(title));
      expect(history.at(-1)).toBe(route);
    }

    // The mark was never written before the finish; no step midway was mistaken for the end.
    // 收尾之前一次都没写过标记——中途没有哪一步误判成结束。
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();

    click(".driver-popover-next-btn"); // the finishing bubble shows "Done" / 收尾气泡上是「完成」
    await waitFor(() => expect(API.markOnboardingSeen).toHaveBeenCalledTimes(1));
    expect(popoverTitle()).toBeNull();
    expect(useOnboardingStore.getState().seen).toBe(true);

    anchors.forEach((el) => el.remove());
  }, 20_000);

  it("advances straight into the workbench segment when the user follows the demo card in", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const anchors = mountAllAnchors();

    const { hook, history, navigate } = memoryLocation({ path: "/app/projects", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    for (let i = 0; i < 5; i++) click(".driver-popover-next-btn"); // -> Demo card (interactive) / → 演示卡（interactive）
    await waitFor(() => expect(popoverTitle()).toBe("演示项目"));

    // Click into the demo workbench via the entry this step offers: no pull back to the lobby, and the tour moves on to the workbench's first step (Agent);
    // clicking the card and clicking "Next" lead to the same place.
    // 顺着这一步给的入口点进演示工作台：不拽回大厅，引导顺势推进到工作台首步（Agent）——
    // 点卡片和点「下一步」殊途同归。
    act(() => navigate(DEMO_WORKBENCH));

    await waitFor(() => expect(popoverTitle()).toBe("Agent"));
    expect(history.at(-1)).toBe(DEMO_WORKBENCH);
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();

    anchors.forEach((el) => el.remove());
  }, 20_000);

  it("still marks the tour as seen when skipped inside the read-only demo workbench", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    const anchors = mountAllAnchors();

    const { hook, history } = memoryLocation({ path: "/app/projects", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    for (let i = 0; i < 6; i++) click(".driver-popover-next-btn"); // -> Agent (first workbench step) / → Agent（工作台第一步）
    await waitFor(() => expect(popoverTitle()).toBe("Agent"));
    expect(history.at(-1)).toBe(DEMO_WORKBENCH);

    click(".arc-tour-skip-btn");

    // The read-only gate has a narrow exemption for /onboarding/seen, so skipping inside the workbench still writes it.
    // 只读闸门对 /onboarding/seen 有窄豁免，工作台里跳过照样写得进去。
    await waitFor(() => expect(API.markOnboardingSeen).toHaveBeenCalledTimes(1));
    expect(popoverTitle()).toBeNull();
    expect(useOnboardingStore.getState().seen).toBe(true);

    anchors.forEach((el) => el.remove());
  }, 20_000);

  it("replays the full twelve steps from the settings entry", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: true });
    const anchors = mountAllAnchors();

    // The replay entry is on the settings page, so starting requires going back to the lobby first; replay and first show share the same 12-step outline.
    // 重看入口在设置页，起步得先跨回大厅——重看和首弹共用同一条 12 步大纲。
    const { hook, history } = memoryLocation({ path: "/app/settings", record: true });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(API.getOnboardingStatus).toHaveBeenCalled());

    act(() => useOnboardingStore.getState().start());
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));
    expect(history.at(-1)).toBe("/app/projects");

    for (let i = 0; i < 11; i++) click(".driver-popover-next-btn");
    await waitFor(() => expect(popoverTitle()).toBe("开始你的第一个项目"));
    expect(history.at(-1)).toBe("/app/projects");

    click(".driver-popover-next-btn");
    await waitFor(() => expect(popoverTitle()).toBeNull());
    // Replay does not rewrite the mark; it is already marked as seen.
    // 重看不改写标记——本来就已经是已看过。
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();

    anchors.forEach((el) => el.remove());
  }, 20_000);

  it("issues no requests of its own beyond the seen flag across the whole twelve-step run", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });
    // The tour itself should send no other requests: no generation calls, no enqueues, no project writes. Page components outside the mount point
    // are not in this case; what is guarded here is the tour's own chain.
    // 引导自身不该发任何别的请求：没有生成调用、没有入队、没有项目写入。挂载点之外
    // 的页面组件不在本用例里，这里守的是引导这条链路自己。
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const anchors = mountAllAnchors();

    const { hook } = memoryLocation({ path: "/app/projects" });
    render(
      <Router hook={hook}>
        <OnboardingTour />
      </Router>,
    );
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    for (let i = 0; i < 11; i++) click(".driver-popover-next-btn");
    await waitFor(() => expect(popoverTitle()).toBe("开始你的第一个项目"));
    click(".driver-popover-next-btn");
    await waitFor(() => expect(API.markOnboardingSeen).toHaveBeenCalledTimes(1));

    // Both endpoints that query/write the mark are blocked by spies before fetch, so nothing should reach the network here.
    // 两个查询/写标记的端点都被 spy 挡在 fetch 之前，所以这里应当一次都没到网络。
    expect(fetchMock).not.toHaveBeenCalled();

    vi.unstubAllGlobals();
    anchors.forEach((el) => el.remove());
  }, 20_000);

  it("takes the tour down when the user navigates back to the login page mid-tour", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    const { navigate } = renderWithNavigation("/app/projects");
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    act(() => navigate("/login"));

    expect(popoverTitle()).toBeNull();
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();
  });

  it("takes the tour down when the mount point unmounts", async () => {
    vi.spyOn(API, "getOnboardingStatus").mockResolvedValue({ seen: false });

    const { unmount } = renderAt("/app/projects");
    await waitFor(() => expect(popoverTitle()).toBe("欢迎使用 YouReel Studio"));

    unmount();

    expect(document.querySelector(".driver-popover")).toBeNull();
    expect(API.markOnboardingSeen).not.toHaveBeenCalled();
  });
});
