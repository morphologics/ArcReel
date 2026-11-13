import { create } from "zustand";

/**
 * Edit view focus mode: the Edit view alone, in browser full screen, without the app's top bar, sidebar, Agent panel
 * or the episode header's extras (StudioLayout and EpisodePageHeader read `on`). Esc (leaving full screen) ends it;
 * so does leaving the Edit view. Where the browser has no page full screen it still hides the chrome.
 */
interface EditFocusState {
  on: boolean;
  enter: () => void;
  exit: () => void;
}

export const useEditFocusStore = create<EditFocusState>((set, get) => ({
  on: false,
  enter: () => {
    if (get().on) return;
    set({ on: true });
    if (document.fullscreenEnabled && !document.fullscreenElement) {
      void document.documentElement.requestFullscreen().catch(() => undefined);
    }
  },
  exit: () => {
    if (!get().on) return;
    set({ on: false });
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
  },
}));

// Esc / the browser's own exit leaves full screen: end focus mode with it
if (typeof document !== "undefined") {
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement && useEditFocusStore.getState().on) useEditFocusStore.setState({ on: false });
  });
}
