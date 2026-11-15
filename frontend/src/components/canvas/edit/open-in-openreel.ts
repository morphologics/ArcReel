import { useEffect, useState } from "react";
import { API } from "@/api";
import { pickDefaultTimeline } from "./timeline-view";

/** Where YouReel Studio serves its OpenReel fork, next to the studio. */
const OPENREEL_PAGE = "/reel/index.html";

// Whether this studio serves OpenReel (experimental, so far only on the DEV studio): asked once per page load. A
// studio without it answers the path with its own app page (SPA fallback), which doesn't name OpenReel.
let available: Promise<boolean> | null = null;
function openReelAvailable(): Promise<boolean> {
  available ??= fetch(OPENREEL_PAGE, { credentials: "same-origin" })
    .then(async (response) => response.ok && (await response.text()).includes("OpenReel"))
    .catch(() => false);
  return available;
}

/** Forget the cached probe so the next caller asks again. */
export function resetOpenReelProbeForTests(): void {
  available = null;
}

/** Whether this studio serves OpenReel; null until the (once per page load) probe has answered. */
export function useOpenReelAvailable(): boolean | null {
  const [served, setServed] = useState<boolean | null>(null);
  useEffect(() => {
    let active = true;
    void openReelAvailable().then((yes) => {
      if (active) setServed(yes);
    });
    return () => {
      active = false;
    };
  }, []);
  return served;
}

/**
 * Experimental: the address that opens an edit timeline's latest revision in OpenReel (OpenReel downloads it from
 * the studio as a project backup: shots, holds as stills, transitions, subtitles, narration, BGM, and asks to open
 * it); null where the studio doesn't serve OpenReel.
 */
export function useOpenReelHref(projectName: string, timelineId: string): string | null {
  const served = useOpenReelAvailable();
  if (!served) return null;
  const pack = API.getEditTimelineOreelpackUrl(projectName, timelineId);
  return `${OPENREEL_PAGE}#/arcreel?pack=${encodeURIComponent(pack)}`;
}

/** The browser tab the studio keeps OpenReel in (one, reused). */
const OPENREEL_TAB = "youreel-openreel";

/**
 * Open an episode in OpenReel, in its own reused tab: when that tab already shows OpenReel, it switches to the
 * episode there (OpenReel's `youreelOpenPack`, as its episode selector does) instead of reloading; otherwise the tab
 * loads OpenReel with the episode. Must be called from the click itself (the tab opens before the timeline list
 * arrives, so pop-up blockers allow it).
 */
export function openEpisodeInOpenReel(projectName: string, episode: number): void {
  const tab = window.open("", OPENREEL_TAB);
  if (!tab) return;
  void API.listEditTimelines(projectName, episode)
    .then(({ timelines }) => {
      const timelineId = pickDefaultTimeline(timelines);
      if (!timelineId) {
        tab.close();
        return;
      }
      const pack = API.getEditTimelineOreelpackUrl(projectName, timelineId);
      const packUrl = new URL(pack, window.location.href).href;
      const open = (tab as Window & { youreelOpenPack?: (url: string) => void }).youreelOpenPack;
      if (typeof open === "function") open(packUrl);
      else tab.location.href = `${OPENREEL_PAGE}#/arcreel?pack=${encodeURIComponent(pack)}`;
      tab.focus();
    })
    .catch(() => tab.close());
}
