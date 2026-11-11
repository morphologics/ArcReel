import { useTranslation } from "react-i18next";
import { History } from "lucide-react";
import {
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";
import { useResourceVersions, type ShotMediaResource } from "@/hooks/useResourceVersions";
import type { ShotMediaKind } from "@/stores/shot-media-store";
import { versionMenuLine } from "./version-summary";

const KINDS: { kind: ShotMediaKind; resource: ShotMediaResource; label: string }[] = [
  { kind: "storyboard", resource: "storyboards", label: "media_storyboard_title" },
  { kind: "video", resource: "videos", label: "media_video_title" },
];

/**
 * "Versions ›" on a shot of the strip: each medium's versions (v, time, origin, model, cost), newest first;
 * picking one previews it in that pane, "Show history" switches the pane to its History view. The lists load
 * when the submenu opens.
 */
export function ShotVersionsMenu({
  projectName,
  segmentId,
  onPick,
  onShowHistory,
}: {
  projectName: string;
  segmentId: string;
  onPick: (kind: ShotMediaKind, version: number) => void;
  onShowHistory: (kind: ShotMediaKind) => void;
}) {
  const { t } = useTranslation("dashboard");
  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger>
        <History aria-hidden />
        {t("version_mgmt")}
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent className="max-h-[70vh] min-w-64 overflow-y-auto">
        {KINDS.map(({ kind, resource, label }, index) => (
          <KindVersions
            key={kind}
            projectName={projectName}
            segmentId={segmentId}
            resource={resource}
            label={t(label)}
            separator={index > 0}
            onPick={(version) => onPick(kind, version)}
            onShowHistory={() => onShowHistory(kind)}
          />
        ))}
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  );
}

function KindVersions({
  projectName,
  segmentId,
  resource,
  label,
  separator,
  onPick,
  onShowHistory,
}: {
  projectName: string;
  segmentId: string;
  resource: ShotMediaResource;
  label: string;
  separator: boolean;
  onPick: (version: number) => void;
  onShowHistory: () => void;
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  const { data, loading, error } = useResourceVersions(projectName, resource, segmentId);
  return (
    <>
      {separator && <DropdownMenuSeparator />}
      <DropdownMenuGroup label={label}>
        {loading ? (
          <DropdownMenuItem disabled>{t("common:loading")}</DropdownMenuItem>
        ) : error ? (
          <DropdownMenuItem disabled>{t("version_history_load_failed")}</DropdownMenuItem>
        ) : !data || data.versions.length === 0 ? (
          <DropdownMenuItem disabled>{t("no_history")}</DropdownMenuItem>
        ) : (
          data.versions.map((v) => (
            <DropdownMenuItem key={v.version} onClick={() => onPick(v.version)}>
              <span className="num text-xs">{versionMenuLine(v, t)}</span>
              {v.is_current && <span aria-hidden className="ml-auto size-1.5 rounded-full bg-primary" />}
            </DropdownMenuItem>
          ))
        )}
        <DropdownMenuItem onClick={onShowHistory}>
          <span className="text-xs text-muted-foreground">{t("version_show_history")}</span>
        </DropdownMenuItem>
      </DropdownMenuGroup>
    </>
  );
}
