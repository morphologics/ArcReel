/**
 * YouReel: performance facts for Settings → Options → Diagnostics, sent through the client error reporter
 * (client-error-reporter.ts: same batching, keepalive send and per-page cap) as two kinds:
 *
 * - `perf`, once per page load, {@link PERF_SETTLE_MS} after the load event (or at once when the page is left before): navigation timings (`ttfb_ms`, `dcl_ms`,
 *   `load_ms`), the page's resources by type (count, transferred and decoded bytes), the 5 heaviest resources (path
 *   without query string, type, sizes, duration), the JS heap where the browser tells it (Chromium's
 *   `performance.memory`), long tasks where supported (Chromium) and the event-loop lag seen so far;
 * - `jank`, when the main thread was blocked for {@link JANK_BLOCKED_MS} or more within {@link JANK_WINDOW_MS}: the
 *   lag and long-task numbers of that window and the resources that started in it (count, bytes, the 3 heaviest). At
 *   most {@link JANK_REPORTS_PER_PAGE} per page load, {@link JANK_GAP_MS} apart.
 *
 * Blocked time is measured two ways and the larger counts: an event-loop lag sampler (a timer every
 * {@link LAG_INTERVAL_MS}: how much later than due it fires; works in every browser, Firefox included), and the
 * `longtask` performance entries where the browser has them. Cost: one timer callback every 250 ms doing a subtraction
 * and a short array prune, nothing per frame; the sampler stops while the tab is hidden (a hidden tab's timers are
 * throttled, which would read as lag) and restarts when it is shown. Reading the resource list happens only when a
 * report is built. Nothing here throws: every step is guarded, and a browser without an API just leaves its numbers
 * out.
 */

import { flushClientErrors, recordClientError, stripQuery } from "@/diagnostics/client-error-reporter";
import type { ClientErrorDetail, PerfResource, PerfResourceGroup } from "@/types/client-errors";

export const PERF_SETTLE_MS = 10_000;
export const LAG_INTERVAL_MS = 250;
/** A tick later than this counts as lag (timer jitter below it is noise) */
export const LAG_MIN_MS = 50;
export const JANK_WINDOW_MS = 10_000;
export const JANK_BLOCKED_MS = 2_000;
export const JANK_REPORTS_PER_PAGE = 5;
export const JANK_GAP_MS = 30_000;
const TOP_RESOURCES = 5;
const JANK_TOP_RESOURCES = 3;
// The browser keeps 250 resource entries by default; the dev server alone loads ~700 modules
const RESOURCE_BUFFER = 5_000;

type PerfKind = "perf" | "jank";

interface ResourceLike {
  name: string;
  initiatorType: string;
  startTime: number;
  duration: number;
  transferSize?: number;
  decodedBodySize?: number;
}

export interface PerfReporterOptions {
  /** Records one report; the default hands it to the client error reporter. */
  record?: (kind: PerfKind, message: string, detail: ClientErrorDetail) => void;
  /** The clock, in the time base of the performance entries (default `performance.now()`). */
  now?: () => number;
  /** The performance entries (default `performance.getEntriesByType`). */
  entries?: (type: "navigation" | "resource") => readonly unknown[];
}

interface Blocked {
  at: number;
  ms: number;
}

let started = false;
let stop: (() => void) | null = null;
let record: NonNullable<PerfReporterOptions["record"]> = recordClientError;
let now: () => number = () => performance.now();
let entries: NonNullable<PerfReporterOptions["entries"]> = (type) => performance.getEntriesByType(type);
let sampler: ReturnType<typeof setTimeout> | null = null;
let settle: ReturnType<typeof setTimeout> | null = null;
let due = 0;
let lagEvents: Blocked[] = [];
let longEvents: Blocked[] = [];
const totals = { lagCount: 0, lagTotal: 0, lagMax: 0, longCount: 0, longTotal: 0 };
let longTasksSupported = false;
let perfSent = false;
let loaded = false;
let jankSent = 0;
let lastJankAt = Number.NEGATIVE_INFINITY;

function round(value: number): number {
  return Math.round(value * 10) / 10;
}

function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

