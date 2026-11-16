import { afterEach, describe, expect, it, vi } from "vitest";
import { ACTIVITY_SIGNAL_MIN_MS, noteActivity, resetActivitySignalForTests, startActivitySignal } from "./activity-signal";

describe("activity signal", () => {
  afterEach(() => {
    resetActivitySignalForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sends at most once a minute", () => {
    const send = vi.fn();
    expect(noteActivity(1_000_000, send)).toBe(true);
    expect(noteActivity(1_000_000 + 30_000, send)).toBe(false);
    expect(noteActivity(1_000_000 + ACTIVITY_SIGNAL_MIN_MS, send)).toBe(true);
    expect(send).toHaveBeenCalledTimes(2);
  });

  it("sends nothing from a hidden tab", () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const send = vi.fn();
    expect(noteActivity(1_000_000, send)).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });

  it("a click on the page sends POST /api/v1/activity, and the undo stops listening", () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);
    const stop = startActivitySignal();
    window.dispatchEvent(new Event("pointerdown"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/activity", expect.objectContaining({ method: "POST", keepalive: true }));
    stop();
    resetActivitySignalForTests();
    window.dispatchEvent(new Event("pointerdown"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
