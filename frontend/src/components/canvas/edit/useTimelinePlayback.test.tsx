import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PlaybackPlan, PlaybackSegment } from "./playback-schedule";
import { attachStream, bufferedAhead, type ReadyStream } from "./stream-source";
import { useTimelinePlayback } from "./useTimelinePlayback";

vi.mock("./stream-source", () => ({ attachStream: vi.fn(), bufferedAhead: vi.fn(), streamSupported: () => true }));

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

describe("useTimelinePlayback gapless stream", () => {
  const stream: ReadyStream = {
    id: "3:a,b",
    codecs: 'video/mp4; codecs="avc1.640028, mp4a.40.2"',
    duration: 20,
    pieces: [
      { start: 0, duration: 10, url: "/a.mp4" },
      { start: 10, duration: 10, url: "/b.mp4" },
    ],
  };

  function setup(initial: ReadyStream | null = stream) {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 206 }));
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    const dispose = vi.fn();
    const focus = vi.fn();
    vi.mocked(attachStream).mockReturnValue({ ready: Promise.resolve(), dispose, focus });
    vi.mocked(bufferedAhead).mockReturnValue(100);   // everything loaded unless a test says otherwise
    const video = document.createElement("video");
    const hook = renderHook(
      ({ current, s }: { current: PlaybackPlan; s: ReadyStream | null }) =>
        useTimelinePlayback(current, urlOf, [], () => null, s),
      { initialProps: { current: plan, s: null as ReadyStream | null } },
    );
    (hook.result.current.streamElement as { current: HTMLVideoElement | null }).current = video;
    hook.rerender({ current: plan, s: initial });
    return { ...hook, video, dispose, focus };
  }

  it("takes over at once while paused, and seeks move the stream", async () => {
    const { result, video } = setup();
    expect(attachStream).toHaveBeenCalledWith(video, stream, expect.objectContaining({ startAt: 0 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.streamActive).toBe(true);

    act(() => result.current.seek(12.5));
    expect(video.currentTime).toBe(12.5);
    expect(result.current.t).toBe(12.5);
  });

  it("takes over once the frame at the playhead is loaded, and a seek points the loader at the new spot", async () => {
    vi.mocked(bufferedAhead).mockReturnValue(0);
    const { result, focus } = setup();
    vi.mocked(bufferedAhead).mockReturnValue(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.streamActive).toBe(false);   // nothing buffered at 0 yet

    const options = vi.mocked(attachStream).mock.calls[0][2];
    vi.mocked(bufferedAhead).mockReturnValue(2);
    act(() => options?.onProgress?.());
    expect(result.current.streamActive).toBe(true);   // paused: the first piece is enough

    vi.mocked(bufferedAhead).mockReturnValue(0);
    act(() => result.current.seek(15));
    expect(result.current.streamActive).toBe(true);   // stays on the stream: its piece at 15 s comes next
    expect(focus).toHaveBeenCalledWith(15);
    expect(result.current.t).toBe(15);
  });

  it("is not attached when its length doesn't match the plan (another revision's stream)", () => {
    setup({ ...stream, duration: 18 });
    expect(attachStream).not.toHaveBeenCalled();
  });

  it("goes back to the clip elements when the plan changes, and releases the stream when it goes away", async () => {
    const { result, rerender, dispose } = setup();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.streamActive).toBe(true);

    const edited: PlaybackPlan = { ...plan, segments: [...plan.segments] };
    rerender({ current: edited, s: stream });
    expect(result.current.streamActive).toBe(false);
    rerender({ current: edited, s: null });
    expect(dispose).toHaveBeenCalled();
  });

  it("while playing, hands over at the next cut instead of jumping mid-shot", async () => {
    const { result, rerender, video } = setup(null);
    act(() => result.current.play());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    rerender({ current: plan, s: stream });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.streamActive).toBe(false);   // still in shot c1

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3500);
    });
    expect(result.current.streamActive).toBe(true);   // at the cut to c2 (5 s)
    expect(video.currentTime).toBeGreaterThanOrEqual(5);
    expect(video.currentTime).toBeLessThan(5.2);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });

  it("doesn't prefetch clip starts while a stream is there", async () => {
    setup();
    const fetchMock = vi.mocked(globalThis.fetch);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
