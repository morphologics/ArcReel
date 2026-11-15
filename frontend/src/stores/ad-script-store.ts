import { create } from "zustand";

export interface AdScriptOpenRequest {
  projectName: string;
  episode: number;
  /** Full redo: replace the existing final script; confirm the loss list before submitting. / 整份重做：替换已有的正式脚本，提交前先确认丢失清单。 */
  regenerate: boolean;
}

interface AdScriptState {
  request: AdScriptOpenRequest | null;
  open: (request: AdScriptOpenRequest) => void;
  close: () => void;
}

/**
 * Open switch for the ad/short "AI generate script" dialog: the empty state, toolbar and workflow status only register which episode to open and whether it is a full redo;
 * the dialog is rendered by the single host on the episode page.
 *
 * 广告/短片「AI 生成脚本」弹窗的开关：空状态、工具栏与制作状态只登记要打开的集和是否整份重做，
 * 弹窗由集页上唯一的宿主渲染。
 */
export const useAdScriptStore = create<AdScriptState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
