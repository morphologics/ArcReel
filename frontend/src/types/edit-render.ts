/**
 * Types related to rendering from the edit timeline: current state of final cut and JianYing draft artifacts, and submit results.
 *
 * Corresponds to the response shapes of backend server/routers/edit_timelines.py.
 *
 * 剪辑时间线出片相关的类型：成片与剪映草稿的产物现状、提交结果。
 *
 * 对应后端 server/routers/edit_timelines.py 的响应形态。
 */

import type { EditTimelineIssue, TimelineNarration } from "./edit-timeline";

/** Freshness of an artifact relative to the edit timeline; stale can still be downloaded. / 产物相对于剪辑时间线的时效；stale 仍可下载。 */
export type RenderArtifactStatus = "current" | "stale" | "missing" | "blocked";

/** The two deliverables of a render. / 出片的两种交付物。 */
export type RenderKind = "final_cut" | "jianying_draft";

/** Whether the final cut burns subtitles into the picture. / 成片是否把字幕烧入画面。 */
export type SubtitleMode = "burned_subtitles" | "no_subtitles";

/** Render options: the narration version applies to both deliverables; burned-in subtitles only to the final cut. / 出片选项：旁白版本两种交付物都有，烧入字幕只有成片。 */
export interface RenderOptions {
  narration: TimelineNarration;
  subtitles: SubtitleMode;
}

/** JianYing version: 6 means 6 and above, 5 means 5.x. / 剪映版本：6 表示 6 及以上，5 表示 5.x。 */
export type JianyingVersion = "5" | "6";

/** The render entry only cares about an issue's level, scope and location; the full shape is in the edit view's readout. / 出片入口只关心 issue 的级别、影响范围与定位；完整形态见剪辑视图的读取结果。 */
export type EditTimelineIssueRef = Pick<EditTimelineIssue, "code" | "severity" | "applies_to" | "unit_id">;

interface RenderArtifactStatusBase {
  episode: number;
  timeline_id: string;
  status: RenderArtifactStatus;
  artifact_path: string;
  version: number | null;
  rendered_at: string | null;
}

export interface FinalCutStatus extends RenderArtifactStatusBase {
  narration: TimelineNarration;
  subtitles: SubtitleMode;
  /** Given only when the file exists; goes through the anonymous media route and can be downloaded directly. / 只在文件存在时给出；走匿名媒体路由，可直接下载。 */
  download_url: string | null;
}

export interface JianyingDraftStatus extends RenderArtifactStatusBase {
  narration: TimelineNarration;
}

export interface RenderSubmission {
  task_id: string;
  deduped: boolean;
  artifact_path: string;
}
