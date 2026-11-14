import { useEffect, useState } from "react";
import { API } from "@/api";

/** Where YouReel Studio serves its OpenReel fork, next to the studio (deploy/studio/OPENREEL.md). */
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

/**
 * Experimental: the address that opens an edit timeline's latest revision in OpenReel (OpenReel downloads it from
 * the studio as a project backup: shots, holds as stills, transitions, subtitles, narration, BGM, and asks to open
 * it); null where the studio doesn't serve OpenReel.
 */
export function useOpenReelHref(projectName: string, timelineId: string): string | null {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    let active = true;
    void openReelAvailable().then((yes) => {
      if (active) setShown(yes);
    });
    return () => {
      active = false;
    };
  }, []);
  if (!shown) return null;
  const pack = API.getEditTimelineOreelpackUrl(projectName, timelineId);
  return `${OPENREEL_PAGE}#/arcreel?pack=${encodeURIComponent(pack)}`;
}
