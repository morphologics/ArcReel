import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ClientErrorDetail } from "@/types/client-errors";

import {
  JANK_GAP_MS,
  JANK_REPORTS_PER_PAGE,
  LAG_INTERVAL_MS,
  PERF_SETTLE_MS,
  buildLoadReport,
  lagSamplerRunning,
  resetPerfReporterForTests,
  startPerfReporter,
} from "./perf-reporter";

interface Recorded {
  kind: string;
  message: string;
  detail: ClientErrorDetail;
}

const NAVIGATION = [{ responseStart: 120, domContentLoadedEventEnd: 900, loadEventEnd: 2300, transferSize: 4000 }];
const RESOURCES = [
  { name: "http://localhost:3000/src/main.tsx?t=1&token=secret", initiatorType: "script", startTime: 10, duration: 30, transferSize: 300, decodedBodySize: 1200 },
  { name: "http://localhost:3000/src/a.tsx", initiatorType: "script", startTime: 20, duration: 5, transferSize: 100, decodedBodySize: 200 },
  { name: "https://cdn.example.org/font.woff2?v=2", initiatorType: "css", startTime: 30, duration: 50, transferSize: 5000, decodedBodySize: 5000 },
];

describe("perf reporter", () => {
  let recorded: Recorded[];
  let extraLag: number;
  let visibility: DocumentVisibilityState;
  let resources: typeof RESOURCES;

  const setVisibility = (state: DocumentVisibilityState) => {
    visibility = state;
    document.dispatchEvent(new Event("visibilitychange"));
  };
  /** The main thread "blocks" for `ms`: the next sampler tick fires that much later than due. */
  const block = (ms: number) => {
    extraLag += ms;
    vi.advanceTimersByTime(LAG_INTERVAL_MS);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    recorded = [];
    extraLag = 0;
    visibility = "visible";
    resources = RESOURCES;
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
    startPerfReporter({
      record: (kind, message, detail) => recorded.push({ kind, message, detail }),
      now: () => Date.now() + extraLag,
      entries: (type) => (type === "navigation" ? NAVIGATION : resources),
    });
  });

  afterEach(() => {
    resetPerfReporterForTests();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("sends one perf report per page load, after the settle time", () => {
    vi.advanceTimersByTime(PERF_SETTLE_MS - 1);
    expect(recorded).toEqual([]);
    vi.advanceTimersByTime(1);
    vi.advanceTimersByTime(PERF_SETTLE_MS * 5);
    // a second start in the same page changes nothing
    startPerfReporter();
    vi.advanceTimersByTime(PERF_SETTLE_MS * 2);
    const perf = recorded.filter((r) => r.kind === "perf");
    expect(perf).toHaveLength(1);
    expect(perf[0].message).toBe("Page load 2.3 s · 3 files · 0.0 MB transferred");
    expect(perf[0].detail.metrics).toMatchObject({ ttfb_ms: 120, dcl_ms: 900, load_ms: 2300, res_count: 3, res_transfer_bytes: 5400, res_decoded_bytes: 6400 });
  });

  it("sends the load report at once when the page is left before the settle time", () => {
    vi.advanceTimersByTime(2000);
    window.dispatchEvent(new Event("pagehide"));
    vi.advanceTimersByTime(PERF_SETTLE_MS * 2);
    expect(recorded.filter((r) => r.kind === "perf")).toHaveLength(1);
  });

  it("summarises resources by type and lists the heaviest without query strings", () => {
    const { detail } = buildLoadReport();
    expect(detail.resource_groups).toEqual([
      { type: "script", count: 2, transfer_bytes: 400, decoded_bytes: 1400 },
      { type: "css", count: 1, transfer_bytes: 5000, decoded_bytes: 5000 },
    ]);
    expect(detail.top_resources?.map((r) => r.path)).toEqual(["cdn.example.org/font.woff2", "/src/main.tsx", "/src/a.tsx"]);
    expect(JSON.stringify(detail)).not.toContain("secret");
  });

  it("reports jank once blocked time in the window passes the threshold", () => {
    block(1500);
    expect(recorded.filter((r) => r.kind === "jank")).toEqual([]);
    block(700);
    const jank = recorded.filter((r) => r.kind === "jank");
    expect(jank).toHaveLength(1);
    expect(jank[0].message).toBe("Main thread blocked 2.2 s in 10.0 s");
    expect(jank[0].detail.metrics).toMatchObject({ blocked_ms: 2200, lag_count: 2, lag_max_ms: 1500 });
  });

  it("does not count lag that left the window", () => {
    block(1500);
    vi.advanceTimersByTime(11_000);
    block(1000);
    expect(recorded.filter((r) => r.kind === "jank")).toEqual([]);
  });

  it("keeps jank reports apart and capped per page load", () => {
    block(3000);
    block(3000); // within the gap: not reported
    expect(recorded.filter((r) => r.kind === "jank")).toHaveLength(1);
    for (let i = 0; i < JANK_REPORTS_PER_PAGE + 3; i += 1) {
      vi.advanceTimersByTime(JANK_GAP_MS);
      block(3000);
    }
    expect(recorded.filter((r) => r.kind === "jank")).toHaveLength(JANK_REPORTS_PER_PAGE);
  });

  it("pauses the lag sampler while the tab is hidden", () => {
    expect(lagSamplerRunning()).toBe(true);
    setVisibility("hidden");
    expect(lagSamplerRunning()).toBe(false);
    // a hidden tab's throttled timers would read as lag: nothing is sampled meanwhile
    extraLag += 60_000;
    vi.advanceTimersByTime(60_000);
    setVisibility("visible");
    expect(lagSamplerRunning()).toBe(true);
    vi.advanceTimersByTime(LAG_INTERVAL_MS);
    expect(recorded.filter((r) => r.kind === "jank")).toEqual([]);
  });

  it("never throws when the browser's entries fail", () => {
    resetPerfReporterForTests();
    startPerfReporter({
      record: (kind, message, detail) => recorded.push({ kind, message, detail }),
      entries: () => {
        throw new Error("no entries");
      },
    });
    expect(() => vi.advanceTimersByTime(PERF_SETTLE_MS + LAG_INTERVAL_MS * 4)).not.toThrow();
  });
});
