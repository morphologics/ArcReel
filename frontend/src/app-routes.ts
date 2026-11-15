/**
 * Top-level app route constants, the single source of truth: both the `<Switch>` route table in `router.tsx` and
 * `OnboardingTour`'s main-interface check take values from here, so two sets of literals are not maintained separately and silently drift.
 *
 * 顶层应用路由常量 —— 唯一真相源，`router.tsx` 的 `<Switch>` 路由表与
 * `OnboardingTour` 的主界面判断都从这里取值，避免两处字面量各自维护、悄悄漂移。
 */

export const ROUTE_APP = "/app";
export const ROUTE_APP_PROJECTS = "/app/projects";
export const ROUTE_APP_SETTINGS = "/app/settings";
export const ROUTE_APP_ASSETS = "/app/assets";

/** Global settings sections, i.e. values of the URL param `section`, in sidebar order; missing or unrecognized falls back to `providers`. / 全局设置的分区，即地址参数 `section` 的取值，顺序同侧栏；缺省或无法识别时落在 `providers`。 */
export const SETTINGS_SECTIONS = [
  "providers",
  "default-models",
  "endpoints",
  "arcreel-agent",
  "agent-memory",
  "external-agent",
  "access-tokens",
  "market",
  "usage",
  "general",
  "prompt-templates",
  "about",
  "options",
  "hotkeys",
] as const;
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

/**
 * URL of a global settings section; `params` are that section's own locator params, such as usage records' `record`.
 * Section names are checked by `SettingsSection`; when a section is renamed, typecheck reports all old links.
 * When navigating from a nested route, prefix with `~`: `` `~${settingsSectionPath("usage")}` ``.
 * Use `providerSettingsPath` and `endpointSettingsPath` for provider and endpoint URLs; do not hand-build query params.
 *
 * 全局设置某个分区的地址；`params` 是该分区自己的定位参数，如使用记录的 `record`。
 * 分区名由 `SettingsSection` 校验，分区改名时 typecheck 报出全部旧链接。
 * 在嵌套路由里跳转时在前面加 `~`：`` `~${settingsSectionPath("usage")}` ``。
 * 供应商与调用端点的地址用 `providerSettingsPath` 与 `endpointSettingsPath`，不手拼查询参数。
 */
export function settingsSectionPath(section: SettingsSection, params: Record<string, string> = {}): string {
  return `${ROUTE_APP_SETTINGS}?${new URLSearchParams({ section, ...params }).toString()}`;
}

/** Tabs of the marketplace section; "Browse" is the default tab and is not written in the URL. / 市场分区的 Tab；「浏览」是默认 Tab，地址里不写。 */
export type MarketTab = "browse" | "shared" | "settings";

/** Values the marketplace "Browse" tab can filter by media type, i.e. values of the URL param `media`. / 市场「浏览」Tab 可按媒体类型筛选的取值，即地址参数 `media` 的取值。 */
export const MARKET_MEDIA_TYPES = ["image", "video"] as const;
export type MarketMediaType = (typeof MARKET_MEDIA_TYPES)[number];

/** Media type parsed from the URL param `media`; null when missing or unrecognized. / 地址参数 `media` 解析出的媒体类型；缺省或无法识别时为 null。 */
export function parseMarketMedia(value: string | null | undefined): MarketMediaType | null {
  return MARKET_MEDIA_TYPES.find((media) => media === value) ?? null;
}

/**
 * URL of a tab in the global settings "Marketplace" section (`tab=`).
 * When `media` is passed it is written as `media=<media type>`, and "Browse" presets its media-type filter from it on open; media types the marketplace lacks (text, audio) are not written.
 *
 * 全局设置「市场」分区某个 Tab 的地址（`tab=`）。
 * 传 `media` 时写成 `media=<媒体类型>`，「浏览」打开时按它预设媒体类型筛选；市场没有的媒体类型（文本、音频）不写。
 */
export function marketSettingsPath(tab: MarketTab = "browse", options: { media?: string | null } = {}): string {
  const params: Record<string, string> = tab === "browse" ? {} : { tab };
  const media = parseMarketMedia(options.media);
  if (media) params.media = media;
  return settingsSectionPath("market", params);
}

