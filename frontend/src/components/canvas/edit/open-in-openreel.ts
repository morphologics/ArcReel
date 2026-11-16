import { useEffect, useState } from "react";
import { navigate } from "wouter/use-browser-location";
import { API } from "@/api";
import { pickDefaultTimeline } from "./timeline-view";

/** Where YouReel Studio serves its OpenReel fork, next to the studio. */
const OPENREEL_PAGE = "/reel/index.html";

// Whether this studio serves OpenReel (experimental, so far only on the DEV studio): asked once per page load. A
// studio without it answers the path with its own app page (SPA fallback), which doesn't name OpenReel.
let available: Promise<boolean> | null = null;
// The served OpenReel build: its main script's hash (assets/index-<hash>.js), new with every build. It goes into
// OpenReel's address as ?v=<build>, so a browser (tablets especially) can't keep showing an older cached page.
let build: string | null = null;
function openReelAvailable(): Promise<boolean> {
  available ??= fetch(OPENREEL_PAGE, { credentials: "same-origin", cache: "no-cache" })
    .then(async (response) => {
      if (!response.ok) return false;
      const html = await response.text();
      if (!html.includes("OpenReel")) return false;
      build = /assets\/index-([\w-]+)\.js/.exec(html)?.[1] ?? null;
      return true;
    })
    .catch(() => false);
  return available;
}

/** OpenReel's page with the served build as ?v= (known once the probe has answered). */
function openReelPage(): string {
  return build ? `${OPENREEL_PAGE}?v=${encodeURIComponent(build)}` : OPENREEL_PAGE;
}

/** Forget the cached probe so the next caller asks again. */
export function resetOpenReelProbeForTests(): void {
  available = null;
  build = null;
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
  return `${openReelPage()}#/arcreel?pack=${encodeURIComponent(pack)}`;
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
  void Promise.all([API.listEditTimelines(projectName, episode), openReelAvailable()])
    .then(([{ timelines }]) => {
      const timelineId = pickDefaultTimeline(timelines);
      if (!timelineId) {
        tab.close();
        return;
      }
      const pack = API.getEditTimelineOreelpackUrl(projectName, timelineId);
      const packUrl = new URL(pack, window.location.href).href;
      const open = (tab as Window & { youreelOpenPack?: (url: string) => void }).youreelOpenPack;
      if (typeof open === "function") open(packUrl);
      else tab.location.href = `${openReelPage()}#/arcreel?pack=${encodeURIComponent(pack)}`;
      tab.focus();
    })
    .catch(() => tab.close());
}

/** The browser tab name of the studio, so OpenReel's links back ("Open shot … in YouReel Studio") land in it. */
export const STUDIO_TAB = "youreel-studio";

/**
 * Let OpenReel move this studio tab to another page without reloading it: the tab takes the name
 * {@link STUDIO_TAB} (unless it already has one) and offers `window.youreelNavigate(url)`, which goes to a same-origin
 * `/app/…` address in-app. OpenReel finds the tab by name (window.open with an empty address doesn't navigate an
 * existing tab) and calls it, as the studio calls OpenReel's `youreelOpenPack`. Returns the undo (tests).
 */
export function exposeStudioNavigate(): () => void {
  if (!window.name) window.name = STUDIO_TAB;
  const target = window as Window & { youreelNavigate?: (url: string) => boolean };
  target.youreelNavigate = (url: string) => {
    let to: URL;
    try {
      to = new URL(url, window.location.href);
    } catch {
      return false;
    }
    if (to.origin !== window.location.origin || !to.pathname.startsWith("/app/")) return false;
    navigate(to.pathname + to.search + to.hash);
    return true;
  };
  return () => {
    delete target.youreelNavigate;
  };
}

