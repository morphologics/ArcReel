import { useId, type ReactNode } from "react";
import { Focus, Minimize } from "lucide-react";
import { cn } from "cn";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

import { episodeViewTabId, type EpisodeView, type EpisodeViewTab } from "./episode-view";

/**
 * Disabled view tab: currently only appears when there is no final script yet (shots, multi-grid storyboard); hover and screen readers both say "Available after the script is generated".
 * A disabled tab receives no pointer events, so the tooltip hangs on the outer element; arrow keys can still focus it, and the tooltip also shows on focus.
 *
 * 置灰的视图 tab：目前只在还没有正式脚本时出现（分镜、多宫格分镜图），悬停与读屏都说明「脚本生成后可用」。
 * 置灰的 tab 不接收指针事件，提示挂在外层；方向键仍能聚焦到它，焦点进入时同样显示提示。
 */
// YouReel: the view tabs look like buttons, Render's height, each view with its own colour (TabsList variant
// "buttons"; owner 2026-11-17)
const VIEW_TAB_TONE: Record<EpisodeView, "neutral" | "plan" | "board" | "edit"> = {
  setting: "neutral",
  plan: "plan",
  grid: "neutral",
  board: "board",
  edit: "edit",
};

function DisabledViewTab({ view, label }: { view: EpisodeView; label: string }) {
  const { t } = useTranslation("dashboard");
  const hintId = useId();
  return (
    <Tooltip>
      <TooltipTrigger render={<span className="flex h-full" />}>
        <TabsTrigger
          value={view}
          id={episodeViewTabId(view)}
          disabled
          aria-describedby={hintId}
          tone={VIEW_TAB_TONE[view]}
        >
          {label}
        </TabsTrigger>
        <span id={hintId} className="sr-only">
          {t("episode_view_needs_script")}
        </span>
      </TooltipTrigger>
      <TooltipContent>{t("episode_view_needs_script")}</TooltipContent>
    </Tooltip>
  );
}

/**
 * Episode page header, one 44px row: the episode head, the view tabs, the current view's action slot, then the
 * workflow progress entry. Each view puts its actions into the slot via `EpisodeHeaderActions`; the header does not
 * know the specific actions. (YouReel: the second row that held the actions is gone, owner 2026-11-17.)
 *
 * 集页页头，单行 44px：集头、视图 tab、当前视图的动作插槽，行尾是制作进度入口。
 * 动作由各视图经 `EpisodeHeaderActions` 放进插槽，页头不认识具体动作。
 */
export function EpisodePageHeader({
  head,
  progress,
  tabs,
  view,
  onViewChange,
  boardLabel,
  onActionsSlot,
  compact = false,
  focus = null,
}: {
  head: ReactNode;
  /** Workflow progress entry; demo projects have none. / 制作进度入口；演示项目没有。 */
  progress: ReactNode;
  tabs: EpisodeViewTab[];
  view: EpisodeView;
  onViewChange: (view: EpisodeView) => void;
  /** Name of the "Shots" tab: reference-to-video projects call it "Video units". / 「分镜」tab 的名称：参考生视频项目称「视频单元」。 */
  boardLabel: string;
  onActionsSlot: (node: HTMLDivElement | null) => void;
  /** Edit view: no side padding beyond 12px (the row is the same single row in every view). */
  compact?: boolean;
  /**
   * Edit view focus mode (compact only): a Focus button right of the tabs; while `on`, the row holds only the episode
   * switcher and "Exit focus" (head, tabs, actions and progress hidden).
   */
  focus?: { on: boolean; onEnter: () => void; onExit: () => void; switcher: ReactNode } | null;
}) {
  const { t } = useTranslation("dashboard");
  const label: Record<EpisodeView, string> = {
    setting: t("episode_view_setting"),
    plan: t("episode_view_plan"),
    grid: t("episode_view_grid"),
    board: boardLabel,
    edit: t("episode_view_edit"),
  };
  const viewTabs = (
    <Tabs value={view} onValueChange={(next: EpisodeView) => onViewChange(next)} className="shrink-0">
      <TabsList variant="buttons" aria-label={t("episode_view_aria")}>
        {tabs.map((tab) =>
          tab.disabled ? (
            <DisabledViewTab key={tab.view} view={tab.view} label={label[tab.view]} />
          ) : (
            <TabsTrigger
              key={tab.view}
              value={tab.view}
              id={episodeViewTabId(tab.view)}
              tone={VIEW_TAB_TONE[tab.view]}
            >
              {label[tab.view]}
            </TabsTrigger>
          ),
        )}
      </TabsList>
    </Tabs>
  );
  // The view tabs sit centred in the space between the episode head (title … ⋯) and the right-hand group (the view's
  // actions such as Write prompts and Render, then Production progress). The tabs keep their full width; the head
  // shrinks first (its title truncates).
  // Edit view: right of the tabs, the button into focus mode (the Edit view alone, full screen, no app chrome)
  const centredTabsWithFocus = (
    <div className="flex flex-1 items-center justify-center gap-1">
      {viewTabs}
      {focus && (
        <Tooltip>
          <TooltipTrigger
            render={<Button variant="ghost" size="icon-sm" aria-label={t("edit_focus")} onClick={focus.onEnter} />}
          >
            <Focus aria-hidden />
          </TooltipTrigger>
          <TooltipContent side="bottom">{t("edit_focus_hint")}</TooltipContent>
        </Tooltip>
      )}
    </div>
  );
  const headSlot = <div className="flex min-w-0 shrink">{head}</div>;
  return (
    <header className="shrink-0 border-b border-border">
      <div className={cn("flex h-11 items-center gap-3", compact ? "px-3" : "px-4")}>
        {focus?.on ? focus.switcher : headSlot}
        {focus?.on ? <span className="flex-1" /> : centredTabsWithFocus}
        {/* the actions slot stays mounted (views portal their actions into it), hidden in focus mode; it keeps its
            full width (the episode title truncates first) */}
        <div className={cn("flex shrink-0", focus?.on && "hidden")}>
          <div ref={onActionsSlot} className="ml-auto flex shrink-0 items-center gap-1.5" />
        </div>
        {focus?.on ? (
          <Button variant="ghost" size="sm" onClick={focus.onExit}>
            <Minimize aria-hidden data-icon="inline-start" />
            {t("edit_focus_exit")}
          </Button>
        ) : (
          progress
        )}
      </div>
    </header>
  );
}
