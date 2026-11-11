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

export function isPhoneWidth(): boolean {
  return matches(PHONE_QUERY);
}
