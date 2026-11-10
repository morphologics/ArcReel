import { useCallback, useEffect, useState } from "react";
import type { TFunction } from "i18next";

import { API } from "@/api";
import { pickDefaultTimeline } from "@/components/canvas/edit/timeline-view";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { errMsg } from "@/utils/async";

/** Shots with a leave-out / restore in flight: a second click on the same shot is ignored until it settles. */
const pendingShots = new Set<string>();

/**
 * The timeline as it is now (not the strip's copy, which can lag behind a write or an agent edit): the
 * revision to write against and the shot's clips in it.
 */
async function readCurrent(projectName: string, timelineId: string, shotId: string) {
  const readout = await API.getEditTimeline(projectName, timelineId);
  return {
    revision: readout.revision,
    shotClips: readout.clips.filter((clip) => clip.unit_id === shotId).map((clip) => clip.id),
    clipsByShot: readout.clips.reduce((map, clip) => {
      map.set(clip.unit_id, [...(map.get(clip.unit_id) ?? []), clip.id]);
      return map;
    }, new Map<string, string[]>()),
  };
}

/**
 * "Leave out of the cut": remove a shot's clips from the episode's current edit timeline (the one the Edit
 * view opens by default, i.e. the most recently edited) as one new revision, with Undo in the toast (a
 * rollback to the revision before). The shot itself and its media stay as they are.
 */
export async function leaveShotOutOfCut(
  projectName: string,
  episode: number,
  shotId: string,
  t: TFunction,
  onChanged?: () => void,
) {
  const toast = useAppStore.getState().pushToast;
  try {
    const { timelines } = await API.listEditTimelines(projectName, episode);
    const timelineId = pickDefaultTimeline(timelines);
    const timeline = timelines.find((item) => item.id === timelineId);
    if (!timelineId || !timeline) {
      toast(t("shot_leave_out_no_timeline"), "warning");
      return;
    }
    const readout = await API.getEditTimeline(projectName, timelineId);
    const clips = readout.clips.filter((clip) => clip.unit_id === shotId).map((clip) => clip.id);
    if (clips.length === 0) {
      toast(t("shot_leave_out_not_in_cut", { name: timeline.name }), "info");
      return;
    }
    const before = readout.revision;
    await API.editEditTimeline(projectName, timelineId, {
      base_revision: before,
      summary: t("shot_leave_out_summary", { id: shotId }),
      operations: clips.map((clip) => ({ op: "delete", clip })),
    });
    onChanged?.();
    toast(t("shot_leave_out_done", { name: timeline.name }), "success", {
      action: {
        label: t("shot_leave_out_undo"),
        onClick: () => {
          API.restoreEditTimeline(projectName, timelineId, before)
            .then(() => {
              onChanged?.();
              toast(t("shot_leave_out_undone", { name: timeline.name }), "success");
            })
            .catch((err: unknown) => toast(t("shot_leave_out_failed", { message: errMsg(err) }), "error"));
        },
      },
    });
  } catch (err) {
    toast(t("shot_leave_out_failed", { message: errMsg(err) }), "error");
  }
}

/** The episode's current cut as the strip needs it: which shots have clips in it, in playing order. */
export interface EpisodeCut {
  timelineId: string;
  name: string;
  revision: number;
  /** clip IDs per shot (unit) ID */
  clipsByShot: ReadonlyMap<string, readonly string[]>;
}

/**
 * The episode's current edit timeline (most recently edited), re-read after every project change; null while
 * loading, when the episode has none, or when it can't be read (the strip then shows every shot normally).
 */
export function useEpisodeCut(
  projectName: string,
  episode: number | undefined,
): [EpisodeCut | null, () => void, (patch: (cut: EpisodeCut) => EpisodeCut) => void] {
  const snapshotRevision = useProjectsStore((s) => s.projectSnapshotRevisions[projectName] ?? 0);
  const [reloads, setReloads] = useState(0);
  const [cut, setCut] = useState<EpisodeCut | null>(null);
  useEffect(() => {
    if (episode === undefined) return;
    const controller = new AbortController();
    const { signal } = controller;
    (async () => {
      const { timelines } = await API.listEditTimelines(projectName, episode, { signal });
      const timelineId = pickDefaultTimeline(timelines);
      const timeline = timelines.find((item) => item.id === timelineId);
      if (!timelineId || !timeline) return null;
      const readout = await API.getEditTimeline(projectName, timelineId, { signal });
      const clipsByShot = new Map<string, string[]>();
      for (const clip of readout.clips) {
        clipsByShot.set(clip.unit_id, [...(clipsByShot.get(clip.unit_id) ?? []), clip.id]);
      }
      return { timelineId, name: timeline.name, revision: readout.revision, clipsByShot };
    })()
      .then((value) => {
        if (!signal.aborted) setCut(value);
      })
      .catch(() => {
        if (!signal.aborted) setCut(null);
      });
    return () => controller.abort();
  }, [projectName, episode, snapshotRevision, reloads]);
  const reload = useCallback(() => setReloads((n) => n + 1), []);
  // Optimistic change shown at once while the write runs; the reload after it puts the real cut back.
  const patch = useCallback(
    (change: (cut: EpisodeCut) => EpisodeCut) => setCut((current) => (current ? change(current) : current)),
    [],
  );
  return [episode === undefined ? null : cut, reload, patch];
}

