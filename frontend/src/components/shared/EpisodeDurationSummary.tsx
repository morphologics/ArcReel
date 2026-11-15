import { cn } from "cn";
import { AlertTriangle } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * "Episode total X s / target N s" comparison in the script plan review panel.
 *
 * The target is soft: exceeding it only switches to a hint style stating by how much; the confirm button and later generation are unaffected;
 * the product doesn't hard-cap creative dimensions (see `.out-of-scope/product-enforced-creative-limits.md` in the repo).
 * Renders nothing when no target is set (`targetSeconds` is null): without a target there's no fact to compare.
 *
 * The total is the sum of per-entry durations of the current draft (including the user's unsaved changes), so changing one duration is reflected immediately.
 *
 * 脚本规划审核面板的「本集合计 X 秒 / 目标 N 秒」对比。
 *
 * 目标是软的：超出只换成提示样式并说明超了多少，确认按钮与后续生成都不受影响——
 * 产品不对创作维度做硬性截断（见仓库 `.out-of-scope/product-enforced-creative-limits.md`）。
 * 未设目标（`targetSeconds` 为 null）时不渲染任何东西：没有目标就没有可对比的事实。
 *
 * 合计取当前草稿（含用户未保存的改动）逐条时长之和，因此改一条时长即时反映在对比上。
 */
export interface EpisodeDurationSummaryProps {
  /** Sum of this episode's entry durations (seconds). / 本集各条目时长之和（秒）。 */
  totalSeconds: number;
  /** Project-level per-episode target duration (seconds); null = no target, nothing rendered. / 项目级单集目标时长（秒）；null = 未设目标，不渲染。 */
  targetSeconds: number | null;
  className?: string;
}

export function EpisodeDurationSummary({
  totalSeconds,
  targetSeconds,
  className,
}: EpisodeDurationSummaryProps) {
  const { t } = useTranslation("dashboard");
  if (targetSeconds == null) return null;

  const over = totalSeconds - targetSeconds;
  const exceeded = over > 0;

  return (
    <p
      className={cn("flex items-center gap-1.5 text-xs", exceeded ? "text-warn" : "text-muted-foreground", className)}
    >
      {exceeded && <AlertTriangle aria-hidden className="size-3.5 shrink-0 text-warn" />}
      {exceeded
        ? t("episode_duration_over_target", { total: totalSeconds, target: targetSeconds, over })
        : t("episode_duration_vs_target", { total: totalSeconds, target: targetSeconds })}
    </p>
  );
}
