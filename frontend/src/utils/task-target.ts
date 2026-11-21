import type { TFunction } from "i18next";
import {
  WORKSPACE_ROUTE_CHARACTERS,
  WORKSPACE_ROUTE_PRODUCTS,
  WORKSPACE_ROUTE_PROPS,
  WORKSPACE_ROUTE_SCENES,
} from "@/app-routes";
import type { ProjectData, TaskItem, WorkspaceNotificationTarget } from "@/types";
import { episodeDisplayName, episodeItemLabel, itemIdsInEpisodeText, type ProjectScripts } from "@/utils/episode-display";

/**
 * Build a clickable jump-back notification target from a failed task, plus human-readable failure text.
 *
 * Jump-back routes differ by task_type:
 * - character/scene/prop/product -> assets page (no episode needed), resource_id is the asset name
 * - storyboard/video -> the matching episode's shot (ShotSplitView selects by segment id)
 * - grid -> the matching episode's grid canvas (navigation is the jump-back, no DOM anchor)
 * - reference_video -> the matching episode's reference unit (ReferenceVideoCanvas selects the unit)
 * - image_edit -> forwarded by resource_type to the matching route above
 *
 * The episode route is found by looking up task.script_file in projectData.episodes; when not found return
 * null, so the notification still pushes, just not clickable (graceful degradation).
 *
 * 由失败任务构建可点击回跳的通知 target，以及人类可读的失败文案。
 *
 * 回跳路由按 task_type 区分：
 * - character/scene/prop/product → 资产页（不需要剧集），resource_id 即资产名
 * - storyboard/video → 对应剧集的分镜（ShotSplitView 按 segment id 选中）
 * - grid → 对应剧集的宫格画布（导航即回跳，无 DOM 锚点）
 * - reference_video → 对应剧集的参考单元（ReferenceVideoCanvas 选中 unit）
 * - image_edit → 按 resource_type 转发到上述对应路由
 *
 * 剧集路由由 task.script_file 反查 projectData.episodes 得到；查不到时返回
 * null，让通知仍然推送、仅不可点击（优雅降级）。
 */

const ASSET_ROUTES: Record<"character" | "scene" | "prop" | "product", string> = {
  character: `/${WORKSPACE_ROUTE_CHARACTERS}`,
  scene: `/${WORKSPACE_ROUTE_SCENES}`,
  prop: `/${WORKSPACE_ROUTE_PROPS}`,
  product: `/${WORKSPACE_ROUTE_PRODUCTS}`,
};

/** Jump-back target for one asset on the assets page (derivatives locate their base-look card). / 资产页上某张资产的回跳 target（衍生定位到其本体卡片）。 */
export function assetNotificationTarget(
  assetType: keyof typeof ASSET_ROUTES,
  name: string,
): WorkspaceNotificationTarget {
  return { type: assetType, id: name, route: ASSET_ROUTES[assetType], highlight_style: "flash" };
}

const FAILURE_TEXT_KEYS: Partial<
  Record<TaskItem["task_type"], { key: string; idParam: "id" | "unitId" | "episode" }>
> = {
  storyboard: { key: "storyboard_task_failed", idParam: "id" },
  video: { key: "video_task_failed", idParam: "id" },
  character: { key: "character_task_failed", idParam: "id" },
  scene: { key: "scene_task_failed", idParam: "id" },
  prop: { key: "prop_task_failed", idParam: "id" },
  product: { key: "product_task_failed", idParam: "id" },
  grid: { key: "grid_task_failed", idParam: "id" },
  reference_video: { key: "reference_generation_task_failed", idParam: "unitId" },
  image_edit: { key: "image_edit_task_failed", idParam: "id" },
  text_drama_script_plan: { key: "script_plan_task_failed", idParam: "episode" },
  text_narration_script_plan: { key: "script_plan_task_failed", idParam: "episode" },
  text_reference_script_plan: { key: "script_plan_task_failed", idParam: "episode" },
  // YouReel (owner 2026-11-21: "generation error should display a toast with a plain text explanation"): these failed
  // without any toast
  tts: { key: "narration_task_failed", idParam: "id" },
  voice_sample: { key: "voice_sample_generation_failed", idParam: "id" },
  grid_split: { key: "grid_task_failed", idParam: "id" },
  text_episode_plan: { key: "episode_plan_task_failed", idParam: "episode" },
  text_episode_script: { key: "episode_script_task_failed", idParam: "episode" },
  text_draft_repair: { key: "draft_repair_task_failed", idParam: "episode" },
};

