import { useId } from "react";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

/**
 * Fill in the body before adding a video unit. The body is the unit's only content truth; the server rejects creating a unit with an empty body with needs_replan,
 * so it cannot be submitted while empty. The body is held by the caller: kept as is after failure, cleared on reopen.
 *
 * 新增视频单元前填写正文。正文是单元唯一的内容真相，空正文的单元服务端以 needs_replan
 * 拒绝创建，因此正文为空时不能提交。正文由调用方持有：失败后原样保留，重新打开时清空。
 */
export function ReferenceUnitAddDialog({
  open,
  prompt,
  submitting,
  onPromptChange,
  onSubmit,
  onClose,
}: {
  open: boolean;
  prompt: string;
  submitting: boolean;
  onPromptChange: (next: string) => void;
  onSubmit: (prompt: string) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  const textareaId = useId();
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Don't close while submitting; the dialog stays as is until the result is in / 提交中不关闭，结果出来前对话框保持原样
        if (!next && !submitting) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("reference_unit_add_title")}</DialogTitle>
          <DialogDescription>{t("reference_unit_add_desc")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-2">
            <Label htmlFor={textareaId}>{t("reference_unit_add_label")}</Label>
            <Textarea
              id={textareaId}
              value={prompt}
              placeholder={t("reference_editor_placeholder")}
              onChange={(e) => onPromptChange(e.target.value)}
              disabled={submitting}
            />
          </div>
        </DialogBody>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" disabled={submitting} />}>{t("common:cancel")}</DialogClose>
          <Button disabled={submitting || !prompt.trim()} onClick={() => onSubmit(prompt)}>
            {submitting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : null}
            {t("reference_unit_add_confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
