/**
 * YouReel: tell the host the member is using the studio. A hosted studio is stopped after a while without use and
 * started again on the next visit; by requests alone the host can't tell a person browsing from a tab left open (the
 * app polls in the background either way), so the app sends `POST /api/v1/activity` (the backend answers 204, nothing
 * else) on real use: a click, a key, a scroll or a touch, in a visible tab, at most once a minute.
 */

import { API_BASE, withAuth } from "./transport";

export const ACTIVITY_SIGNAL_MIN_MS = 60_000;

let lastSent = 0;

function send(): void {
  try {
    void fetch(`${API_BASE}/activity`, withAuth("/activity", { method: "POST", keepalive: true })).catch(() => {});
  } catch {
    // e.g. the read-only gate refuses writes: nothing to tell then
  }
}

/** One use event: sends the signal unless the tab is hidden or one went out less than a minute ago. */
export function noteActivity(now: number = Date.now(), sendSignal: () => void = send): boolean {
  if (document.visibilityState !== "visible") return false;
  if (now - lastSent < ACTIVITY_SIGNAL_MIN_MS) return false;
  lastSent = now;
  sendSignal();
  return true;
}

const USE_EVENTS = ["pointerdown", "keydown", "wheel", "touchstart"] as const;

/** Listen for use on the whole page (passive, capture: nothing in the page can stop it); returns the undo. */
export function startActivitySignal(): () => void {
  const onUse = () => {
    noteActivity();
  };
  const options = { passive: true, capture: true } as const;
  for (const name of USE_EVENTS) window.addEventListener(name, onUse, options);
  return () => {
    for (const name of USE_EVENTS) window.removeEventListener(name, onUse, options);
  };
}

export function resetActivitySignalForTests(): void {
  lastSent = 0;
}
