import { useTranslation } from "react-i18next";
import { useLocation } from "wouter";
import { ChartBar, MoreHorizontal, Trash2 } from "lucide-react";

import { useConfirmLeave } from "@/components/shared/edit-unit/LeaveGuard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { useEpisodeLedger } from "@/hooks/useEpisodeLedger";
import { useCostStore } from "@/stores/cost-store";
import type { EpisodeCost, EpisodeMeta } from "@/types";
import { currentScriptBreakdown, formatCost, totalBreakdown } from "@/utils/cost-format";
import { episodeDisplayName, episodePosition } from "@/utils/episode-display";
import { itemCountKey, type GenerationRoute } from "@/utils/generation-mode";

import { episodesViewPath } from "../episodes/episodes-view-model";
import { useDeleteEpisode } from "../episodes/useDeleteEpisode";
import { EditableEpisodeTitle } from "./EditableEpisodeTitle";

function formatDuration(seconds: number): string {
  // 先对总秒数取整再拆分，否则 119.6 秒会写成 1:60
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * 一集的费用：「已花」含历史支出（已删改条目留下的），「剩余」只对当前剧本结算，按当前剧本口径扣减。
 * 没有金额时三项都写「—」，不区分 0 与未知。
 */
function episodeCostSummary(cost: EpisodeCost) {
  const estimate = totalBreakdown(cost.totals.estimate);
  const currentActual = currentScriptBreakdown(cost.totals.actual);
  const remaining: Record<string, number> = {};
  for (const [currency, amount] of Object.entries(estimate)) {
    remaining[currency] = Math.max(0, amount - (currentActual[currency] ?? 0));
  }
  return {
    estimate: formatCost(estimate),
    spent: formatCost(totalBreakdown(cost.totals.actual)),
    remaining: formatCost(remaining),
  };
}

/**
 * 集页页头第一行的集头：集徽标、可编辑的集标题、统计图标（条目数、时长、视频进度、预估费用与已花 / 剩余，收在它打开的
 * 弹层里，不占这一行），以及「⋯」菜单。数据都来自项目详情与费用估算，画布不参与。
 * The stats open on hover, click or tap; a hover-opened one closes when the pointer leaves, a clicked one on a click
 * outside or Esc (Base UI Popover, as InfoTip).
 */
export function EpisodeHead({
  projectName,
  episode,
  meta,
  route,
  canEditTitle,
  onSaveTitle,
  canDelete,
}: {
  projectName: string;
  episode: number;
  meta: EpisodeMeta | undefined;
  route: GenerationRoute;
  canEditTitle: boolean;
  onSaveTitle: (title: string) => Promise<void>;
  /** 广告/短片恒单集、演示项目只读：不给删除。 */
  canDelete: boolean;
}) {
  const { t } = useTranslation("dashboard");
  const [, setLocation] = useLocation();
  const ledger = useEpisodeLedger();
  const position = episodePosition(ledger, episode);
  // 费用表是单例：切项目后到新数据到达前仍是上一个项目的，按项目名核对后才用。
  const cost = useCostStore((s) =>
    s.costData?.project_name === projectName ? s._episodeIndex.get(episode) : undefined,
  );
  const confirmLeave = useConfirmLeave();
  // 离开拦截放在确认删除这一步：先问未保存修改再确认删除，放弃后取消删除就白丢了修改
  const deletion = useDeleteEpisode(projectName, () => setLocation(episodesViewPath()), confirmLeave);

  const facts: string[] = [];
  if (meta?.item_count != null) facts.push(t(itemCountKey(route), { count: meta.item_count }));
  if (meta?.duration_seconds) facts.push(t("episode_head_duration", { duration: formatDuration(meta.duration_seconds) }));
  if (meta?.videos && meta.videos.total > 0) {
    facts.push(t("episode_head_videos", { available: meta.videos.available, total: meta.videos.total }));
  }
  const costSummary = cost ? episodeCostSummary(cost) : null;
  const costDetail = costSummary
    ? t("episode_head_cost_detail", { spent: costSummary.spent, remaining: costSummary.remaining })
    : "";

  return (
    <div className="flex min-w-0 flex-1 items-center gap-2.5">
      <Badge variant="secondary" translate="no">
        {t("episode_header_episode_chip", {
          number: position === null ? "—" : String(position).padStart(2, "0"),
        })}
      </Badge>
      <div className="min-w-0 max-w-[24em] shrink">
        <EditableEpisodeTitle
          title={meta?.title ?? ""}
          placeholder={episodeDisplayName(ledger, episode, t)}
          canEdit={canEditTitle}
          onSave={onSaveTitle}
        />
      </div>
      {facts.length > 0 || costSummary ? (
        <Popover>
          <PopoverTrigger
            openOnHover
            delay={150}
            closeDelay={150}
            render={<Button variant="ghost" size="icon-xs" aria-label={t("episode_head_stats")} title={t("episode_head_stats")} />}
          >
            <ChartBar aria-hidden />
          </PopoverTrigger>
          <PopoverContent side="bottom" align="start" className="w-auto min-w-48">
            <PopoverTitle>{t("episode_head_stats")}</PopoverTitle>
            <ul className="num m-0 mt-1 flex list-none flex-col gap-2.5 p-0 text-xs whitespace-nowrap">
              {facts.map((fact) => (
                <li key={fact}>{fact}</li>
              ))}
              {costSummary ? (
                <>
                  <li>{t("episode_head_cost", { cost: costSummary.estimate })}</li>
                  <li className="text-muted-foreground">{costDetail}</li>
                </>
              ) : null}
            </ul>
          </PopoverContent>
        </Popover>
      ) : null}
      {canDelete ? (
        <>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={<Button variant="ghost" size="icon-xs" aria-label={t("episode_head_more")} />}
            >
              <MoreHorizontal aria-hidden />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-auto">
              <DropdownMenuItem variant="destructive" onClick={() => void deletion.requestDelete(episode)}>
                <Trash2 aria-hidden />
                {t("episode_menu_delete")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {deletion.dialog}
        </>
      ) : null}
    </div>
  );
}
