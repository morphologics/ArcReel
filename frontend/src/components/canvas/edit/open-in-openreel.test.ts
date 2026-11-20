import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { STUDIO_TAB, exposeStudioNavigate, openEpisodeInOpenReel, resetOpenReelProbeForTests, useOpenReelHref } from "./open-in-openreel";

type NavigateWindow = Window & { youreelNavigate?: (url: string) => boolean };

describe("exposeStudioNavigate", () => {
  let undo: (() => void) | null = null;
  afterEach(() => {
    undo?.();
    undo = null;
    window.name = "";
    window.history.replaceState(null, "", "/");
  });

  it("names the tab and moves it in-app to a studio address", () => {
    undo = exposeStudioNavigate();
    expect(window.name).toBe(STUDIO_TAB);
    const go = (window as NavigateWindow).youreelNavigate;
    expect(go?.(`${window.location.origin}/app/projects/demo/episodes/3?shot=E3S2`)).toBe(true);
    expect(window.location.pathname).toBe("/app/projects/demo/episodes/3");
    expect(window.location.search).toBe("?shot=E3S2");
  });

  it("keeps a tab name that is already set", () => {
    window.name = "something-else";
    undo = exposeStudioNavigate();
    expect(window.name).toBe("something-else");
  });

  it("refuses other origins and paths outside the app", () => {
    undo = exposeStudioNavigate();
    const go = (window as NavigateWindow).youreelNavigate;
    expect(go?.("https://example.com/app/projects/x")).toBe(false);
    expect(go?.(`${window.location.origin}/api/v1/projects`)).toBe(false);
    expect(window.location.pathname).toBe("/");
  });

  it("is removed by its undo", () => {
    undo = exposeStudioNavigate();
    undo();
    undo = null;
    expect((window as NavigateWindow).youreelNavigate).toBeUndefined();
  });
});

describe("useOpenReelHref", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetOpenReelProbeForTests();
  });

  it("carries the served build as ?v= so a cached older page can't be shown", async () => {
    const page = '<!doctype html><title>OpenReel</title><script type="module" src="/reel/assets/index-BaY9ACxq.js"></script>';
    const fetchMock = vi.fn(async () => new Response(page, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useOpenReelHref("demo", "tl-1"));
    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current).toMatch(/^\/reel\/index\.html\?v=BaY9ACxq#\/arcreel\?pack=/);
    expect(fetchMock).toHaveBeenCalledWith("/reel/index.html", expect.objectContaining({ cache: "no-cache" }));
  });

  it("recognises the editor by its name, YouReel Editor, without the word OpenReel", async () => {
    const page = '<!doctype html><title>YouReel Editor</title><script type="module" src="/reel/assets/index-Xy12AbCd.js"></script>';
    vi.stubGlobal("fetch", vi.fn(async () => new Response(page, { status: 200 })));
    const { result } = renderHook(() => useOpenReelHref("demo", "tl-1"));
    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current).toMatch(/^\/reel\/index\.html\?v=Xy12AbCd#/);
  });

  it("offers nothing where the studio doesn't serve OpenReel", async () => {
    const fetchMock = vi.fn(async () => new Response("<!doctype html><title>YouReel Studio</title>", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useOpenReelHref("demo", "tl-1"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(result.current).toBeNull();
  });
});

describe("openEpisodeInOpenReel without a timeline (YouReel)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    resetOpenReelProbeForTests();
  });

  function stubTab() {
    const tab = { close: vi.fn(), focus: vi.fn(), location: { href: "" } };
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<title>YouReel Editor</title>", { status: 200 })));
    vi.spyOn(API, "listEditTimelines").mockResolvedValue({ timelines: [] } as unknown as Awaited<ReturnType<typeof API.listEditTimelines>>);
    return tab;
  }

  it("creates one first, then opens it", async () => {
    const tab = stubTab();
    const createTimeline = vi.fn(async () => "tl-new");
    openEpisodeInOpenReel("demo", 3, { createTimeline });
    await waitFor(() => expect(tab.location.href).toContain("#/arcreel?pack="));
    expect(createTimeline).toHaveBeenCalledOnce();
    expect(decodeURIComponent(tab.location.href)).toContain("/edit-timelines/tl-new/");
    expect(tab.close).not.toHaveBeenCalled();
  });

  it("closes the tab and reports the error when none can be made", async () => {
    const tab = stubTab();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const onError = vi.fn();
    const onNoTimeline = vi.fn();
    const failure = new Error("422");
    openEpisodeInOpenReel("demo", 3, { createTimeline: async () => { throw failure; }, onError, onNoTimeline });
    await waitFor(() => expect(onError).toHaveBeenCalledWith(failure, true));
    expect(tab.close).toHaveBeenCalledOnce();
    expect(onNoTimeline).not.toHaveBeenCalled();
    expect(consoleError).toHaveBeenCalled();
  });
});
