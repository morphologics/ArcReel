import type { TFunction } from "i18next";
import type { DurationOutOfRangeReason } from "@/hooks/useModelCapabilities";

const INCOMPATIBLE_KEY: Record<DurationOutOfRangeReason, string> = {
  model: "duration_incompatible_warning",
  resolution: "duration_incompatible_resolution_warning",
  reference: "duration_incompatible_reference_warning",
};

/**
 * Explanation for a duration outside the options. Copy is split by cause: "model does not support" only when the model's full set lacks the value; when narrowed by resolution / reference-image
 * path coupling constraints, say which one, so the user changes the matching setting.
 *
 * 时长不在档位内的说明。文案按成因分开：模型全集就不含该值才是「模型不支持」，被分辨率 / 参考图
 * 路径的联动约束收窄掉时说清是哪一条，用户据此改对应设置。
 */
export function durationIncompatibleLabel(
  t: TFunction,
  seconds: number,
  options: readonly number[],
  reason: DurationOutOfRangeReason | null | undefined,
): string {
  return t(INCOMPATIBLE_KEY[reason ?? "model"], { value: seconds, supported: options.join(", ") });
}
