import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { EditTimelineStream } from "@/types/edit-timeline";
import { useTimelineStream } from "./useTimelineStream";

function answer(fields: Partial<EditTimelineStream> = {}): EditTimelineStream {
  return {
    available: true,
    reason: null,
    timeline_id: "tl-1",
    revision: 3,
    duration: 7,
    codecs: 'video/mp4; codecs="avc1.640028, mp4a.40.2"',
    ready: true,
    pieces: [
      { index: 0, start: 0, duration: 4, path: "renders/episode_1/tl-1/stream/a.mp4", key: "a" },
      { index: 1, start: 4, duration: 3, path: "renders/episode_1/tl-1/stream/b.mp4", key: "b" },
    ],
    ...fields,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("MediaSource", { isTypeSupported: () => true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useTimelineStream", () => {
  it("asks again while pieces are being prepared and returns the stream once all are ready", async () => {
    const ask = vi
      .spyOn(API, "getEditTimelineStream")
      .mockResolvedValueOnce(answer({ ready: false, pieces: [{ index: 0, start: 0, duration: 7, path: null, key: "a" }] }))
      .mockResolvedValueOnce(answer());
    const { result } = renderHook(() => useTimelineStream("demo", "tl-1", 3));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(ask).toHaveBeenCalledTimes(2);
    expect(result.current?.id).toBe("3:a,b");
    expect(result.current?.duration).toBe(7);
    expect(result.current?.pieces.map((piece) => [piece.start, piece.duration])).toEqual([
      [0, 4],
      [4, 3],
    ]);
    expect(result.current?.pieces[1].url).toContain("renders/episode_1/tl-1/stream/b.mp4");
  });

  it("shows nothing for an answer about another revision, or when the timeline can't be rendered", async () => {
    vi.spyOn(API, "getEditTimelineStream").mockResolvedValue(answer({ revision: 4 }));
    const { result } = renderHook(() => useTimelineStream("demo", "tl-1", 3));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(result.current).toBeNull();

    vi.spyOn(API, "getEditTimelineStream").mockResolvedValue(answer({ available: false, reason: "final_cut_empty" }));
    const blocked = renderHook(() => useTimelineStream("demo", "tl-1", 3));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(blocked.result.current).toBeNull();
  });

  it("drops the stream at once when the plan changes and asks for the new one", async () => {
    const ask = vi.spyOn(API, "getEditTimelineStream").mockResolvedValue(answer());
    const { result, rerender } = renderHook(({ plan }) => useTimelineStream("demo", "tl-1", 3, plan), {
      initialProps: { plan: "plan-1" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current).not.toBeNull();

    ask.mockResolvedValue(answer({ pieces: [{ index: 0, start: 0, duration: 7, path: "renders/x/c.mp4", key: "c" }] }));
    rerender({ plan: "plan-2" });
    expect(result.current).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(ask).toHaveBeenCalledTimes(2);
    expect(result.current?.id).toBe("3:c");
  });

  it("does nothing without a revision to show", () => {
    const ask = vi.spyOn(API, "getEditTimelineStream");
    const { result } = renderHook(() => useTimelineStream("demo", "tl-1", null));
    expect(ask).not.toHaveBeenCalled();
    expect(result.current).toBeNull();
  });
});
