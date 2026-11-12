import { useRef, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";
import { Gauge } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

import { PLAYBACK_SPEEDS } from "./useTimelinePlayback";

const speedLabel = (speed: number) => `${speed}×`;

/**
 * The speed slider in the player's speed popup: a dot per speed on a vertical track, fastest at the top
 * (5× … 0.5×; 1× = Normal). Click a dot, or use ↑/↓ (←/→) on the focused one.
 */
function SpeedControl({ speed, onChange }: { speed: number; onChange: (speed: number) => void }) {
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
      className="relative flex h-full w-36 flex-col"
    >
      {/* the track behind the dots (drawn first, so the dots sit on it without a z-index) */}
      <span
        aria-hidden
        className="absolute top-[calc(100%/12)] bottom-[calc(100%/12)] left-[0.875rem] w-0.5 rounded-full bg-border"
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
          // each speed is a whole row (equal share of the height): click anywhere in it
          className="focus-ring relative flex w-full flex-1 items-center gap-2.5 rounded-sm px-2.5 text-xs hover:bg-muted"
        >
          <span
            aria-hidden
            className={cn(
              "size-2.5 shrink-0 rounded-full border",
              index === current ? "border-primary bg-primary" : "border-muted-foreground bg-background",
            )}
          />
          <span
            aria-hidden
            className={cn("num whitespace-nowrap", index === current ? "font-medium" : "text-muted-foreground")}
          >
            {speedLabel(value)}
            {value === 1 && <span className="text-muted-foreground"> {t("edit_view_speed_normal")}</span>}
          </span>
        </button>
      ))}
    </div>
  );
}

/** The player's speed button (gauge icon + current speed, e.g. "1×") opening the vertical slider. */
export function SpeedPopover({ speed, onChange }: { speed: number; onChange: (speed: number) => void }) {
  const { t } = useTranslation("dashboard");
  return (
    <Popover>
      <PopoverTrigger
        render={<Button size="sm" variant="ghost" aria-label={`${t("edit_view_speed")} ${speedLabel(speed)}`} />}
      >
        <Gauge data-icon="inline-start" aria-hidden />
        <span className="num">{speedLabel(speed)}</span>
      </PopoverTrigger>
      <PopoverContent side="top" align="end" className="w-auto">
        <div className="h-64">
          <SpeedControl speed={speed} onChange={onChange} />
        </div>
      </PopoverContent>
    </Popover>
  );
}
