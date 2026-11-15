/**
 * Step outline of the first-use tour.
 *
 * Structure (order, anchors, routes) is written here, while all copy goes through i18n keys in the `onboarding` namespace -- the two are
 * separate, so adding a language does not touch structure and reordering does not touch translations. Anchors always reference the `anchors.ts` registry, never
 * literals. `route` is the page the step needs to land on (`OnboardingTour` navigates ahead of step changes from it);
 * omitting it means no particular route is required.
 *
 * 12 steps in all, across three pages: lobby segment (welcome -> new project entry -> settings entry) -> settings segment (providers
 * -> ArcReel Agent) -> back to the lobby (demo card, the bridge into the workbench) -> demo workbench segment (Agent -> project overview ->
 * characters/scenes/props -> shots -> export) -> closing. The Agent panel is expanded by default and most prominent, so the workbench segment covers it first. Every page change is motivated by the entry the previous step highlighted:
 * after the settings icon comes the settings page, after the demo card comes the workbench; there are no jumps without a path.
 *
 * Names used for directions in step copy ("Providers", "ArcReel Agent", etc.) always take the highlighted element's actual UI label,
 * without inventing concepts -- directions count as right only if users can find them in the UI by following the copy.
 *
 * The workbench segment lands on the demo project's read-only workbench (`demo-project.ts`). Step 6's demo card is
 * `interactive`: clicking the card itself or "Next" both enter the demo workbench, and the tour advances to the workbench
 * first step (logic in `OnboardingTour.tsx`); route changes inside the workbench segment are still driven by `route` forced navigation.
 *
 * The closing step lands back in the lobby: the copy tells users to create a project and start producing, so it should land on the page with the new-project entry, not stay
 * on the read-only demo workbench.
 *
 * 首次使用引导的步骤大纲。
 *
 * 结构（顺序、锚点、路由）写在这里，文案全部走 `onboarding` 命名空间的 i18n key —— 两者
 * 分离，加语种不必碰结构，调顺序不必碰翻译。锚点一律引用 `anchors.ts` 的注册表，不写
 * 字面量。`route` 是该步所需落地的页面（`OnboardingTour` 据此在步骤切换前先行导航），
 * 省略表示不要求特定路由。
 *
 * 全程 12 步，跨三个页面：大厅段（欢迎 → 新建项目入口 → 设置入口）→ 设置页段（供应商
 * → ArcReel Agent）→ 回大厅（演示卡，进工作台的桥）→ 演示工作台段（Agent → 项目概览 →
 * 角色/场景/道具 → 分镜 → 导出）→ 收尾。Agent 面板默认展开、位置最显眼，工作台段先讲它。每一次换页都由上一步高亮的入口提供动机：
 * 设置图标讲完进设置页，演示卡讲完进工作台，不存在没有来路的跳转。
 *
 * 步骤文案里指路用的名字（「供应商」「ArcReel Agent」等）一律取被高亮元素在界面上的实际标签，
 * 不另造概念——用户照着文案在界面上找得到，才算指对了路。
 *
 * 工作台段落在演示项目的只读工作台上（`demo-project.ts`）。第 6 步的演示卡是
 * `interactive`：用户点卡片本身或点「下一步」都会进入演示工作台，引导顺势推进到工作台
 * 首步（判定见 `OnboardingTour.tsx`）；工作台段内部的换路由仍由 `route` 强制导航驱动。
 *
 * 收尾步落回大厅：文案让用户新建项目开始制作，落点就该是新建入口所在的页面，而不是停
 * 在只读演示工作台上。
 */

import type { TFunction } from "i18next";
import {
  ROUTE_APP_PROJECTS,
  ROUTE_APP_SETTINGS,
  EPISODE_VIEW_PARAM,
  WORKSPACE_ROUTE_CHARACTERS,
  WORKSPACE_ROUTE_EPISODES,
  type SettingsSection,
} from "@/app-routes";
import { ONBOARDING_ANCHORS } from "./anchors";
import { DEMO_PROJECT_NAME, DEMO_SCRIPTED_EPISODE } from "./demo-project";
import type { TourStep } from "./tour";

/** The three landing routes of the demo workbench. All lowercase -- `OnboardingTour` compares the current route literally after normalization. / 演示工作台的三条落地路由。全小写——`OnboardingTour` 归一化后按字面比对当前路由。 */
const DEMO_WORKBENCH = `${ROUTE_APP_PROJECTS}/${DEMO_PROJECT_NAME}`;
const DEMO_LOREBOOK = `${DEMO_WORKBENCH}/${WORKSPACE_ROUTE_CHARACTERS}`;
const DEMO_EPISODE = `${DEMO_WORKBENCH}/${WORKSPACE_ROUTE_EPISODES}/${DEMO_SCRIPTED_EPISODE}`;

