import { useTranslation } from "react-i18next";
import { Empty, EmptyDescription, EmptyHeader } from "@/components/ui/empty";
import { DEMO_SCRIPTED_EPISODE } from "./demo-project";

/**
 * The episodes in the demo project that have no script.
 *
 * The demo only completes episode 1; the other episodes have titles only. A real project would enter source slice review here, but the demo
 * has no source to slice, so it explains the situation directly and points back to episode 1, instead of leaving people guessing at an empty canvas.
 *
 * 演示项目里没有剧本的那几集。
 *
 * 演示只把第 1 集做完整，其余分集只有标题。真实项目在这个位置会进源文切片审阅，但演示
 * 没有源文可切，所以直接说明情况并指回第 1 集，而不是让人对着一个空画布猜。
 */
export function DemoEpisodePlaceholder() {
  const { t } = useTranslation("onboarding");

  return (
    <Empty className="min-h-0 flex-1">
      <EmptyHeader>
        {/* The demo ledger is sorted by episode ID ascending; the episode ID is the episode order. / 演示账本按集 ID 升序排列，集 ID 即播出位置。 */}
        <EmptyDescription>{t("demo_episode_placeholder", { position: DEMO_SCRIPTED_EPISODE })}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}
