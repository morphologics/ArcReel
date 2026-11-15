import { memo, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Combine,
  History,
  ImageUp,
  Images,
  Layers,
  Library,
  Maximize2,
  MoreHorizontal,
  PenLine,
  Sparkles,
  Tags,
  Trash2,
} from "lucide-react";
import { API } from "@/api";
import { AddToLibraryDialog, type LibraryImportPreview } from "@/components/assets/AddToLibraryDialog";
import { ASSET_SHEET_TYPE_ICON } from "@/components/assets/asset-type-icons";
import { CrossfadeImage } from "@/components/canvas/shared/CrossfadeImage";
import { ImageEditDialog } from "@/components/canvas/timeline/ImageEditButton";
import { VersionTimeMachine } from "@/components/canvas/timeline/VersionTimeMachine";
import { TruncatedText } from "@/components/shared/TruncatedText";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { errMsg } from "@/utils/async";
import { isAssetBusy, rejectIfAssetBusy } from "./assetBusyGuard";
import { VERSION_RESOURCE, type GalleryAsset, type GalleryMarker } from "./gallery-model";
import { GalleryStatusMarker } from "./GalleryStatusMarker";
import { MergeAssetDialog } from "./MergeAssetDialog";
import { ProjectAssetDeleteDialog } from "./ProjectAssetDeleteDialog";
import { useStaleRegenerateConfirm } from "./useStaleRegenerateConfirm";
import type { AssetSheetStatusRow } from "@/types";

type DialogKey = "image-edit" | "versions" | "library" | "merge" | "delete";

export interface AssetBrowseCardProps {
  projectName: string;
  asset: GalleryAsset;
  /** The artifact manifest's verdict on this asset sheet; when unavailable, display by whether the entry has a sheet registered. / 产物清单对这张资产图的判定；未取到时按条目上是否登记了资产图展示。 */
  sheetStatus: AssetSheetStatusRow | undefined;
  marker: GalleryMarker;
  /** An asset sheet task holds the lock (generation or partial edit). / 资产图任务占用中（生成或局部修改）。 */
  generating: boolean;
  /** The base look or derivative file is held by a task. / 本体或衍生文件被任务占用。 */
  busy?: boolean;
  /** Read-only display (guided demo project): the menu keeps only view full image, no delete. / 只读展示（引导演示项目）：菜单只保留查看大图，没有删除。 */
  readOnly: boolean;
  /** Content for the library-import preview; products are not imported into the asset library and do not pass it. / 入库预览的内容；商品不入资产库，不传。 */
  libraryPreview?: LibraryImportPreview;
  /** Card click: open this asset's details. Must be a stable reference. / 点卡片：打开该资产的详情。需传稳定引用。 */
  onOpen: (name: string) => void;
  /** "View full image". Must be a stable reference. / 「查看大图」。需传稳定引用。 */
  onView: (name: string) => void;
  onGenerate: (name: string) => void;
  onRestoreVersion?: () => Promise<unknown> | void;
  onReload?: () => Promise<unknown> | void;
  /** Reported when an upload, version restore or delete on the card starts and ends, so the gallery disables other write entries of the same asset. Must be a stable reference. / 卡片上的上传、版本恢复或删除开始与结束时回报，画廊据此禁用同一资产的其他写入入口。需传稳定引用。 */
  onWritingChange?: (name: string, writing: boolean) => void;
  /** In-flight local writes of all cards in the gallery, used by the merge dialog to tell whether the kept asset is locked. / 画廊里所有卡片在途的本地写入，供合并对话框判断保留方是否被占用。 */
  writingNames?: ReadonlySet<string>;
}

/**
 * Gallery browse card shared by the four asset types. The card only views and selects; editing happens in the details; secondary actions live under "More".
 *
 * 画廊浏览卡：四类资产共用。卡片只负责看和选中，编辑在详情里进行；次要操作都收在「更多」里。
 */
