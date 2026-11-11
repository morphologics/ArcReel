import { API } from "@/api";
import type { JianyingVersion, SubtitleMode } from "@/types";
import type { TimelineNarration } from "@/types/edit-timeline";
import { trackedDownload } from "@/utils/download";
import { finalCutFileName } from "./render-file-name";

/** The Jianying draft folder and version the Render dialog last used (the draft is built for that folder). */
export const DRAFT_PATH_STORAGE_KEY = "arcreel_jianying_draft_path";
export const JIANYING_VERSION_STORAGE_KEY = "arcreel_jianying_version";

/** Downloads keys: a second download of the same file while one runs is refused. */
export function finalCutDownloadKey(projectName: string, timelineId: string, version: number | string | null): string {
  return `final-cut:${projectName}:${timelineId}:${version ?? ""}`;
}

export function draftDownloadKey(projectName: string, timelineId: string): string {
  return `draft:${projectName}:${timelineId}`;
}

export function storedDraftSettings(): { path: string; version: JianyingVersion } {
  try {
    return {
      path: localStorage.getItem(DRAFT_PATH_STORAGE_KEY)?.trim() ?? "",
      version: localStorage.getItem(JIANYING_VERSION_STORAGE_KEY) === "5" ? "5" : "6",
    };
  } catch {
    return { path: "", version: "6" };
  }
}

type DownloadOutcome = Awaited<ReturnType<typeof trackedDownload>> | "missing";

/** Downloads the current file of a final cut version (outside the Render dialog, e.g. the render queue). */
export async function downloadFinalCut(opts: {
  projectName: string;
  timelineId: string;
  timelineName: string;
  fileStem: string;
  narration: TimelineNarration;
  subtitles: SubtitleMode;
}): Promise<DownloadOutcome> {
  const status = await API.getFinalCutStatus(opts.projectName, opts.timelineId, {
    narration: opts.narration,
    subtitles: opts.subtitles,
  });
  if (!status.download_url || status.version === null) return "missing";
  return trackedDownload({
    url: status.download_url,
    filename: finalCutFileName(opts.fileStem, opts.timelineName, status.narration, status.subtitles, status.rendered_at),
    key: finalCutDownloadKey(opts.projectName, opts.timelineId, status.version),
    mimeType: "video/mp4",
  });
}

/** Downloads a Jianying draft for the folder and version last set in the Render dialog. */
export async function downloadJianyingDraft(opts: {
  projectName: string;
  timelineId: string;
  timelineName: string;
  narration: TimelineNarration;
  draftPath: string;
  jianyingVersion: JianyingVersion;
}): Promise<DownloadOutcome> {
  const { download_token } = await API.requestExportToken(opts.projectName, "current");
  return trackedDownload({
    url: API.getJianyingDraftDownloadUrl(
      opts.projectName,
      opts.timelineId,
      opts.draftPath,
      download_token,
      opts.jianyingVersion,
      opts.narration,
    ),
    filename: `${opts.timelineName || "draft"}.zip`,
    key: draftDownloadKey(opts.projectName, opts.timelineId),
    mimeType: "application/zip",
  });
}
