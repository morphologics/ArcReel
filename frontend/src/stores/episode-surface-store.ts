import { useEffect, useRef } from "react";
import { create } from "zustand";

/**
 * Areas on the episode page canvas that can be opened by name: script plan (content review and script plan drafts both live here),
 * reference-to-video prompt authoring drafts, and the episode source (the input box for episodes without source).
 *
 * 集页画布上可以被点名打开的区域：脚本规划（内容确认与脚本规划草稿都在这里），
 * 参考生视频的提示词编写草稿，以及集原文（无原文的集的填写框）。
 */
export type EpisodeSurface = "script_plan" | "prompt_authoring_draft" | "episode_source";

interface EpisodeSurfaceRequest {
  projectName: string;
  episode: number;
  surface: EpisodeSurface;
}

interface EpisodeSurfaceState {
  request: EpisodeSurfaceRequest | null;
  show: (request: EpisodeSurfaceRequest) => void;
}

/**
 * Delivery point for jumps like "Go confirm" and "Edit manually" in the workflow progress panel: the panel only registers which area to view,
 * and the canvas on the episode page switches to the matching tab. Every registration is a new object, so clicking the same place again switches again.
 *
 * 制作进度面板里「去确认」「手动修改」这类跳转的投递处：面板只登记要看的区域，
 * 集页上的画布切到对应的标签页。每次登记都是一个新对象，重复点同一处也会再切一次。
 */
export const useEpisodeSurfaceStore = create<EpisodeSurfaceState>((set) => ({
  request: null,
  show: (request) => set({ request: { ...request } }),
}));

/** Canvas subscription: calls `onShow` when this project, this episode and this area are named. / 画布订阅：点名本项目、本集、这块区域时调用 `onShow`。 */
export function useEpisodeSurfaceRequest(
  projectName: string,
  episode: number,
  surface: EpisodeSurface,
  onShow: () => void,
): void {
  const onShowRef = useRef(onShow);
  useEffect(() => {
    onShowRef.current = onShow;
  });
  useEffect(
    () =>
      useEpisodeSurfaceStore.subscribe((state, previous) => {
        const request = state.request;
        if (request === null || request === previous.request) return;
        if (request.projectName !== projectName || request.episode !== episode || request.surface !== surface) return;
        onShowRef.current();
      }),
    [projectName, episode, surface],
  );
}
