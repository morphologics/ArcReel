import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Grid2x2,
  Loader2,
  MapPin,
  Package,
  RefreshCw,
  Scissors,
  Upload,
  User,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";
import { API } from "@/api";
import { enqueueGridRegenerate } from "@/actions/generation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TruncatedText } from "@/components/shared/TruncatedText";
import { VersionTimeMachine } from "@/components/canvas/timeline/VersionTimeMachine";
import { errMsg } from "@/utils/async";
import { useProjectsStore } from "@/stores/projects-store";
import { useAppStore } from "@/stores/app-store";
import { isResourceBusy, useActiveResourceIds, useTasksStore } from "@/stores/tasks-store";
import type { GridGeneration, ReferenceImage } from "@/types/grid";
import { gridImageAspect, type GridImageAspect } from "./grid-preview-model";

export interface GridPreviewPanelProps {
  projectName: string;
  /** Grids matched to this group; several when a group exceeds the per-image cell limit. / 这一组对上的宫格，一组超过单张格数上限时有多张。 */
  gridIds: string[];
  /** A write request for the same group (e.g. "Generate this group" on the group card) is in flight: panel actions are disabled accordingly. / 组卡上「生成这一组」等同一组的写请求在途：面板动作随之禁用。 */
  busy?: boolean;
  /** When a generate entry point exists, the empty state tells the user to start generating from the group card's title row. / 有生成入口时，空态提示用户从组卡标题行开始生成。 */
  canGenerate?: boolean;
  /** Notify the parent to refresh the grid list after a regenerate submission succeeds. / 重新生成提交成功后通知父级刷新宫格列表。 */
  onRegenerated?: () => void;
}

type GridDisplayStatus = GridGeneration["status"] | "interrupted";

/**
 * Placeholder aspect ratio and width before the grid composite loads: width is the smaller of "column width" and "width at 70cqh height for that ratio".
 * When the ratio is inferred correctly it matches the post-load layout (own image size constrained by max-w-full, max-h-[70cqh]), so nothing jumps before/after load.
 * The width/height attributes only provide the ratio.
 *
 * 联合图加载前的占位比例与宽度：宽度取「栏宽」与「70cqh 高度下按该比例的宽度」中较小者，
 * 比例推算正确时与加载后按图片自身尺寸受 max-w-full、max-h-[70cqh] 约束的结果一致，加载前后不跳动。
 * width/height 属性只提供比例。
 */
const GRID_IMAGE_BOX: Record<GridImageAspect, { width: number; height: number; className: string }> = {
  "16:9": { width: 1920, height: 1080, className: "w-[min(100%,calc(70cqh*16/9))]" },
  "9:16": { width: 1080, height: 1920, className: "w-[min(100%,calc(70cqh*9/16))]" },
  "4:3": { width: 1920, height: 1440, className: "w-[min(100%,calc(70cqh*4/3))]" },
  "3:4": { width: 1440, height: 1920, className: "w-[min(100%,calc(70cqh*3/4))]" },
};

const STATUS_ICON: Record<Exclude<GridDisplayStatus, "generating">, typeof Clock> = {
  pending: Clock,
  completed: CheckCircle2,
  failed: AlertCircle,
  interrupted: AlertCircle,
};

const STATUS_TONE: Record<GridDisplayStatus, string> = {
  pending: "text-muted-foreground",
  generating: "text-primary",
  completed: "text-good",
  failed: "text-destructive",
  interrupted: "text-warn",
};

