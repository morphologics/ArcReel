/**
 * Gapless playback source for the Edit view: the edit timeline's stream pieces (rendered on the server, see
 * server/services/presentation/timeline_stream.py) appended back to back into one `<video>` through Media Source
 * Extensions. Each piece is placed at its own timeline time (`timestampOffset`) and cut at its end
 * (`appendWindowEnd`), so the stream's clock IS the timeline's clock and a piece's audio can't push the next one later.
 */

/** Half a frame at the stream's 30 fps. */
const WINDOW_SLACK_SECONDS = 1 / 60;

export interface StreamPieceSource {
  start: number;
  duration: number;
  url: string;
}

export interface ReadyStream {
  /** Changes whenever the pieces do (revision + piece keys): a new value means a new stream. */
  id: string;
  codecs: string;
  duration: number;
  pieces: readonly StreamPieceSource[];
}

export function streamSupported(codecs: string): boolean {
  return typeof window !== "undefined" && "MediaSource" in window && MediaSource.isTypeSupported(codecs);
}

export interface AttachOptions {
  /** Timeline seconds to load first: the piece holding this time, then onwards, then the rest (see `focus`). */
  startAt?: number;
  /** Called after each appended piece (`video.buffered` has grown). */
  onProgress?: () => void;
}

/**
 * Attach `stream` to `video`: resolves once every piece is appended (the whole timeline is buffered), rejects on any
 * failure. Pieces arrive one at a time; `onProgress` lets the player take over as soon as enough lies ahead (see
 * `bufferedAhead`) instead of waiting for all of them. `dispose()` detaches it and stops the downloads.
 */
export function attachStream(
  video: HTMLVideoElement,
  stream: ReadyStream,
  { startAt = 0, onProgress }: AttachOptions = {},
): { ready: Promise<void>; dispose: () => void; focus: (t: number) => void } {
  const source = new MediaSource();
  const objectUrl = URL.createObjectURL(source);
  const abort = new AbortController();
  video.src = objectUrl;

  // the playhead the loader works from: a seek moves it, and the next fetch is the piece there
  let target = startAt;
  const appended = new Set<number>();
  const ready = (async () => {
    await new Promise<void>((resolve) => source.addEventListener("sourceopen", () => resolve(), { once: true }));
    const buffer = source.addSourceBuffer(stream.codecs);
    buffer.mode = "segments";
    const appendDone = () =>
      new Promise<void>((resolve, reject) => {
        buffer.addEventListener("updateend", () => resolve(), { once: true });
        buffer.addEventListener("error", () => reject(new Error("stream piece could not be appended")), { once: true });
      });
    source.duration = stream.duration;
    while (appended.size < stream.pieces.length) {
      const index = nextPiece(stream.pieces, appended, target);
      const piece = stream.pieces[index];
      appended.add(index);
      const response = await fetch(piece.url, { credentials: "same-origin", signal: abort.signal });
      if (!response.ok) throw new Error(`stream piece ${piece.url}: HTTP ${response.status}`);
      const data = await response.arrayBuffer();
      buffer.timestampOffset = piece.start;
      // the window's start must stay below its end: open it first (pieces come out of order after a seek), then the
      // end, half a frame late: a piece's last frame ends exactly at its end, and floating-point placement can put
      // that a hair beyond, which dropped one frame at every cut; the next piece overwrites any overlap
      buffer.appendWindowStart = 0;
      buffer.appendWindowEnd = piece.start + piece.duration + WINDOW_SLACK_SECONDS;
      buffer.appendWindowStart = piece.start;
      const done = appendDone();
      buffer.appendBuffer(data);
      await done;
      if (!abort.signal.aborted) onProgress?.();
    }
    buffer.appendWindowStart = 0;
    buffer.appendWindowEnd = Infinity;
    if (source.readyState === "open") source.endOfStream();
  })();

  return {
    ready,
    focus: (t) => {
      target = t;
    },
    dispose: () => {
      abort.abort();
      video.pause();
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(objectUrl);
    },
  };
}

/** The piece to fetch next: the first missing one at or after `target`, else the first missing one before it. */
export function nextPiece(pieces: readonly StreamPieceSource[], appended: ReadonlySet<number>, target: number): number {
  const ahead = pieces.findIndex((piece, index) => !appended.has(index) && target < piece.start + piece.duration);
  return ahead >= 0 ? ahead : pieces.findIndex((_, index) => !appended.has(index));
}

/** Seconds of continuous picture `video` holds from `t` on (0 when `t` itself isn't buffered). */
export function bufferedAhead(video: HTMLVideoElement, t: number): number {
  const ranges = video.buffered;
  for (let i = 0; i < ranges.length; i++) {
    // a hair of tolerance: a seek lands on a frame boundary that floating point may put just outside the range
    if (ranges.start(i) <= t + 0.05 && t < ranges.end(i)) return ranges.end(i) - t;
  }
  return 0;
}
