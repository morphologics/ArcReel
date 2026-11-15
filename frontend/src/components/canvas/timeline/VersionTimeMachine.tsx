import { useCallback, useEffect, useId, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, Download, History } from "lucide-react";
import { cn } from "cn";
import { API, type VersionInfo } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { errMsg } from "@/utils/async";
import { useTrackWrite } from "@/components/canvas/lorebook/useAssetWrites";
import { PresentationPlayer } from "@/components/shared/PresentationPlayer";

interface VersionTimeMachineProps {
  projectName: string;
  resourceType: "storyboards" | "videos" | "audio" | "characters" | "character_derivatives" | "scenes" | "props" | "products" | "reference_videos" | "grids";
  resourceId: string;
  onRestore?: (version: number) => void | Promise<unknown>;
  /** Icon-only trigger button: hides label and chevron for narrow card headers. */
  iconOnly?: boolean;
  /** Allow preview/download history without exposing the restore mutation. */
  readOnly?: boolean;
  /**
   * The same resource is occupied by generation/editing (including the image_edit optimistic hold): disable version restore.
   * When an image_edit task finishes it unconditionally overwrites current with the edit result; restoring an old version during the hold would
   * show success but then be overwritten by the edit task, losing the user's last choice.
   *
   * 同资源正被生成/编辑占用（含 image_edit 乐观占用）：禁用版本恢复。
   * image_edit 任务完成时会无条件把 current 覆盖为编辑结果，占用期间恢复旧版本会
   * 显示成功但随后被编辑任务覆盖，用户最后一次选择丢失。
   */
  busy?: boolean;
  /**
   * Report the restore request's in-flight state to the parent: `busy` only covers the "external hold -> disable restore" direction; sibling controls
   * (generate, upload) also need to know in reverse that a restore is writing the same resource file, otherwise they stay clickable until the restore returns,
   * two requests write the same path concurrently, the later one overwrites the earlier, and both report success.
   *
   * 恢复请求在途状态回传父级：`busy` 只做「外部占用 → 禁恢复」这一向，兄弟控件
   * （生成、上传）还需反向知道恢复正在写同一个资源文件，否则恢复返回前它们仍可点，
   * 两个请求并发写同一路径、后完成者覆盖前者且双方都提示成功。
   */
  onRestoringChange?: (restoring: boolean) => void;
  /**
   * Hold re-check at submit time (fresh read): `busy` is the snapshot from the latest render; while the version panel is open,
   * an Agent enqueue, a batch entry or a poll landing in the DB may occupy the resource, and a click before the new prop reaches the button
   * would still send a restore request, writing the same resource file concurrently with the running task. Returning true rejects this restore.
   *
   * 提交时刻的占用复核（新鲜读）：`busy` 是最近一次渲染的快照，版本面板打开期间
   * Agent 入队、批量入口或轮询落库都可能占用该资源，新 prop 冲刷到按钮之前的点击
   * 仍会发出恢复请求，与在跑的任务并发写同一个资源文件。返回 true 即拒绝本次恢复。
   */
  checkBusy?: () => boolean;
  /**
   * Controlled open: open/close is decided externally (e.g. the card's "More" menu); no trigger button of its own is rendered,
   * the popover is positioned by `anchor`, and focus returns to it on close.
   *
   * 受控打开：由外部（如卡片的「更多」菜单）决定开合，不渲染自己的触发按钮，
   * 弹层按 `anchor` 定位，关闭后焦点回到它。
   */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  anchor?: RefObject<HTMLElement | null>;
}

function getImagePreviewHeightClass(
  resourceType: VersionTimeMachineProps["resourceType"],
): string {
  if (resourceType === "characters" || resourceType === "character_derivatives") return "h-80";
  if (resourceType === "scenes" || resourceType === "props" || resourceType === "products") return "h-56";
  return "h-64";
}

// The preview selection is stored under the owning resource identity and version fingerprint: after the resource or its versions change, another resource's selection is not reused
// 预览选中态记在所属资源身份与版本指纹之下：资源或其版本改变后不沿用另一份资源的选中
interface VersionSelection {
  projectName: string;
  resourcePath: string;
  fingerprint: number | null;
  version: number;
}

