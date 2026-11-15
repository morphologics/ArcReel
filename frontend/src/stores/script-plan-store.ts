import { create } from "zustand";

/**
 * Existing content a replan would replace (the first plan of an episode with no content yet goes through the episode page's starter area, not here):
 * - `formal_script`: no script plan, but a final script already exists (e.g. started from blank); the new plan awaits confirmation, and confirming goes through the final script overwrite confirmation;
 * - `confirmed_plan`: the plan is confirmed; the new plan awaits confirmation, and confirming goes through the final script overwrite confirmation;
 * - `pending_plan`: an unconfirmed plan, replaced whole; edits on the content review page are lost;
 * - `draft`: an invalid draft, replaced whole; edits on the draft are lost.
 *
 * 重新规划会替换掉的现有内容（本集还没有任何内容时的首次规划走集页起步区，不经这里）：
 * - `formal_script`：没有脚本规划，但已有正式脚本（如从空白开始），新规划待确认，确认时再走正式脚本的覆盖确认；
 * - `confirmed_plan`：规划已确认，新规划待确认，确认时再走正式脚本的覆盖确认；
 * - `pending_plan`：未确认的规划，整份替换，内容确认页上的修改会丢失；
 * - `draft`：待修复草稿，整份替换，草稿上的修改会丢失。
 */
export type ScriptPlanReplacement = "formal_script" | "confirmed_plan" | "pending_plan" | "draft";

export interface ScriptPlanOpenRequest {
  projectName: string;
  episode: number;
  replaces: ScriptPlanReplacement;
}

interface ScriptPlanState {
  request: ScriptPlanOpenRequest | null;
  open: (request: ScriptPlanOpenRequest) => void;
  close: () => void;
}

/**
 * Open switch for the "AI plan script" dialog: the episode page, content review page and the workflow status's next step only register which episode to open and what content would be replaced;
 * the dialog is rendered by the single host on the episode page.
 *
 * 「AI 规划脚本」弹窗的开关：集页、内容确认页与制作状态的下一步只登记要打开的集和会替换的内容，
 * 弹窗由集页上唯一的宿主渲染。
 */
export const useScriptPlanStore = create<ScriptPlanState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