function megabytes(bytes: number): string {
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

/** Same-origin resources by path, others with their host; never a query string. */
function resourcePath(name: string): string {
  try {
    const url = new URL(name, globalThis.location?.href);
    return url.origin === globalThis.location?.origin ? url.pathname : `${url.host}${url.pathname}`;
  } catch {
    return stripQuery(name);
  }
}

function asResources(list: readonly unknown[]): ResourceLike[] {
  return list.filter((entry): entry is ResourceLike => typeof (entry as ResourceLike)?.name === "string");
}

function weight(entry: ResourceLike): number {
  return Math.max(entry.transferSize ?? 0, entry.decodedBodySize ?? 0);
}

function topResources(list: ResourceLike[], count: number): PerfResource[] {
  return [...list]
    .sort((a, b) => weight(b) - weight(a))
    .slice(0, count)
    .map((entry) => ({
      path: resourcePath(entry.name),
      type: entry.initiatorType || "other",
      transfer_bytes: entry.transferSize ?? 0,
      decoded_bytes: entry.decodedBodySize ?? 0,
      duration_ms: round(entry.duration),
    }));
}

function resourceGroups(list: ResourceLike[]): PerfResourceGroup[] {
  const groups = new Map<string, PerfResourceGroup>();
  for (const entry of list) {
    const type = entry.initiatorType || "other";
    const group = groups.get(type) ?? { type, count: 0, transfer_bytes: 0, decoded_bytes: 0 };
    group.count += 1;
    group.transfer_bytes += entry.transferSize ?? 0;
    group.decoded_bytes += entry.decodedBodySize ?? 0;
    groups.set(type, group);
  }
  return [...groups.values()].sort((a, b) => b.count - a.count);
}

function sum(list: ResourceLike[], key: "transferSize" | "decodedBodySize"): number {
  return list.reduce((total, entry) => total + (entry[key] ?? 0), 0);
}

function heapMetrics(): Record<string, number> {
  const memory = (performance as { memory?: { usedJSHeapSize: number; totalJSHeapSize: number; jsHeapSizeLimit: number } })
    .memory;
  if (!memory) return {};
  return {
    heap_used_bytes: memory.usedJSHeapSize,
    heap_total_bytes: memory.totalJSHeapSize,
    heap_limit_bytes: memory.jsHeapSizeLimit,
  };
}

function lagMetrics(): Record<string, number> {
  const metrics: Record<string, number> = {
    lag_count: totals.lagCount,
    lag_total_ms: round(totals.lagTotal),
    lag_max_ms: round(totals.lagMax),
  };
  if (longTasksSupported) {
    metrics.longtask_count = totals.longCount;
    metrics.longtask_total_ms = round(totals.longTotal);
  }
  return metrics;
}

/** The `perf` report of this page load (exported for tests). */
export function buildLoadReport(): { message: string; detail: ClientErrorDetail } {
  const nav = entries("navigation")[0] as Partial<PerformanceNavigationTiming> | undefined;
  const resources = asResources(entries("resource"));
  const transfer = sum(resources, "transferSize");
  const metrics: Record<string, number> = {};
  if (nav) {
    if (nav.responseStart) metrics.ttfb_ms = round(nav.responseStart);
    if (nav.domContentLoadedEventEnd) metrics.dcl_ms = round(nav.domContentLoadedEventEnd);
    if (nav.loadEventEnd) metrics.load_ms = round(nav.loadEventEnd);
    if (nav.transferSize !== undefined) metrics.doc_transfer_bytes = nav.transferSize;
  }
  Object.assign(metrics, {
    res_count: resources.length,
    res_transfer_bytes: transfer,
    res_decoded_bytes: sum(resources, "decodedBodySize"),
    ...heapMetrics(),
    ...lagMetrics(),
  });
  const parts = [`Page load ${metrics.load_ms !== undefined ? seconds(metrics.load_ms) : "?"}`];
  parts.push(`${resources.length} files`, `${megabytes(transfer)} transferred`);
  return {
    message: parts.join(" · "),
    detail: {
      tag: "load",
      metrics,
      resource_groups: resourceGroups(resources),
      top_resources: topResources(resources, TOP_RESOURCES),
    },
  };
}

function sendLoadReport(): void {
  settle = null;
  if (perfSent) return;
  perfSent = true;
  try {
    const report = buildLoadReport();
    record("perf", report.message, report.detail);
  } catch {
    // dropped
  }
}

function inWindow(list: Blocked[], from: number): number {
  return list.reduce((total, event) => (event.at >= from ? total + event.ms : total), 0);
}

function checkJank(at: number): void {
  const from = at - JANK_WINDOW_MS;
  lagEvents = lagEvents.filter((event) => event.at >= from);
  longEvents = longEvents.filter((event) => event.at >= from);
  const lag = inWindow(lagEvents, from);
  const long = inWindow(longEvents, from);
  const blocked = Math.max(lag, long);
  if (blocked < JANK_BLOCKED_MS || jankSent >= JANK_REPORTS_PER_PAGE || at - lastJankAt < JANK_GAP_MS) return;
  jankSent += 1;
  lastJankAt = at;
  const window = asResources(entries("resource")).filter((entry) => entry.startTime >= from);
  const metrics: Record<string, number> = {
    window_ms: JANK_WINDOW_MS,
    blocked_ms: round(blocked),
    lag_count: lagEvents.length,
    lag_total_ms: round(lag),
    lag_max_ms: round(lagEvents.reduce((max, event) => Math.max(max, event.ms), 0)),
    page_age_ms: round(at),
    res_count: window.length,
    res_transfer_bytes: sum(window, "transferSize"),
    res_decoded_bytes: sum(window, "decodedBodySize"),
    ...heapMetrics(),
  };
  if (longTasksSupported) {
    metrics.longtask_count = longEvents.length;
    metrics.longtask_total_ms = round(long);
  }
  // this window is reported: the next report needs new blocked time
  lagEvents = [];
  longEvents = [];
  record("jank", `Main thread blocked ${seconds(blocked)} in ${seconds(JANK_WINDOW_MS)}`, {
    tag: "main_thread",
    metrics,
    top_resources: topResources(window, JANK_TOP_RESOURCES),
  });
}

function tick(): void {
  sampler = null;
  try {
    const at = now();
    const lag = at - due;
    if (lag >= LAG_MIN_MS) {
      lagEvents.push({ at, ms: lag });
      totals.lagCount += 1;
      totals.lagTotal += lag;
      totals.lagMax = Math.max(totals.lagMax, lag);
    }
    checkJank(at);
  } catch {
    // keep sampling
  }
  schedule();
}

function schedule(): void {
  if (!started || sampler !== null) return;
  due = now() + LAG_INTERVAL_MS;
  sampler = setTimeout(tick, LAG_INTERVAL_MS);
}

function pauseSampler(): void {
  if (sampler !== null) clearTimeout(sampler);
  sampler = null;
}

function onVisibility(): void {
  if (document.visibilityState === "hidden") pauseSampler();
  else schedule();
}

/** Whether the lag sampler is running now (for tests). */
export function lagSamplerRunning(): boolean {
  return sampler !== null;
}

function observeLongTasks(): () => void {
  try {
    if (typeof PerformanceObserver === "undefined" || !PerformanceObserver.supportedEntryTypes?.includes("longtask")) {
      return () => {};
    }
    longTasksSupported = true;
    const observer = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        longEvents.push({ at: entry.startTime + entry.duration, ms: entry.duration });
        totals.longCount += 1;
        totals.longTotal += entry.duration;
      }
    });
    observer.observe({ type: "longtask", buffered: true });
    return () => observer.disconnect();
  } catch {
    return () => {};
  }
}

