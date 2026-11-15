/**
 * LayeredModelFields: the shared interaction shape for "default model + model per purpose" (docs/adr/0054).
 *
 * Shared by text tiers, image task-type buckets and video task-type buckets: an always-visible default main dropdown, with a collapsed-by-default
 * section below holding the breakdown items. Global settings and project settings reuse the same component; the creation wizard passes no subFields, leaving only the default layer.
 *
 * When a breakdown item is empty the trigger button shows the final effective model after fall-through resolution, so the user sees the model that will really run without expanding;
 * when all layers are empty it shows the model auto-selected by the backend. The resolution result is computed by the caller per layer key and passed in; this component holds no layer knowledge.
 *
 * UI text consistently says "model per purpose" and never uses the words "capability" or "bucket" (see the task-type bucket entry in GLOSSARY.md).
 *
 * LayeredModelFields —— 「默认模型 + 按用途指定模型」的同源交互形态（docs/adr/0054）。
 *
 * 文本档位、图片任务类型桶、视频任务类型桶三处共用：一个常驻的默认主下拉，下方一个默认收起的
 * 折叠区收纳细分项。全局设置、项目设置两层复用同一组件，创建向导不传 subFields 即只剩默认层。
 *
 * 细分项留空时触发按钮显示穿透演算后的最终生效模型，用户不展开也能看到会真正执行的模型；
 * 各层全空时显示后端自动选中的模型。演算结果由调用方按各层键位算好传入，本组件不持有层级知识。
 *
 * 界面文案统一用「按用途指定模型」，不出现「能力」「桶」字样（见 GLOSSARY.md 任务类型桶词条）。
 */

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { InlineWarning } from "@/components/shared/InlineWarning";
import { ProviderModelSelect } from "@/components/shared/ProviderModelSelect";
import type { GenerationTypeBucket } from "@/types/system";

/** UI labels and override descriptions of task-type buckets, one set of text shared by the image and video call sites. / 任务类型桶的界面标签与覆盖说明，图片 / 视频两处调用点共用一份文案。 */
export function useGenerationTypeBucketLabels(): Record<GenerationTypeBucket, { label: string; caption?: string }> {
  const { t } = useTranslation("templates");
  return useMemo(
    () => ({
      t2i: { label: t("bucket_t2i_label") },
      i2i: { label: t("bucket_i2i_label"), caption: t("bucket_i2i_caption") },
      i2v: { label: t("bucket_i2v_label"), caption: t("bucket_i2v_caption") },
      r2v: { label: t("bucket_r2v_label"), caption: t("bucket_r2v_caption") },
    }),
    [t],
  );
}

export interface LayeredSubField {
  key: string;
  /** Breakdown item label, already passed through t(), named by generation path (text-to-image / image-to-image / image-to-video / reference-to-video). / 已 t() 的细分项标签，以生成路径命名（文生图 / 图生图 / 图生视频 / 参考生视频）。 */
  label: string;
  /** Override scope description, already passed through t(), shown below the dropdown; only given when the scope isn't self-evident from the label. / 已 t() 的覆盖范围说明，展示在下拉下方；仅在覆盖范围不可从标签自明时提供。 */
  caption?: string;
  value: string;
  /** Candidates of this breakdown item (the breakdown layer filters by capability). / 该细分项的候选（细分层按能力过滤）。 */
  options: string[];
  /** Final effective model when empty (provider/model); omitted when it can't be resolved. / 留空时的最终生效模型（provider/model）；演算不出即省略。 */
  effective?: string;
  /** Model auto-selected when all layers are empty; same semantics as `autoValue` of `ProviderModelSelect`. / 各层全空时自动选中的模型，语义同 `ProviderModelSelect` 的 `autoValue`。 */
  auto?: string | null;
  onChange: (next: string) => void;
}