export function buildTourSteps(t: TFunction<"onboarding">): TourStep[] {
  return [
    { anchor: null, title: t("welcome_title"), body: t("welcome_body"), route: ROUTE_APP_PROJECTS },
    {
      anchor: ONBOARDING_ANCHORS.lobbyCreateProject,
      title: t("lobby_create_title"),
      body: t("lobby_create_body"),
      route: ROUTE_APP_PROJECTS,
    },
    {
      anchor: ONBOARDING_ANCHORS.lobbySettings,
      title: t("lobby_settings_title"),
      body: t("lobby_settings_body"),
      route: ROUTE_APP_PROJECTS,
    },
    {
      anchor: ONBOARDING_ANCHORS.settingsProviders,
      title: t("settings_providers_title"),
      body: t("settings_providers_body"),
      route: ROUTE_APP_SETTINGS,
      // The settings page content area is driven by the `section` query param (`SystemConfigPage`) and the anchor is only on the sidebar
      // entry -- without declaring the query param the content area would not switch between the two steps, leaving providers on the right
      // while explaining Agent.
      // 设置页的内容区由 `section` 查询参数驱动（`SystemConfigPage`），锚点只在侧栏
      // 入口上——不声明查询参数的话，两步之间内容区不会跟着切，讲 Agent 时右边还摆着
      // 供应商。
      query: { section: "providers" satisfies SettingsSection },
    },
    {
      anchor: ONBOARDING_ANCHORS.settingsAgent,
      title: t("settings_agent_title"),
      body: t("settings_agent_body"),
      route: ROUTE_APP_SETTINGS,
      query: { section: "arcreel-agent" satisfies SettingsSection },
    },
    {
      anchor: ONBOARDING_ANCHORS.lobbyDemoCard,
      title: t("lobby_demo_title"),
      body: t("lobby_demo_body"),
      route: ROUTE_APP_PROJECTS,
      // The exception to read-only throughout: this step's landing action is navigating into the demo workbench, not a write, so it allows
      // interaction (see the `interactive` semantics in tour.ts); the demo card itself is only mounted while the tour runs,
      // and unmounts right after exit, leaving no writable entry.
      // 全程只读的例外：这一步的落点动作是导航进演示工作台，不是写操作，因此开放
      // 交互（见 tour.ts 的 `interactive` 语义）；演示卡本身仅在引导运行期间挂载，
      // 退出后随即卸载，不留可写入口。
      interactive: true,
      // Clicking the card lands in the demo workbench (including its subroutes). Landing here counts as "following the tour", and the tour advances
      // to the next step (the workbench first step); landing elsewhere is still dragged back to the lobby by forced navigation.
      // 点卡片落到演示工作台（含其子路由）。落到这里算「顺着引导走」，引导顺势推进
      // 到下一步（工作台首步）；落到别处仍按强制导航拽回大厅。
      interactiveTarget: DEMO_WORKBENCH,
    },
    {
      anchor: ONBOARDING_ANCHORS.workbenchAgent,
      title: t("workbench_agent_title"),
      body: t("workbench_agent_body"),
      // The Agent panel lives on the workbench shell and renders on every workbench route; explain it on the overview page so the next step needs no navigation.
      // Agent 面板挂在工作台外壳上，所有工作台路由都渲染；落在概览页讲，下一步不必导航。
      route: DEMO_WORKBENCH,
    },
    {
      anchor: ONBOARDING_ANCHORS.workbenchOverview,
      title: t("workbench_overview_title"),
      body: t("workbench_overview_body"),
      route: DEMO_WORKBENCH,
    },
    {
      anchor: ONBOARDING_ANCHORS.workbenchLorebook,
      title: t("workbench_lorebook_title"),
      body: t("workbench_lorebook_body"),
      route: DEMO_LOREBOOK,
    },
    {
      anchor: ONBOARDING_ANCHORS.workbenchTimeline,
      title: t("workbench_timeline_title"),
      body: t("workbench_timeline_body"),
      route: DEMO_EPISODE,
      // The episode page view is stored in the `view` query param; land explicitly on the shot view, where the anchor is on the shot view body.
      // 集页的视图记在 `view` 查询参数上，显式落到分镜视图，锚点挂在分镜视图主体上。
      query: { [EPISODE_VIEW_PARAM]: "board" },
    },
    {
      anchor: ONBOARDING_ANCHORS.workbenchExport,
      title: t("workbench_export_title"),
      body: t("workbench_export_body"),
      // The header is on every workbench route; explain it on the previous step's shot canvas, saving a needless navigation.
      // 顶栏在所有工作台路由上都挂着，留在上一步的分镜画布讲，省掉一次无谓导航。
      route: DEMO_EPISODE,
    },
    { anchor: null, title: t("finish_title"), body: t("finish_body"), route: ROUTE_APP_PROJECTS },
  ];
}
