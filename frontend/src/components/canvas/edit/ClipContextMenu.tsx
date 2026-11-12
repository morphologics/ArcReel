import { useCallback, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ArrowRight, Clapperboard, ParkingSquare, Pause, Play, Scissors } from "lucide-react";

import { API } from "@/api";
import { DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { useAppStore } from "@/stores/app-store";
import type { EditClip, EditTimelineReadout } from "@/types/edit-timeline";
import { errMsg } from "@/utils/async";

interface ClipMenuOptions {
  projectName: string;
  timelineId: string;
  timelineName: string;
  readout: EditTimelineReadout;
  playing: boolean;
  play: () => void;
  pause: () => void;
  seek: (t: number) => void;
  /** Open the clip's shot in the episode's Shot view; absent = no such entry. */
  onViewUnit?: (unitId: string) => void;
  /** The timeline changed (moved / left out / undone): read it again. */
  onChanged: () => void;
}

/** Clips with a move / leave-out in flight: a second click on the same clip is ignored until it settles. */
const pendingClips = new Set<string>();

/**
 * The right-click menu of a clip on the Edit view's timeline (and of the playhead's handle, for the clip under it),
 * like the shots strip's: Play from here (where the menu was opened) / Pause,
 * open the shot in the Shot view, move the clip left / right, leave it out of the cut (with Undo), Park.
 * Moves and leave-outs are timeline operations written as one new revision against the revision on screen.
 * Returns a stable item renderer for the tracks (they are memoised; the playhead re-renders the view every frame).
 */
export function useClipMenu({
  projectName,
  timelineId,
  timelineName,
  readout,
  playing,
  play,
  pause,
  seek,
  onViewUnit,
  onChanged,
}: ClipMenuOptions): (clip: EditClip, at: number | null, options?: { compact?: boolean }) => ReactNode {
  const { t } = useTranslation("dashboard");

  return useCallback(
    // `at`: the timeline time the menu was opened at (under the pointer, or the playhead's); Play starts there
    // `compact` (the video preview's menu): only Open in Shot view and Leave out of the cut
    (clip: EditClip, at: number | null, options?: { compact?: boolean }) => {
      const toast = useAppStore.getState().pushToast;
      const live = readout.clips.filter((item) => item.status !== "unit_deleted");
      const index = live.findIndex((item) => item.id === clip.id);

      // Written against the timeline as it is NOW, not the copy on screen: it is re-read after each change, and a
      // second action right after the first would otherwise carry a stale revision (refused as a conflict).
      // `operation` is built from the current clip order. Returns the revision before the change (for Undo).
      const write = async (
        summary: string,
        operation: (live: readonly EditClip[]) => Record<string, unknown> | null,
      ) => {
        if (pendingClips.has(clip.id)) return null;
        pendingClips.add(clip.id);
        try {
          const now = await API.getEditTimeline(projectName, timelineId);
          const op = operation(now.clips.filter((item) => item.status !== "unit_deleted"));
          if (op === null) {
            onChanged();
            return null;
          }
          await API.editEditTimeline(projectName, timelineId, {
            base_revision: now.revision,
            summary,
            operations: [op],
          });
          onChanged();
          return now.revision;
        } catch (err) {
          onChanged();
          toast(t("shot_leave_out_failed", { message: errMsg(err) }), "error");
          return null;
        } finally {
          pendingClips.delete(clip.id);
        }
      };

      // earlier: after the clip two places before (none = first); later: after the next clip. Nothing to do
      // when the clip is already at that end (or gone).
      const move = (direction: "earlier" | "later") => {
        const summary = t(direction === "earlier" ? "edit_clip_moved_earlier" : "edit_clip_moved_later", { clip: clip.id });
        void write(summary, (current) => {
          const at = current.findIndex((item) => item.id === clip.id);
          if (at < 0 || (direction === "earlier" ? at === 0 : at === current.length - 1)) return null;
          const after = direction === "earlier" ? (at >= 2 ? current[at - 2].id : null) : current[at + 1].id;
          return { op: "move", clip: clip.id, after };
        });
      };

      const leaveOut = async () => {
        const before = await write(t("shot_leave_out_summary", { id: clip.unit_id }), (current) =>
          current.some((item) => item.id === clip.id) ? { op: "delete", clip: clip.id } : null,
        );
        if (before === null) return;
        toast(t("shot_leave_out_done", { name: timelineName }), "success", {
          action: {
            label: t("shot_leave_out_undo"),
            onClick: () => {
              API.restoreEditTimeline(projectName, timelineId, before)
                .then(() => toast(t("shot_leave_out_undone", { name: timelineName }), "success"))
                .catch((err: unknown) => toast(t("shot_leave_out_failed", { message: errMsg(err) }), "error"))
                .finally(onChanged);
            },
          },
        });
      };

      const missing = index < 0;
      const shotViewItem = onViewUnit && (
        <DropdownMenuItem onClick={() => onViewUnit(clip.unit_id)}>
          <Clapperboard aria-hidden />
          {t("edit_clip_shot_view")}
        </DropdownMenuItem>
      );
      const leaveOutItem = (
        <DropdownMenuItem disabled={missing} onClick={() => void leaveOut()}>
          <Scissors aria-hidden />
          {t("shot_strip_leave_out")}
        </DropdownMenuItem>
      );
      if (options?.compact) {
        return (
          <>
            {shotViewItem}
            {leaveOutItem}
          </>
        );
      }
      return (
        <>
          {playing ? (
            <DropdownMenuItem onClick={pause}>
              <Pause aria-hidden />
              {t("edit_clip_pause")}
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem
              onClick={() => {
                seek(at ?? clip.start);
                play();
              }}
            >
              <Play aria-hidden />
              {t("edit_clip_play_from_here")}
            </DropdownMenuItem>
          )}
          {shotViewItem}
          <DropdownMenuSeparator />
          <DropdownMenuItem disabled={missing || index === 0} onClick={() => move("earlier")}>
            <ArrowLeft aria-hidden />
            {t("shot_strip_move_left")}
          </DropdownMenuItem>
          <DropdownMenuItem disabled={missing || index === live.length - 1} onClick={() => move("later")}>
            <ArrowRight aria-hidden />
            {t("shot_strip_move_right")}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {leaveOutItem}
          <DropdownMenuItem disabled>
            <ParkingSquare aria-hidden />
            {t("shot_strip_park")}
          </DropdownMenuItem>
        </>
      );
    },
    [projectName, timelineId, timelineName, readout, playing, play, pause, seek, onViewUnit, onChanged, t],
  );
}