export interface LayeredModelFieldsProps {
  /** Accessible label of the default layer dropdown; the visual label is carried by the outer card title. / 默认层下拉的无障碍标签；视觉标签由外层卡片标题承担。 */
  defaultLabel: string;
  defaultValue: string;
  /** Default layer candidates, unfiltered: the default layer promises no capability, and unmet capabilities are caught by the resolution gate's error. / 默认层候选，不过滤——默认层不承诺能力，能力不满足由解析闸报错兜底。 */
  defaultOptions: string[];
  onDefaultChange: (next: string) => void;
  /** Label of the default layer's empty option: "Auto select" in global settings, "Use global default" in project settings. / 默认层空值选项的标签：全局设置为「自动选择」，项目设置为「使用全局默认」。 */
  emptyLabel: string;
  emptyHint?: string;
  /** Effective model when the default layer is empty (project layer = global default layer); the global layer is the baseline and doesn't pass it. / 默认层留空时的生效模型（项目层 = 全局默认层）；全局层是基准、不传。 */
  defaultEffective?: string;
  /**
   * Model auto-selected when the default layer and its inheritance sources are all empty; same semantics as `autoValue` of `ProviderModelSelect`.
   * Only passed when there is no inheritance layer beyond `defaultEffective`: when any layer in the inheritance chain has a value, auto-selection doesn't happen.
   *
   * 默认层与其继承来源全空时自动选中的模型，语义同 `ProviderModelSelect` 的 `autoValue`。
   * 仅在 `defaultEffective` 之外再无继承层时传入：继承链上任一层有值时，自动选择不会发生。
   */
  defaultAuto?: string | null;
  providerNames: Record<string, string>;
  /** "provider/model" -> model name in the current language, same source as `providerNames`; missing keys fall back to model id. / "provider/model" → 当前语言下的模型名，与 `providerNames` 同源；缺键退回 model id。 */
  modelNames?: Record<string, string>;
  /**
   * Extra info for dropdown option rows. The second argument is the `key` of the breakdown item that owns the dropdown; the default layer doesn't pass it, since it spans all purposes
   * and has no single generation path. The default layer and breakdown items on the same screen share one renderer, so path-dependent values must branch on this argument,
   * otherwise one dropdown's path would be taken as the path of all dropdowns.
   *
   * 下拉选项行的补充信息。第二个参数是发起该下拉的细分项 `key`；默认层不传——它跨全部用途，
   * 没有单一生成路径。同屏的默认层与各细分项共用一个渲染器，路径相关的取值必须按此参数分流，
   * 否则一个下拉的路径会被当成全部下拉的路径。
   */
  renderOptionMeta?: (fullValue: string, subFieldKey?: string) => React.ReactNode;
  /** Extra content between the default layer dropdown and the collapsible section (model spec bar, resolution, duration, etc.). / 默认层下拉与折叠区之间的附加内容（模型规格条、分辨率、时长等）。 */
  children?: React.ReactNode;
  /** Breakdown items; omitted or an empty array means the collapsible section isn't rendered (the creation wizard exposes only the default layer). / 细分项；省略或空数组即不渲染折叠区（创建向导只暴露默认层）。 */
  subFields?: LayeredSubField[];
  /**
   * Failure state of fetching breakdown candidates: when non-empty, the collapsible section is rendered (even with no breakdown items) with error text and a
   * retry entry inside it; the text is maintained by this component and the caller only passes the retry callback, so both call sites word it the same.
   *
   * Only for "fetch failed"; while candidates are still loading, or succeeded but empty, this isn't passed; in those cases the collapsible section renders
   * based on whether `subFields` exist, with no error narrative.
   *
   * 细分项候选拉取失败态：非空即渲染折叠区（哪怕一个细分项都没有）并在其中给出错误文案与
   * 重试入口，文案由本组件维护、调用方只传重试回调，两处调用点因而措辞一致。
   *
   * 只对应「拉取失败」；候选仍在加载、或成功但为空都不传此项——那两种情况下折叠区按
   * `subFields` 自身的有无渲染，不出现错误叙事。
   */
  subFieldsError?: {
    onRetry: () => void;
    /** Retry in flight; when set the button is greyed out, so clicks under a slow response don't feel unresponsive. / 重试在途；置位时按钮灰化，避免慢响应下点击毫无反馈。 */
    retrying?: boolean;
  };
  /** Supplementary note always shown after the collapsible section (e.g. the Agent boundary for text tiers). / 折叠区之后常驻的补充说明（如文本档位的 Agent 边界）。 */
  footnote?: React.ReactNode;
}

