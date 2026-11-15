import { useTranslation } from "react-i18next";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { LOBBY_FILTERS, type LobbyFilter } from "./lobby-projects";

interface LobbyToolbarProps {
  filter: LobbyFilter;
  onFilterChange: (filter: LobbyFilter) => void;
  counts: Record<LobbyFilter, number>;
}

/**
 * Toolbar sticky at the top of the shell body: filters with counts on the left, the sort order explained on the right. Sorting is fixed to most recent activity, with no toggle.
 * The background is opaque so cards scrolling past do not show through.
 *
 * 吸顶在外壳主体顶端的工具栏：左边是带计数的筛选，右边说明排序方式。排序固定按最近活动，不提供切换。
 * 底色不透明，滚过的卡片不会透出来。
 */
export function LobbyToolbar({ filter, onFilterChange, counts }: LobbyToolbarProps) {
  const { t } = useTranslation("dashboard");
  return (
    <div className="sticky top-0 z-sticky flex items-center gap-3 border-b border-border bg-background py-2">
      <ToggleGroup
        aria-label={t("lobby_filter_label")}
        size="sm"
        value={[filter]}
        onValueChange={(next: string[]) => {
          // Clicking the current filter again does not deselect it. / 再次点击当前筛选不会取消选中。
          const picked = next.find((value): value is LobbyFilter => (LOBBY_FILTERS as readonly string[]).includes(value));
          if (picked) onFilterChange(picked);
        }}
      >
        {LOBBY_FILTERS.map((key) => (
          <ToggleGroupItem key={key} value={key}>
            {t(`lobby_filter_${key}`)}
            <span className="text-muted-foreground tabular-nums">{counts[key]}</span>
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <span className="ml-auto shrink-0 text-xs text-muted-foreground">{t("lobby_sort_recent")}</span>
    </div>
  );
}
