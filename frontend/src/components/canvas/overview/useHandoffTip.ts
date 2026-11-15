import { create } from "zustand";

import { useAssistantStore } from "@/stores/assistant-store";

/**
 * One-time in-place tip after the story setting is extracted.
 *
 * Projects pending the tip and whether each project's last seen story setting was empty are kept in memory: the tip survives the overview unmounting on a view switch and returning,
 * and a story setting filled by background generation while away also counts as going from empty to content; it no longer appears after a page reload.
 * After "Got it" or sending the Agent a message in this project (including ones sent before the tip appeared) it is recorded per project in localStorage,
 * and this project is not tipped again.
 *
 * 故事设定提炼完成后的一次性就地提示。
 *
 * 待显示的项目与各项目上一次看到的故事设定是否为空都记在内存里：概览因切换视图卸载再回来时提示还在，
 * 离开期间后台生成填好的故事设定也算由空变为有内容；刷新页面后不再出现。
 * 点「知道了」或在这个项目里向 Agent 发出消息（提示出现之前发出的也算）后按项目记入 localStorage，
 * 之后这个项目不再提示。
 */

const DISMISSED_KEY_PREFIX = "arcreel_handoff_tip_dismissed:";

function readDismissed(projectName: string): boolean {
  try {
    return localStorage.getItem(DISMISSED_KEY_PREFIX + projectName) === "1";
  } catch {
    return false;
  }
}

interface HandoffTipState {
  /** Projects waiting to show the tip in this session. / 本次会话内等待显示提示的项目。 */
  pending: ReadonlySet<string>;
  /** Whether each project's last seen story setting was empty in this session. / 本次会话内各项目上一次看到的故事设定是否为空。 */
  seenEmpty: Readonly<Record<string, boolean>>;
  /** Record whether the seen story setting is empty: show when it goes from empty to content and the project never dismissed the tip. / 记下看到的故事设定是否为空：由空变为有内容且项目此前没有关闭过提示时显示。 */
  observe: (projectName: string, empty: boolean) => void;
  /** Dismiss the tip and remember per project; also remembered if the tip hasn't appeared yet, so it never appears. / 关闭提示并按项目记住；提示尚未出现时同样记住，之后不再出现。 */
  dismiss: (projectName: string) => void;
}

export const useHandoffTipStore = create<HandoffTipState>((set, get) => ({
  pending: new Set(),
  seenEmpty: {},
  observe: (projectName, empty) => {
    const wasEmpty = get().seenEmpty[projectName];
    if (wasEmpty === empty) return;
    set((state) => ({ seenEmpty: { ...state.seenEmpty, [projectName]: empty } }));
    if (!wasEmpty || empty || readDismissed(projectName)) return;
    set((state) => ({ pending: new Set(state.pending).add(projectName) }));
  },
  dismiss: (projectName) => {
    try {
      localStorage.setItem(DISMISSED_KEY_PREFIX + projectName, "1");
    } catch {
      // When localStorage is unavailable, dismiss only for this session / localStorage 不可用时只在本次会话内关闭
    }
    if (!get().pending.has(projectName)) return;
    set((state) => {
      const pending = new Set(state.pending);
      pending.delete(projectName);
      return { pending };
    });
  },
}));

// Sending the Agent a message in any view of the project dismisses and remembers that project's tip, including messages sent before the tip appeared:
// the subscription hangs on the store and doesn't unmount with the overview. Both send and rewrite set sending true; only the current project's session counts
// 在项目的任何视图里向 Agent 发出消息，都关闭并记住该项目的提示，提示出现之前发出的也算：
// 订阅挂在 store 上而不随概览卸载。发送与改写都会把 sending 置真；只认当前项目的会话
useAssistantStore.subscribe((state, prev) => {
  if (!state.sending || prev.sending || !state.currentProject) return;
  if (!readDismissed(state.currentProject)) useHandoffTipStore.getState().dismiss(state.currentProject);
});