export const AssetBrowseCard = memo(function AssetBrowseCard({
  projectName,
  asset,
  sheetStatus,
  marker,
  generating,
  busy: externalBusy = false,
  readOnly,
  libraryPreview,
  onOpen,
  onView,
  onGenerate,
  onRestoreVersion,
  onReload,
  onWritingChange,
  writingNames,
}: AssetBrowseCardProps) {
  const { t } = useTranslation(["assets", "dashboard"]);
  const titleId = useId();
  const moreRef = useRef<HTMLButtonElement>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const [dialog, setDialog] = useState<DialogKey | null>(null);
  const [uploading, setUploading] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const { type, name } = asset;
  const Icon = ASSET_SHEET_TYPE_ICON[type];

  const sheetFp = useProjectsStore((s) => (asset.sheetPath ? s.getAssetFingerprint(asset.sheetPath) : null));
  const sheetUrl =
    asset.sheetPath && sheetStatus?.status !== "missing"
      ? API.getFileUrl(projectName, asset.sheetPath, sheetFp, { width: 640 })
      : null;
  const imageUrl = sheetUrl && sheetUrl !== failedUrl ? sheetUrl : null;

  // When the asset sheet is held by generation, partial edit, upload or version restore, or the asset is being deleted, sibling actions are disabled too.
  // 资产图被生成、局部修改、上传或版本恢复占用，或资产正在删除时，兄弟操作一起禁用。
  const busy = externalBusy || generating || uploading || restoring || deleting;
  const writing = uploading || restoring || deleting;
  useEffect(() => {
    if (!writing || !onWritingChange) return;
    onWritingChange(name, true);
    return () => onWritingChange(name, false);
  }, [writing, name, onWritingChange]);
  // Merge is offered only for types with aliases that sync with the asset library; products do not pass a library-import preview.
  // 合并只开放带别名、与资产库互通的类型；商品不传入库预览。
  const mergeable = libraryPreview !== undefined && type !== "product";
  const describable = asset.description.trim().length > 0;
  const staleConfirm = useStaleRegenerateConfirm({
    projectName,
    assetType: type,
    name,
    status: sheetStatus,
    hasSheet: Boolean(asset.sheetPath),
    onGenerate: () => {
      if (!rejectIfAssetBusy(type, projectName, name, t, "assets:gallery_busy_hint")) onGenerate(name);
    },
  });

  const handleUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || busy) return;
    if (rejectIfAssetBusy(type, projectName, name, t)) return;
    setUploading(true);
    try {
      await API.uploadFile(projectName, type, file, name);
      await onReload?.();
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    } finally {
      // react-doctor-disable-next-line react-doctor/no-unowned-async-error-clear -- while an upload is in flight busy disables the entry and blocks at the start, so no newer upload on the same card shares this flag; errors go to toasts, not local state / 上传在途时 busy 禁用入口并在开头拦截，同一卡片不会有更新的上传共享这个标志；错误走提示，不存本地 state
      setUploading(false);
    }
  };

  // Re-check lock state before opening a dialog that writes the asset sheet: beyond the render snapshot, the resource may have just been locked by the Agent or another tab.
  // 打开会写资产图的对话框前复核占用态：渲染快照之外，资源可能刚被 Agent 或其他标签页占用。
  const openGuarded = (key: DialogKey) => {
    if (rejectIfAssetBusy(type, projectName, name, t, "assets:gallery_busy_hint")) return;
    setDialog(key);
  };
  const closeDialog = (open: boolean) => {
    if (!open) setDialog(null);
  };

  return (
    <article
      id={`${type}-${name}`}
      aria-labelledby={titleId}
      className="relative flex min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-card transition-colors duration-fast hover:border-input"
    >
      <div className="relative flex aspect-video items-center justify-center overflow-hidden bg-muted text-muted-foreground">
        <CrossfadeImage
          src={imageUrl}
          alt=""
          className="object-contain"
          loading="lazy"
          onError={() => setFailedUrl(sheetUrl)}
          fallback={<Icon aria-hidden className="size-8" />}
        />
        <GalleryStatusMarker marker={marker} className="absolute top-2 left-2" />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1 p-3 pt-2.5">
        <div className="flex min-w-0 items-center gap-1">
          <h3 id={titleId} className="min-w-0 flex-1 text-sm font-medium">
            {/* The name button's pseudo-element covers the whole card, so clicking anywhere opens details; "More" comes later in the DOM, is self-positioned, and sits on top of it. / 名称按钮的伪元素铺满整张卡片，点卡片任意处都打开详情；「更多」在 DOM 中靠后、自身定位，盖在它上面。 */}
            <button
              type="button"
              onClick={() => onOpen(name)}
              aria-label={name}
              className="block w-full text-left outline-none after:absolute after:inset-0 after:rounded-lg focus-visible:after:ring-3 focus-visible:after:ring-ring/50"
            >
              <TruncatedText text={name} focusable={false} />
            </button>
          </h3>
          <DropdownMenu>
            <DropdownMenuTrigger
              ref={moreRef}
              render={<Button variant="ghost" size="icon-sm" className="relative -mr-1.5 shrink-0" />}
              aria-label={t("assets:asset_menu_label", { name })}
            >
              <MoreHorizontal aria-hidden />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-44">
              <DropdownMenuGroup>
                <DropdownMenuItem disabled={!imageUrl} onClick={() => onView(name)}>
                  <Maximize2 aria-hidden />
                  {t("assets:gallery_menu_view")}
                </DropdownMenuItem>
                {!readOnly && (
                  <>
                    <DropdownMenuItem disabled={busy || !describable} onClick={staleConfirm.request}>
                      <Sparkles aria-hidden />
                      {asset.sheetPath ? t("dashboard:regenerate_design") : t("dashboard:generate_design")}
                    </DropdownMenuItem>
                    <DropdownMenuItem disabled={busy} onClick={() => uploadRef.current?.click()}>
                      <ImageUp aria-hidden />
                      {t("assets:upload_sheet")}
                    </DropdownMenuItem>
                    <DropdownMenuItem disabled={busy || !imageUrl} onClick={() => openGuarded("image-edit")}>
                      <PenLine aria-hidden />
                      {t("dashboard:image_edit_action")}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => setDialog("versions")}>
                      <History aria-hidden />
                      {t("assets:gallery_menu_versions")}
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuGroup>
              {!readOnly && libraryPreview && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuGroup>
                    <DropdownMenuItem disabled={busy} onClick={() => openGuarded("library")}>
                      <Library aria-hidden />
                      {t("assets:add_to_library")}
                    </DropdownMenuItem>
                    <DropdownMenuItem disabled={busy} onClick={() => openGuarded("merge")}>
                      <Combine aria-hidden />
                      {t("assets:merge_into")}
                    </DropdownMenuItem>
                  </DropdownMenuGroup>
                </>
              )}
              {!readOnly && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuGroup>
                    <DropdownMenuItem variant="destructive" disabled={busy} onClick={() => openGuarded("delete")}>
                      <Trash2 aria-hidden />
                      {t("assets:delete")}
                    </DropdownMenuItem>
                  </DropdownMenuGroup>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <p className="line-clamp-2 text-sm text-subtle-foreground">
          {asset.description.trim() || <span className="text-muted-foreground">{t("assets:no_description")}</span>}
        </p>
        <CardCounts asset={asset} />
      </div>

      {!readOnly && (
        <>
          <input
            ref={uploadRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp"
            aria-label={t("assets:upload_sheet")}
            className="hidden"
            onChange={(event) => void handleUpload(event)}
          />
          {staleConfirm.dialog}
          <ImageEditDialog
            open={dialog === "image-edit"}
            onOpenChange={closeDialog}
            projectName={projectName}
            resourceType={type}
            resourceId={name}
            hasImage={Boolean(imageUrl)}
            busy={busy}
          />
          <VersionTimeMachine
            open={dialog === "versions"}
            onOpenChange={closeDialog}
            anchor={moreRef}
            projectName={projectName}
            resourceType={VERSION_RESOURCE[type]}
            resourceId={name}
            onRestore={onRestoreVersion}
            // In-flight restore is managed by the panel itself; other locks (including derivative tasks and external locks aggregated by the gallery) disable it too
            // 恢复在途由面板自己管；其余占用（含衍生任务与画廊汇总的外部占用）一并禁用
            busy={externalBusy || generating || uploading || deleting}
            onRestoringChange={setRestoring}
            checkBusy={() => isAssetBusy(type, projectName, name)}
          />
          <ProjectAssetDeleteDialog
            open={dialog === "delete"}
            onOpenChange={closeDialog}
            projectName={projectName}
            assetType={type}
            name={name}
            busy={externalBusy || generating || uploading || restoring}
            onDeletingChange={setDeleting}
            onMergeInstead={mergeable ? () => openGuarded("merge") : undefined}
          />
          {libraryPreview && type !== "product" && (
            <>
              <AddToLibraryDialog
                open={dialog === "library"}
                onOpenChange={closeDialog}
                resourceType={type}
                resourceId={name}
                projectName={projectName}
                preview={libraryPreview}
                busy={busy}
              />
              <MergeAssetDialog
                open={dialog === "merge"}
                onOpenChange={closeDialog}
                projectName={projectName}
                assetType={type}
                name={name}
                description={asset.description}
                busy={busy}
                writingNames={writingNames}
              />
            </>
          )}
        </>
      )}
    </article>
  );
});

/** Card footer counts: derivatives, aliases and product originals, listing only non-zero items. / 卡片底部的计数：衍生、别名与商品原图，只列非零项。 */
function CardCounts({ asset }: { asset: GalleryAsset }) {
  const { t } = useTranslation("assets");
  const items = [
    { key: "derivatives", count: asset.derivativeCount, Icon: Layers, label: t("derivatives_count", { count: asset.derivativeCount }) },
    { key: "aliases", count: asset.aliasCount, Icon: Tags, label: t("gallery_alias_count", { count: asset.aliasCount }) },
    { key: "references", count: asset.referenceCount, Icon: Images, label: t("gallery_reference_count", { count: asset.referenceCount }) },
  ].filter((item) => item.count > 0);
  if (items.length === 0) return null;
  return (
    <p className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 pt-1 text-xs text-muted-foreground">
      {items.map(({ key, Icon, label }) => (
        <span key={key} className="inline-flex items-center gap-1">
          <Icon aria-hidden className="size-3.5" />
          {label}
        </span>
      ))}
    </p>
  );
}
