import { useRef, useState, type ReactNode } from "react";
import { cn } from "cn";
import { ContextMenu } from "@base-ui/react/context-menu";
import { DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { isResourceBusy } from "@/stores/tasks-store";
import { Sparkles, ImageIcon, Film, History, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTranslation } from "react-i18next";
import { API, type VersionInfo } from "@/api";
import { usePlaybackStart } from "@/hooks/usePlaybackStart";
import { useProjectsStore } from "@/stores/projects-store";
import { AspectFrame } from "@/components/canvas/shared/AspectFrame";
import { PreviewableImageFrame } from "@/components/canvas/shared/PreviewableImageFrame";
import { PresentationPlayer } from "@/components/shared/PresentationPlayer";
import {
  UPLOAD_IMAGE_ACCEPT,
  UPLOAD_VIDEO_ACCEPT,
  UploadIconButton,
} from "@/components/canvas/shared/UploadIconButton";
import { useDemoWorkbench } from "@/onboarding/use-demo-workbench";
import { formatCost } from "@/utils/cost-format";
import type { CostBreakdown } from "@/types";
import { useItemPositionLabel } from "@/components/shared/episode-item/useEpisodeItem";
import { ImageEditButton } from "./ImageEditButton";
import { Toggle } from "@/components/ui/toggle";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useResourceVersions, invalidateResourceVersions } from "@/hooks/useResourceVersions";
import { useShotMediaStore, type ShotMediaKind } from "@/stores/shot-media-store";
import { useAppStore } from "@/stores/app-store";
import { useTrackWrite } from "@/components/canvas/lorebook/useAssetWrites";
import { errMsg } from "@/utils/async";
import { VersionHistoryPanel } from "./VersionHistoryPanel";
import { versionCost, versionOrigin, versionTime } from "./version-summary";

type MediaKind = "storyboard" | "video";

interface MediaCardProps {
  kind: MediaKind;
  restoring?: boolean;
  onRestoringChange?: (restoring: boolean) => void;
  checkBusy?: () => boolean;
  projectName: string;
  segmentId: string;
  /** 资产相对路径，如 storyboards/E1S2_v1.png */
  assetPath: string | null;
  /** 视频海报缩略图（仅 kind=video 用） */
  posterPath?: string | null;
  /** 渲染比例 */
  aspectRatio: "9:16" | "16:9";
  /** 生成按钮是否禁用（视频生成需要先有分镜图） */
  generateDisabled?: boolean;
  /** 自定义禁用 tooltip，未提供时使用默认（"分镜图未生成"）的视频禁用提示 */
  generateDisabledHint?: string;
  /** 进行中状态 */
  generating?: boolean;
  /** 估算费用（按币种 breakdown，例如 {USD: 0.12} 或 {CNY: 5.25}） */
  estimatedCost?: CostBreakdown;
  /** 触发生成 */
  onGenerate?: () => void;
  /** 生成按钮文案，缺省按有无产物写「生成」或「重新生成」；有未保存修改时传「保存并生成」。 */
  generateLabel?: string;
  /** 版本恢复回调；未提供时不显示版本入口（Preview / History 切换）（只读展示无版本可回滚） */
  onRestore?: () => Promise<unknown> | void;
  /** 自主上传回调（替换该分镜的分镜图/视频）；未提供时不显示上传入口 */
  onUpload?: (file: File) => Promise<void> | void;
  /** 本卡片的上传请求进行中 */
  uploading?: boolean;
  /** 其他上传进行中等需要互斥的场景：禁用上传入口但不显示 spinner */
  uploadDisabled?: boolean;
  /** 分镜编辑所需的剧集文件；提供时（且 kind=storyboard、已有图）显示编辑入口 */
  editScriptFile?: string | null;
  /** Buttons placed left of the upload button (the shot's delete and notes). */
  leadingActions?: ReactNode;
  /** Centred in the title row between the title and the buttons (the shot's position and facts). */
  centerContent?: ReactNode;
  /** Items of the right-click menu on the image or video (DropdownMenuItem elements); no menu when absent. */
  /** Make current also puts back the prompt the version was made from (and, for videos, its length). */
  onRestorePrompt?: (version: VersionInfo, kind: MediaKind) => Promise<void>;
  /** The shot has unsaved edits: Make current with a prompt to restore waits until they are saved or discarded. */
  unsavedEdits?: boolean;
  contextMenuItems?: ReactNode;
}

