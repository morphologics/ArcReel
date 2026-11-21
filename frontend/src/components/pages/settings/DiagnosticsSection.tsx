import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronRight, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { API } from "@/api";
import { TruncatedText } from "@/components/shared/TruncatedText";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogBody,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { ClientErrorReport, PerfResource, ReportKind } from "@/types/client-errors";

/** The list's filter: which kinds it loads (none = all) */
const FILTERS = {
  all: undefined,
  errors: ["toast", "exception", "request"],
  perf: ["perf"],
  jank: ["jank"],
  server_slow: ["server_slow"],
} as const satisfies Record<string, readonly ReportKind[] | undefined>;
type Filter = keyof typeof FILTERS;
const FILTER_IDS = Object.keys(FILTERS) as Filter[];
const PERF_KINDS: ReadonlySet<string> = new Set(["perf", "jank", "server_slow"]);
const SUMMARY_ROWS = 3;
const SUMMARY_FILES = 5;

function formatMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1_048_576) return `${(bytes / 1_048_576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${Math.round(bytes)} B`;
}

/** A metric shown by its unit: `*_ms` durations, `*_bytes` sizes, anything else as a count. */
function formatMetric(name: string, value: number): string {
  if (name.endsWith("_ms")) return formatMs(value);
  if (name.endsWith("_bytes")) return formatBytes(value);
  return String(value);
}

function resourceWeight(resource: PerfResource): number {
  return Math.max(resource.transfer_bytes, resource.decoded_bytes);
}

interface SummaryLine {
  key: string;
  value: string;
  label: string;
}

/** The compact performance summary, from the loaded rows: slowest loads / Editor opens, heaviest files, worst jank,
 * slowest server paths. */
