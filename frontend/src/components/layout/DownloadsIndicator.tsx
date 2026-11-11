import { CheckCircle2, Download, Loader2, X, XCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { Progress } from "@/components/ui/progress";
import { useDownloadsStore, type DownloadItem } from "@/stores/downloads-store";
import { cancelTrackedDownload } from "@/utils/download";

const MB = 1024 * 1024;

function megabytes(bytes: number): string {
  return (bytes / MB).toFixed(bytes < 10 * MB ? 1 : 0);
}

/** "12 / 66 MB · 4.2 MB/s · 13 s left" (what is known of it). */
function progressText(item: DownloadItem, t: (key: string, options?: Record<string, unknown>) => string): string {
  const seconds = Math.max(0.5, ((item.finishedAt ?? Date.now()) - item.startedAt) / 1000);
  const speed = item.received / seconds;
  const parts = [
    item.total !== null
      ? t("downloads_size_of", { done: megabytes(item.received), total: megabytes(item.total) })
      : t("downloads_size", { done: megabytes(item.received) }),
  ];
  if (item.status === "running" && item.received > 0) {
    parts.push(t("downloads_speed", { speed: megabytes(speed) }));
    if (item.total !== null && speed > 0) {
      parts.push(t("downloads_left", { seconds: Math.ceil((item.total - item.received) / speed) }));
    }
  }
  return parts.join(" · ");
}

/**
 * Top-bar entry for the studio's large-file downloads: shows while there are any, with the running count;
 * the popover lists each with its progress, size and speed, Cancel while it runs, and its outcome after.
 */
export function DownloadsIndicator() {
  const { t } = useTranslation("dashboard");
  const items = useDownloadsStore((s) => s.items);
  const remove = useDownloadsStore((s) => s.remove);
  const clearFinished = useDownloadsStore((s) => s.clearFinished);
  if (items.length === 0) return null;
  const running = items.filter((item) => item.status === "running");
  const overall =
    running.length > 0 && running.every((item) => item.total !== null)
      ? Math.round((100 * running.reduce((n, i) => n + i.received, 0)) / running.reduce((n, i) => n + (i.total ?? 0), 0))
      : null;

  return (
    <Popover>
      <PopoverTrigger
        render={<Button variant="ghost" size="sm" aria-label={t("downloads_title")} title={t("downloads_title")} />}
      >
        {running.length > 0 ? (
          <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
        ) : (
          <Download aria-hidden data-icon="inline-start" />
        )}
        <span className="num text-xs">
          {running.length > 0 ? (overall !== null ? `${overall}%` : running.length) : items.length}
        </span>
      </PopoverTrigger>
      <PopoverContent align="end" sideOffset={6} className="w-160">
        <div className="flex items-center justify-between gap-2">
          <PopoverTitle>{t("downloads_title")}</PopoverTitle>
          {items.some((item) => item.status !== "running") && (
            <Button variant="ghost" size="xs" onClick={clearFinished}>
              {t("downloads_clear")}
            </Button>
          )}
        </div>
        <ul className="m-0 flex max-h-96 list-none flex-col gap-3 overflow-y-auto p-0">
          {items.map((item) => (
            <li key={item.id} className="flex flex-col gap-1.5">
              <div className="flex min-w-0 items-center gap-2">
                {item.status === "done" ? (
                  <CheckCircle2 aria-hidden className="size-4 shrink-0 text-good" />
                ) : item.status === "failed" ? (
                  <XCircle aria-hidden className="size-4 shrink-0 text-destructive" />
                ) : item.status === "cancelled" ? (
                  <XCircle aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                ) : (
                  <Loader2 aria-hidden className="size-4 shrink-0 animate-spin text-muted-foreground" />
                )}
                <span className="min-w-0 flex-1 truncate text-sm" title={item.name}>
                  {item.name}
                </span>
                {item.status === "running" ? (
                  <Button variant="ghost" size="xs" onClick={() => cancelTrackedDownload(item.id)}>
                    {t("downloads_cancel")}
                  </Button>
                ) : (
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label={t("downloads_dismiss")}
                    onClick={() => remove(item.id)}
                  >
                    <X aria-hidden />
                  </Button>
                )}
              </div>
              {item.status === "running" && item.mode === "stream" && (
                <Progress
                  value={item.total !== null ? Math.min(100, (100 * item.received) / item.total) : null}
                  aria-label={item.name}
                />
              )}
              <p className="m-0 text-xs text-muted-foreground">
                {item.mode === "browser"
                  ? t("downloads_handed_to_browser")
                  : item.status === "failed"
                    ? t("downloads_failed", { message: item.error ?? "" })
                    : item.status === "cancelled"
                      ? t("downloads_cancelled")
                      : progressText(item, t)}
              </p>
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
