/**
 * Opt-in timing trace of the Edit view player for diagnosing pauses in a real browser: in the browser console run
 * `localStorage.setItem("arcreel.debug.playback", "1")` and reload; every seek then logs, under `[playback]`, how long
 * the video element took to finish seeking, to report playing, and to show a moving picture again. Off (and free)
 * unless set; `localStorage.removeItem("arcreel.debug.playback")` turns it off.
 */

const TRACE_KEY = "arcreel.debug.playback";

export function playbackTraceEnabled(): boolean {
  try {
    return localStorage.getItem(TRACE_KEY) === "1";
  } catch {
    return false;
  }
}

/** Log one line (only while tracing). */
export function tracePlayback(message: string): void {
  if (playbackTraceEnabled()) console.info(`[playback] ${message}`);
}

/**
 * Time a seek of `video` to `target` seconds (only while tracing): seeked, playing and the first two painted frames
 * past the target (picture moving), each in ms from now; gives up after 5 s.
 */
export function traceSeek(video: HTMLVideoElement | null, target: number, mode: string): void {
  if (!video || !playbackTraceEnabled()) return;
  const t0 = performance.now();
  const marks: string[] = [];
  const mark = (label: string) => marks.push(`${label} +${Math.round(performance.now() - t0)}`);
  const onSeeked = () => mark("seeked");
  const onPlaying = () => mark("playing");
  const onWaiting = () => mark("waiting");
  video.addEventListener("seeked", onSeeked, { once: true });
  video.addEventListener("playing", onPlaying, { once: true });
  video.addEventListener("waiting", onWaiting, { once: true });
  let painted = 0;
  let last = -1;
  let done = false;
  const finish = (why: string) => {
    if (done) return;
    done = true;
    video.removeEventListener("seeked", onSeeked);
    video.removeEventListener("playing", onPlaying);
    video.removeEventListener("waiting", onWaiting);
    console.info(
      `[playback] seek to ${target.toFixed(2)} s (${mode}, ${video.paused ? "paused" : "playing"}): ${marks.join(", ")} — ${why}`,
    );
  };
  const frame = (_now: number, meta: VideoFrameCallbackMetadata) => {
    if (done) return;
    if (meta.mediaTime >= target - 0.05 && meta.mediaTime !== last) {
      painted += 1;
      last = meta.mediaTime;
      mark(painted === 1 ? "first frame" : "moving");
      if (painted >= 2) {
        finish("ok");
        return;
      }
    }
    video.requestVideoFrameCallback(frame);
  };
  if ("requestVideoFrameCallback" in video) video.requestVideoFrameCallback(frame);
  window.setTimeout(() => finish(painted >= 2 ? "ok" : "no moving picture within 5 s (paused, or still waiting)"), 5000);
}
