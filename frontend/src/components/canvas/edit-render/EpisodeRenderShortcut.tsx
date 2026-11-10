import { useCallback, useEffect, useState } from "react";
import { API } from "@/api";
import type { EditTimelineIssueRef } from "@/types";
import { RenderButton } from "./RenderButton";

interface LatestTimeline {
  key: string;
  timelineId: string;
  timelineName: string;
  issues: readonly EditTimelineIssueRef[];
}

/**
 * Render shortcut in the episode header for the views other than Edit: renders the episode's most
 * recently changed edit timeline without opening the Edit view first. (The Edit view's own button uses
 * the timeline of its current tab; this one uses `latest` from the episode edit overview.) Hidden while
 * the episode has no edit timeline.
 *
 * The timeline and its issues are re-read on mount, when the window regains focus and when the render
 * dialog closes, so the blocked state does not go stale.
 */
export function EpisodeRenderShortcut({
  projectName,
  episode,
  narrationAvailable,
  fileStem,
  onShowIssues,
}: {
  projectName: string;
  episode: number;
  narrationAvailable: boolean;
  fileStem?: string;
  /** Where "view issues" goes when something blocks rendering: the Edit view. */
  onShowIssues: () => void;
}) {
  const key = `${projectName}::${episode}`;
  const [latest, setLatest] = useState<LatestTimeline | null>(null);
  const [refresh, setRefresh] = useState(0);
  const reload = useCallback(() => setRefresh((n) => n + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      const overview = await API.getEpisodeEditOverview(projectName, episode, { signal: controller.signal });
      if (!overview.latest) {
        setLatest(null);
        return;
      }
      const readout = await API.getEditTimeline(projectName, overview.latest.id, { signal: controller.signal });
      setLatest({
        key,
        timelineId: readout.timeline.id,
        timelineName: readout.timeline.name,
        issues: readout.issues,
      });
    })().catch(() => {
      if (!controller.signal.aborted) setLatest(null);
    });
    return () => controller.abort();
  }, [projectName, episode, key, refresh]);

  useEffect(() => {
    window.addEventListener("focus", reload);
    return () => window.removeEventListener("focus", reload);
  }, [reload]);

  if (latest === null || latest.key !== key) return null;
  return (
    <RenderButton
      projectName={projectName}
      timelineId={latest.timelineId}
      timelineName={latest.timelineName}
      issues={latest.issues}
      narrationAvailable={narrationAvailable}
      onShowIssues={onShowIssues}
      fileStem={fileStem}
      onDialogClose={reload}
    />
  );
}
