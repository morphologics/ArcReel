import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StudioWakingOverlay } from "@/components/shared/StudioWakingOverlay";
import {
  STUDIO_WAKING_GIVE_UP_MS,
  STUDIO_WAKING_RETRY_MS,
  isStudioWakingResponse,
  noteStudioWaking,
  reloadIfNewerBuild,
  useStudioWakingStore,
  waitForStudio,
} from "./studio-waking";
import { requestJson } from "./transport";

const waking = () =>
  new Response(JSON.stringify({ detail: "Your studio is starting. Try again in a few seconds." }), {
    status: 503,
    headers: { "X-Studio-Waking": "1", "Content-Type": "application/json" },
  });
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

describe("studio waking", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useStudioWakingStore.setState({ waiting: 0, html: null });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("tells the host's wake answer from the backend's own 503", () => {
    expect(isStudioWakingResponse(waking())).toBe(true);
    expect(isStudioWakingResponse(new Response("", { status: 503 }))).toBe(false);
    expect(isStudioWakingResponse(ok({}))).toBe(false);
  });

  it("a request waits for the woken backend and gets its real answer, with the restarting screen meanwhile", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(waking()).mockResolvedValueOnce(waking()).mockResolvedValueOnce(ok({ projects: [1, 2] }));
    vi.stubGlobal("fetch", fetchMock);
    const result = requestJson<{ projects: number[] }>("/projects");
    await vi.advanceTimersByTimeAsync(0);
    expect(useStudioWakingStore.getState().waiting).toBe(1);
    await vi.advanceTimersByTimeAsync(STUDIO_WAKING_RETRY_MS * 2);
    await expect(result).resolves.toEqual({ projects: [1, 2] });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(useStudioWakingStore.getState().waiting).toBe(0);
  });

  it("passes on the backend's own error once it answers", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(waking()).mockResolvedValueOnce(new Response(JSON.stringify({ detail: "nope" }), { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    const caught = requestJson("/projects/x").then(() => null, (error: unknown) => error);
    await vi.advanceTimersByTimeAsync(STUDIO_WAKING_RETRY_MS);
    expect(await caught).toBeInstanceOf(Error);
    expect(((await caught) as Error).message).toBe("nope");
    expect(useStudioWakingStore.getState().waiting).toBe(0);
  });

  it("stops waiting when the request is cancelled", async () => {
    const controller = new AbortController();
    const caught = waitForStudio(waking(), vi.fn().mockResolvedValue(waking()), controller.signal).then(() => null, (error: unknown) => error);
    await vi.advanceTimersByTimeAsync(STUDIO_WAKING_RETRY_MS);
    controller.abort();
    expect(await caught).toBeInstanceOf(Error);
    expect(useStudioWakingStore.getState().waiting).toBe(0);
  });

  it("gives up after a while and returns the last wake answer", async () => {
    const retry = vi.fn().mockResolvedValue(waking());
    const result = waitForStudio(waking(), retry);
    await vi.advanceTimersByTimeAsync(STUDIO_WAKING_GIVE_UP_MS + STUDIO_WAKING_RETRY_MS);
    const last = await result;
    expect(isStudioWakingResponse(last)).toBe(true);
    expect(useStudioWakingStore.getState().waiting).toBe(0);
  });

  it("a call that can't be repeated still shows the screen until the backend answers", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(waking()).mockResolvedValueOnce(ok({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);
    noteStudioWaking(waking());
    expect(useStudioWakingStore.getState().waiting).toBe(1);
    await vi.advanceTimersByTimeAsync(STUDIO_WAKING_RETRY_MS * 2);
    expect(fetchMock).toHaveBeenCalledWith("/health", expect.anything());
    expect(useStudioWakingStore.getState().waiting).toBe(0);
  });

  it("keeps the host's waiting card from a wake answer and shows it in a sandboxed frame", async () => {
    const card = "<!doctype html><title>Waking</title><main>Waking your studio</main>";
    const answer = () =>
      new Response(JSON.stringify({ detail: "starting", html: card }), { status: 503, headers: { "X-Studio-Waking": "1" } });
    const result = waitForStudio(answer(), vi.fn().mockResolvedValue(ok({})));
    await vi.advanceTimersByTimeAsync(STUDIO_WAKING_RETRY_MS);
    await result;
    expect(useStudioWakingStore.getState().html).toBe(card);
    vi.useRealTimers();
    render(<StudioWakingOverlay />);
    act(() => useStudioWakingStore.setState({ waiting: 1 }));
    const dialog = await screen.findByRole("dialog");
    const frame = dialog.querySelector("iframe");
    expect(frame).toBeInTheDocument();
    expect(frame).toHaveAttribute("sandbox", "");
    expect(frame).toHaveAttribute("srcdoc", card);
  });

  it("the restarting dialog shows only while something waits", async () => {
    vi.useRealTimers();
    render(<StudioWakingOverlay />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => useStudioWakingStore.setState({ waiting: 1 }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
    act(() => useStudioWakingStore.setState({ waiting: 0 }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  describe("after waking: a newer build reloads the page", () => {
    let reload: ReturnType<typeof vi.fn<() => void>>;
    beforeEach(() => {
      vi.useRealTimers();
      reload = vi.fn<() => void>();
      const script = document.createElement("script");
      script.type = "module";
      script.src = "/assets/index-OLD123.js";
      script.id = "main-under-test";
      document.head.appendChild(script);
    });
    afterEach(() => document.getElementById("main-under-test")?.remove());

    it("reloads when the studio now serves another main script", async () => {
      vi.stubGlobal("fetch", vi.fn(async () => new Response('<script type="module" crossorigin src="/assets/index-NEW456.js"></script>')));
      expect(await reloadIfNewerBuild(reload)).toBe(true);
      expect(reload).toHaveBeenCalledTimes(1);
    });

    it("stays when the build is the same", async () => {
      vi.stubGlobal("fetch", vi.fn(async () => new Response('<script type="module" crossorigin src="/assets/index-OLD123.js"></script>')));
      expect(await reloadIfNewerBuild(reload)).toBe(false);
      expect(reload).not.toHaveBeenCalled();
    });

    it("does nothing without a built main script (dev server)", async () => {
      document.getElementById("main-under-test")?.remove();
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      expect(await reloadIfNewerBuild(reload)).toBe(false);
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });
});
