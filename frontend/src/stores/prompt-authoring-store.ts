import { create } from "zustand";

/** Scope of the "Write prompts" dialog: the current single entry, all pending authoring, or a custom selection. / 「编写提示词」弹窗的范围：当前单条、全部待编写、自选多条。 */
export type PromptAuthoringScope = "current" | "pending" | "custom";

export interface PromptAuthoringOpenRequest {
  projectName: string;
  episode: number;
  scope: PromptAuthoringScope;
  /** The entry currently selected on the page; only when given is "Current entry" offered. / 页面上当前选中的条目；给了才提供「当前单条」。 */
  currentEntryId?: string | null;
}

interface PromptAuthoringState {
  request: PromptAuthoringOpenRequest | null;
  open: (request: PromptAuthoringOpenRequest) => void;
  close: () => void;
}

/**
 * Open switch for the "Write prompts" dialog: the shot detail, timeline toolbar, reference-to-video canvas and the workflow status's next step
 * only register the scope to open; the dialog is rendered by the single host on the episode page.
 *
 * 「编写提示词」弹窗的开关：分镜详情、时间线工具栏、参考生视频画布与制作状态的下一步
 * 都只登记要打开的范围，弹窗由集页上唯一的宿主渲染。
 */
export const usePromptAuthoringStore = create<PromptAuthoringState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
