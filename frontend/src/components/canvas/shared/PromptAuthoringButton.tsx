import { useTranslation } from "react-i18next";
import { PenLine } from "lucide-react";
import { promptAuthoringResourceId } from "@/actions/generation";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { usePromptAuthoringStore, type PromptAuthoringScope } from "@/stores/prompt-authoring-store";
import { useActiveResourceIds } from "@/stores/tasks-store";

interface Props {
  projectName: string;
  episode: number;
  scope: PromptAuthoringScope;
  currentEntryId?: string | null;
  variant?: "outline" | "ghost";
  /** Icon only, the label as its tooltip (used beside a section heading). */
  iconOnly?: boolean;
}

/** 打开「编写提示词」弹窗的入口；本集的提示词编写在跑时禁用。 */
export function PromptAuthoringButton({
  projectName,
  episode,
  scope,
  currentEntryId,
  variant = "outline",
  iconOnly = false,
}: Props) {
  const { t } = useTranslation("dashboard");
  const open = usePromptAuthoringStore((s) => s.open);
  const busy = useActiveResourceIds("text_episode_script", projectName).has(promptAuthoringResourceId(episode));
  if (iconOnly) {
    const label = busy ? t("prompt_authoring_busy") : t("prompt_authoring_open");
    return (
      <Tooltip>
        <TooltipTrigger
          render={
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={t("prompt_authoring_open")}
              disabled={busy}
              onClick={() => open({ projectName, episode, scope, currentEntryId })}
            />
          }
        >
          <PenLine aria-hidden />
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    );
  }
  return (
    <Button
      variant={variant}
      size="sm"
      disabled={busy}
      title={busy ? t("prompt_authoring_busy") : undefined}
      onClick={() => open({ projectName, episode, scope, currentEntryId })}
    >
      <PenLine aria-hidden data-icon="inline-start" />
      {t("prompt_authoring_open")}
    </Button>
  );
}
