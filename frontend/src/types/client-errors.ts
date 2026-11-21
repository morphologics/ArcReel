/** YouReel: errors the studio's browser UI reported (server/routers/diagnostics.py; Settings → Options → Diagnostics). */

export type ClientErrorKind = "toast" | "exception" | "request";
/** Performance records: `perf` (one per page load) and `jank` (main thread blocked) from the page; `server_slow` is
 * written by the server itself (server/perf_timing.py) */
export type PerfKind = "perf" | "jank" | "server_slow";
export type ReportKind = ClientErrorKind | PerfKind;

/** The page's resources of one type (`initiatorType`) */
export interface PerfResourceGroup {
  type: string;
  count: number;
  transfer_bytes: number;
  decoded_bytes: number;
}

/** One heavy resource (path without query string) */
export interface PerfResource {
  path: string;
  type?: string | null;
  transfer_bytes: number;
  decoded_bytes: number;
  duration_ms: number;
}

export interface ClientErrorDetail {
  /** The page's path (no query string) */
  page?: string | null;
  /** A failed request's HTTP method, API path (no query string) and status (none for a network error) */
  method?: string | null;
  api_path?: string | null;
  status?: number | null;
  /** Component / action label, or the script file of an exception */
  source?: string | null;
  stack?: string | null;
  /** Performance records: what it is about (load, pack_open, api, transfer, stream, …), the X-Client of a slow server
   * answer, numbers by name (`*_ms`, `*_bytes`, counts), resources by type and the heaviest ones */
  tag?: string | null;
  client?: string | null;
  metrics?: Record<string, number> | null;
  resource_groups?: PerfResourceGroup[] | null;
  top_resources?: PerfResource[] | null;
}

export interface ClientErrorEntry {
  kind: Exclude<ReportKind, "server_slow">;
  message: string;
  detail?: ClientErrorDetail;
}

export interface ClientErrorReport {
  id: number;
  /** UTC, ISO 8601 */
  created_at: string;
  kind: ReportKind;
  message: string;
  detail: ClientErrorDetail | null;
  user_agent: string | null;
  app_build: string | null;
}

export interface ClientErrorList {
  entries: ClientErrorReport[];
  /** How many error reports the studio keeps (older ones are pruned) */
  kept: number;
  /** How many performance records (perf / jank / server_slow) it keeps */
  kept_perf?: number;
}
