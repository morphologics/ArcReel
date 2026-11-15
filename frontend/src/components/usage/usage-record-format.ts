import { AudioLines, FileText, Image, Video } from "lucide-react";

import type { TFunction } from "i18next";

import type { CallType, EpisodeItemRef, UsageRecordStatus, UsageSummary } from "@/types";
import { parseIsoTimestamp } from "@/utils/date-format";
import { itemIdWithinEpisode } from "@/utils/episode-display";
import { episodeItemKind, isPositionedItemId, type EpisodeItemKind } from "@/utils/episode-item";
import { formatElapsedMs } from "@/utils/task-elapsed";
import type { ElapsedTranslate } from "@/utils/task-elapsed";

/** Glyph and tone of a media type: `iconClass` colors the icon, `swatchClass` the swatch, `color` fills the chart; all three are the same `--media-*` token. / 媒体类型的字形与色调：`iconClass` 给图标着色，`swatchClass` 给色块，`color` 供图表填充，三者是同一个 `--media-*` token。 */
export const MEDIA_META: Record<
  CallType,
  { Icon: typeof Image; iconClass: string; swatchClass: string; color: string; labelKey: string }
> = {
  image: { Icon: Image, iconClass: "text-media-image", swatchClass: "bg-media-image", color: "var(--media-image)", labelKey: "usage_media_image" },
  video: { Icon: Video, iconClass: "text-media-video", swatchClass: "bg-media-video", color: "var(--media-video)", labelKey: "usage_media_video" },
  text: { Icon: FileText, iconClass: "text-media-text", swatchClass: "bg-media-text", color: "var(--media-text)", labelKey: "usage_media_text" },
  audio: { Icon: AudioLines, iconClass: "text-media-audio", swatchClass: "bg-media-audio", color: "var(--media-audio)", labelKey: "usage_media_audio" },
};

/** Placeholder when there is no value. / 无值时的占位。 */
const DASH = "—";

export const STATUS_LABEL_KEYS: Record<UsageRecordStatus, string> = {
  pending: "usage_status_pending",
  success: "usage_status_success",
  failed: "usage_status_failed",
  cancelled: "usage_status_cancelled",
};

/** Status text and status dot color. / 状态文字与状态点的颜色。 */
export const STATUS_TEXT_CLASSES: Record<UsageRecordStatus, string> = {
  pending: "text-primary",
  success: "text-good",
  failed: "text-destructive",
  cancelled: "text-muted-foreground",
};

export const STATUS_DOT_CLASSES: Record<UsageRecordStatus, string> = {
  pending: "bg-primary",
  success: "bg-good",
  failed: "bg-destructive",
  cancelled: "bg-muted-foreground",
};

/** Failure phrases that have translations. Lists show only the phrase; the raw error appears only in details. / 有翻译的失败短语。列表只显示短语，原始报错只在详情里显示。 */
const FAILURE_PHRASE_KEYS: Record<string, string> = {
  rate_limited: "usage_error_rate_limited",
  content_policy: "usage_error_content_policy",
  timeout: "usage_error_timeout",
  download_failed: "usage_error_download_failed",
  interrupted: "usage_error_interrupted",
};

export function failurePhraseKey(errorCode: string | null): string | null {
  if (!errorCode) return null;
  return FAILURE_PHRASE_KEYS[errorCode] ?? null;
}

const PURPOSE_KEYS: Record<string, string> = {
  generation_task: "usage_purpose_generation_task",
  script_generation: "usage_purpose_script_generation",
  episode_planning: "usage_purpose_episode_planning",
  project_overview: "usage_purpose_project_overview",
  style_analysis: "usage_purpose_style_analysis",
  assistant_session: "usage_purpose_assistant_session",
  endpoint_trial: "usage_purpose_endpoint_trial",
};

export function purposeKey(purpose: string | null): string | null {
  if (!purpose) return null;
  return PURPOSE_KEYS[purpose] ?? null;
}