export function LayeredModelFields({
  defaultLabel,
  defaultValue,
  defaultOptions,
  onDefaultChange,
  emptyLabel,
  emptyHint,
  defaultEffective,
  defaultAuto,
  providerNames,
  modelNames,
  renderOptionMeta,
  children,
  subFields,
  subFieldsError,
  footnote,
}: LayeredModelFieldsProps) {
  const { t } = useTranslation(["templates", "common"]);
  const fields = subFields ?? [];
  const configuredCount = fields.filter((f) => !!f.value).length;
  const hasError = !!subFieldsError;

  // Collapsed by default; but initially expanded when breakdown items are already specified, since collapsing would make effective overrides completely invisible,
  // and the user changing the default model would think the change takes effect immediately. After mount, the user opens and closes it.
  // 默认收起；但已指定过细分项时初始展开——收起会让已生效的覆盖完全不可见，
  // 用户改默认模型时会误以为改动即刻生效。挂载后由用户自行开合。
  const [open, setOpen] = useState(() => fields.some((f) => !!f.value) || hasError);

  // Candidate fetch failures usually happen after mount (only when the async request returns), too late for the open computed in initial state;
  // when the error goes from absent to present, force-expand once so the user doesn't have to expand the section to see the failure notice. After that the user may
  // collapse it manually; don't keep reopening while hasError stays true, since the retry button already gives the next step.
  // 候选拉取失败通常发生在挂载之后（异步请求才回来），初始 state 已算过的 open 赶不上；
  // 错误从无到有时强制展开一次，确保用户不必先展开折叠区才能看到失败提示。之后允许用户
  // 手动收起——不在 hasError 保持 true 期间反复重开，重试按钮已给出下一步。
  const [prevHasError, setPrevHasError] = useState(hasError);
  if (hasError !== prevHasError) {
    setPrevHasError(hasError);
    if (hasError) setOpen(true);
  }

  return (
    <div className="flex flex-col gap-3">
      <ProviderModelSelect
        value={defaultValue}
        options={defaultOptions}
        providerNames={providerNames}
        modelNames={modelNames}
        onChange={onDefaultChange}
        allowDefault
        defaultLabel={emptyLabel}
        defaultHint={emptyHint}
        // When all layers are empty and the auto-select preview is also absent, the trigger button falls to the placeholder. What's given here is the empty-value semantics itself
        // ("Auto select" / "Use global default"); otherwise the generic "Select model" would read as if a choice were required.
        // 全层皆空且自动选择预览也缺席时，触发按钮落到 placeholder。这里给的是空值语义本身
        // （「自动选择」/「使用全局默认」），否则通用的「选择模型」会读成这项必须选。
        placeholder={emptyLabel}
        fallbackValue={defaultEffective}
        autoValue={defaultAuto}
        aria-label={defaultLabel}
        renderOptionMeta={renderOptionMeta}
      />

      {children}

      {(fields.length > 0 || hasError) && (
        <div className="border-t border-border pt-2">
          <Collapsible open={open} onOpenChange={setOpen}>
            <CollapsibleTrigger render={<Button variant="ghost" size="xs" className="-ml-2" />}>
              <ChevronRight aria-hidden className="transition-transform group-aria-expanded/button:rotate-90" />
              {t("model_bucket_section")}
              {configuredCount > 0 && (
                <Badge variant="secondary">{t("model_bucket_configured_count", { n: configuredCount })}</Badge>
              )}
            </CollapsibleTrigger>

            <CollapsibleContent className="mt-2">
              <div className="flex flex-col gap-4 border-l border-border pl-3">
                {subFieldsError ? (
                  <InlineWarning
                    message={t("model_bucket_candidates_error")}
                    action={{
                      label: t("common:retry"),
                      onClick: subFieldsError.onRetry,
                      disabled: subFieldsError.retrying,
                    }}
                  />
                ) : (
                  <p className="text-xs text-muted-foreground">{t("model_bucket_section_hint")}</p>
                )}
                {fields.map((field) => (
                  <div key={field.key} className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium text-muted-foreground">{field.label}</span>
                    <ProviderModelSelect
                      value={field.value}
                      options={field.options}
                      providerNames={providerNames}
                      modelNames={modelNames}
                      onChange={field.onChange}
                      allowDefault
                      defaultLabel={t("follow_model_default")}
                      placeholder={t("follow_model_default")}
                      fallbackValue={field.effective}
                      fallbackLabel={t("follow_model_default")}
                      autoValue={field.auto}
                      aria-label={field.label}
                      renderOptionMeta={
                        renderOptionMeta && ((fullValue: string) => renderOptionMeta(fullValue, field.key))
                      }
                    />
                    {field.caption && <p className="text-xs text-muted-foreground">{field.caption}</p>}
                  </div>
                ))}
              </div>
            </CollapsibleContent>
          </Collapsible>
        </div>
      )}

      {footnote}
    </div>
  );
}
