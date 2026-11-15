import { AlertTriangle } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { ConfigIssue } from "@/stores/config-status-store";

/**
 * In-place config issue notice: appears only in the section the issue belongs to. Full-bleed sections get it at the top from the settings page,
 * width-limited sections place it themselves after the header note. A single issue becomes the title; multiple are all listed.
 *
 * 配置问题就地提示：只出现在问题所属分区。全出血分区由设置页放在分区顶部，
 * 限宽分区由分区自己放在页头说明之后。只有一条问题时以它为标题，多条时列出全部。
 */
export function ConfigIssueNotice({ issues }: { issues: ConfigIssue[] }) {
  const { t } = useTranslation("dashboard");
  const titleId = useId();
  if (issues.length === 0) return null;
  const [only] = issues;
  return (
    // A static notice present when the page opens; don't use Alert's default role="alert", so screen readers aren't interrupted immediately
    // 页面打开时就在的静态提示，不用 Alert 默认的 role="alert"，以免读屏立即打断播报
    <Alert role="note" aria-labelledby={titleId}>
      <AlertTriangle aria-hidden />
      <AlertTitle id={titleId}>{t(issues.length === 1 ? only.label : "config_incomplete")}</AlertTitle>
      {issues.length === 1 ? (
        only.description && <AlertDescription>{t(only.description)}</AlertDescription>
      ) : (
        <AlertDescription>
          <ul className="flex flex-col gap-0.5">
            {issues.map((issue) => (
              <li key={issue.key}>{t(issue.label)}</li>
            ))}
          </ul>
        </AlertDescription>
      )}
    </Alert>
  );
}
