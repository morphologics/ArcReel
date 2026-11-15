import { useEffect, useId, useState } from "react";
import { AlertTriangle, BadgeCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { TruncatedText } from "@/components/shared/TruncatedText";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { MarketEntry, MarketEntryAggregate, MarketSourceKind } from "@/types";
import { MarketEntryStats } from "./MarketEntryStats";
import { MarketInstallBadges } from "./MarketInstallBadges";

const ICON_SIZE = 40;

/** Fetches the icon through the backend proxy and turns it into an object URL; returns null when unavailable, and the caller shows a first-letter placeholder. / 经后端代理取 icon 转成 object URL；取不到时返回 null，由调用方显示首字占位。 */
function useEntryIconUrl(entry: MarketEntry): string | null {
  const [url, setUrl] = useState<string | null>(null);
  const { source_id: sourceId, slug, version, icon } = entry;

  useEffect(() => {
    if (icon === null) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    API.getMarketEntryIcon(sourceId, slug, version, { signal: controller.signal })
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        // An unavailable icon doesn't affect browsing: keep the first-letter placeholder. / icon 不可用不影响浏览：保持首字占位。
      });
    return () => {
      controller.abort();
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
      setUrl(null);
    };
  }, [icon, slug, sourceId, version]);

  return url;
}

export function EntryIcon({ entry }: { entry: MarketEntry }) {
  const url = useEntryIconUrl(entry);
  if (url !== null) {
    return <img src={url} alt="" width={ICON_SIZE} height={ICON_SIZE} className="size-10 shrink-0 rounded-md object-contain" />;
  }
  return (
    <div
      aria-hidden
      className="flex size-10 shrink-0 items-center justify-center rounded-md bg-muted text-lg font-medium text-muted-foreground"
    >
      {entry.name.trim().charAt(0).toUpperCase()}
    </div>
  );
}

/** Which market source the entry comes from; the official source has a marker before it, read by screen readers as "Official". / 条目来自哪个市场源；官方源前带标记，读屏读出「官方」。 */
export function SourceChip({ name, kind }: { name: string; kind: MarketSourceKind | null }) {
  const { t } = useTranslation("dashboard");
  return (
    <span className="relative inline-flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
      {kind === "official" && (
        <BadgeCheck className="size-3.5 shrink-0 text-primary" role="img" aria-label={t("market_source_official")} />
      )}
      <TruncatedText text={name} />
    </span>
  );
}

/**
 * Market entry card: name (opens the install dialog; the click area covers the whole card), author and version, two-line description, media type and install status,
 * source (plus install count and rating from the official service) and the primary button.
 * Primary button: not installed "Install", updatable "Update" open the install dialog; when already latest, "Open endpoint" jumps straight to the call endpoint.
 * When the current app version doesn't satisfy `min_app_version`, the primary button is disabled and the version requirement shown; details can still be opened.
 *
 * 市场条目卡片：名称（点开安装弹窗，点击区域覆盖整张卡片）、作者与版本、两行说明、媒体类型与安装状态、
 * 来源（及官方服务的安装量与评分）和主按钮。
 * 主按钮：未安装「安装」、可更新「更新」打开安装弹窗；已是最新时「打开端点」直接跳到调用端点。
 * 当前应用版本不满足 `min_app_version` 时主按钮禁用并标出版本要求，详情仍可打开。
 */
export function MarketEntryCard({
  entry,
  sourceName,
  sourceKind,
  aggregate = null,
  onOpen,
  onInstalledOpen,
}: {
  entry: MarketEntry;
  /** Current display name of the source; falls back to the entry's own display name when not found in the source list. / 源的当前显示名；源列表里找不到时退回条目自带的显示名。 */
  sourceName: string;
  sourceKind: MarketSourceKind | null;
  /** Only present when the official service is on and aggregates were fetched; when missing, no numbers are shown. / 官方服务开启且取到聚合时才有；缺失即不显示数字。 */
  aggregate?: MarketEntryAggregate | null;
  onOpen: () => void;
  onInstalledOpen: () => void;
}) {
  const { t } = useTranslation("dashboard");
  const titleId = useId();
  const unmet = !entry.min_app_version_satisfied && entry.min_app_version !== null;
  const { installation } = entry;
  const current = installation?.state === "current";
  const media = entry.media_type === "image" || entry.media_type === "video" ? entry.media_type : null;

  return (
    <article
      aria-labelledby={titleId}
      className="relative flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-card p-4 transition-colors hover:border-input"
    >
      <div className="flex min-w-0 items-start gap-3">
        <EntryIcon entry={entry} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <h3 id={titleId} className="text-sm font-medium">
            {/* The name button's pseudo-element covers the whole card, so clicking any blank area opens the detail; the primary button is later in the DOM and positioned itself, sitting on top of it. / 名称按钮的伪元素铺满整张卡片，点卡片任意空白处都打开详情；主按钮在 DOM 中靠后、自身定位，盖在它上面。 */}
            <button
              type="button"
              onClick={onOpen}
              className="line-clamp-2 text-left outline-none after:absolute after:inset-0 after:rounded-lg focus-visible:after:ring-3 focus-visible:after:ring-ring/50"
            >
              {entry.name}
            </button>
          </h3>
          <TruncatedText
            text={t("market_entry_byline", { author: entry.author, version: entry.version })}
            className="text-xs text-muted-foreground"
            focusable={false}
          />
        </div>
      </div>
      {entry.description && <p className="line-clamp-2 text-sm text-subtle-foreground">{entry.description}</p>}
      {(media || installation || unmet) && (
        <div className="flex flex-wrap items-center gap-1">
          {media && <Badge variant="secondary">{t(media === "image" ? "media_type_image" : "media_type_video")}</Badge>}
          {installation && <MarketInstallBadges state={installation.state} modified={installation.modified} />}
          {unmet && (
            <Badge variant="outline">
              <AlertTriangle data-icon="inline-start" className="text-warn" aria-hidden />
              {t("market_requires_app", { version: entry.min_app_version })}
            </Badge>
          )}
        </div>
      )}
      <div className="mt-auto flex min-w-0 items-center gap-3">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <SourceChip name={sourceName} kind={sourceKind} />
          {aggregate && <MarketEntryStats aggregate={aggregate} />}
        </div>
        <Button
          variant={current ? "ghost" : "outline"}
          size="sm"
          disabled={unmet && !current}
          onClick={current ? onInstalledOpen : onOpen}
          className="relative shrink-0"
        >
          {t(current ? "market_open_endpoint" : installation ? "market_update" : "market_install")}
        </Button>
      </div>
    </article>
  );
}
