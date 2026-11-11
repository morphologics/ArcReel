import {
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type PointerEvent,
  type WheelEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { ArrowDownUp, ArrowLeft, ArrowRight, Eye, EyeOff, Film, ImageIcon, ParkingSquare, Scissors, Undo2 } from "lucide-react";
import { ContextMenu } from "@base-ui/react/context-menu";
import { useLocation, useSearchParams } from "wouter";
import { cn } from "cn";
import { API } from "@/api";
import { EPISODE_VIEW_PARAM, WORKSPACE_ROUTE_EPISODES } from "@/app-routes";
import { Button } from "@/components/ui/button";
import { DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ItemPositionBadge } from "@/components/shared/episode-item/ItemPositionBadge";
import { statusFromAssets } from "@/components/canvas/timeline/shot-status";
import { StatusBadge } from "@/components/canvas/timeline/StatusBadge";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { useShotMediaStore, type ShotMediaKind } from "@/stores/shot-media-store";
import { isResourceBusy } from "@/stores/tasks-store";
import type { EpisodeMeta } from "@/types";
import { episodeDisplayName } from "@/utils/episode-display";
import { getScriptItemId, getScriptItems, type EditorContentMode, type ScriptItem } from "@/utils/script-shape";
import { leaveShotOutOfCut, leaveShotOutOfLoadedCut, restoreShotToCut, useEpisodeCut } from "./leave-out-of-cut";
import { InsertShotButton, type InsertShotHandler } from "./ShotStructureActions";
import { ShotVersionsMenu } from "./ShotVersionsMenu";

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
  /** Move a shot of the current episode one place left / right (right-click menu); without it the items are off. */
  onMoveStep?: (shotId: string, direction: "earlier" | "later") => void;
  moveDisabled?: boolean;
  /** Generate / regenerate a shot's storyboard image or video (right-click menu, current episode). */
  onGenerateStoryboard?: (segmentId: string) => void;
  onGenerateVideo?: (segmentId: string) => void | Promise<void>;
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
  onMoveStep,
  moveDisabled = false,
  onGenerateStoryboard,
  onGenerateVideo,
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
  const [hidden, toggleHidden] = useHiddenShots(projectName);
  // The current episode's cut: shots left out of it show as a placeholder, like collapsed ones.
  const [cut, reloadCut, patchCut] = useEpisodeCut(projectName, episode);
  const shotOrder = useMemo(() => segments.map((seg) => getScriptItemId(seg, contentMode)), [segments, contentMode]);

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

  // Generate from the menu: the open shot goes through its detail pane (which saves unsaved edits first)
  const generate = (kind: ShotMediaKind, shotId: string) => {
    const open = useShotMediaStore.getState().openShot;
    if (open?.segmentId === shotId) {
      (kind === "storyboard" ? open.generateStoryboard : open.generateVideo)?.();
      return;
    }
    if (kind === "storyboard") onGenerateStoryboard?.(shotId);
    else void onGenerateVideo?.(shotId);
  };
  // A version picked in the menu: open the shot, preview the version in its pane (or show the pane's history)
  const showVersion = (target: StripEpisode, shotId: string, index: number, kind: ShotMediaKind, version: number | null) => {
    if (target.current) onSelect(index);
    else openShot(target, shotId, index);
    if (version === null) useShotMediaStore.getState().setMode(kind, "history");
    else useShotMediaStore.getState().view(kind, { segmentId: shotId, version });
  };

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
                const outOfCut = group.current && cut !== null && !cut.clipsByShot.has(id);
                const isHidden = hidden.has(id) || outOfCut;
                return (
                  <li key={id} data-shot-id={id} data-strip-current={group.current ? "true" : undefined}>
                    <ContextMenu.Root>
                      <ContextMenu.Trigger
                        render={<button type="button" />}
                        id={group.current ? `segment-${id}` : undefined}
                        onClick={() => openShot(group, id, index)}
                        aria-current={active || undefined}
                        title={outOfCut ? t("shot_out_of_cut_title", { name: cut?.name ?? "" }) : text || undefined}
                        className={cn(
                          "focus-ring flex flex-col gap-1 rounded-md p-1 text-left transition-colors hover:bg-muted/60",
                          active && "bg-primary/15 ring-2 ring-primary hover:bg-primary/15",
                          !group.current && "opacity-70 hover:opacity-100",
                        )}
                      >
                        {isHidden ? (
                          <span
                            className={cn(
                              "flex w-5 flex-col items-center justify-center gap-1 rounded-sm bg-muted/50 text-muted-foreground",
                              portrait ? "h-[118px]" : "h-[85px]",
                            )}
                          >
                            {outOfCut ? <Scissors aria-hidden className="size-3" /> : <EyeOff aria-hidden className="size-3" />}
                            <span className="num text-[10px]">{index + 1}</span>
                          </span>
                        ) : (
                          <>
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
                          </>
                        )}
                      </ContextMenu.Trigger>
                      {outOfCut && cut ? (
                        <DropdownMenuContent className="w-auto min-w-40">
                          <DropdownMenuItem
                            onClick={() => void restoreShotToCut(projectName, cut, id, shotOrder, t, reloadCut, patchCut)}
                          >
                            <Undo2 aria-hidden />
                            {t("shot_strip_restore_to_cut")}
                          </DropdownMenuItem>
                          <DropdownMenuItem disabled>
                            <ParkingSquare aria-hidden />
                            {t("shot_strip_park")}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      ) : (
                      <DropdownMenuContent className="w-auto min-w-40">
                        {group.current && (onGenerateStoryboard || onGenerateVideo) && (
                          <>
                            <DropdownMenuItem
                              disabled={!onGenerateStoryboard || isResourceBusy("storyboard", projectName, id)}
                              onClick={() => generate("storyboard", id)}
                            >
                              <ImageIcon aria-hidden />
                              {t(imagePath ? "media_regenerate_storyboard" : "media_generate_storyboard")}
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              disabled={!onGenerateVideo || !imagePath || isResourceBusy("video", projectName, id)}
                              onClick={() => generate("video", id)}
                            >
                              <Film aria-hidden />
                              {t(seg.generated_assets?.video_clip ? "media_regenerate_video" : "media_generate_video")}
                            </DropdownMenuItem>
                          </>
                        )}
                        <ShotVersionsMenu
                          projectName={projectName}
                          segmentId={id}
                          onPick={(kind, version) => showVersion(group, id, index, kind, version)}
                          onShowHistory={(kind) => showVersion(group, id, index, kind, null)}
                        />
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          disabled={!group.current || !onMoveStep || moveDisabled || index === 0}
                          onClick={() => onMoveStep?.(id, "earlier")}
                        >
                          <ArrowLeft aria-hidden />
                          {t("shot_strip_move_left")}
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          disabled={!group.current || !onMoveStep || moveDisabled || index === group.shots.length - 1}
                          onClick={() => onMoveStep?.(id, "later")}
                        >
                          <ArrowRight aria-hidden />
                          {t("shot_strip_move_right")}
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => toggleHidden(id)}>
                          {isHidden ? <Eye aria-hidden /> : <EyeOff aria-hidden />}
                          {t(isHidden ? "shot_strip_unhide" : "shot_strip_hide")}
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          disabled={!group.current || episode === undefined}
                          onClick={() => {
                            // the strip holds the cut already: show the change at once, write in the background
                            if (cut) void leaveShotOutOfLoadedCut(projectName, cut, id, t, patchCut, reloadCut);
                            else if (episode !== undefined) void leaveShotOutOfCut(projectName, episode, id, t, reloadCut);
                          }}
                        >
                          <Scissors aria-hidden />
                          {t("shot_strip_leave_out")}
                        </DropdownMenuItem>
                        <DropdownMenuItem disabled>
                          <ParkingSquare aria-hidden />
                          {t("shot_strip_park")}
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                      )}
                    </ContextMenu.Root>
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
    </nav>
  );
}

/**
 * Shots collapsed on the strip (a view setting, kept per project in this browser): a collapsed shot shows as a
 * narrow stub until it is expanded. Nothing in the script or the cut changes (leaving a shot out of the cut
 * is a separate, planned action).
 */
function useHiddenShots(projectName: string): [ReadonlySet<string>, (id: string) => void] {
  const key = `arcreel.strip.hidden.${projectName}`;
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => readHidden(key));
  const toggle = useCallback(
    (id: string) =>
      setHidden((current) => {
        const next = new Set(current);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        try {
          localStorage.setItem(key, JSON.stringify([...next]));
        } catch {
          // storage blocked: hidden only until the page reloads
        }
        return next;
      }),
    [key],
  );
  return [hidden, toggle];
}

function readHidden(key: string): ReadonlySet<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(key) ?? "[]") as unknown;
    return new Set(Array.isArray(raw) ? raw.filter((v): v is string => typeof v === "string") : []);
  } catch {
    return new Set();
  }
}
