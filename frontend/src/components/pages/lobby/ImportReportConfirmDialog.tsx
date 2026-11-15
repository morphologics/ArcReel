import { useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { MigrationReportContent } from "@/components/shared/MigrationReportContent";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogBody,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import type { WorkflowMigrationReport } from "@/types/workflow";
import type { EpisodeLedger } from "@/utils/episode-display";

export interface PendingImportReport {
  report: WorkflowMigrationReport;
  episodes: EpisodeLedger;
}

interface ImportReportConfirmDialogProps {
  /** Upgrade report awaiting confirmation; the dialog is closed when null. / 待确认的升级报告；为 null 时对话框关闭。 */
  pending: PendingImportReport | null;
  importing: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * The import package's upgrade adjusted content or had files that could not be brought in: ask the user to confirm once before the project appears in the project list.
 * On cancel the server keeps no project data at all; on confirm the import is resubmitted with the confirmation.
 *
 * 导入包升级时调整了内容或有没能带入的文件：项目出现在项目列表之前请用户确认一次。
 * 取消时服务端没有留下任何项目数据；确认后带上确认重新提交导入。
 */
export function ImportReportConfirmDialog({ pending, importing, onConfirm, onCancel }: ImportReportConfirmDialogProps) {
  const { t } = useTranslation(["dashboard", "common"]);
  // Keep the previous report during the close animation so the content does not flash blank. / 关闭动画期间保留上一份报告，避免内容闪成空白。
  const [shown, setShown] = useState(pending);
  if (pending !== null && pending !== shown) setShown(pending);

  return (
    <AlertDialog
      open={pending !== null}
      onOpenChange={(open) => {
        // Ignore Esc while importing, so the dialog does not disappear while the request is still in flight / 导入中不响应 Esc，避免请求还在途时对话框先消失
        if (!open && !importing) onCancel();
      }}
    >
      <AlertDialogContent size="lg">
        <AlertDialogHeader>
          <AlertDialogTitle>{t("dashboard:import_confirm_title")}</AlertDialogTitle>
          <AlertDialogDescription>{t("dashboard:import_confirm_description")}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogBody tabIndex={0} role="region" aria-label={t("dashboard:import_confirm_title")}>
          {shown ? <MigrationReportContent report={shown.report} episodes={shown.episodes} /> : null}
        </AlertDialogBody>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={importing}>{t("common:cancel")}</AlertDialogCancel>
          <AlertDialogAction disabled={importing} onClick={onConfirm}>
            {importing && <Loader2 data-icon="inline-start" className="animate-spin" aria-hidden />}
            {t("dashboard:import_confirm_action")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
