import { Link } from "wouter";
import { ChevronDown } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ROUTE_APP_PROJECTS, episodeEditViewPath } from "@/app-routes";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import type { EpisodeLedger } from "@/utils/episode-display";
import { episodeDisplayName } from "@/utils/episode-display";
import { cn } from "cn";

/**
 * Focus mode's way to other episodes (the sidebar is hidden there): the current episode's name opens a list of the
 * project's episodes; picking one opens its Edit view, staying in focus mode.
 */
export function EpisodeSwitcher({
  projectName,
  episodes,
  current,
}: {
  projectName: string;
  episodes: EpisodeLedger;
  current: number;
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  return (
    <Popover>
      <PopoverTrigger render={<Button variant="ghost" size="sm" aria-label={t("dashboard:edit_focus_episodes")} />}>
        <span className="max-w-[24ch] truncate">{episodeDisplayName(episodes, current, t)}</span>
        <ChevronDown aria-hidden data-icon="inline-end" />
      </PopoverTrigger>
      <PopoverContent side="bottom" align="start" className="w-72">
        <PopoverTitle>{t("dashboard:edit_focus_episodes")}</PopoverTitle>
        <ol className="relative m-0 flex max-h-[60dvh] list-none flex-col overflow-y-auto p-0">
          {episodes.map((ep, index) => (
            <li key={ep.episode}>
              <Link
                href={`~${ROUTE_APP_PROJECTS}/${encodeURIComponent(projectName)}${episodeEditViewPath(ep.episode)}`}
                aria-current={ep.episode === current ? "page" : undefined}
                className={cn(
                  "focus-ring flex gap-2 rounded-md px-2 py-1.5 text-sm no-underline hover:bg-muted",
                  ep.episode === current ? "bg-primary/15 text-foreground" : "text-muted-foreground",
                )}
              >
                <span className="num w-6 shrink-0 text-right">{index + 1}</span>
                <span className="min-w-0 truncate">{episodeDisplayName(episodes, ep.episode, t)}</span>
              </Link>
            </li>
          ))}
        </ol>
      </PopoverContent>
    </Popover>
  );
}
