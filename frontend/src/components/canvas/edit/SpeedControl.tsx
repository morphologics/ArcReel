import { useRef, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";

import { PLAYBACK_SPEEDS } from "./useTimelinePlayback";

const speedLabel = (speed: number) => `${speed}×`;

/**
 * The player's speed control: a dot per speed on a track. From 1200px a vertical slider beside the picture, fastest
 * at the top (5× … 0.5×; 1× = Normal); narrower, a row under it, slowest on the left, without the Normal label.
 * Click a dot, or use ↑/↓ (←/→) on the focused one.
 */
export function SpeedControl({
  speed,
  onChange,
  vertical,
}: {
  speed: number;
  onChange: (speed: number) => void;
  /** beside the picture (wide screens); otherwise a row under it */
  vertical: boolean;
}) {
  const { t } = useTranslation("dashboard");
  const dots = useRef<(HTMLButtonElement | null)[]>([]);
  const current = Math.max(0, PLAYBACK_SPEEDS.indexOf(speed as (typeof PLAYBACK_SPEEDS)[number]));
  // top to bottom: fastest first
  const order = PLAYBACK_SPEEDS.map((value, index) => ({ value, index })).reverse();

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const step =
      event.key === "ArrowUp" || event.key === "ArrowRight" ? 1 : event.key === "ArrowDown" || event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = Math.min(PLAYBACK_SPEEDS.length - 1, Math.max(0, current + step));
    onChange(PLAYBACK_SPEEDS[next]);
    dots.current[next]?.focus();
  };

  return (
    <div
      role="radiogroup"
      aria-label={t("edit_view_speed")}
      title={t("edit_view_speed")}
      className={cn(
        "relative flex items-start justify-between",
        vertical ? "h-full max-h-80 flex-col py-1" : "w-full max-w-md flex-row-reverse px-1",
      )}
    >
      {/* the track behind the dots (drawn first, so the dots sit on it without a z-index): horizontal under the
          picture, vertical beside it */}
      <span
        aria-hidden
        className={cn(
          "absolute rounded-full bg-border",
          vertical ? "inset-y-3 left-[0.6875rem] w-0.5" : "inset-x-6 top-[0.4375rem] h-0.5",
        )}
      />
      {order.map(({ value, index }) => (
        <button
          key={value}
          ref={(node) => {
            dots.current[index] = node;
          }}
          type="button"
          role="radio"
          aria-checked={index === current}
          aria-label={value === 1 ? `${speedLabel(value)} ${t("edit_view_speed_normal")}` : speedLabel(value)}
          tabIndex={index === current ? 0 : -1}
          onClick={() => onChange(value)}
          onKeyDown={onKeyDown}
          className={cn(
            "focus-ring relative flex items-center rounded-sm px-1 text-xs",
            vertical ? "gap-1.5" : "flex-col gap-1",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "size-3.5 shrink-0 rounded-full border-2",
              index === current
                ? "border-primary bg-primary"
                : index < current
                  ? "border-primary/60 bg-primary/60"
                  : "border-muted-foreground bg-background",
            )}
          />
          <span
            aria-hidden
            className={cn("num whitespace-nowrap", index === current ? "font-medium" : "text-muted-foreground")}
          >
            {speedLabel(value)}
            {value === 1 && vertical && <span className="text-muted-foreground"> {t("edit_view_speed_normal")}</span>}
          </span>
        </button>
      ))}
    </div>
  );
}
