import i18n from "@/i18n";
import { useAppStore } from "@/stores/app-store";
import { downloadControllers, runningDownload, useDownloadsStore } from "@/stores/downloads-store";

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

/**
 * 通过隐藏 <a> 让浏览器导航到下载地址，避免 window.open 产生空白标签页。
 * 同源的媒体文件传 `filename`，让浏览器保存而不是在标签页里播放。
 */
export function triggerBrowserDownload(url: string, filename?: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  if (filename !== undefined) anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

/**
 * A readable download file name: drops characters file systems reject and control characters,
 * collapses whitespace, cuts the stem to `maxLength`. Falls back to `fallback` when nothing is left.
 */
export function safeFileName(stem: string, extension: string, fallback = "download", maxLength = 150): string {
  // control characters replaced one by one (a regex with them trips no-control-regex)
  const printable = Array.from(stem, (ch) => (ch.charCodeAt(0) < 32 || ch.charCodeAt(0) === 127 ? " " : ch)).join("");
  const cleaned = printable
    .replace(/[\\/:*?"<>|]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maxLength)
    .trim();
  return `${cleaned || fallback}.${extension}`;
}

type SaveFilePicker = (options: {
  suggestedName?: string;
  types?: { description?: string; accept: Record<string, string[]> }[];
}) => Promise<{ createWritable: () => Promise<WritableStream<Uint8Array>> }>;

/**
 * "Save As" download: where the browser has a save dialog (showSaveFilePicker, Chromium browsers) the
 * user picks folder and file name first, then the file is streamed into it; elsewhere a plain download
 * named `filename` into the browser's download folder.
 * Call it in the same call stack as the user's click: the save dialog only opens on a user gesture.
 * Resolves false when the user cancels the dialog; rejects when writing fails.
 */
export async function saveDownloadAs(url: string, filename: string, mimeType = "video/mp4"): Promise<boolean> {
  const picker = (window as Window & { showSaveFilePicker?: SaveFilePicker }).showSaveFilePicker;
  if (typeof picker !== "function") {
    triggerBrowserDownload(url, filename);
    return true;
  }
  const extension = filename.includes(".") ? filename.slice(filename.lastIndexOf(".")) : "";
  let handle: Awaited<ReturnType<SaveFilePicker>>;
  try {
    handle = await picker({
      suggestedName: filename,
      types: extension ? [{ accept: { [mimeType]: [extension] } }] : undefined,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") return false;
    // the dialog could not open (e.g. disabled in an embedded page): plain download instead
    triggerBrowserDownload(url, filename);
    return true;
  }
  const response = await fetch(url);
  if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
  await response.body.pipeTo(await handle.createWritable());
  return true;
}

/** Above this size a download without a save dialog goes to the browser's own downloads, not through memory. */
const IN_MEMORY_LIMIT = 1024 * 1024 * 1024;
/** How often (ms) the progress of a running download is published to the Downloads indicator. */
const PROGRESS_INTERVAL = 250;

export type TrackedDownloadResult = "started" | "duplicate" | "cancelled";

/**
 * A large-file download the studio tracks (top bar → Downloads): progress, size, cancel, done / failed, and
 * no second copy while the same file (`key`) is still downloading. With a save dialog (showSaveFilePicker)
 * the file streams straight to the chosen place; without one it is collected in memory and saved under
 * `filename`, or handed to the browser's own downloads when it is larger than 1 GB.
 * Call it in the click handler's own call stack: the save dialog only opens on a user gesture.
 */
export async function trackedDownload({
  url,
  filename,
  key = url,
  mimeType = "application/octet-stream",
}: {
  url: string;
  filename: string;
  key?: string;
  mimeType?: string;
}): Promise<TrackedDownloadResult> {
  if (runningDownload(key)) return "duplicate";

  const picker = (window as Window & { showSaveFilePicker?: SaveFilePicker }).showSaveFilePicker;
  let handle: Awaited<ReturnType<SaveFilePicker>> | null = null;
  if (typeof picker === "function") {
    const extension = filename.includes(".") ? filename.slice(filename.lastIndexOf(".")) : "";
    try {
      handle = await picker({
        suggestedName: filename,
        types: extension ? [{ accept: { [mimeType]: [extension] } }] : undefined,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return "cancelled";
      handle = null; // the dialog could not open: download without it
    }
  }

  const store = useDownloadsStore.getState();
  const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const controller = new AbortController();
  downloadControllers.set(id, controller);
  store.add({
    id,
    name: filename,
    key,
    status: "running",
    received: 0,
    total: null,
    startedAt: Date.now(),
    finishedAt: null,
    error: null,
    mode: "stream",
  });
  const finish = (patch: Parameters<typeof store.update>[1]) => {
    downloadControllers.delete(id);
    useDownloadsStore.getState().update(id, { finishedAt: Date.now(), ...patch });
    // the outcome as a toast too: the Downloads list is only seen when opened
    const toast = useAppStore.getState().pushToast;
    if (patch.status === "done" && patch.mode !== "browser") toast(i18n.t("dashboard:downloads_done", { name: filename }), "success");
    else if (patch.status === "done") toast(i18n.t("dashboard:downloads_handed_to_browser"), "info");
    else if (patch.status === "failed") toast(i18n.t("dashboard:downloads_failed_named", { name: filename, message: patch.error ?? "" }), "error");
  };

  let writable: WritableStream<Uint8Array> | null = null;
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    const length = Number(response.headers.get("Content-Length"));
    const total = Number.isFinite(length) && length > 0 ? length : null;
    useDownloadsStore.getState().update(id, { total });
    if (!handle && total !== null && total > IN_MEMORY_LIMIT) {
      // too big to collect in memory: the browser's own downloads take it (and show their own progress)
      controller.abort();
      triggerBrowserDownload(url, filename);
      finish({ status: "done", mode: "browser" });
      return "started";
    }
    writable = handle ? await handle.createWritable() : null;
    const writer = writable?.getWriter() ?? null;
    const chunks: Uint8Array[] = [];
    const reader = response.body.getReader();
    let received = 0;
    let published = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (writer) await writer.write(value);
      else chunks.push(value);
      if (Date.now() - published > PROGRESS_INTERVAL) {
        published = Date.now();
        useDownloadsStore.getState().update(id, { received });
      }
    }
    if (writer) await writer.close();
    else downloadBlob(new Blob(chunks as BlobPart[], { type: mimeType }), filename);
    finish({ status: "done", received });
    return "started";
  } catch (err) {
    if (writable) await writable.abort().catch(() => {});
    if (controller.signal.aborted) finish({ status: "cancelled" });
    else finish({ status: "failed", error: err instanceof Error ? err.message : String(err) });
    return "started";
  }
}

/** Cancel a running tracked download. */
export function cancelTrackedDownload(id: string): void {
  downloadControllers.get(id)?.abort();
}
