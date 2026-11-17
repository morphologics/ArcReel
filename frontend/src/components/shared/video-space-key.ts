import type { KeyboardEvent, PointerEvent } from "react";

/*
 * YouReel (owner 2026-11-17): the space bar plays / pauses a player with the browser's own video controls, also after
 * a seek. A click on the native seek bar can leave the focus outside the video, and the space bar then scrolled the
 * page instead. Call these from the player wrapper's pointer-up and key-down handlers: a pointer press in it keeps the
 * focus on the video, and Space inside it toggles playback (not when a button or a field in the wrapper has the focus).
 */

const CONTROLS = "button, input, select, textarea, [role=button], [contenteditable=true]";

/**
 * After a seek (the video's `seeked` event): if the focus is in the video (on the browser's seek bar inside it), move
 * it to the video itself so the keys reach the page again.
 */
export function refocusVideoAfterSeek(video: HTMLVideoElement): void {
  if (document.activeElement !== video) return;
  video.blur();
  video.focus({ preventScroll: true });
}

/** Wrapper pointer-up: move the focus back to the video after the browser has handled the press (a seek). */
export function keepVideoFocus(event: PointerEvent<HTMLElement>, video: HTMLVideoElement | null): void {
  if (!video || (event.target as HTMLElement).closest(CONTROLS)) return;
  // the browser's seek bar inside the video keeps the keys to itself while it has the focus, and the video already
  // counts as focused then: blur it first, so the focus moves from the seek bar to the video itself
  requestAnimationFrame(() => {
    video.blur();
    video.focus({ preventScroll: true });
  });
}

/** Wrapper key-down, capture phase: Space plays or pauses the video. Capture, so it runs before the browser's own
 * controls inside the video (they toggle too, or swallow the key once the seek bar has the focus). */
export function toggleVideoOnSpace(event: KeyboardEvent<HTMLElement>, video: HTMLVideoElement | null): void {
  if (!video || event.key !== " " || event.repeat || event.altKey || event.ctrlKey || event.metaKey) return;
  const target = event.target as HTMLElement;
  if (target !== video && target.closest(CONTROLS)) return;
  event.preventDefault();
  event.stopPropagation();
  if (video.paused) void video.play().catch(() => undefined);
  else video.pause();
}
