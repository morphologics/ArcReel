import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PlaybackPlan, PlaybackSegment } from "./playback-schedule";
import { useTimelinePlayback } from "./useTimelinePlayback";

function segment(clipId: string, start: number, hasVideo = true): PlaybackSegment {
  return {
    clipId,
    unitId: clipId,
    start,
    end: start + 5,
    sourceIn: 0,
    sourceOut: 5,
    sourceVolume: 1,
    videoVersion: 1,
    hasVideo,
    fadeIn: 0,
    fadeOut: 0,
  } as PlaybackSegment;
}

const plan: PlaybackPlan = {
  segments: [segment("c1", 0), segment("c2", 5), segment("c3", 10, false), segment("c4", 15)],
  duration: 20,
};
const urlOf = (s: PlaybackSegment) => `/api/v1/files/p/videos/${s.clipId}.mp4?v=1`;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useTimelinePlayback ahead-of-time loading", () => {
  it("fetches the opening bytes of each clip with video, one at a time, after a short delay", async () => {
    const order: string[] = [];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      order.push(`${String(input)} ${new Headers(init?.headers).get("Range")}`);
      return new Response(new Uint8Array(4), { status: 206 });
    });
    renderHook(() => useTimelinePlayback(plan, urlOf, [], () => null));
    expect(fetchMock).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(order).toEqual([
      "/api/v1/files/p/videos/c1.mp4?v=1 bytes=0-262143",
      "/api/v1/files/p/videos/c2.mp4?v=1 bytes=0-262143",
      "/api/v1/files/p/videos/c4.mp4?v=1 bytes=0-262143",
    ]);
  });

  it("is primed by a seek from the timeline, so the clip there buffers before Play", () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 206 }));
    const { result } = renderHook(() => useTimelinePlayback(plan, urlOf, [], () => null));
    expect(result.current.primed).toBe(false);
    act(() => result.current.seek(12));
    expect(result.current.primed).toBe(true);
  });
});
