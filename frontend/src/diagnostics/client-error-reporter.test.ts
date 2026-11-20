import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";

import {
  REPORT_DEDUPE_MS,
  REPORT_FLUSH_MS,
  REPORTS_PER_PAGE_LOAD,
  flushClientErrors,
  recordClientError,
  resetClientErrorReporterForTests,
  startClientErrorReporter,
  type ClientErrorBatchPayload,
} from "./client-error-reporter";

function jsonResponse(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("client error reporter", () => {
  let clock = 1_000_000;
  let sent: { payload: ClientErrorBatchPayload; keepalive: boolean }[];

  beforeEach(() => {
    vi.useFakeTimers();
    clock = 1_000_000;
    sent = [];
    startClientErrorReporter({ send: (payload, keepalive) => sent.push({ payload, keepalive }), now: () => clock });
  });

  afterEach(() => {
    resetClientErrorReporterForTests();
    useAppStore.setState({ toast: null });
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const entries = () => sent.flatMap((s) => s.payload.entries);

  it("records error toasts only, and sends them in one batch 5 s after the first", () => {
    useAppStore.getState().pushToast("Could not open the editor", "error");
    useAppStore.getState().pushToast("Saved", "success");
    useAppStore.getState().pushNotification("Render failed", "error");
    expect(sent).toEqual([]);

    vi.advanceTimersByTime(REPORT_FLUSH_MS);
    expect(sent).toHaveLength(1);
    expect(sent[0].keepalive).toBe(false);
    expect(entries().map((e) => [e.kind, e.message])).toEqual([
      ["toast", "Could not open the editor"],
      ["toast", "Render failed"],
    ]);
    expect(entries()[0].detail?.page).toBe(window.location.pathname);
  });

  it("records an identical message once within 10 s, then again", () => {
    recordClientError("exception", "boom");
    clock += REPORT_DEDUPE_MS - 1;
    recordClientError("exception", "boom");
    recordClientError("toast", "boom");
    clock += 1;
    recordClientError("exception", "boom");
    flushClientErrors(false);
    expect(entries().map((e) => e.kind)).toEqual(["exception", "toast", "exception"]);
  });

  it("caps reports per page load and sends at most 20 per request", () => {
    for (let i = 0; i < REPORTS_PER_PAGE_LOAD + 5; i += 1) recordClientError("exception", `e${i}`);
    flushClientErrors(false);
    expect(sent.map((s) => s.payload.entries.length)).toEqual([20, 20, 10]);
  });

  it("sends with keepalive when the page is left", () => {
    recordClientError("exception", "boom");
    window.dispatchEvent(new Event("pagehide"));
    expect(sent).toHaveLength(1);
    expect(sent[0].keepalive).toBe(true);
  });

  it("records uncaught errors and unhandled rejections, not aborts", () => {
    window.dispatchEvent(new ErrorEvent("error", { message: "x is undefined", filename: "http://h/a.js?v=1", lineno: 3, colno: 7 }));
    const rejection = (reason: unknown) => {
      const event = new Event("unhandledrejection") as PromiseRejectionEvent;
      Object.defineProperty(event, "reason", { value: reason });
      window.dispatchEvent(event);
    };
    rejection(new TypeError("bad"));
    rejection(new DOMException("Aborted", "AbortError"));
    flushClientErrors(false);
    expect(entries().map((e) => [e.kind, e.message, e.detail?.source])).toEqual([
      ["exception", "x is undefined", "http://h/a.js:3:7"],
      ["exception", "TypeError: bad", "unhandledrejection"],
    ]);
  });

  it("records network errors and 5xx from the transport, without query strings, but not 4xx", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(500, { detail: "kaboom" }))
      .mockResolvedValueOnce(jsonResponse(404, { detail: "missing" }))
      .mockResolvedValueOnce(jsonResponse(422, { detail: "invalid" }))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(API.request("/projects/demo?token=secret", { method: "POST" })).rejects.toThrow();
    await expect(API.request("/projects/missing")).rejects.toThrow();
    await expect(API.request("/projects/demo/x", { method: "PUT" })).rejects.toThrow();
    await expect(API.request("/projects")).rejects.toThrow("Failed to fetch");
    flushClientErrors(false);

    expect(entries().map((e) => e.detail)).toEqual([
      expect.objectContaining({ method: "POST", api_path: "/api/v1/projects/demo", status: 500 }),
      expect.objectContaining({ method: "GET", api_path: "/api/v1/projects", status: null }),
    ]);
    expect(JSON.stringify(sent)).not.toContain("secret");
  });

  it("does not report failures of its own endpoint, nor aborted requests", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(500))
        .mockRejectedValueOnce(new DOMException("Aborted", "AbortError")),
    );
    await expect(API.listClientErrors()).rejects.toThrow();
    const controller = new AbortController();
    controller.abort();
    await expect(API.request("/projects", { signal: controller.signal })).rejects.toThrow();
    flushClientErrors(false);
    expect(sent).toEqual([]);
  });

  it("never throws when sending fails", () => {
    resetClientErrorReporterForTests();
    startClientErrorReporter({
      send: () => {
        throw new Error("offline");
      },
    });
    recordClientError("exception", "boom");
    expect(() => flushClientErrors(false)).not.toThrow();
  });

  it("posts to the studio by default", () => {
    resetClientErrorReporterForTests();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(200, { accepted: 1 })));
    vi.stubGlobal("fetch", fetchMock);
    startClientErrorReporter();
    recordClientError("exception", "boom");
    flushClientErrors(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/diagnostics/client-errors",
      expect.objectContaining({ method: "POST", keepalive: true }),
    );
    const body = JSON.parse((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body as string) as ClientErrorBatchPayload;
    expect(body.entries[0].message).toBe("boom");
  });
});