const UPLOAD_ACCEPT: Record<MediaKind, string> = {
  storyboard: UPLOAD_IMAGE_ACCEPT,
  video: UPLOAD_VIDEO_ACCEPT,
};

export function MediaCard({
  kind,
  projectName,
  segmentId,
  assetPath,
  posterPath,
  aspectRatio,
  generateDisabled,
  generateDisabledHint,
  generating,
  estimatedCost,
  onGenerate,
  generateLabel: generateLabelOverride,
  onRestore,
  onUpload,
  leadingActions,
  centerContent,
  contextMenuItems,
  onRestorePrompt,
  unsavedEdits,
  uploading,
  uploadDisabled,
  editScriptFile,
  restoring: externalRestoring,
  onRestoringChange,
  checkBusy,
}: MediaCardProps) {
  const { t } = useTranslation("dashboard");
  const itemLabel = useItemPositionLabel(segmentId);
  const [restoring, setRestoring] = useState(false);
  const restoringRef = useRef(false);
  const freshBusy = () => restoringRef.current || Boolean(checkBusy?.()) || isResourceBusy(kind, projectName, segmentId);
  const setRestoreBusy = (next: boolean) => {
    restoringRef.current = next;
    setRestoring(next);
    onRestoringChange?.(next);
  };
  // 演示态只读：卡片上的四个写入口（上传 / 编辑 / 版本恢复 / 生成）从同一处判定关闭，
  // 不再各自靠「对应回调是否传入」推断——那让版本入口与其余入口分属两套机制。
  const demoReadOnly = useDemoWorkbench();

  const assetFp = useProjectsStore((s) =>
    assetPath ? s.getAssetFingerprint(assetPath) : null,
  );
  const playbackStart = usePlaybackStart("videos", segmentId);
  // 原图只给全屏预览；卡片里按媒体栏宽度从 640 / 1280 两档缩略图中取，媒体栏最宽约 720px。
  const assetUrl = assetPath ? API.getFileUrl(projectName, assetPath, assetFp) : null;
  const storyboardSrcSet =
    assetPath && kind === "storyboard"
      ? `${API.getFileUrl(projectName, assetPath, assetFp, { width: 640 })} 640w, ${API.getFileUrl(projectName, assetPath, assetFp, { width: 1280 })} 1280w`
      : undefined;

  const Icon = kind === "storyboard" ? ImageIcon : Film;
  const otherTitle = kind === "storyboard" ? t("media_video_title") : t("media_storyboard_title");
  // the media grid shows both side by side from this width of the media column, tabs below it
  const portrait = aspectRatio === "9:16";
  const tabsHidden = portrait ? "@min-[440px]/shot-media:hidden" : "@min-[720px]/shot-media:hidden";
  const tabsOnly = portrait ? "hidden @min-[440px]/shot-media:block" : "hidden @min-[720px]/shot-media:block";
  const title =
    kind === "storyboard" ? t("media_storyboard_title") : t("media_video_title");
  const generateLabel =
    generateLabelOverride ??
    (kind === "storyboard"
      ? assetPath
        ? t("media_regenerate_storyboard")
        : t("media_generate_storyboard")
      : assetPath
        ? t("media_regenerate_video")
        : t("media_generate_video"));
  const resourceType: "storyboards" | "videos" =
    kind === "storyboard" ? "storyboards" : "videos";
  // uploadDisabled 是本卡片之外的互斥占用（如同一分镜另一张卡在上传中）；
  // 编辑/版本恢复/生成同样写这个资源，须一并禁用，否则会与占用中的写操作并发冲突。
  const resourceBusy = generating || uploading || uploadDisabled || restoring || externalRestoring;
  // Preview / History: the pane shows the media (the current version, or one picked from the history or the
  // strip's menu) or the version list in its place. Mode and picked version live in the shot media store, which
  // the strip's right-click menu also sets.
  const hasVersions = Boolean(onRestore) && !demoReadOnly;
  const mode = useShotMediaStore((s) => s.mode[kind]);
  const viewed = useShotMediaStore((s) => s.viewing[kind]);
  const setMode = useShotMediaStore((s) => s.setMode);
  const view = useShotMediaStore((s) => s.view);
  const history = hasVersions && mode === "history";
  const historyLabel = t(kind === "storyboard" ? "media_history_storyboard" : "media_history_video");
  const shownTitle = history ? historyLabel : title;
  const viewedVersion = hasVersions && viewed?.segmentId === segmentId ? viewed.version : null;
  const versions = useResourceVersions(projectName, resourceType, segmentId, history || viewedVersion !== null);
  const viewedInfo =
    viewedVersion !== null ? (versions.data?.versions.find((v) => v.version === viewedVersion) ?? null) : null;
  // the picked version is shown only while it isn't the current one
  const showingOld = viewedInfo !== null && !viewedInfo.is_current;
  const track = useTrackWrite();
  const [restoringVersion, setRestoringVersion] = useState<number | null>(null);
  const restore = async (version: number) => {
    if (resourceBusy || restoringVersion !== null) return;
    if (freshBusy()) {
      useAppStore.getState().pushToast(t("version_restore_busy_hint"), "error");
      return;
    }
    const info = versions.data?.versions.find((v) => v.version === version);
    const restoresPrompt = Boolean(onRestorePrompt) && info?.script_prompt != null;
    if (restoresPrompt && unsavedEdits) {
      useAppStore.getState().pushToast(t("version_restore_unsaved"), "error");
      return;
    }
    setRestoringVersion(version);
    setRestoreBusy(true);
    try {
      await track(
        (async () => {
          const result = await API.restoreVersion(projectName, resourceType, segmentId, version);
          invalidateResourceVersions(projectName, resourceType, segmentId);
          if (result.asset_fingerprints) useProjectsStore.getState().updateAssetFingerprints(result.asset_fingerprints);
          await onRestore?.();
          // the restored version is now the current one: the pane shows it as such
          view(kind, null);
          if (restoresPrompt && info) {
            try {
              await onRestorePrompt?.(info, kind);
            } catch (err) {
              useAppStore.getState().pushToast(t("version_prompt_restore_failed", { message: errMsg(err) }), "error");
            }
          }
        })(),
      );
    } catch (err) {
      useAppStore.getState().pushToast(t("switch_version_failed", { message: errMsg(err) }), "error");
    } finally {
      setRestoringVersion(null);
      setRestoreBusy(false);
    }
  };
  // Right-click menu on the storyboard image: the caller's items (note, edit region) plus upload and versions.
  const menuUploadRef = useRef<HTMLInputElement>(null);
  const menuItems =
    contextMenuItems ? (
      <>
        {onRestore && !demoReadOnly && (
          <DropdownMenuItem onClick={() => setMode(kind, "history")}>
            <History aria-hidden />
            {historyLabel}
          </DropdownMenuItem>
        )}
        {contextMenuItems}
        {onUpload && !demoReadOnly && (
          <DropdownMenuItem disabled={resourceBusy} onClick={() => menuUploadRef.current?.click()}>
            <Upload aria-hidden />
            {t(kind === "storyboard" ? "media_upload_storyboard" : "media_upload_video")}
          </DropdownMenuItem>
        )}
      </>
    ) : undefined;

  return (
    <div>
      <div className="mb-2 flex min-h-7 items-center gap-1.5">
        {/* Side by side (wide media column): the title only. Tabs (narrow): the storyboard / video view icons, and
            the title itself switches to the other one, in the same mode (Preview or History); this pane's own icon
            returns to Preview. History sits right of the title. */}
        <Icon aria-hidden className={cn("size-3.5 text-muted-foreground", tabsOnly)} />
        <h3 className={cn("text-xs font-medium text-subtle-foreground", tabsOnly)}>{shownTitle}</h3>
        <ToggleGroup
          aria-label={t("media_tabs")}
          variant="outline"
          size="sm"
          className={tabsHidden}
          value={[kind]}
          onValueChange={(next: string[]) => {
            const to = (next[0] ?? kind) as ShotMediaKind;
            // the other pane opens in this pane's mode (History stays History); this pane's own icon returns to Preview
            setMode(to, to === kind ? "preview" : useShotMediaStore.getState().mode[kind]);
          }}
        >
          {(["storyboard", "video"] as const).map((value) => {
            const label = t(value === "storyboard" ? "media_storyboard_title" : "media_video_title");
            return (
              <Tooltip key={value}>
                <TooltipTrigger render={<ToggleGroupItem value={value} aria-label={label} />}>
                  {value === "storyboard" ? <ImageIcon aria-hidden /> : <Film aria-hidden />}
                </TooltipTrigger>
                <TooltipContent>{label}</TooltipContent>
              </Tooltip>
            );
          })}
        </ToggleGroup>
        <button
          type="button"
          className={cn("focus-ring rounded-sm text-xs font-medium text-subtle-foreground hover:text-foreground", tabsHidden)}
          title={t("media_switch_to", { name: otherTitle })}
          onClick={() =>
            setMode(kind === "storyboard" ? "video" : "storyboard", useShotMediaStore.getState().mode[kind])
          }
        >
          {shownTitle}
        </button>
        {hasVersions && (
          <Tooltip>
            <TooltipTrigger
              render={
                <Toggle
                  size="sm"
                  aria-label={historyLabel}
                  pressed={history}
                  onPressedChange={(pressed) => setMode(kind, pressed ? "history" : "preview")}
                />
              }
            >
              <History aria-hidden />
            </TooltipTrigger>
            <TooltipContent>{historyLabel}</TooltipContent>
          </Tooltip>
        )}
        {centerContent ? (
          <div className="flex min-w-0 flex-1 flex-wrap items-center justify-center gap-x-2 gap-y-1">{centerContent}</div>
        ) : (
          <span className="flex-1" />
        )}
        {leadingActions}
        {onUpload && !demoReadOnly && (
          <UploadIconButton
            accept={UPLOAD_ACCEPT[kind]}
            label={
              kind === "storyboard"
                ? t("media_upload_storyboard")
                : t("media_upload_video")
            }
            busy={uploading}
            disabled={resourceBusy}
            onSelect={(f) => { if (!freshBusy()) void onUpload(f); }}
          />
        )}
        {kind === "storyboard" && editScriptFile && !demoReadOnly && (
          <ImageEditButton
            projectName={projectName}
            resourceType="storyboard"
            resourceId={segmentId}
            scriptFile={editScriptFile}
            hasImage={Boolean(assetPath)}
            busy={resourceBusy}
          />
        )}
      </div>

      {showingOld && !history && (
        <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md bg-warn/15 px-2 py-1 text-xs">
          <span className="min-w-0 flex-1">
            <span className="num font-medium">{t("version_viewing", { version: viewedInfo.version })}</span>
            <span className="text-subtle-foreground">
              {" · "}
              {[versionTime(viewedInfo), versionOrigin(viewedInfo, t), viewedInfo.generation?.model, versionCost(viewedInfo)]
                .filter(Boolean)
                .join(" · ")}
            </span>
          </span>
          {!demoReadOnly && viewedInfo.restorable !== false && (
            <Button
              size="xs"
              disabled={resourceBusy || restoringVersion !== null}
              onClick={() => void restore(viewedInfo.version)}
            >
              {restoringVersion === viewedInfo.version ? t("switching_version") : t("version_make_current")}
            </Button>
          )}
          <Button size="xs" variant="ghost" onClick={() => view(kind, null)}>
            {t("version_back_to_current")}
          </Button>
        </div>
      )}
      {menuItems && onUpload && (
        <input
          ref={menuUploadRef}
          type="file"
          accept={UPLOAD_ACCEPT[kind]}
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f && !freshBusy()) void onUpload(f);
          }}
        />
      )}
      {history ? (
        <VersionHistoryPanel
          kind={kind}
          aspectRatio={aspectRatio}
          versions={versions.data?.versions ?? null}
          loading={versions.loading}
          error={versions.error}
          viewingVersion={viewedVersion}
          onView={(version) => view(kind, { segmentId, version })}
          onRestore={demoReadOnly ? undefined : (version) => void restore(version)}
          restoringVersion={restoringVersion}
          restoreDisabled={Boolean(resourceBusy)}
        />
      ) : showingOld && viewedInfo.file_url ? (
        kind === "storyboard" ? (
          <ImageContextMenu items={menuItems}>
            <PreviewableImageFrame src={viewedInfo.file_url} alt={t("version_preview_alt", { version: viewedInfo.version })}>
              <AspectFrame ratio={aspectRatio} className="ring-2 ring-warn">
                <img
                  src={viewedInfo.file_url}
                  alt={t("version_preview_alt", { version: viewedInfo.version })}
                  decoding="async"
                  className="size-full object-cover"
                />
              </AspectFrame>
            </PreviewableImageFrame>
          </ImageContextMenu>
        ) : (
          <div className="overflow-hidden rounded-lg ring-2 ring-warn">
            <AspectFrame ratio={aspectRatio}>
              {viewedInfo.presentation_available === true ? (
                <PresentationPlayer
                  key={`${segmentId}:v${viewedInfo.version}`}
                  projectName={projectName}
                  resourceType="videos"
                  resourceId={segmentId}
                  videoVersion={viewedInfo.version}
                />
              ) : (
                // eslint-disable-next-line jsx-a11y/media-has-caption -- an older version without the shared reader shows the raw file
                <video
                  src={viewedInfo.file_url}
                  aria-label={t("version_preview_alt", { version: viewedInfo.version })}
                  className="size-full bg-black object-contain"
                  controls
                  playsInline
                  preload="metadata"
                />
              )}
            </AspectFrame>
          </div>
        )
      ) : assetUrl ? (
        kind === "storyboard" ? (
          <ImageContextMenu items={menuItems}>
            <PreviewableImageFrame src={assetUrl} alt={`${itemLabel} ${title}`}>
              <AspectFrame ratio={aspectRatio}>
                <img
                  src={assetUrl}
                  srcSet={storyboardSrcSet}
                  sizes="(min-width: 1280px) 720px, 640px"
                  alt={`${itemLabel} ${title}`}
                  loading="lazy"
                  decoding="async"
                  className="size-full object-cover"
                />
              </AspectFrame>
            </PreviewableImageFrame>
          </ImageContextMenu>
        ) : (
          <ImageContextMenu items={menuItems}>
          <div className="overflow-hidden rounded-lg ring-1 ring-border">
            <AspectFrame ratio={aspectRatio}>
              <PresentationPlayer
                key={`${segmentId}:${assetFp ?? "current"}`}
                projectName={projectName}
                resourceType="videos"
                resourceId={segmentId}
                posterPath={posterPath}
                {...playbackStart}
              />
            </AspectFrame>
          </div>
          </ImageContextMenu>
        )
      ) : (
        // not generated yet: the same right-click menu (Generate, Generate with …, upload)
        <ImageContextMenu items={menuItems}>
          <AspectFrame ratio={aspectRatio}>
            <div className="flex size-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-muted/30 text-muted-foreground">
              <Icon aria-hidden className="size-5" />
              <span className="text-xs">{t("media_not_generated")}</span>
            </div>
          </AspectFrame>
        </ImageContextMenu>
      )}

      {onGenerate && !demoReadOnly && (
        <Button
          className="mt-2.5 w-full"
          size="lg"
          onClick={onGenerate}
          disabled={generateDisabled || resourceBusy}
          title={
            generateDisabled
              ? (generateDisabledHint ?? t("media_generate_video_disabled_hint"))
              : undefined
          }
        >
          <Sparkles aria-hidden data-icon="inline-start" />
          {generateLabel}
          {estimatedCost && Object.values(estimatedCost).some((v) => v > 0) && (
            <span className="num text-xs font-normal">~{formatCost(estimatedCost)}</span>
          )}
        </Button>
      )}
    </div>
  );
}

/** Right-click menu on the image or video (alternative to the title row's buttons); plain children without items. */
function ImageContextMenu({ items, children }: { items?: ReactNode; children: ReactNode }) {
  if (!items) return <>{children}</>;
  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger render={<div />}>{children}</ContextMenu.Trigger>
      <DropdownMenuContent className="w-auto min-w-40">{items}</DropdownMenuContent>
    </ContextMenu.Root>
  );
}