/** Episode-level tasks have resource_id like `episode-3`; failure text refers to it by episode name. / 集级任务的 resource_id 形如 `episode-3`，失败文案按集名指称。 */
const EPISODE_RESOURCE_ID = /^episode-(\d+)$/;

function failureTarget(
  idParam: "id" | "unitId" | "episode",
  task: TaskItem,
  projectData: ProjectData | null,
  scripts: ProjectScripts,
  t: TFunction,
): string {
  const episodes = projectData?.episodes ?? [];
  if (idParam !== "episode") return episodeItemLabel(task.resource_id, scripts, episodes, t);
  const match = EPISODE_RESOURCE_ID.exec(task.resource_id);
  return match ? episodeDisplayName(episodes, Number(match[1]), t) : task.resource_id;
}

/**
 * Normalize script_file: episode metadata always carries the `scripts/` prefix, while task rows and grid records
 * are passed in by each enqueueing caller without guaranteed consistent format, so strip the prefix before comparing.
 *
 * 归一化 script_file：episode 元数据固定带 `scripts/` 前缀，任务行与 grid 记录
 * 由各入队调用方自行传入、格式不保证一致，比较前统一剥前缀。
 */
export function stripScriptsPrefix(path: string): string {
  return path.replace(/^scripts\//, "");
}

function resolveEpisodeRoute(
  projectData: ProjectData | null,
  scriptFile: string | null,
): string | null {
  if (!projectData || !scriptFile) return null;
  const normalized = stripScriptsPrefix(scriptFile);
  const ep = projectData.episodes.find(
    (e) => stripScriptsPrefix(e.script_file) === normalized,
  );
  return ep ? `/episodes/${ep.episode}` : null;
}

export function buildTaskFailureTarget(
  task: TaskItem,
  projectData: ProjectData | null,
): WorkspaceNotificationTarget | null {
  switch (task.task_type) {
    case "character":
    case "scene":
    case "prop":
    case "product":
      return {
        type: task.task_type,
        id: task.resource_id,
        route: ASSET_ROUTES[task.task_type],
        highlight_style: "flash",
      };
    case "storyboard":
    case "video": {
      const route = resolveEpisodeRoute(projectData, task.script_file);
      return route
        ? { type: "segment", id: task.resource_id, route, highlight_style: "flash" }
        : null;
    }
    case "grid": {
      const route = resolveEpisodeRoute(projectData, task.script_file);
      return route ? { type: "grid", id: task.resource_id, route } : null;
    }
    case "reference_video": {
      const route = resolveEpisodeRoute(projectData, task.script_file);
      return route ? { type: "reference_unit", id: task.resource_id, route } : null;
    }
    case "image_edit": {
      // image_edit shares one task_type across character/scene/prop/product/storyboard; the real
      // resource kind is in resource_type.
      // image_edit 跨 character/scene/prop/product/storyboard 共用 task_type，真正
      // 的资源种类在 resource_type。
      switch (task.resource_type) {
        case "character":
        case "scene":
        case "prop":
        case "product":
          return {
            type: task.resource_type,
            id: task.resource_id,
            route: ASSET_ROUTES[task.resource_type],
            highlight_style: "flash",
          };
        case "storyboard": {
          const route = resolveEpisodeRoute(projectData, task.script_file);
          return route
            ? { type: "segment", id: task.resource_id, route, highlight_style: "flash" }
            : null;
        }
        default:
          return null;
      }
    }
    default:
      return null;
  }
}

/**
 * Max characters of the rejection-reason summary in notification text. Notifications are one-line toasts; overflow is truncated and ends with an ellipsis,
 * and the full summary isn't expanded within a one-line notification toast.
 *
 * 通知文案里拒因摘要的最大字符数。通知是一行 toast，超出部分截断并省略号收尾，
 * 完整摘要不在单行通知 toast 内展开。
 */
const PROVIDER_REASON_MAX_LENGTH = 120;

/**
 * Upstream rejection-reason summary: the upstream raw text the backend returns separately in `provider_rejected`'s error_params,
 * not translated, shown with entry episode ID prefixes stripped, rendered separately from the i18n template's error_message. Other failure codes lack
 * this field and return null.
 *
 * 上游拒因摘要：后端在 `provider_rejected` 的 error_params 里单独回传的上游原文，
 * 不参与翻译，显示时剥去条目的集 ID 前缀，与 i18n 模板的 error_message 分开渲染。其它失败码没有
 * 这个字段，返回 null。
 */
export function providerReasonOf(task: TaskItem): string | null {
  if (task.error_code !== "provider_rejected") return null;
  const reason = task.error_params?.provider_reason;
  if (typeof reason !== "string") return null;
  const trimmed = itemIdsInEpisodeText(reason.trim());
  return trimmed || null;
}

function truncateProviderReason(reason: string): string {
  // Split by code point: slicing UTF-16 code units directly could break in the middle of a surrogate pair, producing lone surrogates.
  // 按码点切分：直接对 UTF-16 code unit 切片会从代理对中间断开，产出孤立代理。
  const codePoints = [...reason];
  return codePoints.length > PROVIDER_REASON_MAX_LENGTH
    ? `${codePoints.slice(0, PROVIDER_REASON_MAX_LENGTH).join("")}…`
    : reason;
}

/**
 * Notification text of a failed task. YouReel: an unknown task_type gets the generic text (upstream returned null and
 * the caller pushed nothing).
 * Entries are referred to as "title · 5" (current position in the episode); an upstream rejection summary, if any, is appended after the text chosen by task_type.
 *
 * 失败任务的通知文案。未知 task_type 返回 null（调用方据此跳过推送）。
 * 条目按「标题 · 5」（本集里的当前位置）指称；有上游拒因摘要时追加在按 task_type 选出的文案之后。
 */
export function describeTaskFailure(
  t: TFunction,
  task: TaskItem,
  projectData: ProjectData | null = null,
  scripts: ProjectScripts = {},
): string | null {
  const reason = task.error_message ?? t("reference_status_failed");
  const config = FAILURE_TEXT_KEYS[task.task_type];
  // YouReel: any other task type still gets a toast with the reason (it got none)
  const message = config
    ? t(config.key, { [config.idParam]: failureTarget(config.idParam, task, projectData, scripts, t), reason })
    : t("task_failed_generic", { reason });
  const providerReason = providerReasonOf(task);
  if (!providerReason) return message;
  // The rejection suffix takes its own template and is concatenated, rather than passing the rendered message back to i18next as an interpolation variable: interpolation
  // runs String.replace per match and only hits the first same-name placeholder, so if message contains
  // a literal placeholder like `{{reason}}` (error_message is upstream raw text and may contain it), the reason would land
  // in the wrong position.
  // 拒因后缀单独取模板再拼接，不把已渲染的 message 当插值变量传回 i18next：插值
  // 对每个 match 走 String.replace，只命中首个同名占位符，message 里若含
  // `{{reason}}` 这类字面占位符（error_message 是上游原文，可能包含），拒因会落到
  // 错误位置。
  const suffix = t("task_failed_provider_reason_suffix", { reason: truncateProviderReason(providerReason) });
  return `${message}${suffix}`;
}
