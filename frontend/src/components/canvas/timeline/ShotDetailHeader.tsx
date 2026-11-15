import type { ReactNode } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ItemPositionOfTotal } from "@/components/shared/episode-item/ItemPositionOfTotal";
import { TooltipIconButton } from "./TooltipIconButton";

interface ShotDetailHeaderProps {
  segmentId: string;
  index: number;
  total: number;
  /** Attributes after the position: duration, status, segment break toggle, etc. / 位置后面的属性：时长、状态、切分点开关等。 */
  meta: ReactNode;
  /** Move forward or backward by one; hidden when omitted. / 前移、后移一位；缺省时不显示。 */
  onMoveEarlier?: () => void;
  onMoveLater?: () => void;
  /** "Insert after" and "Remove shot". / 「在此后插入」「移除分镜」。 */
  structureActions?: ReactNode;
  onPrev: () => void;
  onNext: () => void;
  /** Disable condition and reason shared by shot switching, moving and add/remove. / 切换分镜、移动与增删共用的禁用条件及原因。 */
  navDisabled: boolean;
  navDisabledHint?: string;
  /** Note entry at the end of the row. / 行尾的备注入口。 */
  notes?: ReactNode;
  /** No previous / next buttons (the storyboard strip and J / K do that). */
  hideNav?: boolean;
}

/** Shot details header: "position / total" and attributes on the left; move, add/remove, previous / next shot and note on the right. / 分镜详情页头：左侧「位置 / 总数」与属性，右侧移动、增删、上一镜 / 下一镜与备注。 */
export function ShotDetailHeader({
  segmentId,
  index,
  total,
  meta,
  onMoveEarlier,
  onMoveLater,
  structureActions,
  onPrev,
  onNext,
  navDisabled,
  navDisabledHint,
  notes,
  hideNav = false,
}: ShotDetailHeaderProps) {
  const { t } = useTranslation("dashboard");
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-2.5 gap-y-1.5 border-b border-border/50 px-5 py-2.5">
      <ItemPositionOfTotal itemId={segmentId} kind="shot" position={index + 1} total={total} />
      {meta}
      <div className="ml-auto flex items-center gap-0.5">
        {onMoveEarlier && onMoveLater ? (
          <>
            <TooltipIconButton
              label={t("shot_move_earlier")}
              hint={navDisabledHint}
              disabled={navDisabled || index === 0}
              onClick={onMoveEarlier}
            >
              <ChevronUp aria-hidden />
            </TooltipIconButton>
            <TooltipIconButton
              label={t("shot_move_later")}
              hint={navDisabledHint}
              disabled={navDisabled || index === total - 1}
              onClick={onMoveLater}
            >
              <ChevronDown aria-hidden />
            </TooltipIconButton>
          </>
        ) : null}
        {structureActions}
        {hideNav ? null : (
          <>
            <TooltipIconButton
              label={t("shot_detail_prev")}
              hint={navDisabledHint}
              disabled={navDisabled}
              onClick={onPrev}
              shortcut="J"
            >
              <ChevronLeft aria-hidden />
            </TooltipIconButton>
            <TooltipIconButton
              label={t("shot_detail_next")}
              hint={navDisabledHint}
              disabled={navDisabled}
              onClick={onNext}
              shortcut="K"
            >
              <ChevronRight aria-hidden />
            </TooltipIconButton>
          </>
        )}
        {notes}
      </div>
    </div>
  );
}