/**
 * The target column splits into two parts: the shot number (in-episode ID) as an untruncated prefix, followed by the episode name, truncated when out of room.
 * Calls without a shot are shown by purpose, with an empty prefix.
 *
 * 目标列拆成两段：分镜号（集内 ID）作为不截断的前缀，集名在后、放不下时截断。
 * 没有分镜的调用按用途显示，前缀为空。
 */
export interface TargetItem {
  kind: EpisodeItemKind;
  /** Current position in the episode; null when the entry is no longer in the script. / 在本集里的当前位置；条目已不在剧本里时为 null。 */
  position: number | null;
  excerpt: string;
  thumbnail: string | null;
}

export interface TargetParts {
  /** Shot or video unit: identified by position, thumbnail and body excerpt. / 分镜或视频单元：以位置、缩略图与正文摘录指认。 */
  item: TargetItem | null;
  /** In-episode ID of entries not referred to by position (grids). / 不按位置指称的条目（宫格）的集内 ID。 */
  prefix: string | null;
  name: string;
}

export function targetParts(
  segmentId: string | null,
  segmentRef: EpisodeItemRef | null,
  purpose: string | null,
  t: TFunction,
): TargetParts {
  if (segmentId) {
    const positioned = isPositionedItemId(segmentId);
    if (segmentRef) {
      const name =
        segmentRef.episode_title.trim() ||
        t("common:episode_position_name", { position: segmentRef.episode_position });
      if (!positioned) return { item: null, prefix: segmentRef.item_id, name };
      const item = {
        kind: episodeItemKind(segmentId),
        position: segmentRef.item_position ?? null,
        excerpt: segmentRef.item_excerpt ?? "",
        thumbnail: segmentRef.item_thumbnail ?? null,
      };
      return { item, prefix: null, name };
    }
    const itemId = itemIdWithinEpisode(segmentId);
    if (itemId === segmentId) return { item: null, prefix: segmentId, name: "" };
    // Has an episode prefix but no reference: the episode was removed from the ledger, so the episode name is "Untitled episode"; the entry went with it, shown as "Deleted".
    // 带集前缀却没有指称：集已移出账本，集名用「未命名集」；条目随集一起不在了，显示「已删除」。
    const name = t("common:episode_unlisted_name");
    if (!positioned) return { item: null, prefix: itemId, name };
    return { item: { kind: episodeItemKind(segmentId), position: null, excerpt: "", thumbnail: null }, prefix: null, name };
  }
  const key = purposeKey(purpose);
  return { item: null, prefix: null, name: key ? t(`dashboard:${key}`) : DASH };
}

/** Provider display name prefers the label in the filter candidate values, falling back to the id when not found. / 供应商显示名优先取筛选候选值里的 label，查不到回退 id。 */
export function providerLabelResolver(
  summary: UsageSummary | null,
): (provider: string | null) => string {
  const labels = new Map(
    (summary?.filter_options.providers ?? []).map((option) => [
      option.provider,
      option.label,
    ]),
  );
  return (provider) => (provider ? (labels.get(provider) ?? provider) : DASH);
}

/** Unpriced reason code -> copy key; codes come from backend `UnpricedReason`, unknown codes fall back to a generic explanation. / 未定价原因码 → 文案 key；码来自后端 `UnpricedReason`，不认识的码回落到通用说明。 */
const UNPRICED_REASON_KEYS: Record<string, string> = {
  no_entry: "usage_unpriced_reason_no_entry",
  no_price_table: "usage_unpriced_reason_no_price_table",
  missing_rate: "usage_unpriced_reason_missing_rate",
  unrecognized_terms: "usage_unpriced_reason_unrecognized_terms",
  no_usage_estimate: "usage_unpriced_reason_no_usage_estimate",
};

export function unpricedReasonKey(reason: string | null): string {
  return (reason && UNPRICED_REASON_KEYS[reason]) || "usage_unpriced_reason_unknown";
}

