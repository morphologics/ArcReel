/**
 * Generation mode utilities — mirrors lib/project/project_manager.py.
 *
 * Generation mode is binary `storyboard | reference_video`, locked at creation and unchangeable afterwards. Grid is an assembly option within storyboard mode
 * (the `grid_storyboard` boolean) that doesn't change the input contract fed to the video model, so it's not a third generation mode.
 *
 * 生成模式工具 — mirrors lib/project/project_manager.py。
 *
 * 生成模式二值 `storyboard | reference_video`，创建时锁定、之后不可更改。宫格是分镜图生视频内的
 * 装配选项（`grid_storyboard` 布尔），不改变喂给视频模型的输入契约，故不是第三种生成模式。
 */

export type GenerationRoute = "storyboard" | "reference_video";

const ROUTES: readonly string[] = ["storyboard", "reference_video"];

/**
 * Parse an untyped project field into a generation mode value.
 *
 * Generation mode is required in persisted data and always one of the two values. When project data isn't loaded or the file on disk was broken externally, present as
 * storyboard so the page stays usable instead of crashing on text lookup.
 *
 * 把未类型化的项目字段解析成生成模式值。
 *
 * 生成模式在持久化数据中必填且恒为二值之一。项目数据未加载或磁盘文件被外部改坏时按
 * storyboard 呈现，让页面可用而不是崩在取文案上。
 */
export function normalizeRoute(value: unknown): GenerationRoute {
  return ROUTES.includes(value as string) ? (value as GenerationRoute) : "storyboard";
}

/**
 * Whether grid is in effect — mirrors lib/project/project_manager.py:grid_storyboard_enabled().
 * A leftover `grid_storyboard=true` on reference-to-video doesn't activate grid.
 *
 * 宫格是否生效 — mirrors lib/project/project_manager.py:grid_storyboard_enabled()。
 * 参考生视频上残留的 `grid_storyboard=true` 不激活宫格。
 */
export function gridStoryboardEnabled(
  project: { generation_mode?: GenerationRoute | null; grid_storyboard?: boolean } | null | undefined,
): boolean {
  if (!project) return false;
  return normalizeRoute(project.generation_mode) === "storyboard" && project.grid_storyboard === true;
}

/**
 * Text key for entry counts — the noun follows generation mode, independent of content mode: storyboard mode reports "shot count",
 * reference-to-video reports "video unit count". Every place showing entry counts reads the same mapping instead of writing its own ternary.
 *
 * `withStatus` takes the one with a status suffix ("N shots · in production"), otherwise the bare count one.
 *
 * 条目数的文案 key — 名词按生成模式定，与创作类型无关：分镜图生视频报「分镜数」、
 * 参考生视频报「视频单元数」。所有展示条目数的位置读同一份映射，不各自写三元判断。
 *
 * `withStatus` 取带状态后缀的那条（「N 分镜 · 制作中」），否则取裸计数那条。
 */
const ITEM_COUNT_NOUNS: Record<GenerationRoute, string> = {
  storyboard: "storyboard_count",
  reference_video: "video_unit_count",
};

export function itemCountKey(
  route: GenerationRoute,
  { withStatus = false }: { withStatus?: boolean } = {},
): string {
  const noun = ITEM_COUNT_NOUNS[route];
  return withStatus ? `${noun}_and_status` : noun;
}
