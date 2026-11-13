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

/**
 * Attach `stream` to `video`: resolves once every piece is appended (the whole timeline is buffered or buffering),
 * rejects on any failure. `dispose()` detaches it and stops the downloads.
 */
export function attachStream(video: HTMLVideoElement, stream: ReadyStream): { ready: Promise<void>; dispose: () => void } {
  const source = new MediaSource();
  const objectUrl = URL.createObjectURL(source);
  const abort = new AbortController();
  video.src = objectUrl;

  const ready = (async () => {
    await new Promise<void>((resolve) => source.addEventListener("sourceopen", () => resolve(), { once: true }));
    const buffer = source.addSourceBuffer(stream.codecs);
    buffer.mode = "segments";
    const appended = () =>
      new Promise<void>((resolve, reject) => {
        buffer.addEventListener("updateend", () => resolve(), { once: true });
        buffer.addEventListener("error", () => reject(new Error("stream piece could not be appended")), { once: true });
      });
    source.duration = stream.duration;
    for (const piece of stream.pieces) {
      const response = await fetch(piece.url, { credentials: "same-origin", signal: abort.signal });
      if (!response.ok) throw new Error(`stream piece ${piece.url}: HTTP ${response.status}`);
      const data = await response.arrayBuffer();
      buffer.timestampOffset = piece.start;
      // the window's end first (its start must stay below it), half a frame late: a piece's last frame ends exactly at
      // its end, and floating-point placement can put that a hair beyond, which dropped one frame at every cut; the
      // next piece overwrites any overlap
      buffer.appendWindowEnd = piece.start + piece.duration + WINDOW_SLACK_SECONDS;
      buffer.appendWindowStart = piece.start;
      const done = appended();
      buffer.appendBuffer(data);
      await done;
    }
    buffer.appendWindowStart = 0;
    buffer.appendWindowEnd = Infinity;
    if (source.readyState === "open") source.endOfStream();
  })();

  return {
    ready,
    dispose: () => {
      abort.abort();
      video.pause();
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(objectUrl);
    },
  };
}