function onLoaded(): void {
  loaded = true;
  if (settle === null && !perfSent) settle = setTimeout(sendLoadReport, PERF_SETTLE_MS);
}

/** Leaving before the settle time: the load report goes now (with what is known), sent along with the rest. */
function onPageHide(): void {
  if (!loaded || perfSent) return;
  if (settle !== null) clearTimeout(settle);
  sendLoadReport();
  flushClientErrors(true);
}

/** Start measuring (once per page load; later calls return the same undo). Returns the undo. */
export function startPerfReporter(options: PerfReporterOptions = {}): () => void {
  if (started && stop) return stop;
  started = true;
  record = options.record ?? recordClientError;
  now = options.now ?? (() => performance.now());
  entries = options.entries ?? ((type) => performance.getEntriesByType(type));
  try {
    performance.setResourceTimingBufferSize?.(RESOURCE_BUFFER);
  } catch {
    // keep the default buffer
  }
  const disconnect = observeLongTasks();
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", onPageHide);
  if (document.visibilityState !== "hidden") schedule();
  if (document.readyState === "complete") onLoaded();
  else window.addEventListener("load", onLoaded, { once: true });
  stop = () => {
    disconnect();
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("load", onLoaded);
    window.removeEventListener("pagehide", onPageHide);
    pauseSampler();
    if (settle !== null) clearTimeout(settle);
    settle = null;
    started = false;
    stop = null;
  };
  return stop;
}

export function resetPerfReporterForTests(): void {
  stop?.();
  lagEvents = [];
  longEvents = [];
  Object.assign(totals, { lagCount: 0, lagTotal: 0, lagMax: 0, longCount: 0, longTotal: 0 });
  longTasksSupported = false;
  perfSent = false;
  loaded = false;
  jankSent = 0;
  lastJankAt = Number.NEGATIVE_INFINITY;
  record = recordClientError;
}
