import type { TFunction } from "i18next";

import type { EpisodeItemRef, EpisodeMeta } from "@/types";
import { episodeItemKind, findEpisodeItem, isPositionedItemId, type EpisodeItemKind } from "@/utils/episode-item";

/** All of the project's scripts (`projects-store`'s `currentScripts`). / 项目的全部剧本（`projects-store` 的 `currentScripts`）。 */
export type ProjectScripts = Readonly<Record<string, unknown>>;

export type EpisodeLedger = readonly Pick<EpisodeMeta, "episode" | "title">[];

// Episode ID prefix of entry IDs: the E3 in E3S01 / E3U02 / E3G01. / 条目 ID 的集 ID 前缀：E3S01 / E3U02 / E3G01 里的 E3。
const ITEM_ID_EPISODE_PREFIX = /^E(\d+)(?=[A-Z]\d)/;

/** An episode's position in episode order (episodes[] ordering), from 1; null when not in the ledger. / 一集在播出顺序（episodes[] 排列）中的位置，从 1 起；不在账本里时为 null。 */
export function episodePosition(episodes: EpisodeLedger, episodeId: number): number | null {
  const index = episodes.findIndex((entry) => entry.episode === episodeId);
  return index < 0 ? null : index + 1;
}

/** The episode name creators see: the title, or the order position when untitled. / 创作者看到的集名：标题，没有标题时是播出位置。 */
export function episodeDisplayName(episodes: EpisodeLedger, episodeId: number, t: TFunction): string {
  const title = episodes.find((entry) => entry.episode === episodeId)?.title?.trim();
  if (title) return title;
  const position = episodePosition(episodes, episodeId);
  return position === null
    ? t("common:episode_unlisted_name")
    : t("common:episode_position_name", { position });
}

/** Episode reference sent to the Agent: episode name and episode ID together; the Agent uses only the ID for tool calls and the name when speaking to the creator. / 发给 Agent 的集指称：集名与集 ID 一起给，Agent 调工具只用集 ID，对创作者复述用集名。 */
export function episodeAgentRef(episodes: EpisodeLedger, episodeId: number, t: TFunction): string {
  return t("common:episode_agent_ref", { name: episodeDisplayName(episodes, episodeId, t), id: episodeId });
}

/** The in-episode part of an entry ID: E3S01 -> S01; returned unchanged without an episode ID prefix. Only for entries that aren't shots or video units (grids). / 条目 ID 的集内部分：E3S01 → S01；没有集 ID 前缀时原样返回。只用于不是分镜或视频单元的条目（宫格）。 */
export function itemIdWithinEpisode(itemId: string): string {
  return itemId.replace(ITEM_ID_EPISODE_PREFIX, "");
}

/** An entry's type name plus position: "Shot 5", "Video unit 5"; "Deleted" when the entry is no longer in the script. / 条目的类型名加位置：「分镜 5」「视频单元 5」；条目已不在剧本里时为「已删除」。 */
export function itemPositionLabel(kind: EpisodeItemKind, position: number | null, t: TFunction): string {
  return position === null ? t("common:deleted") : t(`common:item_position_${kind}`, { position });
}

/**
 * Refer to an entry outside the episode page: "title · 5", or "title · Deleted" when the entry is no longer in the script; episodes removed from the ledger use the unnamed-episode name.
 * Entries such as grids that aren't referred to by position keep "title · G01".
 *
 * 集页面之外指称条目：「标题 · 5」，条目已不在剧本里时「标题 · 已删除」；已移出账本的集使用未命名集。
 * 宫格这类不按位置指称的条目保持「标题 · G01」。
 */
export function episodeItemLabel(
  itemId: string,
  scripts: ProjectScripts,
  episodes: EpisodeLedger,
  t: TFunction,
): string {
  const match = ITEM_ID_EPISODE_PREFIX.exec(itemId);
  const episodeId = match ? Number(match[1]) : null;
  if (episodeId === null) return itemId;
  const within = isPositionedItemId(itemId)
    ? String(findEpisodeItem(scripts, itemId)?.position ?? t("common:deleted"))
    : itemIdWithinEpisode(itemId);
  return `${episodeDisplayName(episodes, episodeId, t)} · ${within}`;
}

