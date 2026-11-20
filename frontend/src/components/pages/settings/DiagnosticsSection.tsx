import { useCallback, useEffect, useId, useRef, useState } from "react";
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
import type { ClientErrorReport } from "@/types/client-errors";

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
 * reporter.ts), newest first, with Refresh and Clear (confirmed first: it can't be undone). A failure to load or
 * clear shows here, never as a toast (an error toast would be reported again).
 */
export function DiagnosticsSection() {
  const { t } = useTranslation("dashboard");
  const headingId = useId();
  const [reports, setReports] = useState<ClientErrorReport[] | null>(null);
  const [kept, setKept] = useState(1000);
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
      const list = await API.listClientErrors({ signal: controller.signal });
      if (controller.signal.aborted) return;
      setReports(list.entries);
      setKept(list.kept);
      setError(null);
    } catch (err) {
      if (controller.signal.aborted) return;
      setError(errorText(err));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load the list after mount; state is written back after the await
    void load();
    return () => loadRef.current?.abort();
  }, [load]);

  const handleClear = async () => {
    setClearing(true);
    setClearError(null);
    try {
      await API.clearClientErrors();
      setConfirming(false);
      setReports([]);
    } catch (err) {
      setClearError(errorText(err));
    } finally {
      setClearing(false);
    }
  };

  const total = reports?.length ?? 0;

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h3 id={headingId} className="text-sm font-medium">
            {t("options_diagnostics")}
          </h3>
          <p className="text-sm text-muted-foreground">{t("options_diagnostics_desc", { kept })}</p>
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

      {error && (
        <p role="alert" className="text-sm wrap-break-word text-destructive">
          {t("options_diagnostics_load_failed", { error })}
        </p>
      )}

      {reports !== null && reports.length === 0 && !error && (
        <p className="rounded-lg border border-border bg-card px-4 py-3 text-sm text-muted-foreground">
          {t("options_diagnostics_empty")}
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
              <AlertDialogDescription>{t("options_diagnostics_clear_desc", { total })}</AlertDialogDescription>
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
  if (report.user_agent) rows.push([t("options_diagnostics_detail_browser"), report.user_agent, false]);
  if (report.app_build) rows.push([t("options_diagnostics_detail_build"), report.app_build, false]);
  if (detail?.stack) rows.push([t("options_diagnostics_detail_stack"), detail.stack, true]);

  return (
    <li className="flex flex-col gap-1 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Badge variant={report.kind === "toast" ? "outline" : "destructive"}>
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
