import { useEffect } from "react";

import { API } from "@/api";

import { pickDefaultTimeline } from "./timeline-view";

/** Neighbour streams already in this browser's cache this session (piece URLs carry their content key). */
const prefetched = new Set<string>();

/**
 * Once the shown episode's stream is fully loaded (`ready`), fetch the neighbouring episodes' stream pieces into the
 * browser cache, one at a time at low priority: switching to the next or previous episode then starts at once. Asking
 * also makes the server build a neighbour's missing pieces (they are skipped here until a later visit). Episodes in
 * the given order (next first); stopped when the view changes.
 */
export function useNeighbourStreamPrefetch(projectName: string, episodes: readonly number[], ready: boolean): void {
  const key = episodes.join(",");
  useEffect(() => {
    if (!ready || key === "") return;
    const controller = new AbortController();
    const { signal } = controller;
    const run = async () => {
      for (const episode of key.split(",").map(Number)) {
        if (signal.aborted) return;
        try {
          const { timelines } = await API.listEditTimelines(projectName, episode, { signal });
          const timelineId = pickDefaultTimeline(timelines);
          if (!timelineId) continue;
          const stream = await API.getEditTimelineStream(projectName, timelineId, { signal, prefetch: true });
          if (!stream.available || !stream.ready) continue;
          for (const piece of stream.pieces) {
            if (signal.aborted) return;
            if (!piece.path || prefetched.has(piece.key)) continue;
            const response = await fetch(API.getFileUrl(projectName, piece.path, piece.key), {
              credentials: "same-origin",
              signal,
              priority: "low",
            });
            await response.arrayBuffer();
            if (response.ok) prefetched.add(piece.key);
          }
        } catch {
          if (signal.aborted) return;
          // one neighbour failing doesn't stop the other; it simply loads when opened
        }
      }
    };
    void run();
    return () => controller.abort();
  }, [projectName, key, ready]);
}