/**
 * Entry reference sent to the Agent: position and entry ID together; the Agent uses the ID for tool calls and the position when speaking to the creator.
 * With non-empty `episodes`, include the episode name (outside the episode page); otherwise write just "Shot 5 (ID E3S05)".
 *
 * 发给 Agent 的条目指称：位置与条目 ID 一起给，Agent 调工具用 ID，对创作者复述用位置。
 * `episodes` 非空时带上集名（集页面之外），否则只写「分镜 5（ID E3S05）」。
 */
export function episodeItemAgentRef(
  itemId: string,
  scripts: ProjectScripts,
  t: TFunction,
  episodes?: EpisodeLedger,
): string {
  const item = findEpisodeItem(scripts, itemId);
  const label = itemPositionLabel(episodeItemKind(itemId), item?.position ?? null, t);
  const match = ITEM_ID_EPISODE_PREFIX.exec(itemId);
  const named = episodes && match ? `${episodeDisplayName(episodes, Number(match[1]), t)} · ${label}` : label;
  return t("common:item_agent_ref", { label: named, id: itemId });
}

/**
 * Join the entry reference attached by the backend into "title · 5", or "title · Deleted" when the backend judges the entry no longer in the script.
 * Without a reference (project unreadable, episode removed from the ledger) the episode ID is still hidden.
 *
 * 后端附上的条目指称拼成「标题 · 5」，后端判定条目已不在剧本里时「标题 · 已删除」。
 * 缺少指称时（项目读不到、集已移出账本）仍隐藏集 ID。
 */
export function episodeItemRefLabel(
  itemId: string,
  ref: EpisodeItemRef | null | undefined,
  t: TFunction,
): string {
  if (!ref) return episodeItemLabel(itemId, {}, [], t);
  const name = ref.episode_title.trim() || t("common:episode_position_name", { position: ref.episode_position });
  if (!isPositionedItemId(itemId)) return `${name} · ${ref.item_id}`;
  return `${name} · ${ref.item_position ?? t("common:deleted")}`;
}

/** Diagnostic raw text on the episode page shows only in-episode entry IDs; locations and the raw text handed to the Agent stay unchanged. / 集页里的诊断原文只显示集内条目 ID，定位与交给 Agent 的原文保持不变。 */
export function itemIdsInEpisodeText(text: string): string {
  return text.replace(/(?<![A-Za-z0-9])E\d+([A-Z](?:\d+|##))/g, "$1");
}

/**
 * Server-written text listing final script entries (e.g. the loss list of an overwrite confirmation): IDs of shots and video units become position names like "Shot 5",
 * those no longer in the script become "Deleted"; other entry IDs only lose the episode prefix. Only for text whose entries are all in the final script;
 * IDs in draft diagnostics refer to draft entries and don't go through here.
 *
 * 服务端成文、列出正式剧本条目的文本（如覆盖确认的丢失清单）：分镜与视频单元的 ID 换成「分镜 5」这类
 * 位置称呼，已不在剧本里的写「已删除」；其余条目 ID 只去掉集前缀。只用于条目都在正式剧本里的文本，
 * 草稿诊断里的 ID 指的是草稿条目，不经这里。
 */
export function itemIdsAsPositionsInText(text: string, scripts: ProjectScripts, t: TFunction): string {
  const positioned = text.replace(/(?<![A-Za-z0-9])E\d+[SU]\d+(?:_\d+)?(?![A-Za-z0-9])/g, (itemId) =>
    itemPositionLabel(episodeItemKind(itemId), findEpisodeItem(scripts, itemId)?.position ?? null, t),
  );
  return itemIdsInEpisodeText(positioned);
}
