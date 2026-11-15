import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Check, Loader2, Pencil, X } from "lucide-react";
import { API, type AssetRenameResult, type ProjectAssetType } from "@/api";
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
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { errMsg } from "@/utils/async";
import { useTrackWrite } from "./useAssetWrites";
import { rejectIfAssetBusy, useAssetBusyNames } from "./assetBusyGuard";

interface EditableAssetNameProps {
  projectName: string;
  name: string;
  assetType: ProjectAssetType;
  /**
   * Name heading. While editing `hidden` is true but the heading still renders (visually hidden), so the containing popover's accessible name does not disappear.
   *
   * 名称标题。编辑时 `hidden` 为 true，标题仍需渲染（视觉隐藏），所在弹层的无障碍名称不随之消失。
   */
  renderTitle: (hidden: boolean) => ReactNode;
  /** Read-only display (guided demo project): do not render the rename entry. / 只读展示（引导演示项目）：不渲染重命名入口。 */
  readOnly?: boolean;
  /** Lock state shared with sibling controls in the details (generating, uploading, saving). / 与详情里兄弟控件共享的占用态（生成中、上传中、保存中）。 */
  busy?: boolean;
  /** Called after a rename is submitted and before project data refreshes, so the caller can move the selection to the new name. / 改名已提交、刷新项目数据之前调用，供调用方把选中项改到新名称上。 */
  onRenamed?: (from: string, to: string) => void;
}

/**
 * In-place rename of an asset name: click the pencil → the name becomes an input (Enter submits, Esc cancels) → fetch the impact preview (dry-run) first,
 * then show a confirmation; on confirm run the cascading rename. Rename is an immediate action, not an unsaved change, with no toast on success.
 *
 * Rename cascades rewrites to all episode script references and related files, so it is lock-aware: both entering edit and confirming submission
 * re-check lock state via `rejectIfAssetBusy`. On success it refreshes through the single `refreshProject` funnel.
 *
 * 资产名称的就地重命名：点铅笔 → 名称变输入框（Enter 提交，Esc 取消）→ 先取影响预览（dry-run）
 * 再弹确认框，确认后执行级联重命名。改名是立即执行的动作，不进入未保存修改，成功不弹提示。
 *
 * 重命名会级联改写全部剧集剧本引用与关联文件，属占用感知型操作：进入编辑与确认提交时
 * 均经 `rejectIfAssetBusy` 复核占用态。成功后经 `refreshProject` 单一漏斗刷新。
 */
