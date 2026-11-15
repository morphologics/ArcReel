import { useTranslation } from "react-i18next";
import { LayeredModelFields, type LayeredSubField } from "./LayeredModelFields";

/** Text task tier values (docs/adr/0051). Field names are tier keys: call sites are assigned to tiers fixed in code, and users only configure each tier's backend. / 文本任务档位取值（docs/adr/0051）。字段名即档位键：调用点在代码里固定归档，用户只配置每档 backend。 */
export interface TextTierValue {
  default: string;
  simple: string;
  complex: string;
}

/** Breakdown tiers inside the collapsible section; "Default model" is the default layer and not among them. / 折叠区内的细分档位；「默认模型」是默认层、不在其中。 */
const SUB_TIERS = [
  { key: "simple", labelKey: "text_tier_simple_label", captionKey: "text_tier_simple_caption" },
  { key: "complex", labelKey: "text_tier_complex_label", captionKey: "text_tier_complex_caption" },
] as const;

export interface TextTierFieldsProps {
  value: TextTierValue;
  onChange: (next: TextTierValue) => void;
  options: string[];
  providerNames: Record<string, string>;
  /** "provider/model" -> model name in the current language, same source as `providerNames`; missing keys fall back to model id. / "provider/model" → 当前语言下的模型名，与 `providerNames` 同源；缺键退回 model id。 */
  modelNames?: Record<string, string>;
  /** Label of the default tier's empty option: "Auto select" in global settings, "Use global default" in project/wizard. / 默认档空选项的标签：全局设置为「自动选择」，项目/向导为「使用全局默认」。 */
  defaultLabel: string;
  /** Secondary hint beside the default tier's empty option (e.g. "Auto" in global settings). / 默认档空选项旁的次要提示（如全局设置的「自动」）。 */
  defaultHint?: string;
  /**
   * Actual effective value of each tier when empty (computed by the project-first resolution chain and passed in, format provider/model).
   * The trigger button shows "Follow... · effective value", so the user sees the inherited result. Global settings' default tier doesn't pass it (it is the baseline).
   *
   * 各档留空时的实际生效值（按项目优先解析链算好后传入，格式 provider/model）。
   * 触发按钮以「跟随…· 生效值」呈现，让用户看到继承结果。全局设置的默认档不传（它即基准）。
   */
  fallbacks?: Partial<TextTierValue>;
  /**
   * Text model auto-selected when each tier and its inheritance sources are all empty (auto-inference is the same for all tiers); null means no provider is available.
   * Only shown when a tier's `fallbacks` is absent; `fallbacks` must already fall through to the global default model.
   *
   * 各档与其继承来源全空时自动选中的文本模型（各档位的自动推断相同）；null 表示没有可用供应商。
   * 只在某档的 `fallbacks` 缺席时显示——`fallbacks` 须已穿透到全局默认模型。
   */
  auto?: string | null;
  /** Omitted renders only the default tier; the creation wizard exposes only the default layer (docs/adr/0054). / 省略即只渲染默认档——创建向导只暴露默认层（docs/adr/0054）。 */
  showTiers?: boolean;
}

/**
 * Shared component for text tier config: the default model main dropdown always visible, the simple task / complex task tiers inside the "model per purpose"
 * collapsible section, each tier with an always-visible note listing the call sites it covers, and the card bottom noting the Agent provider boundary. Reused in global settings, project settings and
 * the creation wizard, keeping a single source of truth for the coverage text. Tier resolution behavior is unaffected by this component's shape.
 *
 * 文本档位配置的同源组件：默认模型主下拉常驻，简单任务 / 复杂任务两档收在「按用途指定模型」
 * 折叠区内，每档常驻说明列出覆盖调用点，卡片底部注明 Agent 供应商边界。全局设置、项目设置、
 * 创建向导三处复用，保证覆盖范围文案单一真相源。档位的解析行为不受本组件形态影响。
 */
export function TextTierFields({
  value,
  onChange,
  options,
  providerNames,
  modelNames,
  defaultLabel,
  defaultHint,
  fallbacks,
  auto,
  showTiers = true,
}: TextTierFieldsProps) {
  const { t } = useTranslation("templates");

  const subFields: LayeredSubField[] = SUB_TIERS.map(({ key, labelKey, captionKey }) => ({
    key,
    label: t(labelKey),
    caption: t(captionKey),
    value: value[key],
    options,
    effective: fallbacks?.[key] || undefined,
    auto,
    onChange: (next: string) => onChange({ ...value, [key]: next }),
  }));

  return (
    <LayeredModelFields
      defaultLabel={t("text_tier_default_label")}
      defaultValue={value.default}
      defaultOptions={options}
      onDefaultChange={(next) => onChange({ ...value, default: next })}
      emptyLabel={defaultLabel}
      emptyHint={defaultHint}
      defaultEffective={fallbacks?.default || undefined}
      defaultAuto={auto}
      providerNames={providerNames}
      modelNames={modelNames}
      subFields={showTiers ? subFields : undefined}
      footnote={
        <p className="border-t border-border pt-3 text-xs text-muted-foreground">
          {t("text_tier_agent_boundary")}
        </p>
      }
    />
  );
}