/**
 * A resource's version history: the trigger button opens a popover listing version numbers; selecting one previews the media and generation notes,
 * and a non-current version can be made current. The popover is positioned by the trigger button, follows it on scroll, and does not close by itself.
 *
 * 资源的历史版本：触发按钮打开弹层，列出版本号，选中一个即预览媒体与生成说明，
 * 非当前版本可切换为当前。弹层随触发按钮定位，滚动时跟随，不会自行关闭。
 */
export function VersionTimeMachine({
  projectName,
  resourceType,
  resourceId,
  onRestore,
  iconOnly = false,
  readOnly = false,
  busy = false,
  onRestoringChange,
  checkBusy,
  open: controlledOpen,
  onOpenChange,
  anchor,
}: VersionTimeMachineProps) {
  const { t } = useTranslation(["dashboard", "common"]);
  const track = useTrackWrite();
  const titleId = useId();
  const busyHintId = useId();
  const resourcePath =
    resourceType === "storyboards" ? `storyboards/scene_${resourceId}.png` :
    resourceType === "videos" ? `videos/scene_${resourceId}.mp4` :
    resourceType === "reference_videos" ? `reference_videos/${resourceId}.mp4` :
    resourceType === "audio" ? `audio/segment_${resourceId}.wav` :
    resourceType === "characters" ? `characters/${resourceId}.png` :
    // A derivative's resource id is itself `base/derivative`; both segments become path levels as is.
    // 衍生的 resource id 本身是 `本体/衍生`，两段原样成为路径层级。
    resourceType === "character_derivatives" ? `characters/derivatives/${resourceId}.png` :
    resourceType === "scenes" ? `scenes/${resourceId}.png` :
    resourceType === "grids" ? `grids/${resourceId}.png` :
    resourceType === "products" ? `products/${resourceId}.png` :
    `props/${resourceId}.png`;
  const resourceFp = useProjectsStore((s) => s.getAssetFingerprint(resourcePath));
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const controlled = controlledOpen !== undefined;
  const open = controlled ? controlledOpen : uncontrolledOpen;

  const [versions, setVersions] = useState<VersionInfo[]>([]);
  const [currentVersion, setCurrentVersion] = useState(0);
  const [loading, setLoading] = useState(false);
  const [reload, setReload] = useState(0);
  const [selection, setSelection] = useState<VersionSelection | null>(null);
  const selectedVersion =
    selection?.projectName === projectName &&
    selection.resourcePath === resourcePath &&
    selection.fingerprint === resourceFp
      ? selection.version
      : null;
  const [restoringVersion, setRestoringVersion] = useState<number | null>(null);
  const setOpen = (next: boolean) => {
    if (!next && restoringVersion !== null) return;
    if (controlled) onOpenChange?.(next);
    else setUncontrolledOpen(next);
  };

  const loadVersions = useCallback(async (signal: AbortSignal) => {
    setLoading(true);
    try {
      const data = await API.getVersions(projectName, resourceType, resourceId, { signal });
      if (signal.aborted) return;
      setVersions(data.versions);
      setCurrentVersion(data.current_version);
    } catch {
      if (!signal.aborted) setVersions([]);
    } finally {
      // react-doctor-disable-next-line react-doctor/no-loading-flag-reset-outside-finally -- already reset in finally; when aborted, the new load round that takes over is responsible for resetting / 已在 finally 中复位；被 abort 时由接管的新一轮加载负责复位
      if (!signal.aborted) setLoading(false);
    }
  }, [projectName, resourceType, resourceId]);

  useEffect(() => {
    if (!open || !resourceId) return;
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- on open or resource update, start a version load with a cancel signal and show the loading state in sync / 打开或资源更新时启动带取消信号的版本加载，并同步展示加载状态
    void loadVersions(controller.signal);
    return () => controller.abort();
  }, [open, resourceId, resourceFp, reload, loadVersions]);


  async function handleRestore(version: number) {
    // disabled is the reactive restoringVersion/busy: it updates when the resource becomes occupied while the panel is open;
    // this is a fallback so a click before the disabled state takes effect still does not send a restore request.
    // disabled 是响应式的 restoringVersion/busy：面板打开期间资源转为占用中时随之更新，
    // 这里兜底防止禁用态生效前的一次点击仍发出恢复请求。
    if (busy || restoringVersion !== null) return;
    // Do one fresh read on top of the render snapshot: in the window where state has changed but the render has not landed, the button is still clickable.
    // 渲染快照之外再做一次新鲜读：状态已变、渲染未到的窗口里按钮仍可点。
    if (checkBusy?.()) {
      useAppStore.getState().pushToast(t("version_restore_busy_hint"), "error");
      return;
    }
    setRestoringVersion(version);
    onRestoringChange?.(true);
    try {
      await track((async () => {
        const result = await API.restoreVersion(projectName, resourceType, resourceId, version);
        if (result.asset_fingerprints) {
          useProjectsStore.getState().updateAssetFingerprints(result.asset_fingerprints);
        }
        await onRestore?.(version);
        setReload((current) => current + 1);
        // The switch result shows directly in the "Current" marker and the media, so no toast on success; the restore changed the fingerprint, so record the selection under the latest fingerprint
        // 切换结果直接体现在「当前」标记与媒体上，成功时不弹提示；恢复已换了指纹，按最新指纹记选中
        setSelection({
          projectName,
          resourcePath,
          fingerprint: useProjectsStore.getState().getAssetFingerprint(resourcePath),
          version,
        });
      })());
    } catch (err) {
      useAppStore
        .getState()
        .pushToast(t("switch_version_failed", { message: errMsg(err) }), "error");
    } finally {
      setRestoringVersion(null);
      onRestoringChange?.(false);
    }
  }

  if (!resourceId) return null;

  // Derive the selected version's full info from the latest `versions` array
  const selectedInfo =
    selectedVersion != null
      ? versions.find((v) => v.version === selectedVersion) ?? null
      : null;
  const label = t("version_mgmt");

  const trigger = controlled ? null : iconOnly ? (
    <Tooltip>
      <TooltipTrigger
        render={<PopoverTrigger render={<Button variant="ghost" size="icon-sm" aria-label={label} />} />}
      >
        <History aria-hidden />
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  ) : (
    <PopoverTrigger render={<Button variant="ghost" size="xs" className="group/version" />}>
      <History aria-hidden data-icon="inline-start" />
      {label}
      <ChevronDown
        aria-hidden
        data-icon="inline-end"
        className="transition-transform duration-fast group-aria-expanded/version:rotate-180"
      />
    </PopoverTrigger>
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      {trigger}
      <PopoverContent align="end" anchor={anchor} finalFocus={anchor}>
        <div className="flex items-center justify-between gap-2">
          <PopoverTitle id={titleId}>{t("history_versions")}</PopoverTitle>
          {currentVersion > 0 && (
            <Badge variant="secondary">
              <span className="num">{t("current_version", { version: currentVersion })}</span>
            </Badge>
          )}
        </div>

        {loading ? (
          <p className="text-xs text-subtle-foreground">{t("common:loading")}</p>
        ) : versions.length === 0 ? (
          <div className="flex flex-col gap-1">
            <p className="text-sm">{t("no_history")}</p>
            <p className="text-xs text-subtle-foreground">{t("history_hint")}</p>
          </div>
        ) : (
          <>
            <ToggleGroup
              aria-labelledby={titleId}
              variant="outline"
              size="sm"
              className="flex-wrap"
              value={selectedVersion != null ? [String(selectedVersion)] : []}
              onValueChange={(next: string[]) =>
                setSelection(next[0] ? { projectName, resourcePath, fingerprint: resourceFp, version: Number(next[0]) } : null)
              }
            >
              {versions.map((v) => (
                <ToggleGroupItem key={v.version} value={String(v.version)}>
                  <span className="num">v{v.version}</span>
                  {v.is_current ? <span aria-hidden className="size-1.5 rounded-full bg-primary" /> : null}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>

            {selectedInfo ? (
              <div className="flex flex-col gap-2 rounded-md bg-muted/50 p-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="flex min-w-0 flex-wrap items-center gap-1.5">
                    <span className="num text-sm font-medium">v{selectedInfo.version}</span>
                    {selectedInfo.source === "image_edit" && (
                      <Badge variant="outline">{t("version_image_edit_badge")}</Badge>
                    )}
                    <span className="num text-xs text-subtle-foreground">{selectedInfo.created_at}</span>
                  </span>
                  {selectedInfo.is_current ? (
                    <Badge variant="secondary">{t("current_version_badge")}</Badge>
                  ) : !readOnly && selectedInfo.restorable !== false ? (
                    <Button
                      size="xs"
                      disabled={restoringVersion !== null || busy}
                      aria-describedby={busy ? busyHintId : undefined}
                      onClick={() => void handleRestore(selectedInfo.version)}
                    >
                      {restoringVersion === selectedInfo.version ? t("switching_version") : t("switch_to_version")}
                    </Button>
                  ) : null}
                </div>
                {busy && !selectedInfo.is_current && !readOnly && selectedInfo.restorable !== false ? (
                  <p id={busyHintId} className="text-xs text-subtle-foreground">
                    {t("version_restore_busy_hint")}
                  </p>
                ) : null}

                {selectedInfo.file_url &&
                  (resourceType === "videos" || resourceType === "reference_videos" ? (
                    <div className="aspect-video w-full overflow-hidden rounded-md bg-black">
                      {selectedInfo.presentation_available !== true ? (
                        // eslint-disable-next-line jsx-a11y/media-has-caption -- historical videos that cannot enter the shared final-cut reader only show the raw media / 无法进入共享成片读取器的历史视频仅展示原始媒体
                        <video
                          src={selectedInfo.file_url}
                          aria-label={t("version_preview_alt", { version: selectedInfo.version })}
                          className="size-full object-contain"
                          controls
                          playsInline
                          preload="none"
                        />
                      ) : (
                        <PresentationPlayer
                          key={`${resourceType}:${resourceId}:${selectedInfo.version}`}
                          projectName={projectName}
                          resourceType={resourceType}
                          resourceId={resourceId}
                          videoVersion={selectedInfo.version}
                        />
                      )}
                    </div>
                  ) : resourceType === "audio" ? (
                    // eslint-disable-next-line jsx-a11y/media-has-caption -- the transcript of historical narration is shown in the same preview card / 历史旁白的文字记录显示在同一预览卡片
                    <audio
                      src={selectedInfo.file_url}
                      aria-label={t("version_audio_preview_label", { version: selectedInfo.version })}
                      className="h-9 w-full"
                      controls
                      preload="metadata"
                    />
                  ) : (
                    <div
                      className={cn(
                        "flex w-full items-center justify-center rounded-md bg-background p-2",
                        getImagePreviewHeightClass(resourceType),
                      )}
                    >
                      <img
                        src={selectedInfo.file_url}
                        alt={t("version_preview_alt", { version: selectedInfo.version })}
                        className="max-h-full w-full object-contain"
                      />
                    </div>
                  ))}

                {resourceType === "audio" && selectedInfo.file_url && (
                  <a
                    href={selectedInfo.file_url}
                    download
                    className={cn(buttonVariants({ variant: "outline", size: "xs" }), "self-start")}
                  >
                    <Download aria-hidden data-icon="inline-start" />
                    {t("version_download_audio")}
                  </a>
                )}

                <p className="line-clamp-4 text-xs text-subtle-foreground">
                  {selectedInfo.prompt ||
                    (selectedInfo.source === "manual_upload"
                      ? t("version_manual_upload")
                      : t("version_no_notes"))}
                </p>
              </div>
            ) : (
              <p className="text-xs text-subtle-foreground">{t("version_click_hint")}</p>
            )}
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
