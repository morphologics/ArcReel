import { create } from "zustand";

import { API } from "@/api";
import { errMsg } from "@/utils/async";
import { outputTruncationOfError, type OutputTruncation } from "@/utils/output-truncation";

import { useProjectsStore } from "./projects-store";

/**
 * Reason a story setup generated from the source was not filled in place: `generate` means the generation request failed, with a way out when the output was truncated;
 * `refresh` means generation was saved to disk but fetching project data afterwards failed; cleared once that project's data reloads successfully.
 *
 * 从原文生成故事设定没有就地填入的原因：`generate` 是生成请求失败，输出被截断时附带出路；
 * `refresh` 是生成已落盘，之后取回项目数据失败，该项目的数据之后重新加载成功即清除。
 */
export type StoryGenerateError =
  | { kind: "generate"; message: string; truncation: OutputTruncation | null }
  | { kind: "refresh" };

interface OverviewGenerateState {
  /** Projects currently generating a story setup from the source. / 正在从原文生成故事设定的项目。 */
  generating: Record<string, true>;
  /** Reason each project's latest generation was not filled in place; cleared on the next generation. / 各项目最近一次生成没有就地填入的原因；再次生成时清除。 */
  errors: Record<string, StoryGenerateError>;
  generate: (projectName: string) => Promise<void>;
}

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

/**
 * Progress state of the overview's "Generate from source". Kept in the store rather than the overview component: if the user leaves the overview mid-generation the server still writes the result,
 * and coming back to the overview must keep showing loading and fill in place when done. The request is not cancelled on leaving; it is not restored after a page refresh.
 *
 * 概览「从原文生成」的进行状态。放在 store 而不是概览组件里：生成途中离开概览，服务端仍会写入结果，
 * 回到概览时要接着显示读取中，完成后就地填入。请求不随离开取消；页面刷新后不恢复。
 */
export const useOverviewGenerateStore = create<OverviewGenerateState>((set, get) => ({
  generating: {},
  errors: {},
  generate: async (projectName) => {
    if (get().generating[projectName]) return;
    set((s) => ({ generating: { ...s.generating, [projectName]: true }, errors: without(s.errors, projectName) }));
    let error: StoryGenerateError | null = null;
    try {
      await API.generateOverview(projectName);
      // refreshProject reports failure via its settlement value instead of rejecting: generation saved to disk but stuck on old content would tempt a second generation.
      // When switched to another project it settles as cancelled, and reloading on return to that project brings back the result.
      // refreshProject 以结算值报告失败而不 reject：生成已落盘却停在旧内容上，会引人再生成一次。
      // 已切到别的项目时结算为 cancelled，回到该项目时重新加载即带回结果。
      const refreshed = await useProjectsStore.getState().refreshProject(projectName);
      if (refreshed === "failed") error = { kind: "refresh" };
    } catch (err) {
      error = { kind: "generate", message: errMsg(err), truncation: outputTruncationOfError(err) };
    }
    set((s) => ({
      generating: without(s.generating, projectName),
      errors: error ? { ...s.errors, [projectName]: error } : s.errors,
    }));
  },
}));

// "Generation saved, fetch failed" only holds while the page is still on old data: once that project's data later reloads successfully (e.g. switching away and back),
// it already carries the generation result and the notice is cleared. The generation-failure notice is unrelated to data and stays until the next generation.
// 「生成已落盘、取回失败」只在页面还停在旧数据时成立：该项目的数据之后重新加载成功（如切走再回来），
// 已带上生成结果，提示随之清除。生成失败的提示与数据无关，保留到再次生成。
useProjectsStore.subscribe((state, prev) => {
  const projectName = state.currentProjectName;
  if (!projectName || !state.currentProjectData || state.currentProjectData === prev.currentProjectData) return;
  if (useOverviewGenerateStore.getState().errors[projectName]?.kind !== "refresh") return;
  useOverviewGenerateStore.setState((s) => ({ errors: without(s.errors, projectName) }));
});
