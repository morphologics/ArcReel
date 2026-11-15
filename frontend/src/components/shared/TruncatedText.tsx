import { useLayoutEffect, useRef, useState } from "react";
import { cn } from "cn";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

interface TruncatedTextProps {
  text: string;
  className?: string;
  /**
   * Pass false when placed inside a focusable element such as a button: when truncated it no longer enters the Tab order on its own (nested focusable elements violate WCAG 4.1.2);
   * hover still shows the full text, and screen readers read the full text from the outer element's accessible name.
   *
   * 放在按钮等可聚焦元素里时传 false：截断时不再单独进入 Tab 顺序（嵌套的可聚焦元素违反 WCAG 4.1.2），
   * 悬停仍显示全文，读屏从外层元素的可访问名称读到全文。
   */
  focusable?: boolean;
}

/**
 * Single-line truncated text: when truncated it can be keyboard-focused, and hover or focus shows the full text; when not truncated it doesn't enter the Tab order or show a tooltip.
 * For short text that can't wrap, such as names, paths and model IDs; inside flex or table cells the parent must allow it to shrink (min-w-0 / max-w-0).
 *
 * 单行截断的文字：被截断时可经键盘聚焦，悬停或聚焦都显示全文；没有截断时不进入 Tab 顺序、不弹提示。
 * 用于名称、路径、模型 ID 这类不能折行的短文本；放进 flex 或表格单元格时，父级需允许它收缩（min-w-0 / max-w-0）。
 */
export function TruncatedText({ text, className, focusable = true }: TruncatedTextProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const [truncated, setTruncated] = useState(false);

  // Container width changes (panel dragged wider, window resized) change whether it truncates, which can only be determined in the browser by comparing content width with visible width.
  // 容器宽度变化（面板拖宽、窗口缩放）都会改变是否截断，只能在浏览器里比较内容宽度与可见宽度。
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setTruncated(el.scrollWidth - el.clientWidth > 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text]);

  return (
    <Tooltip>
      <TooltipTrigger
        disabled={!truncated}
        render={
          <span
            ref={ref}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- when truncated it must be keyboard-focusable to show the full text; it has no executable action, so it isn't rendered as a button / 截断时须能用键盘聚焦以显示全文；它没有可执行的动作，不渲染为 button
            tabIndex={truncated && focusable ? 0 : undefined}
            className={cn("block min-w-0 truncate", className)}
          />
        }
      >
        {text}
      </TooltipTrigger>
      <TooltipContent>{text}</TooltipContent>
    </Tooltip>
  );
}
