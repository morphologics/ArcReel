import { useTranslation } from "react-i18next";
import { EpisodeItemRef } from "@/components/shared/episode-item/EpisodeItemRef";

/**
 * Which units' requests the provider has already accepted.
 *
 * It takes its own line and isn't folded into the task status word: accepted means a retry may be billed twice, while the task status word answers
 * a different question (how far this attempt got). When several tasks on the same step are accepted, say it once and list the units;
 * when no task has been accepted it doesn't appear: without this fact there's no empty sentence.
 *
 * 哪些单元的请求供应商已经收单。
 *
 * 它自己占一行，不折进任务状态词里：已收单意味着重试可能重复计费，而任务状态词回答的是
 * 另一个问题（这次尝试跑到哪了）。同一步上多个任务已收单时只说一次、列出单元；
 * 没有已收单的任务时不出现——没有这条事实就不摆一句空话。
 */
export function CheckpointNote({ unitIds }: { unitIds: string[] }) {
  const { t } = useTranslation("workflow");
  if (unitIds.length === 0) return null;
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
      {unitIds.map((unitId) => (
        <EpisodeItemRef key={unitId} itemId={unitId} />
      ))}
      <span>{t("checkpoint_submitted")}</span>
    </p>
  );
}