/**
 * "Restore to cut": insert the shot back into the current cut, after the clips of the nearest earlier shot
 * (script order) that is in the cut, or at the start. It comes back whole (untrimmed, default volume): the
 * toast's Undo right after leaving it out is the way to get its earlier trim back.
 */
export async function restoreShotToCut(
  projectName: string,
  cut: EpisodeCut,
  shotId: string,
  shotOrder: readonly string[],
  t: TFunction,
  onChanged?: () => void,
  patch?: (change: (cut: EpisodeCut) => EpisodeCut) => void,
) {
  const toast = useAppStore.getState().pushToast;
  if (pendingShots.has(shotId)) return;
  pendingShots.add(shotId);
  patch?.((current) => withShot(current, shotId));
  try {
    // the live timeline, not the strip's copy: a shot that is already back in (a second click, Undo, the
    // agent) is not inserted twice
    const now = await readCurrent(projectName, cut.timelineId, shotId);
    if (now.shotClips.length > 0) {
      onChanged?.();
      return;
    }
    const index = shotOrder.indexOf(shotId);
    let after: string | null = null;
    for (let i = index - 1; i >= 0; i--) {
      const clips = now.clipsByShot.get(shotOrder[i]);
      if (clips && clips.length > 0) {
        after = clips[clips.length - 1];
        break;
      }
    }
    await API.editEditTimeline(projectName, cut.timelineId, {
      base_revision: now.revision,
      summary: t("shot_restore_summary", { id: shotId }),
      operations: [{ op: "insert", unit_id: shotId, after }],
    });
    onChanged?.();
    toast(t("shot_leave_out_undone", { name: cut.name }), "success");
  } catch (err) {
    onChanged?.();
    toast(t("shot_leave_out_failed", { message: errMsg(err) }), "error");
  } finally {
    pendingShots.delete(shotId);
  }
}

/** The cut with a shot's clips taken out (optimistic "leave out"). */
export function withoutShot(cut: EpisodeCut, shotId: string): EpisodeCut {
  const clipsByShot = new Map(cut.clipsByShot);
  clipsByShot.delete(shotId);
  return { ...cut, clipsByShot };
}

/** The cut with a shot marked as in it (optimistic "restore"; the real clip ID comes with the reload). */
export function withShot(cut: EpisodeCut, shotId: string): EpisodeCut {
  const clipsByShot = new Map(cut.clipsByShot);
  clipsByShot.set(shotId, []);
  return { ...cut, clipsByShot };
}

/**
 * "Leave out of the cut" from the strip, which already holds the cut: no list / read round trip first,
 * and the placeholder shows at once (`patch`); a failed write puts the shot back (`reload`).
 */
export async function leaveShotOutOfLoadedCut(
  projectName: string,
  cut: EpisodeCut,
  shotId: string,
  t: TFunction,
  patch: (change: (cut: EpisodeCut) => EpisodeCut) => void,
  reload: () => void,
) {
  const toast = useAppStore.getState().pushToast;
  if (pendingShots.has(shotId)) return;
  pendingShots.add(shotId);
  patch((current) => withoutShot(current, shotId));
  let before = cut.revision;
  try {
    const now = await readCurrent(projectName, cut.timelineId, shotId);
    before = now.revision;
    if (now.shotClips.length === 0) {
      reload();
      toast(t("shot_leave_out_not_in_cut", { name: cut.name }), "info");
      return;
    }
    await API.editEditTimeline(projectName, cut.timelineId, {
      base_revision: now.revision,
      summary: t("shot_leave_out_summary", { id: shotId }),
      operations: now.shotClips.map((clip) => ({ op: "delete", clip })),
    });
    reload();
    toast(t("shot_leave_out_done", { name: cut.name }), "success", {
      action: {
        label: t("shot_leave_out_undo"),
        onClick: () => {
          patch((current) => withShot(current, shotId));
          API.restoreEditTimeline(projectName, cut.timelineId, before)
            .then(() => toast(t("shot_leave_out_undone", { name: cut.name }), "success"))
            .catch((err: unknown) => toast(t("shot_leave_out_failed", { message: errMsg(err) }), "error"))
            .finally(reload);
        },
      },
    });
  } catch (err) {
    reload();
    toast(t("shot_leave_out_failed", { message: errMsg(err) }), "error");
  } finally {
    pendingShots.delete(shotId);
  }
}
