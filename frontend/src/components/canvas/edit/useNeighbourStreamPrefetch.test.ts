import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { EditTimelineStream, EditTimelineSummary } from "@/types/edit-timeline";
import { useNeighbourStreamPrefetch } from "./useNeighbourStreamPrefetch";

function stream(fields: Partial<EditTimelineStream>): EditTimelineStream {
  return {
    available: true,
    reason: null,
    timeline_id: "tl",
    revision: 1,
    duration: 8,
    codecs: "",
    ready: true,
    pieces: [],
    ...fields,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useNeighbourStreamPrefetch", () => {
  it("waits for the shown stream, then fetches the neighbours' ready pieces in order; unbuilt ones are skipped", async () => {
    vi.spyOn(API, "listEditTimelines").mockImplementation(async (_project, episode) => ({
      timelines: [{ id: `tl-${episode}`, updated_at: "2026-11-13T00:00:00Z" } as unknown as EditTimelineSummary],
    }));
    const ask = vi.spyOn(API, "getEditTimelineStream").mockImplementation(async (_project, timelineId) =>
      timelineId === "tl-3"
        ? stream({
            pieces: [
              { index: 0, start: 0, duration: 4, path: "renders/episode_3/tl-3/stream/n1.mp4", key: "n1" },
              { index: 1, start: 4, duration: 4, path: "renders/episode_3/tl-3/stream/n2.mp4", key: "n2" },
            ],
          })
        : stream({ ready: false, pieces: [{ index: 0, start: 0, duration: 8, path: null, key: "p1" }] }),
    );
    const fetched: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      fetched.push(String(input));
      return new Response(new Uint8Array(4));
    });

    const { rerender } = renderHook(({ ready }) => useNeighbourStreamPrefetch("demo", [3, 1], ready), {
      initialProps: { ready: false },
    });
    await act(async () => {});
    expect(ask).not.toHaveBeenCalled();

    rerender({ ready: true });
    await vi.waitFor(() => expect(ask).toHaveBeenCalledTimes(2));
    expect(ask.mock.calls.map((call) => call[1])).toEqual(["tl-3", "tl-1"]);
    expect(ask.mock.calls[0][2]).toMatchObject({ prefetch: true });
    expect(fetched).toHaveLength(2);
    expect(fetched[0]).toContain("renders/episode_3/tl-3/stream/n1.mp4");
    expect(fetched[1]).toContain("n2.mp4");
  });
});
