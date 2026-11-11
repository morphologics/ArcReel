import type { TFunction } from "i18next";
import type { VersionInfo } from "@/api";
import { formatCost } from "@/utils/cost-format";

/** Where a version came from: generated, image edit, upload, or restored from another version. */
export function versionOrigin(version: VersionInfo, t: TFunction): string {
  if (version.source === "manual_upload") return t("version_origin_upload");
  if (version.source === "image_edit") return t("version_origin_edit");
  if (version.restored_from != null) return t("version_origin_restored", { version: version.restored_from });
  return t("version_origin_generated");
}

/** Local "11-10 14:02" (year added when not this year). */
export function versionTime(version: VersionInfo): string {
  const date = new Date(version.created_at);
  if (Number.isNaN(date.getTime())) return version.created_at;
  const pad = (n: number) => String(n).padStart(2, "0");
  const year = date.getFullYear() !== new Date().getFullYear() ? `${date.getFullYear()}-` : "";
  return `${year}${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** "$0.45" when the version's generation call was priced; null when unknown. */
export function versionCost(version: VersionInfo): string | null {
  const g = version.generation;
  if (!g || g.cost_amount == null) return null;
  return formatCost({ [g.currency]: g.cost_amount });
}

export function versionSize(version: VersionInfo): string | null {
  if (!version.file_size) return null;
  const mb = version.file_size / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(version.file_size / 1024))} KB`;
}

/** One line for the strip's menu: "v3 · 11-10 14:02 · Generated · seedance · $0.45". */
export function versionMenuLine(version: VersionInfo, t: TFunction): string {
  return [
    `v${version.version}`,
    versionTime(version),
    versionOrigin(version, t),
    version.generation?.model,
    versionCost(version),
  ]
    .filter(Boolean)
    .join(" · ");
}
