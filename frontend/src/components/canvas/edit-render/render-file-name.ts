import type { SubtitleMode } from "@/types";
import type { TimelineNarration } from "@/types/edit-timeline";
import { safeFileName } from "@/utils/download";

/** Local time as YYYYMMDD-HHMM; null when the time cannot be read. */
function fileTimestamp(iso: string | null): string | null {
  const date = iso ? new Date(iso) : null;
  if (!date || Number.isNaN(date.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`;
}

/**
 * Download name of a final cut: prefix (project and episode), timeline name, non-default version tags,
 * render time, e.g. "Broken Mirror - E1 - Final Cut - 20261110-0142.mp4". Different versions and
 * renders never share a name.
 */
export function finalCutFileName(
  fileStem: string | undefined,
  timelineName: string,
  narration: TimelineNarration,
  subtitles: SubtitleMode,
  renderedAt: string | null,
): string {
  const parts = [fileStem, timelineName];
  if (narration === "with_narration") parts.push("narration");
  if (subtitles === "no_subtitles") parts.push("no subtitles");
  parts.push(fileTimestamp(renderedAt) ?? undefined);
  return safeFileName(parts.filter((part): part is string => Boolean(part && part.trim())).join(" - "), "mp4", timelineName || "final-cut");
}
