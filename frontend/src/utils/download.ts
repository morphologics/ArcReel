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
