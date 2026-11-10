import {
  startTransition,
  useEffect,
  useMemo,
  useRef,
  type MouseEvent,
  type PointerEvent,
  type ReactNode,
  type WheelEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { ArrowDownUp } from "lucide-react";
import { useLocation, useSearchParams } from "wouter";
import { cn } from "cn";
import { API } from "@/api";
import { EPISODE_VIEW_PARAM, WORKSPACE_ROUTE_EPISODES } from "@/app-routes";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ItemPositionBadge } from "@/components/shared/episode-item/ItemPositionBadge";
import { statusFromAssets } from "@/components/canvas/timeline/shot-status";
import { StatusBadge } from "@/components/canvas/timeline/StatusBadge";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import type { EpisodeMeta } from "@/types";
import { episodeDisplayName } from "@/utils/episode-display";
import { getScriptItemId, getScriptItems, type EditorContentMode, type ScriptItem } from "@/utils/script-shape";
import { InsertShotButton, type InsertShotHandler } from "./ShotStructureActions";

const NO_EPISODES: EpisodeMeta[] = [];

/** Pointer travel (px) before a press on the strip counts as a drag-scroll rather than a click. */
const DRAG_THRESHOLD = 4;

interface ShotStripProps {
  /** Shots of the current episode (the live list, which may run ahead of the project snapshot). */
  segments: ScriptItem[];
  selectedIndex: number;
  onSelect: (index: number) => void;
  contentMode: EditorContentMode;
  projectName: string;
  /** The current episode; without it the strip shows this episode only. */
  episode?: number;
  aspectRatio?: "9:16" | "16:9";
  /** Append a shot to the end of the current episode; without it there is no add button. */
  onAppend?: InsertShotHandler;
  appendDisabled?: boolean;
  /** Switch to reorder mode (the vertical list with drag handles); without it there is no button. */
  onReorderMode?: () => void;
  /** Shown at the right end of the strip (the keyboard shortcut hint). */
  trailing?: ReactNode;
}

interface StripEpisode {
  episode: number;
  label: string;
  current: boolean;
  shots: ScriptItem[];
}

function sceneText(seg: ScriptItem): string {
  const prompt = seg.image_prompt;
  if (typeof prompt === "string") return prompt;
  if (prompt && typeof prompt === "object") {
    const scene = (prompt as { scene?: unknown }).scene;
    if (typeof scene === "string") return scene;
  }
  return "";
}

function shotText(seg: ScriptItem, mode: EditorContentMode): string {
  if (mode === "narration") return (seg as { novel_text?: string }).novel_text || sceneText(seg);
  if (mode === "ad") {
    const voiceover = (seg as { voiceover_text?: unknown }).voiceover_text;
    if (typeof voiceover === "string" && voiceover.trim()) return voiceover;
  }
  return sceneText(seg);
}

/**
 * Storyboard strip: every shot of the production in one horizontal row, episode after episode in airing
 * order, the episode boundaries as labelled markers. Episodes stay separate in storage; the strip only joins
 * them on screen. Picking a shot of the current episode selects it; picking one in another episode opens
 * that episode with the shot selected. Scroll with the wheel, by dragging with the mouse, or by swiping.
 */
