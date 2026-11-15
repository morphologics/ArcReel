import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Wand2 } from "lucide-react";
import { isAssetBusy } from "@/components/canvas/lorebook/assetBusyGuard";
import { enqueueImageEdit } from "@/actions/generation";
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
import { useAppStore } from "@/stores/app-store";
import {
  selectHasActiveTaskForScriptFile,
  useTasksStore,
  type ImageEditResourceKind,
} from "@/stores/tasks-store";
import { errMsg } from "@/utils/async";
import { TooltipIconButton } from "./TooltipIconButton";

export type ImageEditResourceType = ImageEditResourceKind;

interface ImageEditTarget {
  projectName: string;
  resourceType: ImageEditResourceType;
  resourceId: string;
  /** Episode file required for shot editing; ignored for other asset types / 分镜编辑必带的剧集文件；其余资产类型忽略 */
  scriptFile?: string | null;
  /** Whether an editable current image exists; without one, disable and prompt to generate/upload first / 是否存在可编辑的当前图；无图时禁用并提示先生成/上传 */
  hasImage: boolean;
  /** The resource is occupied by a generation/edit task: disable the edit entry / 资源被生成/编辑任务占用：禁用编辑入口 */
  busy?: boolean;
}

/**
 * Icon-style "Local edit" entry on an image card: icon button + instruction dialog.
 *
 * 图片卡片上的图标式「局部修改」入口：图标按钮 + 指令对话框。
 */
export function ImageEditButton(props: ImageEditTarget) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(false);
  const disabled = Boolean(props.busy) || !props.hasImage;
  const label = t("image_edit_action");

  return (
    <>
      <TooltipIconButton
        label={label}
        hint={props.hasImage ? undefined : t("image_edit_no_image_hint")}
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <Wand2 aria-hidden />
      </TooltipIconButton>
      <ImageEditDialog {...props} open={open} onOpenChange={setOpen} />
    </>
  );
}

/**
 * Instruction dialog for local edits: enqueue an i2i edit with the current image as base and the instruction as prompt; refreshes automatically via the SSE fingerprint when done.
 * Controlled open, for entry points without a dedicated button, such as the card's "More" menu.
 *
 * 局部修改的指令对话框：以当前图为底图、指令为 prompt 入队 i2i 编辑；完成后经 SSE fingerprint 自动刷新。
 * 受控打开，供卡片的「更多」菜单等没有专属按钮的入口使用。
 */
export function ImageEditDialog({
  projectName,
  resourceType,
  resourceId,
  scriptFile,
  hasImage,
  busy = false,
  open,
  onOpenChange,
}: ImageEditTarget & { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t } = useTranslation(["dashboard", "common"]);
  const [instruction, setInstruction] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const fieldId = useId();
  const disabled = busy || !hasImage;

  const handleSubmit = async () => {
    const trimmed = instruction.trim();
    // Still guard when disabled (busy||!hasImage): busy also carries dimensions beyond the store's per-resource occupancy: with grid assembly enabled,
    // a grid task running in this episode (the split phase overwrites multiple storyboards, see selectHasActiveTaskForScriptFile),
    // or the base image uploading within the card; neither appears in this resource's occupancy set; and a keyboard-shortcut submit bypasses the button's
    // disabled attribute, so this is the only place this check lands.
    // disabled（busy||!hasImage）仍要拦：busy 还带着 store 按资源占用之外的维度——启用宫格装配时
    // 本集有 grid 任务在跑（切割阶段覆写多张 storyboard，见 selectHasActiveTaskForScriptFile）、
    // 卡片内正在上传底图，二者都不会出现在本资源的占用集里；且键盘快捷键提交绕过按钮的
    // disabled 属性，此处是这层判定唯一的落点。
    if (!trimmed || submitting || disabled) return;
    // Then recheck with a fresh getState() read: while the dialog stays open, updates to the reactive busy prop depend on the parent
    // re-rendering, so there is perceived latency; reading the store's current value directly here hits the same occupancy slot as resourceType/resourceId
    // (taskResourceKind slots image_edit by resource_type).
    // 再用 getState() 新鲜读复核：弹窗停留期间响应式 busy prop 的更新依赖父组件
    // 重渲染，存在感知延迟；这里直接读 store 当前值，与 resourceType/resourceId
    // 命中同一占用槽（taskResourceKind 对 image_edit 按 resource_type 归槽）。
    if (isAssetBusy(resourceType, projectName, resourceId)) {
      useAppStore.getState().pushToast(t("image_edit_resource_busy"), "error");
      return;
    }
    const { tasks, optimisticActiveScriptFile } = useTasksStore.getState();
    // The storyboard resource occupancy set cannot find grid tasks (their resource_id is grid_id); with grid assembly enabled,
    // when a split task is running in this episode, recheck by scriptFile, otherwise the fresh read would miss this dimension the busy prop has not caught up with yet;
    // see selectHasActiveTaskForScriptFile and GridImageToVideoCanvas's
    // gridActiveForEpisode.
    // storyboard 资源占用集查不到 grid 任务（其 resource_id 是 grid_id）；启用宫格装配时
    // 本集有切割任务在跑时需按 scriptFile 复核，否则新鲜读会漏过 busy prop 尚未追上
    // 的这一维度，见 selectHasActiveTaskForScriptFile 与 GridImageToVideoCanvas 的
    // gridActiveForEpisode。
    if (
      resourceType === "storyboard" &&
      scriptFile &&
      selectHasActiveTaskForScriptFile(tasks, "grid", scriptFile, projectName, optimisticActiveScriptFile)
    ) {
      useAppStore.getState().pushToast(t("image_edit_resource_busy"), "error");
      return;
    }
    setSubmitting(true);
    try {
      await enqueueImageEdit(projectName, {
        resourceType,
        resourceId,
        instruction: trimmed,
        scriptFile: resourceType === "storyboard" ? scriptFile ?? null : null,
      });
      setInstruction("");
      onOpenChange(false);
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Ignore Esc and overlay clicks while a submit is in flight / 提交在途时忽略 Esc 与遮罩点击
        if (!next && submitting) return;
        onOpenChange(next);
      }}
    >
      <DialogContent showCloseButton={!submitting}>
        <DialogHeader>
          <DialogTitle>{t("image_edit_modal_title")}</DialogTitle>
          <DialogDescription>{t("image_edit_modal_desc", { name: resourceId })}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={fieldId}>{t("image_edit_instruction_label")}</Label>
            <Textarea
              id={fieldId}
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                  e.preventDefault();
                  void handleSubmit();
                }
              }}
              rows={3}
              // eslint-disable-next-line jsx-a11y/no-autofocus -- focus the instruction input when the dialog opens, matching the "open and write" mental model / 对话框打开即聚焦指令输入，符合“点开就写”的心智
              autoFocus
              placeholder={t("image_edit_instruction_placeholder")}
            />
          </div>
        </DialogBody>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />} disabled={submitting}>
            {t("common:cancel")}
          </DialogClose>
          <Button onClick={() => void handleSubmit()} disabled={submitting || instruction.trim().length === 0 || disabled}>
            {submitting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Wand2 aria-hidden data-icon="inline-start" />}
            {submitting ? t("image_edit_submitting") : t("image_edit_submit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
