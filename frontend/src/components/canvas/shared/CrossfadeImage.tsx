import { useState, type ReactNode } from "react";
import { cn } from "cn";

/**
 * Crossfade when the image changes: keep the previous image until the new one loads, then fade the new one in on top, and remove the previous one after the transition.
 * The first load also fades in; with no image or a failed load, show a placeholder so the previous image does not pose as the new one. Only opacity transitions; with reduced motion the global rule keeps it.
 *
 * 换图时交叉淡入：新图加载完成前保留上一张，加载后在上一张之上淡入，过渡结束再移除上一张。
 * 首次加载同样淡入；没有图或新图加载失败时显示占位，不让上一张冒充新图。只过渡透明度，减少动态效果时由全局规则保留。
 */
export function CrossfadeImage({
  src,
  alt,
  fallback,
  className,
  loading,
  onError,
}: {
  src: string | null;
  alt: string;
  fallback: ReactNode;
  /** Applied to the img element, e.g. `object-contain`. / 作用于图片元素，如 `object-contain`。 */
  className?: string;
  /** Passed through to the img element; cards in lists pass `"lazy"` so they load only near the viewport. / 透传给图片元素；列表里的卡片传 `"lazy"`，滚到视口附近才加载。 */
  loading?: "lazy" | "eager";
  onError?: () => void;
}) {
  const [shown, setShown] = useState<{ src: string | null; loaded: boolean; failed: boolean; previous: string | null }>({
    src,
    loaded: false,
    failed: false,
    previous: null,
  });

  // Adjust state from props: on image change, demote the loaded current image to the base layer; the new image starts transparent.
  // 随 props 调整 state：换图时把已加载的当前图降为底图，新图从透明开始。
  if (shown.src !== src) {
    setShown({ src, loaded: false, failed: false, previous: shown.loaded ? shown.src : shown.previous });
  }

  if (!src || (shown.src === src && shown.failed)) return <>{fallback}</>;

  return (
    <span className="relative block size-full">
      {shown.previous && (
        <img
          src={shown.previous}
          alt=""
          aria-hidden
          loading={loading}
          decoding="async"
          className={cn("absolute inset-0 size-full", className)}
        />
      )}
      <img
        key={src}
        src={src}
        alt={alt}
        loading={loading}
        decoding="async"
        onLoad={() => setShown((current) => (current.src === src ? { ...current, loaded: true } : current))}
        onError={() => {
          setShown((current) => (current.src === src ? { ...current, failed: true, previous: null } : current));
          onError?.();
        }}
        onTransitionEnd={() => setShown((current) => (current.src === src ? { ...current, previous: null } : current))}
        className={cn(
          "relative size-full transition-opacity duration-base ease-emphasized",
          shown.loaded ? "opacity-100" : "opacity-0",
          className,
        )}
      />
    </span>
  );
}
