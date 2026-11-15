import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { DurationTierPicker } from "@/components/shared/DurationTierPicker";
import { EpisodeTargetDurationField } from "@/components/shared/EpisodeTargetDurationField";
import { SpeechRateField } from "@/components/shared/SpeechRateField";
import { ModelConfigSection, type ModelConfigValue } from "@/components/shared/ModelConfigSection";
import { NarrationDeliveryFields } from "@/components/shared/NarrationDeliveryFields";
import type { NarrationDeliveryValue } from "@/components/shared/narration-delivery";
import type { AutoBackendsResponse, ProviderInfo } from "@/types";
import type { CustomProviderInfo } from "@/types/custom-provider";
import { WizardSection } from "./WizardSection";

export interface WizardGenerationData {
  options: {
    video: string[];
    image: string[];
    text: string[];
    audio: string[];
    providerNames: Record<string, string>;
    modelNames: Record<string, string>;
  };
  providers: ProviderInfo[];
  customProviders: CustomProviderInfo[];
  globalDefaults: {
    video: string;
    videoI2V: string;
    videoR2V: string;
    image: string;
    imageT2I: string;
    imageI2I: string;
    textDefault: string;
    textSimple: string;
    textComplex: string;
    /** Model the backend auto-selects when every layer is empty; absent if the fetch fails. / 各层全空时后端自动选中的模型；拉取失败即缺席。 */
    auto?: AutoBackendsResponse | null;
  };
}

export interface WizardDurationValue {
  /** Target total duration of an ad project (seconds); null when the "Custom" input is invalid. / 广告项目的目标总时长（秒）；「自定义」输入无效时为 null。 */
  targetDuration: number | null;
  /** Target duration per episode (seconds); null = no target. Not used by ad projects. / 单集目标时长（秒）；null = 不设目标。广告项目不使用。 */
  episodeTargetDuration: number | null;
  /** Voiceover speech rate estimate (reading units / second); null = estimate from the project language's default. / 口播语速估算（阅读单位 / 秒）；null = 按项目语言的默认值估算。 */
  speechRate: number | null;
}

export interface WizardStepGenerationProps {
  isAd: boolean;
  usesReferenceImages: boolean;
  duration: WizardDurationValue;
  onDurationChange: (next: WizardDurationValue) => void;
  models: ModelConfigValue;
  onModelsChange: (next: ModelConfigValue) => void;
  narration: NarrationDeliveryValue;
  onNarrationChange: (next: NarrationDeliveryValue) => void;
  data: WizardGenerationData | null;
  error: string | null;
}

export function WizardStepGeneration({
  isAd,
  usesReferenceImages,
  duration,
  onDurationChange,
  models,
  onModelsChange,
  narration,
  onNarrationChange,
  data,
  error,
}: WizardStepGenerationProps) {
  const { t } = useTranslation(["dashboard", "common"]);

  return (
    <div className="flex flex-col gap-6">
      <WizardSection
        title={t("wizard_duration_title")}
        description={isAd ? t("wizard_duration_ad_desc") : t("wizard_duration_desc")}
      >
        <div className="flex flex-col gap-4">
          {isAd ? (
            <DurationTierPicker
              value={duration.targetDuration}
              onChange={(targetDuration) => onDurationChange({ ...duration, targetDuration })}
            />
          ) : (
            <EpisodeTargetDurationField
              value={duration.episodeTargetDuration}
              onChange={(episodeTargetDuration) => onDurationChange({ ...duration, episodeTargetDuration })}
            />
          )}
          {/* The project has no language fact yet (written by content analysis), so units are shown for an unknown language / 项目还没有语言事实（由内容分析写入），单位按未知语言呈现 */}
          <SpeechRateField
            value={duration.speechRate}
            onChange={(speechRate) => onDurationChange({ ...duration, speechRate })}
          />
        </div>
      </WizardSection>

      <WizardSection title={t("wizard_models_title")}>
        {!data && !error && (
          <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {t("wizard_models_loading")}
          </p>
        )}
        {error && (
          <Alert variant="destructive">
            <AlertTitle>{t("wizard_models_load_failed")}</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {/* The create wizard exposes only the default layer (docs/adr/0054); per-purpose refinement is left to the project settings page / 创建向导只暴露默认层（docs/adr/0054），按用途细分留给项目设置页 */}
        {data && (
          <ModelConfigSection
            showSubFields={false}
            value={models}
            onChange={onModelsChange}
            providers={data.providers}
            customProviders={data.customProviders}
            options={{
              videoBackends: data.options.video,
              imageBackends: data.options.image,
              textBackends: data.options.text,
              providerNames: data.options.providerNames,
              modelNames: data.options.modelNames,
            }}
            globalDefaults={data.globalDefaults}
            usesReferenceImages={usesReferenceImages}
            // Ad projects plan shot by shot against the target total duration; the default duration isn't exposed
            // 广告项目按目标总时长逐个分镜规划，不暴露默认时长
            enable={isAd ? { duration: false } : undefined}
          />
        )}
      </WizardSection>

      {data && (
        <WizardSection title={t("project_narration_delivery_title")}>
          <NarrationDeliveryFields
            value={narration}
            onChange={onNarrationChange}
            audioBackends={data.options.audio}
            providerNames={data.options.providerNames}
            modelNames={data.options.modelNames}
          />
        </WizardSection>
      )}
    </div>
  );
}
