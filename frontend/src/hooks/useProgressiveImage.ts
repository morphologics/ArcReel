import { useEffect, useState } from "react";
import type { FileUrlWidth } from "@/api";

/**
 * YouReel (owner 2026-11-20): asset images open with a resized copy first (often already cached from the card or the
 * details panel), then the original once it has loaded. `previewWidth` matches the details panel's copy (640 px, 1280
 * px on sharp screens) so that copy is reused; `withWidth` asks the files route for a resized copy of any project file
 * address (version files too).
 *
 * YouReel：资产图先显示缩放副本（常已从卡片或详情面板缓存），原图加载完成后再换上。`previewWidth` 与详情面板的副本
 * 一致（640px，高清屏 1280px）以复用缓存；`withWidth` 为任一项目文件地址（含版本文件）请求缩放副本。
 */
export function previewWidth(): FileUrlWidth {
  return typeof window !== "undefined" && window.devicePixelRatio > 1.25 ? 1280 : 640;
}

export function withWidth(url: string, width: number): string {
  if (url.startsWith("data:")) return url;
  const clean = url.replace(/([?&])w=\d+(&|$)/, (_m, lead: string, tail: string) => (tail ? lead : ""));
  return `${clean}${clean.includes("?") ? "&" : "?"}w=${width}`;
}

/** The address to show: the resized copy until the original at `full` has loaded, then `full`. */
export function useProgressiveImage(full: string | null, width: number = previewWidth()): string | null {
  const [loaded, setLoaded] = useState<string | null>(null);
  useEffect(() => {
    if (!full || full.startsWith("data:")) return;
    let live = true;
    const image = new Image();
    image.onload = () => { if (live) setLoaded(full); };
    image.src = full;
    return () => { live = false; image.onload = null; };
  }, [full]);
  if (!full) return null;
  return loaded === full || full.startsWith("data:") ? full : withWidth(full, width);
}