function perfSummary(
  reports: readonly ClientErrorReport[],
  packOpen: string,
): { id: string; lines: SummaryLine[] }[] {
  const metric = (report: ClientErrorReport, name: string) => report.detail?.metrics?.[name];
  const page = (report: ClientErrorReport) => report.detail?.page ?? "?";
  const top = (list: ClientErrorReport[], value: (report: ClientErrorReport) => number | undefined) =>
    list
      .map((report) => ({ report, value: value(report) }))
      .filter((row): row is { report: ClientErrorReport; value: number } => row.value !== undefined)
      .sort((a, b) => b.value - a.value)
      .slice(0, SUMMARY_ROWS);

  const perf = reports.filter((report) => report.kind === "perf");
  const loads = top(perf, (report) => metric(report, "open_ms") ?? metric(report, "load_ms")).map(({ report, value }) => ({
    key: String(report.id),
    value: formatMs(value),
    label: report.detail?.tag === "pack_open" ? `${page(report)} · ${packOpen}` : page(report),
  }));

  const files = new Map<string, PerfResource>();
  for (const report of perf) {
    for (const resource of report.detail?.top_resources ?? []) {
      const seen = files.get(resource.path);
      if (!seen || resourceWeight(resource) > resourceWeight(seen)) files.set(resource.path, resource);
    }
  }
  const heaviest = [...files.values()]
    .sort((a, b) => resourceWeight(b) - resourceWeight(a))
    .slice(0, SUMMARY_FILES)
    .map((resource) => ({ key: resource.path, value: formatBytes(resourceWeight(resource)), label: resource.path }));

  const jank = top(
    reports.filter((report) => report.kind === "jank"),
    (report) => metric(report, "blocked_ms"),
  ).map(({ report, value }) => ({ key: String(report.id), value: formatMs(value), label: page(report) }));

  const server = top(
    reports.filter((report) => report.kind === "server_slow"),
    (report) => (report.detail?.tag === "stream" ? metric(report, "ttfb_ms") : metric(report, "wall_ms")),
  ).map(({ report, value }) => ({
    key: String(report.id),
    value: formatMs(value),
    label: `${report.detail?.method ?? ""} ${report.detail?.api_path ?? "?"}`.trim(),
  }));

  return [
    { id: "loads", lines: loads },
    { id: "files", lines: heaviest },
    { id: "jank", lines: jank },
    { id: "server", lines: server },
  ].filter((section) => section.lines.length > 0);
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** "POST /api/v1/x · 500", or the page when the report isn't about a request. */
function whereText(report: ClientErrorReport, networkError: string): string | null {
  const detail = report.detail;
  if (!detail) return null;
  if (detail.api_path) {
    const status = detail.status ?? networkError;
    return `${detail.method ? `${detail.method} ` : ""}${detail.api_path} · ${status}`;
  }
  return detail.page ?? null;
}

/**
 * YouReel: Settings → Options → Diagnostics. The errors the studio's UI reported (src/diagnostics/client-error-
 * reporter.ts) and the performance records (perf / jank from src/diagnostics/perf-reporter.ts and the Editor,
 * server_slow from the server), newest first, with a kind filter, a compact performance summary computed from the
 * loaded rows, Refresh and Clear (confirmed first: it can't be undone; with a filter on, it clears only that kind). A
 * failure to load or clear shows here, never as a toast (an error toast would be reported again).
 */
export function DiagnosticsSection() {
  const { t } = useTranslation("dashboard");
  const headingId = useId();
  const [reports, setReports] = useState<ClientErrorReport[] | null>(null);
  const [kept, setKept] = useState(1000);
  const [keptPerf, setKeptPerf] = useState(500);
  const [filter, setFilter] = useState<Filter>("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);
  const loadRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    loadRef.current?.abort();
    const controller = new AbortController();
    loadRef.current = controller;
    try {
      const list = await API.listClientErrors({ kinds: FILTERS[filter], signal: controller.signal });
      if (controller.signal.aborted) return;
      setReports(list.entries);
      setKept(list.kept);
      if (list.kept_perf !== undefined) setKeptPerf(list.kept_perf);
      setError(null);
    } catch (err) {
      if (controller.signal.aborted) return;
      setError(errorText(err));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load the list after mount; state is written back after the await
    void load();
    return () => loadRef.current?.abort();
  }, [load]);

  const handleClear = async () => {
    setClearing(true);
    setClearError(null);
    try {
      await API.clearClientErrors({ kinds: FILTERS[filter] });
      setConfirming(false);
      setReports([]);
    } catch (err) {
      setClearError(errorText(err));
    } finally {
      setClearing(false);
    }
  };

  const total = reports?.length ?? 0;
  const summary = useMemo(
    () => perfSummary(reports ?? [], t("options_diagnostics_summary_pack_open")),
    [reports, t],
  );

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h3 id={headingId} className="text-sm font-medium">
            {t("options_diagnostics")}
          </h3>
          <p className="text-sm text-muted-foreground">{t("options_diagnostics_desc", { kept })}</p>
          <p className="text-sm text-muted-foreground">{t("options_diagnostics_perf_desc", { kept: keptPerf })}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={() => {
              setLoading(true);
              void load();
            }}
          >
            {loading ? (
              <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
            ) : (
              <RefreshCw aria-hidden data-icon="inline-start" />
            )}
            {t("common:refresh")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={total === 0}
            onClick={() => {
              setClearError(null);
              setConfirming(true);
            }}
          >
            <Trash2 aria-hidden data-icon="inline-start" />
            {t("options_diagnostics_clear")}
          </Button>
        </div>
      </div>

      <ToggleGroup
        aria-label={t("options_diagnostics_filter")}
        variant="outline"
        size="sm"
        className="flex-wrap"
        value={[filter]}
        onValueChange={(next: string[]) => {
          const to = (next[0] as Filter | undefined) ?? filter;
          if (to === filter) return;
          setLoading(true);
          setFilter(to);
        }}
      >
        {FILTER_IDS.map((id) => (
          <ToggleGroupItem key={id} value={id}>
            {t(`options_diagnostics_filter_${id}`)}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>

      {summary.length > 0 && (
        <section
          aria-label={t("options_diagnostics_summary")}
          className="@container rounded-lg border border-border bg-card px-4 py-3"
        >
          <div className="grid gap-3 @md:grid-cols-2">
            {summary.map((part) => (
              <div key={part.id} className="flex min-w-0 flex-col gap-1">
                <h4 className="text-xs font-medium text-muted-foreground">
                  {t(`options_diagnostics_summary_${part.id}`)}
                </h4>
                <ol className="flex flex-col gap-0.5">
                  {part.lines.map((line) => (
                    <li key={line.key} className="flex min-w-0 gap-2 text-xs">
                      <span className="shrink-0 font-mono tabular-nums">{line.value}</span>
                      <TruncatedText text={line.label} className="font-mono text-subtle-foreground" />
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </div>
        </section>
      )}

      {error && (
        <p role="alert" className="text-sm wrap-break-word text-destructive">
          {t("options_diagnostics_load_failed", { error })}
        </p>
      )}

      {reports !== null && reports.length === 0 && !error && (
        <p className="rounded-lg border border-border bg-card px-4 py-3 text-sm text-muted-foreground">
          {t(filter === "all" ? "options_diagnostics_empty" : "options_diagnostics_empty_filtered")}
        </p>
      )}

      {reports !== null && reports.length > 0 && (
        <ul
          aria-label={t("options_diagnostics_list")}
          className="flex flex-col divide-y divide-border rounded-lg border border-border bg-card"
        >
          {reports.map((report) => (
            <ReportRow key={report.id} report={report} />
          ))}
        </ul>
      )}

      <AlertDialog
        open={confirming}
        onOpenChange={(open) => {
          // Ignore Esc while the request is in flight
          if (!clearing) setConfirming(open);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("options_diagnostics_clear_title")}</AlertDialogTitle>
          </AlertDialogHeader>
          <AlertDialogBody>
            <div className="flex flex-col gap-2">
              <AlertDialogDescription>
                {t(filter === "all" ? "options_diagnostics_clear_desc" : "options_diagnostics_clear_desc_filtered", {
                  total,
                })}
              </AlertDialogDescription>
              {clearError && (
                <p role="alert" className="text-sm wrap-break-word text-destructive">
                  {t("options_diagnostics_clear_failed", { error: clearError })}
                </p>
              )}
            </div>
          </AlertDialogBody>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={clearing}>{t("common:cancel")}</AlertDialogCancel>
            <AlertDialogAction variant="destructive" disabled={clearing} onClick={() => void handleClear()}>
              {clearing && <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />}
              {t("options_diagnostics_clear")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

function ReportRow({ report }: { report: ClientErrorReport }) {
  const { t, i18n } = useTranslation("dashboard");
  const created = new Date(report.created_at);
  const time = Number.isNaN(created.getTime())
    ? report.created_at
    : created.toLocaleString(i18n.language, { dateStyle: "short", timeStyle: "medium" });
  const where = whereText(report, t("options_diagnostics_network_error"));
  const detail = report.detail;
  const rows: [string, string, boolean][] = [];
  if (detail?.page) rows.push([t("options_diagnostics_detail_page"), detail.page, false]);
  if (detail?.api_path) rows.push([t("options_diagnostics_detail_request"), where ?? detail.api_path, false]);
  if (detail?.source) rows.push([t("options_diagnostics_detail_source"), detail.source, false]);
  if (detail?.client) rows.push([t("options_diagnostics_detail_client"), detail.client, false]);
  if (detail?.metrics && Object.keys(detail.metrics).length > 0) {
    const text = Object.entries(detail.metrics)
      .map(([name, value]) => `${name} ${formatMetric(name, value)}`)
      .join("\n");
    rows.push([t("options_diagnostics_detail_metrics"), text, true]);
  }
  if (detail?.resource_groups?.length) {
    const text = detail.resource_groups
      .map((group) => `${group.type}: ${group.count} · ${formatBytes(group.transfer_bytes)} / ${formatBytes(group.decoded_bytes)}`)
      .join("\n");
    rows.push([t("options_diagnostics_detail_resources"), text, true]);
  }
  if (detail?.top_resources?.length) {
    const text = detail.top_resources
      .map(
        (resource) =>
          `${formatBytes(resourceWeight(resource))} · ${formatMs(resource.duration_ms)} · ${resource.type ?? "?"} · ${resource.path}`,
      )
      .join("\n");
    rows.push([t("options_diagnostics_detail_top"), text, true]);
  }
  if (report.user_agent) rows.push([t("options_diagnostics_detail_browser"), report.user_agent, false]);
  if (report.app_build) rows.push([t("options_diagnostics_detail_build"), report.app_build, false]);
  if (detail?.stack) rows.push([t("options_diagnostics_detail_stack"), detail.stack, true]);

  return (
    <li className="flex flex-col gap-1 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Badge variant={PERF_KINDS.has(report.kind) ? "secondary" : report.kind === "toast" ? "outline" : "destructive"}>
          {t(`options_diagnostics_kind_${report.kind}`)}
        </Badge>
        <time dateTime={report.created_at} className="text-xs text-muted-foreground tabular-nums">
          {time}
        </time>
      </div>
      <p className="text-sm wrap-break-word">{report.message}</p>
      {where && (
        <div className="flex min-w-0">
          <TruncatedText text={where} className="font-mono text-xs text-muted-foreground" />
        </div>
      )}
      {rows.length > 0 && (
        <Collapsible render={<div className="flex w-full min-w-0 flex-col gap-1" />}>
          <CollapsibleTrigger render={<Button variant="ghost" size="xs" className="-ml-2 self-start" />}>
            {t("options_diagnostics_details")}
            <ChevronRight
              data-icon="inline-end"
              aria-hidden
              className="transition-transform group-aria-expanded/button:rotate-90"
            />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs text-subtle-foreground">
              {rows.map(([label, value, block]) => (
                <div key={label} className="contents">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className={`m-0 min-w-0 font-mono break-all${block ? " whitespace-pre-wrap" : ""}`}>{value}</dd>
                </div>
              ))}
            </dl>
          </CollapsibleContent>
        </Collapsible>
      )}
    </li>
  );
}
