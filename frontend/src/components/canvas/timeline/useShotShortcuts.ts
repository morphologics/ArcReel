import type { RefObject } from "react";
import { useHotkeys } from "@/preferences/hotkeys";

export { isApplePlatform } from "@/preferences/hotkeys";

interface ShotShortcutsOptions {
  /** Shortcuts only work when focus is inside this container (or the page has no focused element); they do not steal keys from the Agent panel, header or popovers. / 快捷键只在焦点位于这个容器内（或页面没有焦点元素）时生效，不抢 Agent 面板、页头与弹层的按键。 */
  rootRef: RefObject<HTMLElement | null>;
  onPrev: () => void;
  onNext: () => void;
  /** Save the current shot; when passed, its key is intercepted page-wide; when not passed, nothing is intercepted. / 保存当前分镜；传入时整页拦下它的按键，不传时不拦截。 */
  onSave?: () => void;
  /** Shot switching is temporarily unavailable (reorder, add/remove in flight). / 切换分镜暂不可用（改序、增删在途）。 */
  navDisabled: boolean;
}

/**
 * Storyboard shortcuts on the hot key registry (`storyboard` scope; defaults J / K switch to the previous and next
 * shot, ⌘S / Ctrl+S saves the current shot, also inside text fields). Bindings come from this device's Hot keys
 * settings; the rules (popovers, IME composition, typing targets, modifiers) are the dispatcher's, see `useHotkeys`.
 */
export function useShotShortcuts({ rootRef, onPrev, onNext, onSave, navDisabled }: ShotShortcutsOptions): void {
  useHotkeys("storyboard", {
    rootRef,
    handlers: {
      "storyboard.prev_shot": navDisabled ? undefined : onPrev,
      "storyboard.next_shot": navDisabled ? undefined : onNext,
      "storyboard.save_shot": onSave,
    },
  });
}
