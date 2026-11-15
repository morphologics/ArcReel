import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cn } from "cn";

export interface TypewriterSegment {
  text: string;
  className?: string;
  after?: ReactNode;
}

/** How long the caret keeps blinking after typing finishes, matching `--animate-caret-linger` in index.css (1.4s × 3). / 吐字完成后光标继续闪烁的时长，与 index.css 的 `--animate-caret-linger`（1.4s × 3 次）一致。 */
const CARET_LINGER_MS = 1400 * 3;

interface TypewriterProps {
  segments: TypewriterSegment[];
  speed?: number;
  punctuationDelay?: number;
  startDelay?: number;
  segmentGap?: number;
  caret?: boolean;
  /** The same once key plays only once per session; later mounts show the finished state directly / 同一 once key 同会话内只播放一次；后续 mount 直接显示完成态 */
  once?: string;
  className?: string;
  onDone?: () => void;
}

const PUNCTUATION = /[，。！？；：、,.!?;:…—]/;
const playedOnce = new Set<string>();

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Typewriter effect, playing segments in sequence; `after` (e.g. <br />) is inserted at the end of the first segment before the second starts.
 * Punctuation and paragraph boundaries lengthen the pause automatically, for a rhythm close to a film editing machine.
 *
 * 打字机吐字效果，按 segments 串行播放；首段尾部插入 `after`（如 <br />）后再起第二段。
 * 标点和段落边界自动延长停顿，节奏贴近电影编辑机。
 */
export function Typewriter({
  segments,
  speed = 72,
  punctuationDelay = 220,
  startDelay = 140,
  segmentGap = 320,
  caret = true,
  once,
  className,
  onDone,
}: TypewriterProps) {
  const totalLen = useMemo(() => segments.reduce((n, s) => n + s.text.length, 0), [segments]);
  const fullText = useMemo(() => segments.map((s) => s.text).join(""), [segments]);

  const skip =
    totalLen === 0 || prefersReducedMotion() || (once ? playedOnce.has(once) : false);

  const [pos, setPos] = useState(skip ? totalLen : 0);
  const [phase, setPhase] = useState<"typing" | "linger" | "done">(skip ? "done" : "typing");
  // Reset progress immediately when the text / skip changes, so new text is not truncated or stuck at done
  // (the render-time setState pattern React 19 recommends: snapshot prev, setState synchronously on a diff to trigger a re-render)
  // 文案 / skip 变化时立即重置进度，避免新文案被截断或卡在 done
  // (React 19 推荐的 render-time setState 模式：snapshot prev，发现 diff 时同步 setState 触发重渲)
  const [prevSnapshot, setPrevSnapshot] = useState({ fullText, skip });
  if (prevSnapshot.fullText !== fullText || prevSnapshot.skip !== skip) {
    setPrevSnapshot({ fullText, skip });
    setPos(skip ? totalLen : 0);
    setPhase(skip ? "done" : "typing");
  }
  const onDoneRef = useRef(onDone);
  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  // Keep segments in a ref: callers often build segments inline, producing a new reference on every re-render;
  // if segments were an effect dependency, unrelated parent re-renders would clear the old timer and tick(0) again,
  // and the later setPos(next) would rewind pos to the first character. fullText is the real content key.
  // segments 抓在 ref 里：调用方常常内联构造 segments，每次重渲都生成新引用；
  // 若把 segments 写进 effect 依赖，父组件无关重渲就会清旧定时器、重新 tick(0)，
  // 后续 setPos(next) 会把 pos 倒退回首字。fullText 才是真正的内容键。
  const segmentsRef = useRef(segments);
  useEffect(() => {
    segmentsRef.current = segments;
  });

  useEffect(() => {
    if (skip) {
      onDoneRef.current?.();
      return;
    }
    let cancelled = false;
    const timers: ReturnType<typeof setTimeout>[] = [];

    const tick = (current: number) => {
      if (cancelled) return;
      if (current >= totalLen) {
        if (once) playedOnce.add(once);
        setPhase("linger");
        timers.push(
          setTimeout(() => {
            if (cancelled) return;
            setPhase("done");
            onDoneRef.current?.();
          }, CARET_LINGER_MS),
        );
        return;
      }
      let acc = 0;
      let ch = "";
      let crossesSegmentEnd = false;
      for (const seg of segmentsRef.current) {
        if (current < acc + seg.text.length) {
          ch = seg.text[current - acc];
          crossesSegmentEnd = current + 1 === acc + seg.text.length;
          break;
        }
        acc += seg.text.length;
      }
      const next = current + 1;
      const extra =
        crossesSegmentEnd && next < totalLen
          ? segmentGap
          : PUNCTUATION.test(ch)
            ? punctuationDelay
            : 0;
      timers.push(
        setTimeout(() => {
          if (cancelled) return;
          setPos(next);
          tick(next);
        }, speed + extra),
      );
    };

    timers.push(setTimeout(() => tick(0), startDelay));
    return () => {
      cancelled = true;
      timers.forEach(clearTimeout);
    };
  }, [skip, totalLen, fullText, speed, punctuationDelay, segmentGap, startDelay, once]);

  const visibleNodes: ReactNode[] = [];
  let consumed = 0;
  segments.forEach((seg, i) => {
    const segEnd = consumed + seg.text.length;
    const visibleEnd = Math.max(consumed, Math.min(pos, segEnd));
    visibleNodes.push(
      // react-doctor-disable-next-line react-doctor/no-array-index-as-key -- segments are text fragments in fixed order, never reordered, added or removed; position is identity, and texts may repeat so they cannot be the key / segments 是固定顺序的文案片段，不重排不增删，位置即身份，文本可能重复不能作 key
      <span key={`tw-s-${i}`} className={seg.className}>
        {seg.text.slice(0, visibleEnd - consumed)}
      </span>,
    );
    if (seg.after && pos >= segEnd) {
      visibleNodes.push(<span key={`tw-a-${i}`}>{seg.after}</span>);
    }
    consumed = segEnd;
  });

  return (
    <span className={className}>
      <span className="sr-only">{fullText}</span>
      <span aria-hidden="true">
        {visibleNodes}
        {caret && phase !== "done" ? (
          <TypewriterCaret key={phase} finite={phase === "linger"} />
        ) : null}
      </span>
    </span>
  );
}

function TypewriterCaret({ finite }: { finite: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "ml-0.5 inline-block h-[1em] w-1 rounded-xs bg-primary align-[-0.12em]",
        finite ? "animate-caret-linger" : "animate-caret-blink",
      )}
    />
  );
}