/** The summary has only project names; titles are looked up in `filter_options.project_titles`. / 汇总里只有项目名，标题从 `filter_options.project_titles` 查。 */
export function projectTitleResolver(summary: UsageSummary | null): (name: string) => string | null {
  const titles = summary?.filter_options.project_titles ?? {};
  return (name) => titles[name] ?? null;
}

/** i18n language code -> Intl locale; the names differ, hence an explicit map; unknown languages fall back to English. / i18n 语言码 → Intl locale；两者不同名，故显式映射，未知语言回落英文。 */
const INTL_LOCALES: Record<string, string> = {
  zh: "zh-CN",
  en: "en-US",
  vi: "vi-VN",
  ru: "ru-RU",
};

function intlLocale(language: string): string {
  return INTL_LOCALES[language.split("-")[0]] ?? "en-US";
}

const percentFormatters = new Map<string, Intl.NumberFormat>();

function percentFormatter(language: string): Intl.NumberFormat {
  const locale = intlLocale(language);
  let formatter = percentFormatters.get(locale);
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale, {
      style: "percent",
      maximumFractionDigits: 1,
    });
    percentFormatters.set(locale, formatter);
  }
  return formatter;
}

/**
 * Percentage rendering shared by success and failure rates; the KPI strip, breakdown table, trend tooltip and popover all go through here:
 * the same number appears several times on one page, so the format must match. With a zero denominator the backend gives null, shown as a dash.
 *
 * 成功率与失败率共用的百分比渲染，KPI 条、构成表、趋势 tooltip、悬浮层同走这一处：
 * 同一个数在同一页出现多次，格式必须一致。分母为 0 时后端给 null，显示破折号。
 */
export function formatRatio(rate: number | null, language: string): string {
  return rate === null ? DASH : percentFormatter(language).format(rate);
}

const countFormatters = new Map<string, Intl.NumberFormat>();

/**
 * Thousands separators of counts rendered in the UI language. Shared by the settings page KPI strip and the popover KPI row, using `intlLocale` like `formatRatio`:
 * call counts and success rates in one row must use one separator convention, whereas `toLocaleString()` follows the
 * browser language.
 *
 * 计数的千分位按界面语言渲染。设置页 KPI 条与悬浮层 KPI 行共用，与 `formatRatio` 同走
 * `intlLocale`：一行里的调用次数与成功率必须是一套分隔习惯，`toLocaleString()` 跟的是
 * 浏览器语言。
 */
export function formatCount(value: number, language: string): string {
  const locale = intlLocale(language);
  let formatter = countFormatters.get(locale);
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale);
    countFormatters.set(locale, formatter);
  }
  return formatter.format(value);
}

const compactFormatters = new Map<string, Intl.NumberFormat>();

/** Fixed-point decimal with trailing zeros dropped: 12.0 -> 12, 12.34 -> 12.3. / 末位去零的定点小数：12.0 → 12，12.34 → 12.3。 */
function trimmedFixed(value: number): string {
  return String(Number(value.toFixed(1)));
}

/**
 * Compact token counts with the unit written into the value. Chinese carries over at 10^4 and 10^8 units,
 * other languages use Intl compact notation (12.3K tokens); values under ten thousand (thousand) are written as is, with thousands separators.
 *
 * token 数量带单位的紧凑写法，单位写在值里（如「12.3 万 token」）。中文按「万」「亿」进位，
 * 其余语言走 Intl 的紧凑记数（12.3K tokens）；不足一万（千）的原样写出，带千分位。
 */
export function formatTokenAmount(value: number, language: string, t: TFunction): string {
  if (language.split("-")[0] === "zh") {
    if (value >= 100_000_000) return t("usage_tokens_amount_100m", { value: trimmedFixed(value / 100_000_000) });
    if (value >= 10_000) return t("usage_tokens_amount_10k", { value: trimmedFixed(value / 10_000) });
    return t("usage_tokens_amount", { value: formatCount(value, language) });
  }
  const locale = intlLocale(language);
  let formatter = compactFormatters.get(locale);
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 });
    compactFormatters.set(locale, formatter);
  }
  return t("usage_tokens_amount", { value: formatter.format(value) });
}

