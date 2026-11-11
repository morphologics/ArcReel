import { useTranslation } from "react-i18next";
import { cn } from "cn";
import type { VersionInfo } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AspectFrame } from "@/components/canvas/shared/AspectFrame";
import { promptSummary, versionCost, versionOrigin, versionSize, versionTime } from "./version-summary";

interface VersionHistoryPanelProps {
  kind: "storyboard" | "video";
  aspectRatio: "9:16" | "16:9";
  versions: VersionInfo[] | null;
  loading: boolean;
  error: boolean;
  /** The version previewed in the pane, marked in the list. */
  viewingVersion: number | null;
  onView: (version: number) => void;
  /** Make a version the current one; absent when the shot can't be changed (read-only). */
  onRestore?: (version: number) => void;
  restoringVersion: number | null;
  restoreDisabled: boolean;
}

/**
 * The shot's media pane in History mode: every version newest first, with when, where it came from, model and cost
 * of its generation, size (and length for videos) and the start of its prompt. Clicking a version previews it in
 * the pane (back to Preview mode); Make current switches the shot to it.
 */
export function VersionHistoryPanel({
  kind,
  aspectRatio,
  versions,
  loading,
  error,
  viewingVersion,
  onView,
  onRestore,
  restoringVersion,
  restoreDisabled,
}: VersionHistoryPanelProps) {
  const { t } = useTranslation(["dashboard", "common"]);
  return (
    <AspectFrame ratio={aspectRatio}>
      <div className="size-full overflow-y-auto rounded-lg bg-muted/30 ring-1 ring-border">
        {loading ? (
          <p className="p-3 text-xs text-subtle-foreground">{t("common:loading")}</p>
        ) : error ? (
          <p className="p-3 text-xs text-destructive">{t("version_history_load_failed")}</p>
        ) : !versions || versions.length === 0 ? (
          <div className="flex flex-col gap-1 p-3">
            <p className="text-sm">{t("no_history")}</p>
            <p className="text-xs text-subtle-foreground">{t("history_hint")}</p>
          </div>
        ) : (
          <ul className="m-0 flex list-none flex-col p-1">
            {versions.map((v) => {
              const cost = versionCost(v);
              const size = versionSize(v);
              const summary = promptSummary(v.prompt);
              return (
                <li
                  key={v.version}
                  className={cn(
                    "flex items-start gap-2 rounded-md p-1.5 hover:bg-muted/70",
                    viewingVersion === v.version && "bg-primary/10",
                  )}
                >
                  <button
                    type="button"
                    className="focus-ring flex min-w-0 flex-1 items-start gap-2 rounded-sm text-left"
                    onClick={() => onView(v.version)}
                    aria-label={t("version_history_view", { version: v.version })}
                  >
                    {kind === "storyboard" && v.file_url ? (
                      <img
                        src={`${v.file_url}?w=160`}
                        alt=""
                        loading="lazy"
                        className="h-10 w-16 shrink-0 rounded-sm bg-muted object-cover"
                      />
                    ) : null}
                    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <span className="flex flex-wrap items-center gap-x-1.5 text-xs">
                        <span className="num font-medium">v{v.version}</span>
                        {v.is_current && <Badge variant="secondary">{t("current_version_badge")}</Badge>}
                        <span className="num text-subtle-foreground">{versionTime(v)}</span>
                        <span className="text-subtle-foreground">· {versionOrigin(v, t)}</span>
                        {v.generation?.model && <span className="text-subtle-foreground">· {v.generation.model}</span>}
                        {cost && <span className="num font-medium">· {cost}</span>}
                      </span>
                      <span className="num text-[11px] text-subtle-foreground">
                        {[
                          kind === "video" && v.duration_seconds ? t("duration_seconds_value_text", { value: v.duration_seconds }) : null,
                          size,
                          v.seed != null ? t("version_seed", { seed: v.seed }) : null,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                      {summary && (
                        <span className="line-clamp-1 text-[11px] text-subtle-foreground" title={v.prompt}>
                          {summary}
                        </span>
                      )}
                    </span>
                  </button>
                  {onRestore && !v.is_current && v.restorable !== false && (
                    <Button
                      size="xs"
                      variant="outline"
                      disabled={restoreDisabled || restoringVersion !== null}
                      onClick={() => onRestore(v.version)}
                    >
                      {restoringVersion === v.version ? t("switching_version") : t("version_make_current")}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </AspectFrame>
  );
}
