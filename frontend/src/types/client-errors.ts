/** YouReel: errors the studio's browser UI reported (server/routers/diagnostics.py; Settings → Options → Diagnostics). */

export type ClientErrorKind = "toast" | "exception" | "request";

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
}

export interface ClientErrorEntry {
  kind: ClientErrorKind;
  message: string;
  detail?: ClientErrorDetail;
}

export interface ClientErrorReport {
  id: number;
  /** UTC, ISO 8601 */
  created_at: string;
  kind: ClientErrorKind;
  message: string;
  detail: ClientErrorDetail | null;
  user_agent: string | null;
  app_build: string | null;
}

export interface ClientErrorList {
  entries: ClientErrorReport[];
  /** How many reports the studio keeps (older ones are pruned) */
  kept: number;
}
