import type { ReactNode } from "react";
import { ChevronLeft } from "lucide-react";

import { TruncatedText } from "@/components/shared/TruncatedText";
import { Button } from "@/components/ui/button";

interface PageHeaderProps {
  /** "Back" button; to return to where you were before entering the page, get `onClick` from `useReturnTo`. / 「返回」按钮；返回到进入页面之前的位置时，用 `useReturnTo` 取得 `onClick`。 */
  back?: { label: string; onClick: () => void };
  title?: string;
  subtitle?: string;
  /** Custom content after the title group, e.g. the project lobby's brand and search. / 标题组之后的自定义内容，如项目大厅的品牌与搜索。 */
  children?: ReactNode;
  /** Page-level actions on the right. / 靠右的页面级动作。 */
  actions?: ReactNode;
}

/**
 * Top bar of the page shell, 56px tall: "Back | Title | Subtitle" grouped on the left, with the back arrow on the same vertical line as the sidebar icons,
 * and page-level actions on the right.
 *
 * 页面外壳的顶栏，高 56px：「返回 | 标题 | 副标题」成组靠左，返回箭头与侧栏图标在同一条竖线上，
 * 页面级动作靠右。
 */
export function PageHeader({ back, title, subtitle, children, actions }: PageHeaderProps) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border pr-6 pl-3 xl:pr-8">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        {back && (
          <>
            <Button variant="ghost" onClick={back.onClick}>
              <ChevronLeft aria-hidden data-icon="inline-start" />
              {back.label}
            </Button>
            <span aria-hidden className="h-4 w-px shrink-0 bg-border" />
          </>
        )}
        {title && (
          <h1 className="min-w-0 text-base font-medium">
            <TruncatedText text={title} />
          </h1>
        )}
        {subtitle && <TruncatedText text={subtitle} className="text-sm text-muted-foreground" />}
        {children}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
