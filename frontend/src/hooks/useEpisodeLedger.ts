import { useProjectsStore } from "@/stores/projects-store";
import type { EpisodeLedger } from "@/utils/episode-display";

const EMPTY_LEDGER: EpisodeLedger = [];

/** The current project's episode ledger, ordered by episode order; empty while the project isn't loaded. / 当前项目的分集账本，排列即播出顺序；项目未载入时为空。 */
export function useEpisodeLedger(): EpisodeLedger {
  return useProjectsStore((s) => s.currentProjectData?.episodes) ?? EMPTY_LEDGER;
}
