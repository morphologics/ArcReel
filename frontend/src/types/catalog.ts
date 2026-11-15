import type { MediaType } from "./custom-provider";

/** One entry hit by a model catalog search. `maker`, `name` and `media_type` together identify the entry; the UI shows only the display name. / 模型目录搜索命中的一个条目。`maker`、`name` 与 `media_type` 合起来标识条目，界面只展示显示名。 */
export interface CatalogEntryHit {
  maker: string;
  name: string;
  media_type: MediaType;
  display_name: string;
  discontinued: boolean;
}

export interface CatalogSearchResponse {
  entries: CatalogEntryHit[];
}

/** Hints given with the name when manually entering a new entry: a same-name model already in the catalog (if present it should be chosen directly), and for video models the selectable durations prefilled from the name preset. / 手动填写新条目时随名字给出的提示：目录里已有的同名模型（有就应直接选择），视频模型按名字预设预填的可选时长。 */
export interface NewEntryHints {
  existing: CatalogEntryHit | null;
  durations: number[] | null;
}
