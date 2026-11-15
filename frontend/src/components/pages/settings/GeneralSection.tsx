import { Play } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { useOnboardingStore } from "@/stores/onboarding-store";

import { SettingRow } from "./SettingRow";

/**
 * Global settings "General": replaying the onboarding guide. Interface language and theme are in "Options".
 *
 * 全局设置「通用」：重看新手引导。界面语言与主题在「选项」里。
 */
export function GeneralSection() {
  const { t } = useTranslation(["dashboard", "onboarding"]);
  const startTour = useOnboardingStore((s) => s.start);

  return (
    <div className="flex flex-col gap-6">
      <h2 className="text-lg font-medium">{t("dashboard:settings_general")}</h2>
      <div className="flex flex-col divide-y divide-border rounded-lg border border-border bg-card">
        <SettingRow
          label={<span className="text-sm font-medium">{t("onboarding:replay_title")}</span>}
          description={t("onboarding:replay_desc")}
        >
          <Button variant="outline" onClick={startTour}>
            <Play aria-hidden data-icon="inline-start" />
            {t("onboarding:replay_action")}
          </Button>
        </SettingRow>
      </div>
    </div>
  );
}
