import { create } from "zustand";

export type ShotMediaKind = "storyboard" | "video";
export type ShotMediaMode = "preview" | "history";

/** A version shown in a shot's media pane instead of the current one. */
export interface ViewedVersion {
  segmentId: string;
  version: number;
}

/** Generate / regenerate a shot's media through the open shot (saving its unsaved edits first). */
export interface OpenShotActions {
  segmentId: string;
  generateStoryboard?: () => void;
  generateVideo?: () => void;
}

interface ShotMediaState {
  /** The pane shown when the media column is too narrow for both side by side (tabs). */
  tab: ShotMediaKind;
  /** Each pane's mode, kept while moving between shots. */
  mode: Record<ShotMediaKind, ShotMediaMode>;
  /** Version previewed in each pane; only for its shot (another shot shows its current version). */
  viewing: Record<ShotMediaKind, ViewedVersion | null>;
  openShot: OpenShotActions | null;
  setMode: (kind: ShotMediaKind, mode: ShotMediaMode) => void;
  /** Preview a version in the pane (switches it to Preview); null goes back to the current version. */
  view: (kind: ShotMediaKind, viewed: ViewedVersion | null) => void;
  setOpenShot: (actions: OpenShotActions | null) => void;
}

export const useShotMediaStore = create<ShotMediaState>((set) => ({
  tab: "storyboard",
  mode: { storyboard: "preview", video: "preview" },
  viewing: { storyboard: null, video: null },
  openShot: null,
  // switching a pane's mode or picking a version brings that pane forward
  setMode: (kind, mode) => set((s) => ({ tab: kind, mode: { ...s.mode, [kind]: mode } })),
  view: (kind, viewed) =>
    set((s) => ({ tab: kind, viewing: { ...s.viewing, [kind]: viewed }, mode: { ...s.mode, [kind]: "preview" } })),
  setOpenShot: (openShot) => set({ openShot }),
}));
