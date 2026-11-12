import { useSyncExternalStore } from "react";

function matches(query: string): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(query).matches;
}

/** Whether a CSS media query matches now, following changes (false where matchMedia is missing, e.g. tests). */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    () => matches(query),
    () => false,
  );
}

/** Phone width: the layouts that stack or fold what sits side by side on wider screens. */
export const PHONE_QUERY = "(max-width: 639px)";

/** Narrower than a tablet in portrait (< 768px): the header's labelled buttons collapse to their icons. */
export const COMPACT_HEADER_QUERY = "(max-width: 767px)";

/** Wide screens (1600px and up): a shot's storyboard image and video side by side; narrower, one at a time. */
export const MEDIA_SIDE_BY_SIDE_QUERY = "(min-width: 1600px)";
/** The Edit view's speed slider stands beside the player from this width; narrower it is a row under it. */
export const SPEED_BESIDE_PLAYER_QUERY = "(min-width: 1200px)";

export function isPhoneWidth(): boolean {
  return matches(PHONE_QUERY);
}
