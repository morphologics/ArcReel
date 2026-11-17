/**
 * Episode page view routing: the current view is stored in the URL's `?view=`; values and availability conditions are defined only here.
 *
 * | Value | View | When it appears |
 * | --- | --- | --- |
 * | `setting` | Story setting (ad project synopsis, genre, theme and world setting) | Ad projects only, listed first |
 * | `plan` | Script plan (content review page; episode source review before the script is generated) | Non-ad projects with a script plan intermediate draft, episode source pending review, or reference-to-video projects |
 * | `grid` | Multi-grid storyboard | Storyboard mode with grid enabled; not selectable while episode source is pending review |
 * | `board` | Shots (reference-to-video projects call it "Video units") | Always present; not selectable when there is a script plan but no final script yet |
 * | `edit` | Edit | Non-demo projects with a final script |
 *
 * Default view: `board` when there is a final script, otherwise `plan` when there is a script plan. The URL writes `view` only when the selected view is not
 * the default, so a page sitting on the default view automatically lands on shots after the script is generated. Invalid or currently unselectable values show the default view.
 *
 * 集页视图路由：当前视图记在地址的 `?view=` 上，取值与可用条件只在这里定义。
 *
 * | 取值 | 视图 | 何时出现 |
 * | --- | --- | --- |
 * | `setting` | 故事设定（广告项目的梗概、类型、主题与世界观） | 只在广告项目出现，排第一 |
 * | `plan` | 脚本规划（内容确认页；剧本未生成时是集原文确认） | 非广告项目，有脚本规划中间稿、集原文待确认，或参考生视频项目 |
 * | `grid` | 多宫格分镜图 | 分镜图生视频且开启宫格；集原文待确认时不可选 |
 * | `board` | 分镜（参考生视频项目称「视频单元」） | 恒有；有脚本规划却还没有正式剧本时不可选 |
 * | `edit` | 剪辑 | 有正式剧本的非演示项目 |
 *
 * 缺省视图：有正式剧本时是 `board`，否则有脚本规划时是 `plan`。地址只在所选视图不是缺省视图时
 * 写 `view`，剧本生成后停在缺省视图上的页面因此自动落到分镜。取值无效或当前不可选时按缺省视图显示。
 */
import { EPISODE_VIEW_EDIT, EPISODE_VIEW_PARAM } from "@/app-routes";
import type { EpisodeMeta, ProjectData } from "@/types";
import type { EpisodeScript } from "@/types/script";
import type { GenerationRoute } from "@/utils/generation-mode";

export const EPISODE_VIEWS = ["setting", "plan", "grid", "board", EPISODE_VIEW_EDIT] as const;
export type EpisodeView = (typeof EPISODE_VIEWS)[number];
/** Views served by the canvas; story setting and the edit view replace the whole canvas. / 画布承担的视图；故事设定与剪辑视图替换整个画布。 */
export type CanvasView = Exclude<EpisodeView, "setting" | "edit">;

/** Facts deciding which views the episode page has, computed by the routing layer from the project and this episode. / 决定集页有哪些视图的事实，由路由层按项目与这一集算出。 */
export interface EpisodeViewFacts {
  isAd: boolean;
  route: GenerationRoute;
  /** Storyboard mode with multi-grid storyboard enabled. / 分镜图生视频且开启了宫格分镜图。 */
  grid: boolean;
  /** A final script exists. / 已有正式剧本。 */
  hasScript: boolean;
  /** A script plan intermediate draft exists (`script_status` is segmented or generated). / 已有脚本规划中间稿（`script_status` 为 segmented 或 generated）。 */
  hasDraft: boolean;
  /** Neither script nor intermediate draft exists; the script plan view shows episode source review. / 剧本与中间稿都没有，脚本规划视图显示集原文确认。 */
  sourceReview: boolean;
  /** Demo project: no edit view. / 演示项目：没有剪辑视图。 */
  demo: boolean;
}

/** Element id of a view tab; the view area uses it as its accessible name. / 视图 tab 的元素 id，视图区以它作为无障碍名。 */
export function episodeViewTabId(view: EpisodeView): string {
  return `episode-view-tab-${view}`;
}

/** This episode has an intermediate draft: a segmented draft or a generated script draft. / 本集已有中间稿：分段稿或生成好的剧本草稿。 */
export function hasEpisodeDraft(meta: EpisodeMeta | undefined): boolean {
  return meta?.script_status === "segmented" || meta?.script_status === "generated";
}

/**
 * The script plan view shows the episode source: an episode is selected, but there is neither a final script nor an intermediate draft.
 * Ad/short is always a single episode and demo projects have no source to cut; they use their own canvases.
 *
 * 脚本规划视图呈现集原文：已选集，但正式剧本与中间稿都没有。
 * 广告/短片恒单集、演示项目没有源文可切，走各自画布。
 */
export function showsEpisodeSource(
  projectData: ProjectData | null,
  episode: number,
  script: EpisodeScript | null,
  demo: boolean,
): boolean {
  const meta = projectData?.episodes?.find((entry) => entry.episode === episode);
  return meta !== undefined && !script && !hasEpisodeDraft(meta) && projectData?.content_mode !== "ad" && !demo;
}