const dayFormatters = new Map<string, Intl.DateTimeFormat>();

/**
 * Render a calendar day `YYYY-MM-DD` in the current language. The date is a local day computed by the backend, not an instant, so construct the Date in the
 * local time zone -- `new Date(string)` would treat it as UTC midnight, shifting a day in the eastern or western hemisphere.
 *
 * 日历日 `YYYY-MM-DD` 按当前语言渲染。这个日期是后端算好的本地日，不是时刻，故按
 * 本地时区构造 Date——交给 `new Date(string)` 会当成 UTC 午夜，东西半球各挪一天。
 */
export function formatCalendarDay(
  day: string,
  language: string,
  options: Intl.DateTimeFormatOptions,
): string {
  const [year, month, date] = day.split("-").map(Number);
  if (!year || !month || !date) return day;
  const locale = intlLocale(language);
  const cacheKey = `${locale}|${JSON.stringify(options)}`;
  let formatter = dayFormatters.get(cacheKey);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(locale, options);
    dayFormatters.set(cacheKey, formatter);
  }
  return formatter.format(new Date(year, month - 1, date));
}

/**
 * Records of deleted projects are moved to a tombstone name `<project-name>#deleted-<UTC time>` (see `lib/db/repositories/project_records.py`),
 * separate from a new project with the same name. The UI shows the original name and deletion date; filters still use the raw tombstone name.
 *
 * 已删除项目的记录改挂到墓碑名 `<项目名>#deleted-<UTC 时刻>`（见 `lib/db/repositories/project_records.py`），
 * 与同名新项目分开。界面显示原名与删除日期，筛选仍用墓碑名原值。
 */
const DELETED_PROJECT = /^(.+)#deleted-(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/;

/**
 * Project display name on the usage page: show the title when there is one; without a title (project deleted, unreadable or untitled) fall back to the project name,
 * deleted projects show the original name and deletion date; endpoint trial-run records have no project and get "Untitled".
 *
 * 用量页的项目显示名：有标题显示标题；没有标题（项目已删除、读不到或未设标题）时回退到项目名，
 * 已删除的项目显示原名与删除日期；端点试跑的记录没有项目，给「未命名」。
 */
export function usageProjectLabel(
  name: string,
  t: (key: string, params?: Record<string, string>) => string,
  language: string,
  title?: string | null,
): string {
  if (title) return title;
  if (!name) return t("usage_project_untitled");
  const match = DELETED_PROJECT.exec(name);
  if (!match) return name;
  const [, project, ...parts] = match;
  const [year, month, day, hour, minute, second] = parts.map(Number);
  const deletedAt = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  const date = new Intl.DateTimeFormat(intlLocale(language), { dateStyle: "medium" }).format(deletedAt);
  return t("usage_project_deleted", { name: project, date });
}

/** Duration column; a dash when there is no duration to show. Copy shared with task readouts via `formatElapsedMs`. / 耗时列；无时长可显示时给破折号。文案与任务读数共用 `formatElapsedMs`。 */
export function formatDurationMs(
  durationMs: number | null,
  t: ElapsedTranslate,
): string {
  if (durationMs === null || durationMs < 0) return DASH;
  return formatElapsedMs(durationMs, t);
}

/** Live elapsed time of an in-progress row, starting from an ISO instant. / 进行中行的实时耗时，起点为 ISO 时刻。 */
export function elapsedSince(startedAt: string, now: number, t: ElapsedTranslate): string {
  const start = parseIsoTimestamp(startedAt).getTime();
  if (Number.isNaN(start)) return DASH;
  return formatDurationMs(Math.max(0, now - start), t);
}
