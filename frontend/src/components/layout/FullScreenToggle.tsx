import { useEffect, useState } from "react";
import { Maximize, Minimize } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** Whether the whole app is in browser full screen (the Fullscreen API's element is the document root). */
function useFullScreen(): boolean {
  const [on, setOn] = useState(() => typeof document !== "undefined" && document.fullscreenElement !== null);
  useEffect(() => {
    const sync = () => setOn(document.fullscreenElement !== null);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);
  return on;
}

/**
 * The whole app in full screen, like the browser's F11, from the top bar: an icon button, and while full screen an
 * "Exit full screen" link in the same place (Esc also exits; the browser says so). Hidden where the browser offers
 * no page full screen (e.g. iPhone Safari).
 */
export function FullScreenToggle() {
  const { t } = useTranslation("dashboard");
  const on = useFullScreen();
  if (typeof document === "undefined" || !document.fullscreenEnabled) return null;

  if (on) {
    return (
      <Button variant="ghost" size="sm" onClick={() => void document.exitFullscreen()}>
        <Minimize aria-hidden data-icon="inline-start" />
        {t("exit_full_screen")}
      </Button>
    );
  }
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={t("full_screen")}
            onClick={() => void document.documentElement.requestFullscreen().catch(() => undefined)}
          />
        }
      >
        <Maximize aria-hidden />
      </TooltipTrigger>
      <TooltipContent side="bottom">{t("full_screen")}</TooltipContent>
    </Tooltip>
  );
}