export function EditableAssetName({
  projectName,
  name,
  assetType,
  renderTitle,
  readOnly = false,
  busy = false,
  onRenamed,
}: EditableAssetNameProps) {
  const { t } = useTranslation(["assets", "common"]);
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [preview, setPreview] = useState<AssetRenameResult | null>(null);
  const [renaming, setRenaming] = useState(false);
  const track = useTrackWrite();
  const inputRef = useRef<HTMLInputElement>(null);
  // Confirm runs a cascading rewrite in one click: if locks change while the confirmation is open it disables live, and rejectIfBusy still re-checks on submit
  // 确认是一键执行的级联改写：确认框打开后占用发生变化时实时禁用，提交时仍由 rejectIfBusy 复核
  const queueBusy = useAssetBusyNames(assetType, projectName).has(name);

  useEffect(() => {
    if (!isEditing) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [isEditing]);

  const trimmed = draft.trim();
  const submitting = previewLoading || renaming;
  const canSubmit = trimmed.length > 0 && !submitting;

  if (readOnly) return renderTitle(false);

  /**
   * Lock re-check combines three sources: `rejectIfAssetBusy` only looks at the task queue, while in-flight writes in the details (upload, save) and
   * this component's own rename that has not finished refreshing are local state outside the queue, so `busy` and `renaming` must join
   * the same gate. `renaming` only drops once `refreshProject` settles: before that project data still has the old name, and letting it through would resubmit
   * with a stale base name.
   *
   * 占用复核并作三路取：`rejectIfAssetBusy` 只看任务队列，而详情里在途的写请求（上传、保存）与
   * 本组件自己那次尚未刷新完的改名都是本地 state、不进队列，必须把 `busy` 与 `renaming` 并进
   * 同一道闸。`renaming` 直到 `refreshProject` 结算才落下：那之前项目数据仍是旧名，放行会拿
   * 过期的基准名再提交一次。
   */
  const rejectIfBusy = () => {
    if (busy || renaming) {
      useAppStore.getState().pushToast(t("assets:rename_busy_hint"), "info");
      return true;
    }
    return rejectIfAssetBusy(assetType, projectName, name, t, "assets:rename_busy_hint");
  };

  const enterEdit = () => {
    if (rejectIfBusy()) return;
    setDraft(name);
    setIsEditing(true);
  };

  const cancelEdit = () => {
    setDraft(name);
    setIsEditing(false);
  };

  const requestPreview = async () => {
    if (!canSubmit) return;
    if (trimmed === name) {
      setIsEditing(false);
      return;
    }
    if (rejectIfBusy()) return;
    setPreviewLoading(true);
    try {
      const result = await API.renameProjectAsset(projectName, assetType, name, trimmed, { dryRun: true });
      setPreview(result);
    } catch (err) {
      useAppStore.getState().pushToast(t("assets:rename_failed", { message: errMsg(err) }), "error");
    } finally {
      setPreviewLoading(false);
    }
  };

  const executeRename = async () => {
    if (!preview) return;
    if (rejectIfBusy()) {
      setPreview(null);
      return;
    }
    setRenaming(true);
    try {
      await track((async () => {
        const result = await API.renameProjectAsset(projectName, assetType, name, trimmed);
        setPreview(null);
        setIsEditing(false);
        onRenamed?.(name, result.new_name);
        // The rename was submitted; refresh is a separate follow-up step: refreshProject reports failure through its settled value instead of rejecting,
        // and without a separate notice the details would stay on old data, looking as if the rename did not take effect. cancelled means the project switched away, silent.
        // 重命名已提交，刷新是独立的后续步骤：refreshProject 以结算值报告失败而不 reject，
        // 不单独提示的话详情会停在旧数据上，看着像改名没生效。cancelled 是项目已切走，静默。
        const refreshed = await useProjectsStore.getState().refreshProject(projectName);
        if (refreshed === "failed") {
          useAppStore.getState().pushToast(t("assets:rename_refresh_failed"), "warning");
        }
      })());
    } catch (err) {
      useAppStore.getState().pushToast(t("assets:rename_failed", { message: errMsg(err) }), "error");
      // On failure keep the confirmation closed and edit mode on, so the user can change the name and retry / 失败保持确认框关闭、编辑态保留，可以改名后重试
      setPreview(null);
    } finally {
      setRenaming(false);
    }
  };

  if (!isEditing) {
    return (
      <div className="flex min-w-0 items-center gap-1">
        {renderTitle(false)}
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={enterEdit}
          disabled={busy || renaming}
          aria-label={t("assets:rename_asset")}
        >
          <Pencil aria-hidden />
        </Button>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 items-center gap-1">
      {renderTitle(true)}
      <form
        className="flex min-w-0 flex-1 items-center gap-1"
        onSubmit={(e) => {
          e.preventDefault();
          void requestPreview();
        }}
      >
        <Input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "Escape" || e.nativeEvent.isComposing) return;
            // Only exit rename; do not let Esc keep bubbling and close the containing Sheet / 只退出改名，不让 Esc 继续冒泡关掉所在的 Sheet
            e.preventDefault();
            e.stopPropagation();
            cancelEdit();
          }}
          disabled={submitting}
          aria-label={t("assets:rename_asset")}
          className="min-w-0 flex-1"
        />
        <Button type="submit" variant="ghost" size="icon-sm" disabled={!canSubmit} aria-label={t("common:save")}>
          {previewLoading ? <Loader2 aria-hidden className="animate-spin" /> : <Check aria-hidden />}
        </Button>
        <Button variant="ghost" size="icon-sm" onClick={cancelEdit} disabled={submitting} aria-label={t("common:cancel")}>
          <X aria-hidden />
        </Button>
      </form>
      <AlertDialog
        open={preview !== null}
        onOpenChange={(next) => {
          // Ignore Esc while submitting, so the confirmation does not close first while the request is in flight / 提交中不响应 Esc，请求在途时确认框不先关掉
          if (!next && !renaming) setPreview(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("assets:rename_confirm_title", { name })}</AlertDialogTitle>
          </AlertDialogHeader>
          <AlertDialogBody tabIndex={0} role="region" aria-label={t("assets:rename_confirm_title", { name })}>
            <div className="flex flex-col gap-1.5">
              <AlertDialogDescription>
                {preview && (preview.references > 0 || preview.files > 0)
                  ? t("assets:rename_impact", {
                      episodes: preview.episodes,
                      references: preview.references,
                      files: preview.files,
                    })
                  : t("assets:rename_impact_none")}
              </AlertDialogDescription>
              <p className="text-sm text-subtle-foreground">
                {name} → {trimmed}
              </p>
            </div>
          </AlertDialogBody>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={renaming}>{t("common:cancel")}</AlertDialogCancel>
            <AlertDialogAction disabled={renaming || busy || queueBusy} onClick={() => void executeRename()}>
              {renaming ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : null}
              {renaming ? t("assets:renaming") : t("assets:rename_asset")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
