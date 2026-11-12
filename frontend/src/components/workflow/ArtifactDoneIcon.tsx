import { useTranslation } from "react-i18next";
import { Image, SquarePlay, type LucideIcon } from "lucide-react";
import { cn } from "cn";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverDescription, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import type { WorkflowArtifactCollection } from "@/types/workflow";

export type ArtifactDoneKind = "storyboards" | "videos";

const ICON: Record<ArtifactDoneKind, LucideIcon> = { storyboards: Image, videos: SquarePlay };

/**
 * How many items of a collection there are once none is missing; null while any is missing, the collection can't be
 * read, doesn't apply to the project or is empty.
 */
export function artifactDoneCount(collection: WorkflowArtifactCollection | undefined): number | null {
  const missing = collection?.missing_ids;
  if (!Array.isArray(missing) || missing.length > 0) return null;
  if (collection?.state === "blocked" || collection?.state === "not_applicable") return null;
  const count = (collection?.current_ids?.length ?? 0) + (collection?.stale_ids?.length ?? 0);
  return count > 0 ? count : null;
}

/**
 * The "all generated" indicator left of the header's status icon, a little larger than it; it replaces the batch
 * fill button's "… complete" state. It always holds its place (the buttons beside it don't shift): green once complete,
 * grey until then, with the number still missing over it; only a kind the project doesn't have (e.g. no
 * storyboards in reference video projects) is left out. Hovering, clicking or tapping it opens a popup with the
 * count (and outdated ones).
 */
export function ArtifactDoneIcon({ kind, collection }: { kind: ArtifactDoneKind; collection?: WorkflowArtifactCollection }) {
  const { t } = useTranslation("workflow");
  if (collection?.state === "not_applicable") return null;
  const count = artifactDoneCount(collection);
  const done = count !== null;
  const Icon = ICON[kind];
  const title = t(done ? `done_${kind}` : `pending_${kind}`);
  const stale = collection?.stale_ids?.length ?? 0;
  const missing = collection?.missing_ids;
  const generated = (collection?.current_ids?.length ?? 0) + stale;
  const remaining = !done && Array.isArray(missing) && collection?.state !== "blocked" ? missing.length : 0;
  const detail = done
    ? t("done_detail", { count })
    : Array.isArray(missing) && collection?.state !== "blocked"
      ? t("done_progress", { done: generated, total: generated + missing.length })
      : null;
  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        delay={150}
        closeDelay={150}
        data-testid={`workflow-done-${kind}`}
        data-done={done}
        render={<Button variant="ghost" size="icon-lg" aria-label={title} />}
      >
        <span className="relative grid place-items-center">
          {/* 38px (a bit over the 36px button, within the 44px row) so a count reads over it; green when complete,
              60% grey behind a count, faint while there's no count to show (e.g. still loading) */}
          <Icon
            aria-hidden
            className={cn(
              "size-9.5",
              done ? "text-good" : "text-muted-foreground",
              !done && (remaining > 0 ? "opacity-60" : "opacity-20"),
            )}
          />
          {/* not complete: how many are still missing, over the grey icon (the popup says it in words) */}
          {remaining > 0 ? (
            <span aria-hidden className="num absolute inset-0 grid place-items-center text-xs font-semibold text-foreground">
              {remaining}
            </span>
          ) : null}
        </span>
      </PopoverTrigger>
      <PopoverContent side="bottom" align="end" className="w-auto max-w-xs">
        <PopoverTitle>{title}</PopoverTitle>
        {detail ? (
          <PopoverDescription>
            {detail}
            {stale > 0 ? ` · ${t("done_stale", { count: stale })}` : null}
          </PopoverDescription>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