function GridStatus({ status }: { status: GridDisplayStatus }) {
  const { t } = useTranslation("dashboard");
  const Icon = status === "generating" ? null : STATUS_ICON[status];
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${STATUS_TONE[status]}`}>
      {Icon ? (
        <Icon aria-hidden className="size-3.5" />
      ) : (
        <span aria-hidden className="size-1.5 animate-breath rounded-full bg-primary" />
      )}
      {t(`grid_status_${status}`)}
    </span>
  );
}

const REF_ICON: Record<ReferenceImage["ref_type"], typeof User> = {
  character: User,
  scene: MapPin,
  prop: Package,
};

function ReferenceImages({
  references,
  projectName,
}: {
  references: ReferenceImage[];
  projectName: string;
}) {
  const { t } = useTranslation("dashboard");
  const fingerprints = useProjectsStore((s) => s.assetFingerprints);
  return (
    <div className="flex flex-col gap-1.5">
      <h4 className="text-xs font-medium text-muted-foreground">{t("grid_reference_images")}</h4>
      <ul className="flex flex-wrap gap-2">
        {references.map((ref) => {
          const Icon = REF_ICON[ref.ref_type] ?? User;
          return (
            <li key={ref.path} className="flex w-16 flex-col gap-1">
              <img
                src={API.getFileUrl(projectName, ref.path, fingerprints[ref.path] ?? null, { width: 160 })}
                alt=""
                loading="lazy"
                decoding="async"
                className="aspect-square w-full rounded-md border border-border bg-muted object-cover"
              />
              <span className="flex min-w-0 items-center gap-1 text-xs text-subtle-foreground">
                <Icon aria-hidden className="size-3 shrink-0" />
                <TruncatedText text={ref.name} className="min-w-0" />
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * Content of one grid group: grid composite selection, status and hints, actions (versions, upload, grid split, regenerate), reference images, and the grid composite itself.
 * The grid composite fills the remaining width in the media column: when the group card is narrower than 860px the info column is 1fr and the media column 300px; otherwise the info column is 280–360px and the media column fills the rest.
 *
 * 一组宫格的内容：选择联合图、状态与提示、动作（版本、上传、切分落格、重新生成）、参考图，以及联合图本身。
 * 联合图在媒体栏里占满剩余宽度：组卡宽度不足 860px 时信息栏 1fr、媒体栏 300px，否则信息栏 280–360px、媒体栏占满剩余。
 */
export function GridPreviewPanel({
  projectName,
  gridIds,
  busy = false,
  canGenerate = false,
  onRegenerated,
}: GridPreviewPanelProps) {
  const { t } = useTranslation("dashboard");
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [grid, setGrid] = useState<GridGeneration | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const [splitting, setSplitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [restoring, setRestoring] = useState(false);
  // URL of the grid composite that has finished loading: before load, use a placeholder with the ratio inferred from the record; after load, lay out by the original image size
  // 已加载完成的联合图地址：加载前按记录推算的比例占位，加载后改回按原图尺寸排版
  const [loadedImageUrl, setLoadedImageUrl] = useState<string | null>(null);
  const project = useProjectsStore((s) => s.currentProjectData);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const multipleGrids = gridIds.length > 1;
  const safeIdx = Math.min(selectedIdx, Math.max(0, gridIds.length - 1));
  const selectedGridId = gridIds[safeIdx] ?? null;

  // Subscribe directly to the global grid change signal as the only refetch trigger. / 直接订阅全局 grid 变更信号作为唯一 refetch 触发源。
  const gridsRevision = useAppStore((s) => s.gridsRevision);
  const tasksConnected = useTasksStore((s) => s.connected);

  useEffect(() => {
    if (!selectedGridId) return;
    const controller = new AbortController();
    if (!grid || grid.id !== selectedGridId) {
      // When switching grids, clear old data and show the loading state, then fire the async fetch. Clearing also unmounts the
      // version time machine below: it fetches the version list by resourceId with no cancellation; if old data stayed rendered during loading,
      // the previous grid's in-flight version list would land in the new grid's panel, and selecting one would restore with the new grid ID + old version number.
      // 切换宫格时清空旧数据并展示加载状态，再触发异步 fetch。清空同时会卸载下方的
      // 版本时光机——它按 resourceId 拉版本列表且无取消，若改成加载期间留着旧数据渲染，
      // 上一张在途的版本列表会落进新宫格的面板，选中即按新宫格 ID + 旧版本号发起还原。
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Switching grids is an action-driven reset; old data must be cleared before fetching / 切换宫格是动作驱动重置，须先清空旧数据再发起拉取
      setLoading(true);
      setGrid(null);
    }
    setError(null);

    API.getGrid(projectName, selectedGridId, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setGrid(data);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(errMsg(err, t("grid_load_failed")));
        setLoading(false);
      });

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- grid is only used to detect a grid switch; t is stable / grid 仅用于切换宫格判断；t 稳定
  }, [selectedGridId, projectName, gridsRevision]);

  // Occupied state comes from the live tasks store: same source as sibling controls on the page,
  // no longer relying on the local grid.status snapshot (only updated on refresh, so between submission and the next fetch it would wrongly look idle).
  // 占用判定接入 live tasks store：与同页兄弟控件同源，
  // 不再依赖本地 grid.status 快照（刷新才更新，提交后到下次 fetch 之间会误判为空闲）。
  const activeGridIds = useActiveResourceIds("grid", projectName);
  const isInProgress = selectedGridId != null && activeGridIds.has(selectedGridId);
  const isInterrupted =
    tasksConnected &&
    selectedGridId != null &&
    grid != null &&
    (grid.status === "pending" || grid.status === "generating") &&
    !isInProgress;
  const displayStatus: GridDisplayStatus = isInterrupted ? "interrupted" : grid?.status ?? "pending";
  // Panel actions (regenerate/split/upload/version restore) are mutually exclusive: they all write the same grid composite or shot cells,
  // so while any is in flight, sibling controls are disabled too.
  // 面板动作（重生成/切分/上传/版本恢复）互斥：都写同一份联合图或分镜格，
  // 任一在途时兄弟控件同步禁用。
  const actionBusy = busy || regenerating || splitting || uploading || restoring || isInProgress;

  // Fresh occupied read at submit time: beyond the render snapshot (isInProgress), re-check the latest tasks store state
  // 提交时刻的占用新鲜读：渲染快照（isInProgress）之外复核 tasks store 最新状态
  const freshBusy = () =>
    selectedGridId != null && isResourceBusy("grid", projectName, selectedGridId);

  const rejectBusy = () => {
    if (!freshBusy()) return false;
    // Use a toast rather than setError: error is the panel's overall error state and would replace the grid composite and actions,
    // while an occupied rejection is a transient hint that should not wreck the current view.
    // 用 toast 而非 setError：error 是面板的整体错误态，会把联合图与动作一并替换掉，
    // 占用拒绝是瞬态提示，不该毁掉当前视图。
    useAppStore.getState().pushToast(t("grid_regenerate_busy"), "error");
    return true;
  };

  const handleSplit = () => {
    if (!grid || !selectedGridId || actionBusy || rejectBusy()) return;
    setSplitting(true);
    API.splitGrid(projectName, selectedGridId)
      .then((res) => {
        useProjectsStore.getState().updateAssetFingerprints(res.asset_fingerprints);
        // Only write back to the grid that started the split: switching grids is not blocked by action disabling, and the user may have switched to
        // another grid while the request was in flight; writing back unconditionally would record that request's split_at on another grid.
        // 只回写发起切分的那个宫格：切换宫格不受动作禁用限制，请求在途时用户可能已切到
        // 别的宫格，无条件回写会把该请求的 split_at 记到另一个宫格上。
        setGrid((prev) => (prev && prev.id === selectedGridId ? { ...prev, split_at: res.split_at } : prev));
        useAppStore
          .getState()
          .pushToast(t("grid_split_success", { count: res.updated_scene_ids.length }), "success");
        if (res.missing_scene_ids.length > 0) {
          useAppStore
            .getState()
            .pushToast(t("grid_split_missing_skipped", { count: res.missing_scene_ids.length }), "error");
        }
      })
      .catch((err: unknown) => {
        // Transient failures go to a toast, not setError, which would wreck the whole panel view / 瞬态失败走 toast，不用 setError 毁掉整个面板视图
        useAppStore.getState().pushToast(t("grid_split_failed", { message: errMsg(err) }), "error");
      })
      .finally(() => setSplitting(false));
  };

  const handleUploadFile = (file: File) => {
    if (!selectedGridId || actionBusy || rejectBusy()) return;
    setUploading(true);
    API.uploadGridImage(projectName, selectedGridId, file)
      .then((res) => {
        useProjectsStore.getState().updateAssetFingerprints(res.asset_fingerprints);
        // The grid composite content and grid record (status / split_at) have changed; refetch details via the global invalidation signal
        // 联合图内容与宫格记录（status / split_at）已变，走全局失效信号重拉详情
        useAppStore.getState().invalidateGrids();
        useAppStore.getState().pushToast(t("grid_upload_success"), "success");
      })
      .catch((err: unknown) => {
        useAppStore.getState().pushToast(t("grid_upload_failed", { message: errMsg(err) }), "error");
      })
      .finally(() => setUploading(false));
  };

  const handleRegenerate = () => {
    if (!selectedGridId || actionBusy || rejectBusy()) return;
    const targetId = selectedGridId;
    setRegenerating(true);
    enqueueGridRegenerate(projectName, targetId, grid?.script_file ?? null)
      .then(() => {
        // Same as split: the user may have switched to another grid composite while the request was in flight; only write back to the one that sent it
        // 与切分同理：请求在途时可能已切到别的联合图，只回写发起请求的那张
        setGrid((prev) => (prev && prev.id === targetId ? { ...prev, status: "pending" } : prev));
        onRegenerated?.();
      })
      .catch((err: unknown) => {
        // Transient failures go to a toast: after switching away, setError would replace the whole panel of another grid composite
        // 瞬态失败走 toast：切走后再用 setError 会把另一张联合图的面板整个替换掉
        useAppStore.getState().pushToast(errMsg(err, t("grid_regenerate_failed")), "error");
      })
      .finally(() => setRegenerating(false));
  };

  // Use the persisted mtime fingerprint as cache-bust, which still works across page reloads. Versioned URLs are cached long as immutable,
  // so the version can only be the file's own fingerprint; before the fingerprint arrives, send no version and use the server's conditional caching.
  // 用持久化的 mtime 指纹做 cache-bust，跨页面刷新仍然有效。带版本的地址按 immutable 长缓存，
  // 版本只能取文件自身的指纹；指纹尚未送达时不带版本，走服务端的协商缓存。
  const gridFp = useProjectsStore((s) =>
    grid?.grid_image_path ? (s.assetFingerprints[grid.grid_image_path] ?? null) : null,
  );
  // The grid composite in the panel is at most about 1280px wide; use the 1280-wide thumbnail instead of fetching the original
  // 面板里联合图最宽约 1280px，取 1280 宽的缩略图，不拉原图
  const imageUrl = grid?.grid_image_path
    ? API.getFileUrl(projectName, grid.grid_image_path, gridFp, { width: 1280 })
    : null;

  if (gridIds.length === 0) {
    return (
      <div className="flex flex-col items-center gap-1.5 px-4 py-8 text-center">
        <Grid2x2 aria-hidden className="size-6 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">{t("grid_no_grids_yet")}</p>
        {canGenerate && <p className="text-xs text-muted-foreground">{t("grid_generate_instruction")}</p>}
      </div>
    );
  }

  if (error) {
    return (
      <p role="alert" className="m-4 flex items-start gap-2 text-sm text-destructive">
        <AlertCircle aria-hidden className="mt-0.5 size-4 shrink-0" />
        <span className="min-w-0 wrap-anywhere">{error}</span>
      </p>
    );
  }

  if (loading || !grid) {
    return (
      <p className="flex items-center justify-center gap-2 px-4 py-8 text-sm text-muted-foreground">
        <Loader2 aria-hidden className="size-4 animate-spin" />
        {t("grid_loading_data")}
      </p>
    );
  }

  const refs = grid.reference_images ?? [];
  const unsplit = grid.status === "completed" && Boolean(grid.grid_image_path) && !grid.split_at;
  const generatingImage = displayStatus === "generating" || displayStatus === "pending";
  const imageBox = GRID_IMAGE_BOX[gridImageAspect(grid, project)];
  const imageLoaded = imageUrl !== null && loadedImageUrl === imageUrl;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_18.75rem] gap-4 p-4 @min-[53.75rem]/grid:grid-cols-[minmax(17.5rem,22.5rem)_minmax(0,1fr)]">
      <div className="flex min-w-0 flex-col gap-3">
        {multipleGrids && (
          <div role="group" aria-label={t("grid_picker_label")} className="flex flex-wrap gap-1">
            {gridIds.map((id, idx) => (
              <Button
                key={id}
                variant={idx === safeIdx ? "secondary" : "ghost"}
                size="xs"
                aria-pressed={idx === safeIdx}
                aria-label={t("grid_picker_item", { index: idx + 1 })}
                onClick={() => setSelectedIdx(idx)}
              >
                <span className="num">{idx + 1}</span>
              </Button>
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <GridStatus status={displayStatus} />
          {unsplit && (
            <Badge variant="outline">
              <Scissors aria-hidden data-icon="inline-start" />
              {t("grid_unsplit_hint")}
            </Badge>
          )}
        </div>

        {isInterrupted && <p className="text-xs text-warn">{t("grid_interrupted_hint")}</p>}
        {unsplit && <p className="text-xs text-muted-foreground">{t("grid_unsplit_detail")}</p>}
        {grid.error_message && (
          <p className="text-xs wrap-anywhere text-destructive">{grid.error_message}</p>
        )}

        <p className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
          <span className="shrink-0">
            {t("grid_cell_info", { count: grid.cell_count, rows: grid.rows, cols: grid.cols })}
          </span>
          <span aria-hidden>·</span>
          <TruncatedText text={grid.model} className="min-w-0" />
        </p>

        <div className="flex flex-wrap items-center gap-1.5">
          <Button variant="outline" size="sm" disabled={actionBusy} onClick={handleRegenerate}>
            {regenerating || isInProgress ? (
              <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
            ) : (
              <RefreshCw aria-hidden data-icon="inline-start" />
            )}
            {regenerating ? t("grid_regenerating") : isInProgress ? t("generating_grid") : t("grid_regenerate_btn")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={actionBusy || !grid.grid_image_path}
            onClick={handleSplit}
          >
            {splitting ? (
              <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
            ) : (
              <Scissors aria-hidden data-icon="inline-start" />
            )}
            {splitting ? t("grid_splitting") : t("grid_split_btn")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={actionBusy}
            onClick={() => fileInputRef.current?.click()}
          >
            {uploading ? (
              <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
            ) : (
              <Upload aria-hidden data-icon="inline-start" />
            )}
            {uploading ? t("grid_uploading") : t("grid_upload_btn")}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              // Reset value: selecting the same file a second time must still trigger change / 复位 value：同一文件二次选择也要触发 change
              e.target.value = "";
              if (file) handleUploadFile(file);
            }}
          />
          <VersionTimeMachine
            projectName={projectName}
            resourceType="grids"
            resourceId={grid.id}
            iconOnly
            busy={actionBusy}
            checkBusy={freshBusy}
            onRestoringChange={setRestoring}
            onRestore={() => {
              // Restore swaps back to a historical grid composite and resets the split state; refetch the record to sync split_at
              // 还原换回历史联合图并复位切分态，重拉记录同步 split_at
              useAppStore.getState().invalidateGrids();
            }}
          />
        </div>

        {refs.length > 0 && (
          <ReferenceImages references={refs} projectName={projectName} />
        )}
      </div>

      {imageUrl ? (
        <img
          src={imageUrl}
          alt={t("grid_composite_image_alt")}
          width={imageBox.width}
          height={imageBox.height}
          loading="lazy"
          decoding="async"
          onLoad={() => setLoadedImageUrl(imageUrl)}
          className={cn(
            "block h-auto max-h-[70cqh] max-w-full justify-self-start rounded-md border border-border bg-muted object-contain",
            imageLoaded ? "w-auto" : imageBox.className,
          )}
        />
      ) : (
        <div className="flex min-h-40 items-center justify-center gap-2 rounded-md border border-dashed border-border text-xs text-muted-foreground">
          {generatingImage ? t("generating_grid") : t("grid_no_image")}
        </div>
      )}
    </div>
  );
}
