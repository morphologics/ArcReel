import { create } from "zustand";
import { API } from "@/api";

interface OnboardingState {
  /** Backend "seen" flag; null = not queried yet / 后端「已看过」标记；null = 尚未查询 */
  seen: boolean | null;
  /**
   * Whether `seen: true` has been confirmed by the backend (GET returned true, or a POST succeeded).
   * A failed query also sets `seen` true to suppress auto-popup this time, but that does not count as confirmed;
   * this field separates the two meanings of "true" so `exit()` does not mistake the former for no longer needing a write.
   *
   * `seen: true` 是否已被后端确认过(GET 返回过 true，或 POST 成功过)。
   * 查询失败时也会把 `seen` 置真用于本次抑制自动弹出，但那不算确认——
   * 靠这个字段把两种「true」的含义分开，避免 `exit()` 把前者误判成不必再写。
   */
  seenConfirmed: boolean;
  /** Whether the guide is running / 引导是否正在运行 */
  active: boolean;
  /** Fetch "seen or not". On failure treat as seen: better not to pop up than to pop up wrongly. / 拉取「是否已看过」。失败时按已看过处置，宁可不弹也不误弹。 */
  loadStatus: (options?: { signal?: AbortSignal }) => Promise<void>;
  /** Open the guide. The automatic first popup and "Replay guide" share this path. / 打开引导。自动首弹与「重看引导」共用这一条路径。 */
  start: () => void;
  /** Any exit path (skip / close / finish). Marks seen on the first exit; a replay does not write again. / 任一退出路径（跳过 / 关闭 / 走完）。首次退出时标记已看；重看不重复写。 */
  exit: () => void;
}

export const useOnboardingStore = create<OnboardingState>((set, get) => ({
  seen: null,
  seenConfirmed: false,
  active: false,

  loadStatus: async (options = {}) => {
    try {
      const { seen } = await API.getOnboardingStatus({ signal: options.signal });
      if (options.signal?.aborted) return;
      // A late query result must not roll back an established "seen": the user may have finished and exited a round of the guide
      // while this query was in flight (the local exit() already wrote seen: true).
      // 迟到的查询结果不得回退已确立的「已看过」——用户可能在这次查询飞行途中
      // 已经跑完并退出过一轮引导(本地 exit() 早已写入 seen: true)。
      if (get().seen === true) return;
      set({ seen, seenConfirmed: seen });
    } catch (err) {
      if (options.signal?.aborted) return;
      // Backend jitter should not show the guide again to existing users. Only suppress for this session and re-judge after refresh;
      // it does not count as backend confirmation, so a later exit() still really writes once.
      // 后端抖动时不该给老用户重复弹引导。仅抑制本次会话，刷新后重新判定；
      // 不算后端确认，后续 exit() 仍要真正写一次。
      console.warn("[onboarding] status fetch failed; suppressing auto-start", err);
      set({ seen: true });
    }
  },

  start: () => {
    if (get().active) return;
    set({ active: true });
  },

  exit: () => {
    const { active, seenConfirmed } = get();
    if (!active) return;
    set({ active: false, seen: true });
    // Writing again once the backend has confirmed "seen" is pointless: the guide allows only this one write operation,
    // so skip it when possible. A local temporary suppression caused by a failed query does not count as confirmation and still writes.
    // 后端已确认过「已看过」时再写一次没有意义 —— 引导全程只允许这一个写操作，
    // 能省则省。查询失败导致的本地临时抑制不算确认，仍要写。
    if (seenConfirmed) return;
    void API.markOnboardingSeen()
      .then(() => set({ seenConfirmed: true }))
      .catch((err) => {
        console.warn("[onboarding] failed to mark tour as seen", err);
      });
  },
}));