export interface EpisodeViewTab {
  view: EpisodeView;
  disabled: boolean;
  /**
   * YouReel: selectable but not shown as a tab unless it is the current view. The script plan once the episode has its
   * script: it is then a read-only record, reached from the episode's "⋯" menu (owner 2026-11-17).
   */
  hidden?: boolean;
}

function planAvailable(facts: EpisodeViewFacts): boolean {
  if (facts.isAd) return false;
  return facts.sourceReview || facts.hasDraft || (facts.route === "reference_video" && facts.hasScript);
}

/** View tabs of the episode page header, in display order. / 集页页头的视图 tab，按显示顺序排列。 */
export function episodeViewTabs(facts: EpisodeViewFacts): EpisodeViewTab[] {
  const plan = planAvailable(facts);
  const tabs: EpisodeViewTab[] = [];
  if (facts.isAd) tabs.push({ view: "setting", disabled: false });
  if (plan) tabs.push({ view: "plan", disabled: false, ...(facts.hasScript ? { hidden: true } : {}) });
  if (facts.grid) tabs.push({ view: "grid", disabled: facts.sourceReview });
  // Reference-to-video's unit list is viewable already at the intermediate draft stage; storyboard mode waits for the final script. Without a script plan, shots is the only view.
  // 参考生视频的单元列表在中间稿阶段就可查看；分镜图生视频要等正式剧本。没有脚本规划时分镜是唯一视图。
  const boardEnabled =
    facts.hasScript || !plan || (facts.route === "reference_video" && !facts.sourceReview);
  tabs.push({ view: "board", disabled: !boardEnabled });
  if (facts.hasScript && !facts.demo) tabs.push({ view: EPISODE_VIEW_EDIT, disabled: false });
  return tabs;
}

export function defaultEpisodeView(facts: EpisodeViewFacts): EpisodeView {
  if (facts.hasScript) return "board";
  return planAvailable(facts) ? "plan" : "board";
}

/** Which view a URL value lands on: the default view when invalid or unselectable. / 地址上的取值落到哪个视图：无效或不可选时按缺省视图。 */
export function resolveEpisodeView(requested: string | null, facts: EpisodeViewFacts): EpisodeView {
  const tab = episodeViewTabs(facts).find((entry) => entry.view === requested);
  return tab && !tab.disabled ? tab.view : defaultEpisodeView(facts);
}

/**
 * Query params after switching to `next`: the default view does not write `view`, other params are kept as-is. Returns a new object without modifying the passed params:
 * when the navigation is blocked by the leave guard, the current URL's params must not have changed already.
 *
 * 切到 `next` 后的查询参数：缺省视图不写 `view`，其余参数原样保留。返回新对象、不改动传入的参数——
 * 跳转被离开拦截挡下时，当前地址的参数不能已经变了。
 */
export function withEpisodeView(params: URLSearchParams, next: EpisodeView, facts: EpisodeViewFacts): URLSearchParams {
  const result = new URLSearchParams(params);
  if (next === defaultEpisodeView(facts)) result.delete(EPISODE_VIEW_PARAM);
  else result.set(EPISODE_VIEW_PARAM, next);
  return result;
}

/**
 * Whether the in-app navigation to `to` still stays in the current view: same episode, and the URL's `view` lands on the same view by the rules above.
 *
 * Switching among script plan, multi-grid storyboard and shots does not unmount the canvas instance, but edit units in the canvas hang under only one of these views
 * (shot details and video unit bodies render only in the shot view): the canvas instance not unmounting does not mean the edit unit does not. Edit units use it as
 * `allowNavigation`, so only navigations that will not unmount them skip the prompt; going to another view, the edit view or another episode is blocked as usual.
 * Both `to` and `here` include the nested route's base; when `view` is omitted, the default view computed from `facts` is used.
 *
 * 应用内跳转 `to` 之后是否仍停留在当前视图：同一集，且地址上的 `view` 按上面的规则落到同一个视图。
 *
 * 脚本规划、多宫格分镜图与分镜之间切换时画布实例不卸载，但画布里的编辑单元只挂在其中一个视图下
 * （分镜详情、视频单元正文只在分镜视图渲染）：画布实例不卸载不等于编辑单元不卸载。编辑单元把它作为
 * `allowNavigation`，只有不会卸载自己的跳转才不询问；去别的视图、剪辑视图或别的集都照常拦截。
 * `to` 与 `here` 都带嵌套路由的 base，`view` 缺省时按 `facts` 算出的缺省视图判断。
 */
export function staysInEpisodeView(to: string, here: string, facts: EpisodeViewFacts): boolean {
  const current = new URL(here, "http://episode-view.invalid");
  const target = new URL(to, current);
  if (target.pathname !== current.pathname) return false;
  return (
    resolveEpisodeView(target.searchParams.get(EPISODE_VIEW_PARAM), facts) ===
    resolveEpisodeView(current.searchParams.get(EPISODE_VIEW_PARAM), facts)
  );
}
