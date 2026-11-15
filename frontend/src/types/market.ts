/** Refresh status of a market source. / 市场源的刷新状态。 */
export type MarketSourceStatus =
  | "never_fetched"
  | "ok"
  | "unreachable"
  | "invalid_index"
  | "unsupported_schema";

export type MarketSourceKind = "official" | "custom";

/** Top-level info of the snapshot index. / 快照索引的顶层信息。 */
export interface MarketIndexSummary {
  name: string;
  description: string | null;
  homepage: string | null;
}

export interface MarketSourceInfo {
  id: number;
  kind: MarketSourceKind;
  display_name: string;
  address: string;
  index_url: string;
  canonical_key: string;
  is_enabled: boolean;
  position: number;
  status: MarketSourceStatus;
  last_error: string | null;
  /** Time of the most recent successful refresh (including 304); null if never successful. / 最近一次成功刷新（含 304）的时间；从未成功时为 null。 */
  fetched_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  entry_count: number;
  /** Null if never fetched successfully. / 从未成功抓取时为 null。 */
  index: MarketIndexSummary | null;
}

export interface MarketSourceListResponse {
  sources: MarketSourceInfo[];
}

/** One item in the merged entry list: index entry fields plus its source. / 合并条目列表里的一条：索引条目字段加所在源。 */
export interface MarketEntry {
  source_id: number;
  source_display_name: string;
  type: string;
  slug: string;
  path: string;
  name: string;
  author: string;
  version: string;
  media_type: string;
  description: string | null;
  homepage: string | null;
  /** Relative icon path in the index; when non-null the image is fetched through the icon proxy. / 索引里的 icon 相对路径；非 null 时经 icon 代理取图。 */
  icon: string | null;
  min_app_version: string | null;
  /** True when there is no version requirement or the app version cannot be read. / 无版本要求或读不到应用版本时为 true。 */
  min_app_version_satisfied: boolean;
  installation: MarketEntryInstallation | null;
}

export interface MarketEntryListResponse {
  entries: MarketEntry[];
  /** Current app version; null when unreadable. / 当前应用版本；读不到时为 null。 */
  app_version: string | null;
}

/**
 * Market axis of an installed endpoint: updatable when the installed record version string differs from the index entry version; unavailable when the source is disabled, deleted or the entry was removed
 * from the index. Independent of the local modification axis `modified`.
 *
 * 已安装端点的市场轴：安装记录版本与索引条目版本字符串不等即可更新；来源被禁用、被删除或条目已从索引
 * 移除即不可用。与本地修改轴 `modified` 互相独立。
 */
export type MarketInstallationState = "current" | "update_available" | "unavailable";

export interface MarketEntryInstallation {
  endpoint_id: number;
  endpoint_key: string;
  endpoint_display_name: string;
  installed_version: string;
  /** The entry is right there in the market list, so it cannot be unavailable. / 条目就在市场列表里，不会是 unavailable。 */
  state: Exclude<MarketInstallationState, "unavailable">;
  /** The current definition digest differs from the one at install time. / 当前定义摘要与安装时不同。 */
  modified: boolean;
}

export interface EndpointInstallation {
  source_key: string;
  source_id: number | null;
  source_display_name: string | null;
  /** Null when the source was deleted. / 来源已删除时为 null。 */
  source_enabled: boolean | null;
  slug: string;
  installed_version: string;
  installed_at: string;
  state: MarketInstallationState;
  /** The current definition digest differs from the one at install time. / 当前定义摘要与安装时不同。 */
  modified: boolean;
}

export interface MarketEntryDetail {
  entry: MarketEntry;
  source: Pick<MarketSourceInfo, "id" | "kind" | "display_name" | "canonical_key" | "is_enabled" | "status" | "fetched_at" | "index">;
  app_version: string | null;
}

/** Toggle state of the official service; when `enabled` is false the market page shows no official service elements. / 官方服务的开关状态；`enabled` 为 false 时市场页不展示任何官方服务元素。 */
export interface OfficialServiceState {
  /** Official service address is non-empty. / 官方服务地址非空。 */
  available: boolean;
  /** Address non-empty and the master switch on. / 地址非空且总开关为开。 */
  enabled: boolean;
  /** The market page's first-time notice has been acknowledged. / 市场页的首次说明已确认。 */
  notice_seen: boolean;
  /** Instance identifier sent with requests to the official service; null when not yet generated. / 随请求发往官方服务的实例标识；尚未生成时为 null。 */
  instance_id: string | null;
}

/** Install count and rating of an official market source entry on the official service. / 官方市场源条目在官方服务的安装量与评分。 */
export interface MarketEntryAggregate {
  source_id: number;
  slug: string;
  /** Display value rounded by the official service, shown as is. / 官方服务取整后的展示值，原样展示。 */
  installs: number;
  rating_count: number;
  /** Null when the number of raters is below the official service's threshold; only the count is shown. / 评分人数不足官方服务的阈值时为 null，只展示人数。 */
  rating_average: number | null;
}

/** Status of a share submission: in review / accepted / rejected. / 分享提交的状态：审核中 / 已采纳 / 已拒绝。 */
export type MarketSubmissionStatus = "open" | "merged" | "closed";

/** The most recent submission sharing an endpoint to the official market. / 一个端点最近一次分享到官方市场的提交。 */
export interface MarketSubmission {
  endpoint_id: number;
  endpoint_key: string;
  endpoint_display_name: string;
  type: string;
  slug: string;
  status: MarketSubmissionStatus;
  pr_url: string;
  /** Could not fetch the status from the official service this time; showing the last saved value. / 本次没能从官方服务取回状态，展示的是上次保存的值。 */
  stale: boolean;
}

/** One validation diagnostic of a share submission; `file` is relative to the entry directory, empty string when the slug itself is invalid. / 分享提交的一条校验诊断；`file` 相对条目目录，slug 本身不合规时为空串。 */
export interface MarketSubmissionDiagnostic {
  file: string;
  path: string;
  code: string;
  message: string;
}

export interface MarketSubmissionIcon {
  filename: "icon.png" | "icon.webp" | "icon.svg";
  /** Standard base64. / 标准 base64。 */
  content: string;
}

export interface MarketSubmissionDraft {
  endpoint_id: number;
  slug: string;
  icon: MarketSubmissionIcon | null;
}
