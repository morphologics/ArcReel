import { useState, type ReactNode } from "react";
import { Link } from "wouter";
import { cn } from "cn";

import { TruncatedText } from "@/components/shared/TruncatedText";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export interface SecondaryRailItem {
  /** Unique within the whole secondary rail; compared with `activeId`. / 在整个二级栏内唯一，与 `activeId` 比较。 */
  id: string;
  label: string;
  /** Second line: status or count, e.g. "2 keys configured", "3 models". / 第二行：状态或数量，如「已配置 2 个密钥」「3 个模型」。 */
  description?: string;
  /** Left of the name in the standard tier, the only visible content in the compact tier. Size is given by the caller (`size-4`). / 标准档放在名称左侧，紧凑档是唯一可见的内容。尺寸由调用方给（`size-4`）。 */
  icon: ReactNode;
  /** URL that selects this item. The selection lives in the URL and switching goes through the router, so the leave guard covers switching. / 选中这一项的地址。选中项记在地址里，切换经路由，离开拦截因此覆盖切换。 */
  href: string;
}

export interface SecondaryRailGroup {
  id: string;
  /** Tab name when there are several groups; in the compact tier, the accessible name of this group's list. / 多组时是 Tab 名；紧凑档是这一组列表的可访问名称。 */
  label: string;
  items: SecondaryRailItem[];
  /** Action entry at the end of the group, e.g. "Add custom provider". Not counted in the Tab's count. / 组末尾的动作条目，如「添加自定义供应商」。不计入 Tab 上的数量。 */
  action?: SecondaryRailItem;
  /** Explanation when the group has no entries; only shown in the standard tier. / 组内没有条目时的说明，只在标准档显示。 */
  emptyText?: string;
}

interface SecondaryRailProps {
  /** Accessible name of the navigation region, e.g. "Provider list". / 导航区的可访问名称，如「供应商列表」。 */
  label: string;
  groups: SecondaryRailGroup[];
  activeId: string | null;
  /** Replace the current history entry on switching, so browser "Back" does not step between entries. / 切换时替换当前历史记录，浏览器「后退」不在条目之间来回。 */
  replace?: boolean;
}

/**
 * Secondary rail of the full-bleed master-detail layout (shared by providers, endpoints and Agent memory); scrolls on its own.
 *
 * Switches between two forms by the width of `@container/page`, not the viewport:
 * - Standard tier (content area at least 64rem): 264px, two-line entries (name; status or count). With several groups there are Tabs at the top listing one group at a time,
 *   with counts on the Tabs; until switched manually the Tab follows the group of the selected item, and remembers once switched manually.
 * - Compact tier: 56px icon rail, name and second line shown on hover or focus, groups stacked vertically.
 *
 * Both forms are in the DOM and container queries show only one; the hidden one is `display: none` and not in the accessibility tree.
 * jsdom does not compute styles, so both are visible in Vitest: scope entry queries within `getByRole("tabpanel")`, or use `getAllBy*`.
 *
 * 全出血主从布局的二级栏（供应商、调用端点、Agent 记忆共用），自己滚动。
 *
 * 按 `@container/page` 的宽度切换两种形态，不读视口：
 * - 标准档（内容区不窄于 64rem）：264px，条目两行（名称；状态或数量）。多组时顶部是 Tab，每次只列一组，
 *   Tab 带数量；没有手动切换过时 Tab 跟随选中项所在的组，手动切换后记住。
 * - 紧凑档：56px 图标栏，悬停或聚焦显示名称与第二行，多组上下叠放。
 *
 * 两种形态都在 DOM 里，靠容器查询只显示一种；被隐藏的那份是 `display: none`，不进入可访问树。
 * jsdom 不计算样式，Vitest 中两份都可见：按条目查询时限定在 `getByRole("tabpanel")` 内，或用 `getAllBy*`。
 */
