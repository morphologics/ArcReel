import { useId, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { MEDIA_SIDE_BY_SIDE_QUERY, useMediaQuery } from "@/hooks/useMediaQuery";
import { useShotMediaStore } from "@/stores/shot-media-store";

/**
 * 分镜详情的栏位：页头在上，中间是「编辑栏 + 媒体栏」两栏，各自滚动，未保存提示条在最下方。
 * 不做拖拽调宽，按分镜详情自身的宽度切换：窄于 480px 时（如演示工作台在紧凑档里，Agent 面板
 * 不覆盖画布）上下排成一栏、整体滚动；480–860px 编辑栏占满、媒体栏 300px；
 * 再宽时编辑栏 360–560px，多出的宽度全部给媒体栏。
 */
export function ShotDetailLayout({
  header,
  footer,
  main,
  media,
}: {
  header: ReactNode;
  /** 两栏下方横跨整宽的未保存提示条：出现时不推动正在编辑的字段。 */
  footer?: ReactNode;
  main: ReactNode;
  media: ReactNode;
}) {
  return (
    <div className="flex min-h-0 min-w-0 flex-col">
      {header}
      <div className="@container/shot-detail flex min-h-0 flex-1 flex-col">
        <div className="relative grid min-h-0 flex-1 auto-rows-max grid-cols-[minmax(0,1fr)] content-start overflow-y-auto [scrollbar-gutter:stable] @min-[480px]/shot-detail:grid-cols-[minmax(0,1fr)_300px] @min-[480px]/shot-detail:grid-rows-[minmax(0,1fr)] @min-[480px]/shot-detail:content-stretch @min-[480px]/shot-detail:overflow-visible @min-[860px]/shot-detail:grid-cols-[minmax(360px,560px)_minmax(0,1fr)]">
          <div className="relative min-h-0 @min-[480px]/shot-detail:overflow-y-auto @min-[480px]/shot-detail:[scrollbar-gutter:stable]">
            <div className="flex flex-col gap-3 px-5 pt-2 pb-8">{main}</div>
          </div>
          <div className="@container/shot-media relative min-h-0 border-t border-border/50 px-4 pt-4 pb-8 @min-[480px]/shot-detail:overflow-y-auto @min-[480px]/shot-detail:border-t-0 @min-[480px]/shot-detail:border-l">
            {media}
          </div>
        </div>
      </div>
      {footer}
    </div>
  );
}

/** 编辑栏里的一组内容；组与组之间用分隔线隔开。 */
export function ShotGroup({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col gap-4 border-t border-border/50 pt-4 first:border-t-0 first:pt-0">{children}</div>
  );
}

/**
 * 编辑栏里的一段：小标题加内容。给了 `htmlFor` 时标题是对应输入框的 label，
 * 否则是一个三级标题；`actions` 放在标题行右侧。
 */
export function ShotSection({
  title,
  icon,
  htmlFor,
  afterTitle,
  actions,
  children,
}: {
  title: string;
  icon?: ReactNode;
  htmlFor?: string;
  /** Right next to the title (e.g. an icon button that belongs to it); `actions` sit at the far right. */
  afterTitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const titleId = useId();
  const heading = htmlFor ? (
    <label id={titleId} htmlFor={htmlFor} className="text-xs font-medium text-muted-foreground">
      {title}
    </label>
  ) : (
    <h3 id={titleId} className="text-xs font-medium text-muted-foreground">
      {title}
    </h3>
  );
  return (
    <section aria-labelledby={titleId} className="flex min-w-0 flex-col gap-2">
      <div className="flex min-h-7 items-center gap-1.5">
        {icon ? <span className="flex text-muted-foreground">{icon}</span> : null}
        {heading}
        {afterTitle}
        {actions ? <div className="ml-auto flex items-center gap-1">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** 默认折叠的「对应原文与参考」。 */
export function ShotSourceCollapsible({ children }: { children: ReactNode }) {
  const { t } = useTranslation("dashboard");
  return (
    <Collapsible>
      <CollapsibleTrigger render={<Button variant="ghost" size="sm" className="-ml-2" />}>
        <ChevronRight aria-hidden data-icon="inline-start" className="transition-transform group-aria-expanded/button:rotate-90" />
        {t("detail_section_source_and_refs")}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="flex flex-col gap-4 pt-3">{children}</div>
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * 媒体栏：屏幕 1600px 及以上时分镜图与视频并排，否则一次只显示一个，
 * 由卡片标题行切换（标题本身与两个视图图标）；配音卡在下方横跨整栏。
 */
export function ShotMediaGrid({
  storyboard,
  video,
  audio,
}: {
  aspectRatio: "9:16" | "16:9";
  storyboard: ReactNode;
  video: ReactNode;
  audio?: ReactNode;
}) {
  // Side by side only on screens 1600px and wider; narrower, one at a time, switched from the cards' title rows
  // (no scrolling between them)
  const sideBySide = useMediaQuery(MEDIA_SIDE_BY_SIDE_QUERY);
  const tab = useShotMediaStore((s) => s.tab);
  return (
    <div className={cn("grid items-start gap-x-4 gap-y-5", sideBySide && "grid-cols-2")}>
      <div className={cn("min-w-0", !sideBySide && tab !== "storyboard" && "hidden")}>{storyboard}</div>
      <div className={cn("flex min-w-0 flex-col", !sideBySide && tab !== "video" && "hidden")}>{video}</div>
      {audio ? <div className={cn("min-w-0", sideBySide && "col-span-2")}>{audio}</div> : null}
    </div>
  );
}