export function ShotStrip({
  segments,
  selectedIndex,
  onSelect,
  contentMode,
  projectName,
  episode,
  aspectRatio = "16:9",
  onAppend,
  appendDisabled = false,
  onReorderMode,
  trailing,
}: ShotStripProps) {
  const { t } = useTranslation("dashboard");
  // The episode records themselves (not the display ledger): the strip needs each one's script file.
  const ledger = useProjectsStore((s) => s.currentProjectData?.episodes) ?? NO_EPISODES;
  const scripts = useProjectsStore((s) => s.currentScripts);
  const fingerprints = useProjectsStore((s) => s.assetFingerprints);
  const [, navigate] = useLocation();
  const [searchParams] = useSearchParams();
  const scrollRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; scrollLeft: number; moved: boolean } | null>(null);
  const portrait = aspectRatio === "9:16";

  const episodes = useMemo<StripEpisode[]>(() => {
    const own = (shots: ScriptItem[]): StripEpisode => ({
      episode: episode ?? 0,
      label: episode !== undefined ? episodeDisplayName(ledger, episode, t) : t("shots_section_title"),
      current: true,
      shots,
    });
    if (episode === undefined || !ledger.some((entry) => entry.episode === episode)) return [own(segments)];
    return ledger.map((entry) => {
      if (entry.episode === episode) return own(segments);
      const file = entry.script_file?.replace(/^scripts\//, "");
      return {
        episode: entry.episode,
        label: episodeDisplayName(ledger, entry.episode, t),
        current: false,
        shots: getScriptItems(file ? scripts[file] : null, contentMode),
      };
    });
  }, [episode, ledger, scripts, segments, contentMode, t]);

  const selectedId = segments[selectedIndex] ? getScriptItemId(segments[selectedIndex], contentMode) : null;

  // Keep the selected shot in view (centred) when it changes from the keyboard, the detail pane or a link.
  useEffect(() => {
    if (!selectedId) return;
    const node = scrollRef.current?.querySelector<HTMLElement>(
      `[data-strip-current="true"][data-shot-id="${CSS.escape(selectedId)}"]`,
    );
    node?.scrollIntoView?.({ inline: "center", block: "nearest" });
  }, [selectedId]);

  const openShot = (target: StripEpisode, shotId: string, index: number) => {
    if (target.current) {
      onSelect(index);
      return;
    }
    // Another episode: open it in the same view, and ask its canvas to select the shot once it has mounted.
    const view = searchParams.get(EPISODE_VIEW_PARAM);
    const query = view ? `?${new URLSearchParams({ [EPISODE_VIEW_PARAM]: view }).toString()}` : "";
    startTransition(() => navigate(`/${WORKSPACE_ROUTE_EPISODES}/${target.episode}${query}`));
    useAppStore.getState().triggerScrollTo({ type: "segment", id: shotId });
  };

  // Mouse drag scrolls the strip (touch and pens scroll natively). A press that moved past the threshold
  // is a drag, and the click it ends with is swallowed so it doesn't also pick a shot.
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== "mouse" || event.button !== 0 || !scrollRef.current) return;
    drag.current = { x: event.clientX, scrollLeft: scrollRef.current.scrollLeft, moved: false };
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    const box = scrollRef.current;
    if (!state || !box) return;
    const dx = event.clientX - state.x;
    if (!state.moved && Math.abs(dx) < DRAG_THRESHOLD) return;
    if (!state.moved) {
      state.moved = true;
      box.setPointerCapture(event.pointerId);
    }
    box.scrollLeft = state.scrollLeft - dx;
  };
  const onPointerUp = () => {
    // Keep `moved` until the click that follows has been swallowed.
    if (drag.current && !drag.current.moved) drag.current = null;
  };
  const onClickCapture = (event: MouseEvent) => {
    if (drag.current?.moved) {
      event.stopPropagation();
      event.preventDefault();
    }
    drag.current = null;
  };
  // A plain mouse wheel scrolls the strip sideways (it has nothing to scroll vertically).
  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    const box = scrollRef.current;
    if (!box || event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
    box.scrollLeft += event.deltaY;
  };

  return (
    <nav
      aria-label={t("shot_list_label")}
      className="flex min-w-0 shrink-0 items-stretch border-b border-border/50 bg-sidebar/40"
    >
      {onReorderMode ? (
        <div className="flex shrink-0 items-center border-r border-border/50 px-1.5">
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t("shot_strip_reorder")}
                  onClick={onReorderMode}
                />
              }
            >
              <ArrowDownUp aria-hidden />
            </TooltipTrigger>
            <TooltipContent>{t("shot_strip_reorder")}</TooltipContent>
          </Tooltip>
        </div>
      ) : null}
      <div
        ref={scrollRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onClickCapture={onClickCapture}
        onWheel={onWheel}
        className="relative flex min-w-0 flex-1 cursor-grab items-stretch gap-3 overflow-x-auto overflow-y-hidden px-3 py-2 select-none active:cursor-grabbing"
      >
        {episodes.map((group) => (
          <section
            key={group.episode}
            aria-label={group.label}
            data-strip-episode={group.episode}
            className={cn(
              "flex shrink-0 flex-col gap-1 border-l-2 pl-2",
              group.current ? "border-primary" : "border-border",
            )}
          >
            <h3
              className={cn(
                "max-w-60 truncate text-xs font-medium",
                group.current ? "text-foreground" : "text-muted-foreground",
              )}
              title={group.label}
            >
              {group.label}
            </h3>
            <ul className="flex items-start gap-1.5">
              {group.shots.length === 0 && !group.current ? (
                <li className="flex h-full items-center px-2 text-xs text-muted-foreground">
                  {t("shot_strip_no_script")}
                </li>
              ) : null}
              {group.shots.map((seg, index) => {
                const id = getScriptItemId(seg, contentMode);
                const active = group.current && index === selectedIndex;
                const imagePath = seg.generated_assets?.storyboard_image;
                const imageUrl = imagePath
                  ? API.getFileUrl(projectName, imagePath, fingerprints[imagePath] ?? null, { width: 320 })
                  : null;
                const text = shotText(seg, contentMode);
                return (
                  <li key={id} data-shot-id={id} data-strip-current={group.current ? "true" : undefined}>
                    <button
                      id={group.current ? `segment-${id}` : undefined}
                      type="button"
                      onClick={() => openShot(group, id, index)}
                      aria-current={active || undefined}
                      title={text || undefined}
                      className={cn(
                        "focus-ring flex flex-col gap-1 rounded-md p-1 text-left transition-colors hover:bg-muted/60",
                        active && "bg-primary/15 ring-2 ring-primary hover:bg-primary/15",
                        !group.current && "opacity-70 hover:opacity-100",
                      )}
                    >
                      <span
                        className={cn(
                          "relative block overflow-hidden rounded-sm bg-muted",
                          portrait ? "h-24 w-[54px]" : "h-[63px] w-28",
                        )}
                      >
                        {imageUrl ? (
                          <img
                            src={imageUrl}
                            alt=""
                            draggable={false}
                            className="size-full object-cover"
                            loading="lazy"
                            decoding="async"
                          />
                        ) : null}
                        <ItemPositionBadge position={index + 1} kind="shot" overlay />
                      </span>
                      <span className="flex items-center justify-between gap-1 text-xs text-muted-foreground">
                        <StatusBadge status={statusFromAssets(seg.generated_assets?.status)} />
                        <span className="num">
                          {t("duration_seconds_value_text", { value: seg.duration_seconds ?? 0 })}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
              {group.current && onAppend ? (
                <li className="flex self-center">
                  <InsertShotButton
                    afterId={null}
                    contentMode={contentMode}
                    onInsert={onAppend}
                    label={t("shot_append")}
                    disabled={appendDisabled}
                    variant="icon"
                  />
                </li>
              ) : null}
            </ul>
          </section>
        ))}
      </div>
      {trailing ? (
        <div className="flex shrink-0 flex-col justify-end border-l border-border/50">{trailing}</div>
      ) : null}
    </nav>
  );
}