/**
 * The object to locate in the "Providers" section, and the corresponding URL params:
 * - `{ preset, model? }` -> `provider=<preset provider id>`;
 * - `{ custom, model? }` -> `custom=<custom provider id>`;
 *   with `model=<model ID>`, either opens that model's Sheet;
 * - `{ newCustom }` -> `custom=new`, optionally with `endpoint=<endpoint key>` and `base_url=<API URL>` to prefill the new form
 *   (the endpoint's "Create provider and use").
 *
 * 「供应商」分区里要定位的对象，对应的地址参数：
 * - `{ preset, model? }` → `provider=<预置供应商 id>`；
 * - `{ custom, model? }` → `custom=<自定义供应商 id>`；
 *   两者带 `model=<模型 ID>` 时打开这个模型的 Sheet；
 * - `{ newCustom }` → `custom=new`，可带 `endpoint=<端点 key>` 与 `base_url=<接口地址>` 预填新建表单
 *   （调用端点的「新建供应商并使用」）。
 */
export type ProviderTarget =
  | { preset: string; model?: string }
  | { custom: number; model?: string }
  | { newCustom: { endpoint?: string; baseUrl?: string } };

/** URL of a provider (or a custom model) in the global settings "Providers" section. / 全局设置「供应商」分区中某个供应商（或某个自定义模型）的地址。 */
export function providerSettingsPath(target: ProviderTarget): string {
  if ("preset" in target) {
    const params: Record<string, string> = { provider: target.preset };
    if (target.model) params.model = target.model;
    return settingsSectionPath("providers", params);
  }
  if ("custom" in target) {
    const params: Record<string, string> = { custom: String(target.custom) };
    if (target.model) params.model = target.model;
    return settingsSectionPath("providers", params);
  }
  const params: Record<string, string> = { custom: "new" };
  if (target.newCustom.endpoint) params.endpoint = target.newCustom.endpoint;
  if (target.newCustom.baseUrl) params.base_url = target.newCustom.baseUrl;
  return settingsSectionPath("providers", params);
}

/**
 * URL of an endpoint in the global settings "Endpoints" section: `endpoint=<endpoint key>`.
 * When coming from a custom provider pass `fromCustomProvider`, written as `from=<custom provider id>`: the endpoint page then shows at the top
 * "Back to 'provider name'", and the return URL is `providerSettingsPath({ custom: from })`.
 *
 * 全局设置「调用端点」分区中某个端点的地址：`endpoint=<端点 key>`。
 * 从自定义供应商跳来时传 `fromCustomProvider`，写成 `from=<自定义供应商 id>`：端点页据此在顶部显示
 * 「返回『供应商名』」，返回地址是 `providerSettingsPath({ custom: from })`。
 */
export function endpointSettingsPath(endpointKey?: string, options: { fromCustomProvider?: number } = {}): string {
  const params: Record<string, string> = {};
  if (endpointKey) params.endpoint = endpointKey;
  if (options.fromCustomProvider !== undefined) params.from = String(options.fromCustomProvider);
  return settingsSectionPath("endpoints", params);
}

/** Single-page top-level routes without sub-routes: exact match, prefixes do not count. / 无子路由的单页顶层路由——精确匹配，前缀不算数。 */
export const APP_TOP_LEVEL_ROUTES = [ROUTE_APP, ROUTE_APP_PROJECTS, ROUTE_APP_SETTINGS, ROUTE_APP_ASSETS] as const;

/**
 * Route segment constants under `/app/projects/:projectName`: the `<Route path>` in both `router.tsx` (project settings page) and
 * `StudioCanvasRouter` (inner `<Switch>`) take values from here, and
 * `APP_PROJECT_WORKSPACE_PATTERN` is built from them too, so adding/renaming a route only needs changing here.
 *
 * `/app/projects/:projectName` 下的路由段常量——`router.tsx`（项目设置页）与
 * `StudioCanvasRouter`（内层 `<Switch>`）的 `<Route path>` 都从这里取值，
 * `APP_PROJECT_WORKSPACE_PATTERN` 同样由它们拼出，新增/改名路由只需改这一处。
 */
export const WORKSPACE_ROUTE_SETTINGS = "settings";

/** Project settings tabs, i.e. values of the URL param `tab`, in sidebar order; missing or unrecognized falls back to `basics`. / 项目设置的分页，即地址参数 `tab` 的取值，顺序同侧栏；缺省或无法识别时落在 `basics`。 */
export const PROJECT_SETTINGS_TABS = ["basics", "style", "models", "voice", "memory", "agent"] as const;
export type ProjectSettingsTab = (typeof PROJECT_SETTINGS_TABS)[number];

/**
 * URL of a project settings tab (absolute path of the top-level route). Without `tab` it lands on the default "Basics".
 * `params` are the tab's own URL params, such as the file selected in project memory (`file=`).
 * When navigating from a nested route in the project workspace, prefix with `~`.
 *
 * 项目设置某个分页的地址（顶层路由的绝对路径）。不传 `tab` 时落在默认的「基础」。
 * `params` 是分页自己的地址参数，如项目记忆选中的文件（`file=`）。
 * 在项目工作区的嵌套路由里跳转时在前面加 `~`。
 */
