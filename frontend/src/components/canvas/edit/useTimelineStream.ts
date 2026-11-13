import { useEffect, useState, type RefObject } from "react";

import { API } from "@/api";
import type { EditTimelineStream } from "@/types/edit-timeline";

import { streamSupported, type ReadyStream } from "./stream-source";

/** `localStorage.setItem("arcreel.playback.stream", "off")` + reload: the old clip-by-clip playback only (A/B tests). */
const STREAM_SWITCH_KEY = "arcreel.playback.stream";

function streamSwitchedOff(): boolean {
  try {
    return localStorage.getItem(STREAM_SWITCH_KEY) === "off";
  } catch {
    return false;
  }
}

/** How often to ask while the server is still preparing pieces. */
const STREAM_POLL_MS = 2500;

/**
 * The timeline's gapless stream once every piece of the shown `revision` is ready; null before that, when the browser
 * can't play it, or when the timeline can't be rendered (the player keeps its clip-by-clip playback). Asking the
 * server also makes it prepare the missing pieces; after an edit only the touched ones are rebuilt. `planKey` is the
 * player's plan (any string that changes with it): a new shot video changes the plan, not the revision, and the
 * stream is asked for again. `playhead` (seconds) tells the server which missing piece to build first.
 */
export function useTimelineStream(
  projectName: string,
  timelineId: string | null,
  revision: number | null,
  planKey = "",
  playhead?: RefObject<number>,
): ReadyStream | null {
  // the stream found for a (project, timeline, revision, plan); a different request key shows nothing until its own
  // arrives
  const [found, setFound] = useState<{ key: string; stream: ReadyStream } | null>(null);
  const requestKey = `${projectName}|${timelineId ?? ""}|${revision ?? ""}|${planKey}`;

  useEffect(() => {
    if (!timelineId || revision === null || streamSwitchedOff()) return;
    const controller = new AbortController();
    let timer = 0;
    const ask = async () => {
      let stream: EditTimelineStream;
      try {
        stream = await API.getEditTimelineStream(projectName, timelineId, {
          signal: controller.signal,
          at: playhead?.current,
        });
      } catch {
        if (controller.signal.aborted) return;
        timer = window.setTimeout(() => void ask(), STREAM_POLL_MS * 4);   // server busy or restarting: try later
        return;
      }
      if (controller.signal.aborted || !stream.available || !streamSupported(stream.codecs)) return;
      if (stream.revision !== revision) return;   // the shown revision is older or newer: a new effect takes over
      if (!stream.ready || stream.duration === null) {
        timer = window.setTimeout(() => void ask(), STREAM_POLL_MS);
        return;
      }
      setFound({
        key: requestKey,
        stream: {
          id: `${stream.revision}:${stream.pieces.map((piece) => piece.key).join(",")}`,
          codecs: stream.codecs,
          duration: stream.duration,
          pieces: stream.pieces.map((piece) => ({
            start: piece.start,
            duration: piece.duration,
            url: API.getFileUrl(projectName, piece.path ?? "", piece.key),
          })),
        },
      });
    };
    void ask();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [projectName, timelineId, revision, requestKey, playhead]);

  return found?.key === requestKey ? found.stream : null;
}
