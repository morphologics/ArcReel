/**
 * YouReel: the studio's backend is being woken (hosting stops an idle studio and starts it again on the next
 * request). While it is down, the host answers every request with 503 and the header `X-Studio-Waking: 1` instead
 * of the backend. A page that was already open (a member back from a break) would otherwise show each such answer
 * as an error; instead the request waits for the backend, the app shows its "Studio is restarting… retrying"
 * screen meanwhile ({@link StudioWakingOverlay}), and the caller gets the real answer.
 */

import { create } from "zustand";

/** How often a waiting request asks again, and for how long at most before the wake answer is returned as is. */
export const STUDIO_WAKING_RETRY_MS = 2000;
export const STUDIO_WAKING_GIVE_UP_MS = 3 * 60_000;

interface StudioWakingState {
  /** Requests waiting for the backend right now; the overlay shows while there are any. */
  waiting: number;
}

export const useStudioWakingStore = create<StudioWakingState>(() => ({ waiting: 0 }));

/** Whether this answer is the host's "the studio is starting" rather than the backend's. */
export function isStudioWakingResponse(response: Response): boolean {
  return response.status === 503 && response.headers.get("X-Studio-Waking") === "1";
}

function abortError(signal: AbortSignal): Error {
  const reason: unknown = signal.reason;
  return reason instanceof Error ? reason : new DOMException("Aborted", "AbortError");
}

function pause(ms: number, signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError(signal));
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(abortError(signal));
      },
      { once: true },
    );
  });
}

/**
 * Ask again every {@link STUDIO_WAKING_RETRY_MS} until the answer is the backend's (any status), and return it.
 * Gives up after {@link STUDIO_WAKING_GIVE_UP_MS} with the last wake answer; a network error counts as "still
 * waking". The overlay shows for as long as any request waits here.
 */
export async function waitForStudio(
  first: Response,
  retry: () => Promise<Response>,
  signal?: AbortSignal | null,
): Promise<Response> {
  if (!isStudioWakingResponse(first)) return first;
  useStudioWakingStore.setState((s) => ({ waiting: s.waiting + 1 }));
  const started = Date.now();
  let last = first;
  try {
    while (Date.now() - started < STUDIO_WAKING_GIVE_UP_MS) {
      await pause(STUDIO_WAKING_RETRY_MS, signal);
      try {
        last = await retry();
      } catch (error) {
        if (signal?.aborted) throw error;
        continue;
      }
      if (!isStudioWakingResponse(last)) {
        void reloadIfNewerBuild();
        return last;
      }
    }
    return last;
  } finally {
    useStudioWakingStore.setState((s) => ({ waiting: Math.max(0, s.waiting - 1) }));
  }
}

const MAIN_SCRIPT = /\/assets\/index-[\w-]+\.js/;
let checkingBuild = false;

/**
 * Once the woken backend answers, load the page again if the studio now serves another build: a release restarts
 * every studio, so an open tab passes through here and would otherwise go on running the old app until reloaded.
 * Compares the main script of the page the studio serves now with the one this page runs; nothing to compare on the
 * dev server (no built script), so nothing happens there.
 */
export async function reloadIfNewerBuild(reload: () => void = () => window.location.reload()): Promise<boolean> {
  const running = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/index-"]')?.getAttribute("src");
  if (!running || checkingBuild) return false;
  checkingBuild = true;
  try {
    const html = await (await fetch("/", { cache: "no-store", credentials: "same-origin" })).text();
    const served = MAIN_SCRIPT.exec(html)?.[0];
    if (!served || running.endsWith(served)) return false;
    reload();
    return true;
  } catch {
    return false;
  } finally {
    checkingBuild = false;
  }
}

let noting = false;

/**
 * For calls that can't be repeated here (custom fetches that only check the status): show the overlay until the
 * backend answers again, asking its health address meanwhile. The call itself still fails; the page can be used
 * again once the overlay goes.
 */
export function noteStudioWaking(response: Response): void {
  if (!isStudioWakingResponse(response) || noting) return;
  noting = true;
  void waitForStudio(response, () => fetch("/health", { cache: "no-store" })).finally(() => {
    noting = false;
  });
}