export function projectSettingsPath(
  projectName: string,
  tab?: ProjectSettingsTab,
  params: Record<string, string> = {},
): string {
  const path = `${ROUTE_APP_PROJECTS}/${encodeURIComponent(projectName)}/${WORKSPACE_ROUTE_SETTINGS}`;
  const query = new URLSearchParams({ ...(tab ? { tab } : {}), ...params }).toString();
  return query ? `${path}?${query}` : path;
}

export const WORKSPACE_ROUTE_CHARACTERS = "characters";
export const WORKSPACE_ROUTE_SCENES = "scenes";
export const WORKSPACE_ROUTE_PROPS = "props";
export const WORKSPACE_ROUTE_PRODUCTS = "products";
export const WORKSPACE_ROUTE_EPISODES = "episodes";
export const WORKSPACE_ROUTE_FILES = "files";

/** View query params of the episode page: `?view=edit` opens the edit view, default is the shot view; `tl` specifies which edit timeline to open. / 集页的视图查询参数：`?view=edit` 打开剪辑视图，缺省为分镜视图；`tl` 指定打开哪条剪辑时间线。 */
export const EPISODE_VIEW_PARAM = "view";
export const EPISODE_VIEW_EDIT = "edit";
export const EPISODE_VIEW_TIMELINE_PARAM = "tl";

/** Relative path within the project workspace to open an episode's (episode ID) edit view; with `timelineId`, switch to that edit timeline. / 项目工作区内打开某集（集 ID）剪辑视图的相对路径；给出 `timelineId` 时切到那条剪辑时间线。 */
export function episodeEditViewPath(episode: number, timelineId?: string): string {
  const query = new URLSearchParams({ [EPISODE_VIEW_PARAM]: EPISODE_VIEW_EDIT });
  if (timelineId) query.set(EPISODE_VIEW_TIMELINE_PARAM, timelineId);
  return `/${WORKSPACE_ROUTE_EPISODES}/${episode}?${query.toString()}`;
}

/**
 * Leaf workspace route segments with no sub-path, matched directly. Besides the "Episodes" view itself, `episodes` also accepts `/:episodeId` (the episode page),
 * covered by an extra `episodes/[^/]+` branch in the regex below.
 */
/** 无子路径、直接匹配的工作区叶子路由段。`episodes` 除了「分集」视图本身还接受 `/:episodeId`（集页），
 *  在下面的正则里额外拼一条 `episodes/[^/]+` 分支覆盖后者。 */
const WORKSPACE_STATIC_LEAF_ROUTES = [
  WORKSPACE_ROUTE_SETTINGS,
  WORKSPACE_ROUTE_EPISODES,
  WORKSPACE_ROUTE_CHARACTERS,
  WORKSPACE_ROUTE_SCENES,
  WORKSPACE_ROUTE_PROPS,
  WORKSPACE_ROUTE_PRODUCTS,
  WORKSPACE_ROUTE_FILES,
] as const;

/**
 * Sub-paths under `/app/projects/:projectName` that actually have a route: `.../settings`
 * is the `ProjectSettingsPage` full-screen route registered separately in router.tsx; the rest are
 * the routes actually registered in the inner `<Switch>` of `StudioCanvasRouter` (nest route).
 * The inner `<Switch>`'s fallback route shows an empty state in the canvas; onboarding uses this to decide whether the URL is inside the main interface,
 * and unregistered sub-paths do not count.
 * wouter's underlying regexparam compiles routes with the `i` flag (case-insensitive), so `i` is added here too;
 * otherwise valid case variants of a path would be misjudged by this pattern as unregistered sub-paths.
 *
 * `/app/projects/:projectName` 下真正有路由承接的子路径——`.../settings`
 * 是 router.tsx 里独立注册的 `ProjectSettingsPage` 全屏路由；其余是
 * `StudioCanvasRouter`（nest 路由）内层 `<Switch>` 实际注册的路由集合。
 * 内层 `<Switch>` 的兜底路由在画布内显示空状态；新手引导据此判断地址是否落在主界面内，
 * 未注册的子路径不算。
 * wouter 底层 regexparam 编译路由时带 `i` 标志（大小写不敏感），这里同步加
 * 上 `i`，否则大小写变体的合法路径会被本模式误判为未注册子路径。
 */
export const APP_PROJECT_WORKSPACE_PATTERN = new RegExp(
  `^${ROUTE_APP_PROJECTS}/[^/]+(/(?:${WORKSPACE_STATIC_LEAF_ROUTES.join("|")}|${WORKSPACE_ROUTE_EPISODES}/[^/]+))?$`,
  "i",
);
