import type { TFunction } from "i18next";

import { WORKSPACE_ROUTE_EPISODES } from "@/app-routes";
import type { EpisodeMeta, EpisodesView, EpisodesViewEpisode, EpisodesViewFile } from "@/types";

/** Query params of the "Episodes" view: `upload` opens the upload dialog with a preselected mode, `episode` selects an episode, `create` opens the new episode dialog. / 「分集」视图的查询参数：`upload` 打开上传对话框并预选方式，`episode` 选中一集，`create` 打开新建一集对话框。 */
export const EPISODES_VIEW_UPLOAD_PARAM = "upload";
export const EPISODES_VIEW_EPISODE_PARAM = "episode";
export const EPISODES_VIEW_CREATE_PARAM = "create";

export type SourceUploadMode = "whole_source" | "episode";

/** Address of the "Episodes" view (relative route within the workspace). / 「分集」视图的地址（工作区内的相对路由）。 */
export function episodesViewPath(
  options: { upload?: SourceUploadMode; episode?: number; create?: boolean } = {},
): string {
  const params = new URLSearchParams();
  if (options.upload) params.set(EPISODES_VIEW_UPLOAD_PARAM, options.upload);
  if (options.create) params.set(EPISODES_VIEW_CREATE_PARAM, "1");
  if (options.episode !== undefined) params.set(EPISODES_VIEW_EPISODE_PARAM, String(options.episode));
  const query = params.toString();
  return `/${WORKSPACE_ROUTE_EPISODES}${query ? `?${query}` : ""}`;
}

/** Each episode's identity hue: keyed by episode ID, unchanged by reorders and inserts. Once written to the element's `--episode-hue`, the element and descendants use `bg-episode`, `text-episode`, `border-episode` for this episode's color. / 每集的身份色相：按集 ID 取，调序、插入都不换色。写进元素的 `--episode-hue` 后，元素及其子孙用 `bg-episode`、`text-episode`、`border-episode` 取这一集的身份色。 */
export function episodeHue(episodeId: number): number {
  return (episodeId * 67) % 360;
}

/** Size: "1,234 characters" or "1,234 words". / 体量：「1,234 字」或「1,234 词」。 */
export function formatVolume(t: TFunction, units: number, unit: EpisodesView["unit"]): string {
  return t(unit === "words" ? "dashboard:episodes_view_words" : "dashboard:episodes_view_chars", {
    count: units,
    formatted: units.toLocaleString(),
  });
}

/** Approximate read-aloud duration: under 1 minute, about N minutes. / 约略朗读时长：不足 1 分钟、约 N 分钟。 */
export function formatSpoken(t: TFunction, seconds: number): string {
  const minutes = Math.round(seconds / 60);
  return minutes < 1
    ? t("dashboard:episodes_view_spoken_under_minute")
    : t("dashboard:episodes_view_spoken_minutes", { count: minutes });
}

export type OutlineRow =
  | { kind: "episode"; episode: EpisodeMeta; info: EpisodesViewEpisode }
  | { kind: "gap"; units: number; key: string; sourceFile: string; end: number };

export interface OutlineFileGroup {
  file: EpisodesViewFile;
  index: number;
  rows: OutlineRow[];
  /** Size of unsplit source in this file after the last sliced episode. / 本文件在最后一个切出集之后尚未分集的原文体量。 */
  tailUnits: number;
}

/** Outline "From whole source": grouped by file; within a group, sliced episodes and unsliced source between them listed by source position. / 集目录「来自整本源文」：按文件分组，组内按源文位置列出切出集与夹在其间的未切分原文。 */
export function outlineFileGroups(view: EpisodesView, episodes: EpisodeMeta[]): OutlineFileGroup[] {
  const meta = new Map(episodes.map((episode) => [episode.episode, episode]));
  const info = new Map(view.episodes.map((episode) => [episode.episode, episode]));
  return view.files.flatMap((file, index) => {
    const rows: OutlineRow[] = [];
    let tailUnits = 0;
    for (const segment of file.segments) {
      if (segment.kind === "episode" && segment.episode !== null) {
        // Cross-file episodes are listed only in the group of the file where they start / 跨文件的集只列在起点所在文件的分组里
        if (segment.continued) continue;
        const episode = meta.get(segment.episode);
        const episodeInfo = info.get(segment.episode);
        if (episode && episodeInfo) rows.push({ kind: "episode", episode, info: episodeInfo });
      } else if (segment.gap) {
        rows.push({
          kind: "gap",
          units: segment.units,
          key: `${file.source_file}:${segment.start}`,
          sourceFile: file.source_file,
          end: segment.end,
        });
      } else {
        tailUnits += segment.units;
      }
    }
    return rows.some((row) => row.kind === "episode") ? [{ file, index, rows, tailUnits }] : [];
  });
}

/** Outline "Other episodes": episodes whose source isn't in the whole source (own source, no source, sliced episodes with no recorded source position), in episode order. / 集目录「其他集」：原文不在整本源文里的集（自带原文、无原文、原文位置没有记录的切出集），按播出顺序。 */
export function otherEpisodes(
  view: EpisodesView,
  episodes: EpisodeMeta[],
): { episode: EpisodeMeta; info: EpisodesViewEpisode | null }[] {
  const info = new Map(view.episodes.map((episode) => [episode.episode, episode]));
  return episodes
    .filter((episode) => !info.get(episode.episode)?.placed)
    .map((episode) => ({ episode, info: info.get(episode.episode) ?? null }));
}

/** File name stems like episode_N are reserved for episode source files and cannot be uploaded as whole source (same rule as backend `is_derived_episode_name`). / 形如 episode_N 的文件名主干留给集原文文件，不能作为整本源文上传（与后端 `is_derived_episode_name` 同一口径）。 */
export function isReservedEpisodeFileName(filename: string): boolean {
  const stem = filename.replace(/\.[^.]*$/, "");
  return /^episode_[0-9]+$/.test(stem);
}
