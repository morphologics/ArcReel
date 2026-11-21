/**
 * YouReel: report the errors a member hits in the studio's UI to the studio, which keeps them in its database
 * (`POST /api/v1/diagnostics/client-errors`) and lists them in Settings → Options → Diagnostics, for us to see what
 * went wrong where (the Edit → Editor path used to fail without a word).
 *
 * Recorded: (a) every error toast (pushToast / pushNotification with tone "error", seen at the app store),
 * (b) uncaught exceptions and unhandled promise rejections (window "error" / "unhandledrejection"),
 * (c) failed API requests from the shared transport: network errors and HTTP 5xx. Not recorded: 4xx answers (401 /
 * 403 / 404 / 409 / 422 … are the backend's own refusals and show as their own error toasts, which (a) records),
 * aborted requests, the host's "studio is waking" answers, and the reporter's own calls (they go out by their own
 * fetch, never through the transport).
 *
 * Identical reports within 10 s are recorded once; at most 50 per page load. They go out in batches (at most 20 per
 * request) 5 s after the first one is queued, and when the page is hidden or left (fetch with `keepalive`;
 * `navigator.sendBeacon` can't carry the Authorization header the API needs). The reporter never throws and never
 * shows anything about its own failures: a report that can't be sent is dropped.
 */

import { API_BASE, setRequestFailureObserver, withAuth, type RequestFailure } from "@/api/transport";
import { useAppStore } from "@/stores/app-store";
import type { ClientErrorDetail, ClientErrorEntry } from "@/types/client-errors";

export const CLIENT_ERRORS_ENDPOINT = "/diagnostics/client-errors";
export const REPORT_DEDUPE_MS = 10_000;
export const REPORT_FLUSH_MS = 5_000;
export const REPORTS_PER_PAGE_LOAD = 50;
export const REPORTS_PER_REQUEST = 20;
const MESSAGE_MAX = 2000;
const STACK_MAX = 4000;
// Browsers refuse keepalive requests beyond 64 KiB in flight in all; what is left over when the page goes is dropped
const KEEPALIVE_BUDGET_BYTES = 60_000;

export interface ClientErrorBatchPayload {
  entries: ClientErrorEntry[];
  app_build: string | null;
}

export interface ClientErrorReporterOptions {
  /** Sends one batch; the default POSTs it to the studio. Must not throw (a throw is swallowed anyway). */
  send?: (payload: ClientErrorBatchPayload, keepalive: boolean) => void;
  now?: () => number;
}

function sendToStudio(payload: ClientErrorBatchPayload, keepalive: boolean): void {
  try {
    const init = withAuth(CLIENT_ERRORS_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      keepalive,
    });
    void fetch(`${API_BASE}${CLIENT_ERRORS_ENDPOINT}`, init).catch(() => {});
  } catch {
    // nothing to tell: a report that can't go out is dropped
  }
}

/** The build this page runs: the hashed main script ("index-AbC123"); null on the dev server (no built script). */
function appBuild(): string | null {
  const src = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/index-"]')?.getAttribute("src");
  return src ? (/index-[\w-]+(?=\.js)/.exec(src)?.[0] ?? null) : null;
}

