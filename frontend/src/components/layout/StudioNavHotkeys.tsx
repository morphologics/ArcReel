import { useLocation } from "wouter";
import { useHotkeys } from "@/preferences/hotkeys";
import { useProjectsStore } from "@/stores/projects-store";
import { useAppStore } from "@/stores/app-store";
import { episodesViewPath } from "@/components/canvas/episodes/episodes-view-model";
import { ROUTE_APP_PROJECTS, ROUTE_APP_SETTINGS, episodeEditViewPath } from "@/app-routes";

/**
 * YouReel: the workspace's navigation hot keys (Settings → Hot keys, "Everywhere"): Overview, Episodes, Characters,
 * Scenes, Props, Settings, the editor for this episode (or the first one) and the agent panel. Mounted once in the
 * project layout, so they work on every page of a project. OpenReel reads the same bindings for its way back here.
 */
export function StudioNavHotkeys() {
  const [location, setLocation] = useLocation();
  const { currentProjectData, currentProjectName } = useProjectsStore();

  const project = currentProjectName ? `~${ROUTE_APP_PROJECTS}/${encodeURIComponent(currentProjectName)}` : null;
  const go = (path: string) => (project ? () => setLocation(`${project}${path === "/" ? "" : path}`) : undefined);
  const episodes = currentProjectData?.episodes ?? [];
  const routeEpisode = /\/episodes\/(\d+)/.exec(location)?.[1];
  const editEpisode = routeEpisode !== undefined ? Number(routeEpisode) : episodes[0]?.episode;

  useHotkeys("global", {
    handlers: {
      "nav.overview": go("/"),
      "nav.episodes": go(episodesViewPath()),
      "nav.characters": go("/characters"),
      "nav.scenes": go("/scenes"),
      "nav.props": go("/props"),
      "nav.settings": () => setLocation(`~${ROUTE_APP_SETTINGS}`),
      "nav.editor": editEpisode !== undefined ? go(episodeEditViewPath(editEpisode)) : undefined,
      "nav.agent": () => useAppStore.getState().toggleAssistantPanel(),
    },
  });
  return null;
}
