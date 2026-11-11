import { create } from "zustand";

/**
 * Large-file downloads the studio started (final cut, Jianying draft, project export), shown in the top bar's
 * Downloads indicator with progress, and used to refuse a second download of the same file while one runs.
 */
export type DownloadStatus = "running" | "done" | "failed" | "cancelled";

export interface DownloadItem {
  id: string;
  /** What the file is, for the list (its file name). */
  name: string;
  /** Identifies the file: a second download with the same key while this one runs is refused. */
  key: string;
  status: DownloadStatus;
  /** Bytes received so far. */
  received: number;
  /** Total size from the server; null when it didn't say. */
  total: number | null;
  startedAt: number;
  finishedAt: number | null;
  error: string | null;
  /** "browser": handed to the browser's own downloads (no progress here); otherwise streamed by the studio. */
  mode: "stream" | "browser";
}

interface DownloadsState {
  items: DownloadItem[];
  add: (item: DownloadItem) => void;
  update: (id: string, patch: Partial<DownloadItem>) => void;
  remove: (id: string) => void;
  clearFinished: () => void;
}

/** Abort handles live outside the store (not serialisable state). */
export const downloadControllers = new Map<string, AbortController>();

export const useDownloadsStore = create<DownloadsState>((set) => ({
  items: [],
  add: (item) => set((s) => ({ items: [item, ...s.items].slice(0, 20) })),
  update: (id, patch) => set((s) => ({ items: s.items.map((item) => (item.id === id ? { ...item, ...patch } : item)) })),
  remove: (id) => set((s) => ({ items: s.items.filter((item) => item.id !== id) })),
  clearFinished: () => set((s) => ({ items: s.items.filter((item) => item.status === "running") })),
}));

/** The running download of this file, if any. */
export function runningDownload(key: string): DownloadItem | undefined {
  return useDownloadsStore.getState().items.find((item) => item.key === key && item.status === "running");
}