export function SecondaryRail({ label, groups, activeId, replace }: SecondaryRailProps) {
  const [chosenTab, setChosenTab] = useState<string | null>(null);
  const selectedGroup = groups.find(
    (group) => group.items.some((item) => item.id === activeId) || group.action?.id === activeId,
  );
  const tab = chosenTab ?? selectedGroup?.id ?? groups[0]?.id;

  return (
    <nav
      aria-label={label}
      className="relative w-14 shrink-0 overflow-y-auto border-r border-border @5xl/page:w-66"
    >
      <div className="hidden px-3 py-4 @5xl/page:block">
        {groups.length > 1 ? (
          <Tabs value={tab} onValueChange={(value: string) => setChosenTab(value)}>
            <TabsList className="w-full">
              {groups.map((group) => (
                <TabsTrigger key={group.id} value={group.id}>
                  {group.label}
                  <span className="tabular-nums">{group.items.length}</span>
                </TabsTrigger>
              ))}
            </TabsList>
            {groups.map((group) => (
              <TabsContent key={group.id} value={group.id}>
                <SecondaryRailList group={group} activeId={activeId} replace={replace} />
              </TabsContent>
            ))}
          </Tabs>
        ) : (
          groups[0] && <SecondaryRailList group={groups[0]} activeId={activeId} replace={replace} />
        )}
      </div>

      <div className="flex flex-col gap-3 px-2 py-3 @5xl/page:hidden">
        {groups.map((group, index) => (
          <ul
            key={group.id}
            aria-label={group.label}
            className={cn("flex flex-col items-center gap-1", index > 0 && "border-t border-border pt-3")}
          >
            {[...group.items, ...(group.action ? [group.action] : [])].map((item) => (
              <li key={item.id}>
                <CompactItem item={item} active={item.id === activeId} replace={replace} />
              </li>
            ))}
          </ul>
        ))}
      </div>
    </nav>
  );
}

/**
 * Standard-tier entry list: two-line entries (name; status or count) plus the group's trailing action entry; does not scroll by itself.
 * When a width-capped page has no room for a secondary rail (e.g. "Project memory" in project settings), use it alone to list entries, scrolled by the page body.
 *
 * 标准档的条目列表：两行条目（名称；状态或数量）加组末尾的动作条目，自身不滚动。
 * 限宽页里放不下二级栏时（如项目设置的「项目记忆」），单独用它列出条目，由页面主体滚动。
 */
export function SecondaryRailList({
  group,
  activeId,
  replace,
}: {
  group: SecondaryRailGroup;
  activeId: string | null;
  replace?: boolean;
}) {
  return (
    <ul aria-label={group.label} className="flex flex-col gap-0.5">
      {group.items.length === 0 && group.emptyText && (
        <li className="px-2.5 py-1 text-xs text-muted-foreground">{group.emptyText}</li>
      )}
      {[...group.items, ...(group.action ? [group.action] : [])].map((item) => {
        const active = item.id === activeId;
        return (
          <li key={item.id}>
            <Link
              href={item.href}
              replace={replace}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex min-h-8 items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                active
                  ? "bg-primary/15 text-foreground"
                  : "text-subtle-foreground hover:bg-muted/50 hover:text-foreground",
              )}
            >
              <span aria-hidden className="flex shrink-0 text-muted-foreground">
                {item.icon}
              </span>
              <span className="flex min-w-0 flex-1 flex-col">
                <TruncatedText text={item.label} focusable={false} />
                {item.description && (
                  <span className="truncate text-xs text-muted-foreground">{item.description}</span>
                )}
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function CompactItem({ item, active, replace }: { item: SecondaryRailItem; active: boolean; replace?: boolean }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Link
            href={item.href}
            replace={replace}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex size-10 items-center justify-center rounded-md transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
              active ? "bg-primary/15 text-foreground" : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
            )}
          />
        }
      >
        <span aria-hidden className="flex">
          {item.icon}
        </span>
        {/* The icon rail only shows icons; the name and second line are left for screen readers / 图标栏只显示图标，名称与第二行留给读屏 */}
        <span className="sr-only">{item.label}</span>
        {item.description && <span className="sr-only"> {item.description}</span>}
      </TooltipTrigger>
      <TooltipContent side="right">
        <span className="flex flex-col">
          <span>{item.label}</span>
          {item.description && <span className="text-background/70">{item.description}</span>}
        </span>
      </TooltipContent>
    </Tooltip>
  );
}
