import type { TFunction } from "i18next";
import type { ReferenceVideoBucket, VideoCapabilityProblem } from "@/types";

/** User-readable name of the bucket the unit falls into, interpolated into the issue copy. / 单元所落桶的用户可读名，随问题文案插值。 */
export function bucketLabel(t: TFunction<"dashboard">, bucket: ReferenceVideoBucket): string {
  return t(bucket === "r2v" ? "reference_bucket_r2v" : "reference_bucket_i2v");
}

function tierProblemReasonKey(code: string): string {
  if (code.startsWith("video_capability_missing_")) return "reference_unit_tier_reason_unsupported";
  switch (code) {
    case "video_capability_reference_unavailable":
    case "model_not_in_provider_catalog":
    case "reference_capability_unavailable":
      return "reference_unit_tier_reason_removed";
    case "reference_supported_durations_missing":
      return "reference_unit_tier_reason_missing_tiers";
    case "reference_supported_durations_invalid":
      return "reference_unit_tier_reason_invalid_tiers";
    case "reference_supported_durations_incompatible":
      return "reference_unit_tier_reason_incompatible";
    default:
      return "reference_unit_tier_reason_unresolved";
  }
}

/**
 * The video request facts for the unit's bucket failed -> "Duration option unknown" label plus a hint stating only the cause and fix guidance (issue codes stay out of the UI).
 * The cause copy is interpolated per bucket: an i2v bucket failure names the image-to-video model, an r2v bucket failure names the reference-to-video model, never mixed.
 *
 * 所落桶的视频请求事实失败 → 「档位未知」标签与只写成因与修复指引的提示（问题码不进界面）。
 * 成因文案按桶插值：i2v 桶失败说图生视频模型，r2v 桶失败说参考生视频模型，不混用。
 */
export function tierProblemText(
  t: TFunction<"dashboard">,
  problem: VideoCapabilityProblem,
  bucket: ReferenceVideoBucket,
): { label: string; hint: string } {
  const bucketText = bucketLabel(t, bucket);
  return {
    label: t("reference_unit_tier_unknown_label", { bucket: bucketText }),
    hint: t("reference_unit_tier_unknown_hint", {
      bucket: bucketText,
      reason: t(tierProblemReasonKey(problem.code), { bucket: bucketText }),
    }),
  };
}
