import { useEffect, useState } from "react";
import { parseIsoTimestamp } from "@/utils/date-format";

/** "1:42" since an ISO time, ticking every second. */
export function ElapsedTime({ since, className }: { since: string; className?: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const seconds = Math.max(0, Math.floor((now - parseIsoTimestamp(since).getTime()) / 1000));
  return (
    <span className={className ?? "num"}>
      {Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}
    </span>
  );
}

/** The renderer reports no percentage: a moving bar while it works. */
export function WorkingBar() {
  return (
    <div aria-hidden className="h-1 w-full overflow-hidden rounded-full bg-muted">
      <div className="h-full w-full animate-pulse rounded-full bg-primary/60" />
    </div>
  );
}
