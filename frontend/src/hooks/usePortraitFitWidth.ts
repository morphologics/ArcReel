import { useLayoutEffect, useRef, useState } from "react";

/**
 * YouReel (owner 2026-11-20): a 9:16 project's shot media (Storyboard / Video) filled the panel's width, which made the
 * frame taller than the screen (a 369 × 656 frame starting 316 px down on a 900 px window). For portrait frames this
 * gives the width at which the frame's height fits between where it starts and the window's bottom (a 16 px margin),
 * measured when shown and on window resizes; never below `minWidth`. Landscape: no limit.
 *
 * YouReel：9:16 项目的分镜 / 视频预览占满面板宽度时，画面比屏幕还高。竖屏时按画面起点到窗口底部的可用高度算出宽度上限
 * （留 16px），显示时与窗口尺寸变化时重算，不低于 `minWidth`；横屏不限制。
 */
export function usePortraitFitWidth<T extends HTMLElement>(portrait: boolean, minWidth = 180) {
  const ref = useRef<T | null>(null);
  const [maxWidth, setMaxWidth] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (!portrait) {
      setMaxWidth(null);
      return;
    }
    const measure = () => {
      const el = ref.current;
      if (!el) return;
      const top = el.getBoundingClientRect().top;
      const available = window.innerHeight - Math.max(0, top) - 16;
      setMaxWidth(Math.max(minWidth, Math.floor((available * 9) / 16)));
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [portrait, minWidth]);
  return { ref, style: maxWidth !== null ? { maxWidth } : undefined };
}