export function stripQuery(path: string): string {
  return path.split(/[?#]/, 1)[0];
}

function trim(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

function pagePath(): string {
  return stripQuery(globalThis.location?.pathname ?? "");
}

let started = false;
let stop: (() => void) | null = null;
let send: (payload: ClientErrorBatchPayload, keepalive: boolean) => void = sendToStudio;
let now: () => number = () => Date.now();
let queue: ClientErrorEntry[] = [];
let recorded = 0;
let lastSeen = new Map<string, number>();
let timer: ReturnType<typeof setTimeout> | null = null;

/**
 * Record one error (deduped, capped, batched). Callers with a better label than the automatic hooks (a component or
 * an action) may call it themselves; it does nothing before {@link startClientErrorReporter}. The performance reporter
 * (perf-reporter.ts) sends its `perf` / `jank` records through here too, so they share the batching and the cap.
 */
export function recordClientError(kind: ClientErrorEntry["kind"], message: string, detail: ClientErrorDetail = {}): void {
  try {
    if (!started || recorded >= REPORTS_PER_PAGE_LOAD) return;
    const text = trim(message.trim() || kind, MESSAGE_MAX);
    const key = `${kind}\u0000${text}\u0000${detail.api_path ?? ""}`;
    const at = now();
    const seen = lastSeen.get(key);
    if (seen !== undefined && at - seen < REPORT_DEDUPE_MS) return;
    lastSeen.set(key, at);
    recorded += 1;
    const full: ClientErrorDetail = { page: pagePath(), ...detail };
    if (full.stack) full.stack = trim(full.stack, STACK_MAX);
    queue.push({ kind, message: text, detail: full });
    timer ??= setTimeout(() => flushClientErrors(false), REPORT_FLUSH_MS);
  } catch {
    // never throw from error reporting
  }
}

/** Send what is queued, in batches. With `keepalive` (the page is going) only what fits the browser's budget goes. */
export function flushClientErrors(keepalive: boolean): void {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
  let budget = KEEPALIVE_BUDGET_BYTES;
  const build = appBuild();
  while (queue.length > 0) {
    const payload: ClientErrorBatchPayload = { entries: queue.splice(0, REPORTS_PER_REQUEST), app_build: build };
    if (keepalive) {
      budget -= JSON.stringify(payload).length;
      if (budget < 0) {
        queue = [];
        return;
      }
    }
    try {
      send(payload, keepalive);
    } catch {
      // dropped
    }
  }
}

function onToast(state: ReturnType<typeof useAppStore.getState>, prev: ReturnType<typeof useAppStore.getState>): void {
  const toast = state.toast;
  if (!toast || toast === prev.toast || toast.tone !== "error") return;
  // The listener runs inside pushToast, so this stack also shows which code raised the toast
  const stack = new Error().stack?.split("\n").slice(1).join("\n");
  recordClientError("toast", toast.text, { source: "toast", stack });
}

function errorStack(error: unknown): string | undefined {
  return error instanceof Error && typeof error.stack === "string" ? error.stack : undefined;
}

function onWindowError(event: ErrorEvent): void {
  const where = event.filename ? `${stripQuery(event.filename)}:${event.lineno}:${event.colno}` : undefined;
  recordClientError("exception", event.message || String(event.error), { source: where, stack: errorStack(event.error) });
}

function onUnhandledRejection(event: PromiseRejectionEvent): void {
  const reason: unknown = event.reason;
  if (reason instanceof DOMException && reason.name === "AbortError") return;
  const message = reason instanceof Error ? `${reason.name}: ${reason.message}` : String(reason);
  recordClientError("exception", message, { source: "unhandledrejection", stack: errorStack(reason) });
}

function onRequestFailure(failure: RequestFailure): void {
  const apiPath = stripQuery(failure.path);
  if (apiPath.endsWith(CLIENT_ERRORS_ENDPOINT)) return;
  recordClientError("request", failure.message, { method: failure.method, api_path: apiPath, status: failure.status });
}

function onPageHidden(): void {
  if (document.visibilityState === "hidden") flushClientErrors(true);
}

function onPageHide(): void {
  flushClientErrors(true);
}

/** Start reporting (once per page load; later calls return the same undo). Returns the undo. */
export function startClientErrorReporter(options: ClientErrorReporterOptions = {}): () => void {
  if (started && stop) return stop;
  started = true;
  send = options.send ?? sendToStudio;
  now = options.now ?? (() => Date.now());
  const unsubscribe = useAppStore.subscribe(onToast);
  window.addEventListener("error", onWindowError);
  window.addEventListener("unhandledrejection", onUnhandledRejection);
  window.addEventListener("pagehide", onPageHide);
  document.addEventListener("visibilitychange", onPageHidden);
  setRequestFailureObserver(onRequestFailure);
  stop = () => {
    unsubscribe();
    window.removeEventListener("error", onWindowError);
    window.removeEventListener("unhandledrejection", onUnhandledRejection);
    window.removeEventListener("pagehide", onPageHide);
    document.removeEventListener("visibilitychange", onPageHidden);
    setRequestFailureObserver(null);
    started = false;
    stop = null;
  };
  return stop;
}

export function resetClientErrorReporterForTests(): void {
  stop?.();
  if (timer !== null) clearTimeout(timer);
  timer = null;
  queue = [];
  recorded = 0;
  lastSeen = new Map();
  send = sendToStudio;
  now = () => Date.now();
}
