import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";
import type { DurationOutOfRangeReason } from "@/hooks/useModelCapabilities";
import type {
  NarrationSegment,
  DramaScene,
  AdShot,
} from "@/types";
import { useAppStore } from "@/stores/app-store";
import { getScriptItemId, type EditorContentMode } from "@/utils/script-shape";
import { stepAnchor } from "@/utils/move-anchor";
import { RetainedEditUnit } from "@/components/shared/edit-unit/RetainedEditUnit";
import { useConfirmLeave } from "@/components/shared/edit-unit/LeaveGuard";
import { ShotList } from "./ShotList";
import { ShotStrip } from "./ShotStrip";
import { ShotDetail, type ShotEditHandle } from "./ShotDetail";
import { ShotShortcutHint } from "./ShotShortcutHint";
import type { InsertShotHandler } from "./ShotStructureActions";
import { useShotShortcuts } from "./useShotShortcuts";

type Segment = NarrationSegment | DramaScene | AdShot;

interface ShotSplitViewProps {
  segments: Segment[];
  contentMode: EditorContentMode;
  aspectRatio: "9:16" | "16:9";
  projectName: string;
  /** Current episode number; only when given does the shot detail offer the per-shot "Write prompts" entry / 当前集号；给了才在分镜详情里提供单条「编写提示词」入口 */
  episode?: number;
  /** Current episode's script file name; storyboard image/video self-upload needs it to locate the script entry / 当前剧集剧本文件名，分镜图/视频自主上传需要它定位剧本条目 */
  scriptFile?: string;
  /** Submit incremental shot field changes; throws on failure, resolves to whether the project was refreshed; when omitted, display is read-only. / 提交分镜字段的增量修改，失败时抛错，resolve 为项目是否已刷新；缺省时只读展示。 */
  onUpdatePrompt?: (segmentId: string, patch: Record<string, unknown>) => Promise<boolean>;
  /** Shot reorder: move after afterId, null moves to the front; resolves to whether the move succeeded / 分镜改序：移到 afterId 之后，null 移到最前；resolve 为是否移动成功 */
  onMoveShot?: (shotId: string, afterId: string | null) => Promise<boolean>;
  /** Add a shot (narration carries body text): afterId null appends at the end; resolves to whether it succeeded / 新增分镜（旁白带正文）：afterId 为 null 时追加到末尾；resolve 为是否成功 */
  onInsertShot?: InsertShotHandler;
  /** Remove a shot; resolves to whether it succeeded / 移除分镜，resolve 为是否成功 */
  onRemoveShot?: (itemId: string) => Promise<boolean>;
  /** `imageModel`: "provider/model" for this request only ("Generate with …"). */
  onGenerateStoryboard?: (segmentId: string, scriptFile?: string, imageModel?: string) => void;
  /** `videoModel`: "provider/model" for this request only ("Regenerate with …"). */
  onGenerateVideo?: (segmentId: string, scriptFile?: string, videoModel?: string) => void | Promise<void>;
  onGenerateNarration?: (segmentId: string) => void;
  onRestoreStoryboard?: () => Promise<unknown> | void;
  onRestoreVideo?: () => Promise<unknown> | void;
  generatingStoryboard?: (segmentId: string) => boolean;
  generatingVideo?: (segmentId: string) => boolean;
  generatingNarration?: (segmentId: string) => boolean;
  durationOptions?: number[];
  /** Empty duration options mean this dimension is fixed by the endpoint (the workflow sets clip length itself), not that the model has no registered durations. / 档位为空是因为这一维由端点固定（workflow 自己定片长），不是型号没登记时长。 */
  durationEndpointFixed?: boolean;
  lastFrame?: boolean | null;
  capabilitiesLoading?: boolean;
  /** Cause classification for a saved duration out of range; when omitted, ShotDetail falls back to the generic warning that does not distinguish causes. / 已保存时长越界的成因判定；缺省时 ShotDetail 退回不区分成因的通用警告文案。 */
  durationWarningReason?: (seconds: number) => DurationOutOfRangeReason | null;
}

/**
 * The selection is tracked by shot ID, so it does not jump when the list is reordered or shots are inserted before it;
 * when the ID is null or not in the current list (just removed), fall back to the remembered position and resolve that position's ID at render time.
 *
 * 选中项按分镜 ID 记，列表被重排或在前面插入分镜时选中态不跳走；
 * ID 为 null 或不在当前列表里（刚被移除）时退回记下的位置，并在渲染时认定该位置的 ID。
 */
interface Selection {
  id: string | null;
  index: number;
  /** After adding a shot, select by position: while the list is still the same one, do not resolve the ID; wait for the refresh to bring the new shot. / 新增分镜后按位置选中：列表还是这一份时不认定 ID，等刷新出新分镜。 */
  staleIds?: readonly string[];
}

/**
 * Shot split view: horizontal shot strip on top (the whole work joined episode by episode, see ShotStrip) + full-width shot detail below. / 分镜分屏：上方横向分镜条（整部作品按集连在一起，见 ShotStrip）+ 下方全宽的分镜详情。
 * Reorder mode swaps the strip for the vertical list with drag handles (left, 220px) beside the detail.
 * Switching shots goes through the leave guard: if the current shot has unsaved changes, ask first. J / K switch shots, ⌘S / Ctrl+S saves the current shot. / 切换分镜经离开拦截：当前分镜有未保存修改时先询问。J / K 切换分镜，⌘S / Ctrl+S 保存当前分镜。
 */
export function ShotSplitView({
  segments,
  contentMode,
  aspectRatio,
  projectName,
  episode,
  scriptFile,
  onUpdatePrompt,
  onMoveShot,
  onInsertShot,
  onRemoveShot,
  onGenerateStoryboard,
  onGenerateVideo,
  onGenerateNarration,
  onRestoreStoryboard,
  onRestoreVideo,
  generatingStoryboard,
  generatingVideo,
  generatingNarration,
  durationOptions,
  durationEndpointFixed,
  lastFrame,
  capabilitiesLoading,
  durationWarningReason,
}: ShotSplitViewProps) {
  const { t } = useTranslation("common");
  const [selection, setSelection] = useState<Selection>({ id: null, index: 0 });
  // Reorder mode: the vertical list with drag handles instead of the strip (reordering is rare).
  const [reorderMode, setReorderMode] = useState(false);
  const [movePending, setMovePending] = useState(false);
  const [structurePending, setStructurePending] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const editRef = useRef<ShotEditHandle | null>(null);
  const confirmLeave = useConfirmLeave();

  const ids = useMemo(() => segments.map((s) => getScriptItemId(s, contentMode)), [segments, contentMode]);
  const selectedById = selection.id === null ? -1 : ids.indexOf(selection.id);
  const safeIndex = selectedById !== -1 ? selectedById : Math.max(0, Math.min(selection.index, ids.length - 1));
  const segmentId = ids[safeIndex];
  if (selection.id === null && segmentId !== undefined && selection.staleIds !== ids) {
    setSelection({ id: segmentId, index: safeIndex });
  }

  /** Switch to the shot at index; if the current shot has unsaved changes, the leave guard asks first. / 切到第 index 个分镜；当前分镜有未保存修改时先由离开拦截询问。 */
  const select = useCallback((index: number) => {
    const id = ids[index];
    if (id === undefined) return;
    if (id === selection.id) {
      setSelection({ id, index });
      return;
    }
    confirmLeave(() => setSelection({ id, index }), { saveLabel: t("save_and_switch") });
  }, [ids, selection.id, confirmLeave, t]);
  const selectPrev = useCallback(() => select(Math.max(0, safeIndex - 1)), [select, safeIndex]);
  const selectNext = useCallback(() => select(Math.min(ids.length - 1, safeIndex + 1)), [select, ids.length, safeIndex]);
  const saveSelected = useCallback(() => editRef.current?.save(), []);

  // Shot reorder: drop further actions while a request is in flight (rapid clicks would compute anchors from a stale order). The selection follows the original shot by ID.
  // 分镜改序：请求在途时丢弃后续操作（快速连点会基于过期顺序计算锚点）。选中态按 ID 跟随原来的分镜。
  const handleMoveShot = onMoveShot
    ? async (shotId: string, afterId: string | null) => {
        if (movePending) return;
        setMovePending(true);
        try {
          await onMoveShot(shotId, afterId);
        } finally {
          setMovePending(false);
        }
      }
    : undefined;
  // Convert move-forward / move-back by one in the detail into an anchor. / 详情里的前移、后移一位换算成锚点。
  const handleMoveStep = handleMoveShot
    ? (shotId: string, direction: "earlier" | "later") => {
        const afterId = stepAnchor(ids, ids.indexOf(shotId), direction);
        if (afterId !== undefined) void handleMoveShot(shotId, afterId);
      }
    : undefined;

  // Add / remove shot: while a request is in flight, lock shot switching and the add/remove entries. After a successful add, select the new shot right after;
  // after a successful remove the position stays, landing on what was the next shot (clamped to the new last shot if it was the last).
  // 新增 / 移除分镜：请求在途锁定切镜与增删入口。新增成功后选中紧随其后的新分镜；
  // 移除成功后位置不动，落到原来的下一条（末条时夹紧到新的末条）。
  const runStructureChange = async (change: () => Promise<boolean>, onSuccess: () => void) => {
    if (structurePending) return false;
    setStructurePending(true);
    try {
      const changed = await change();
      if (changed) onSuccess();
      return changed;
    } finally {
      setStructurePending(false);
    }
  };
  const handleInsertShot: InsertShotHandler | undefined = onInsertShot
    ? (afterId, novelText) =>
        runStructureChange(
          () => onInsertShot(afterId, novelText),
          // When inserted after the current shot, select the new shot right after it; when appended at the end, select the new last shot.
          // The new shot's ID is only known after the list refreshes, so select by position first; switching away also goes through the leave guard.
          // 插在当前分镜之后的选中紧随其后的新分镜；追加到末尾的选中新的末条。
          // 新分镜的 ID 要等列表刷新后才知道，先按位置选中；切走前同样经离开拦截。
          () => {
            const index = afterId === null ? ids.length : safeIndex + 1;
            confirmLeave(() => setSelection({ id: null, index, staleIds: ids }), { saveLabel: t("save_and_switch") });
          },
        )
    : undefined;
  const handleRemoveShot = onRemoveShot
    ? (itemId: string) =>
        runStructureChange(
          () => onRemoveShot(itemId),
          () => setSelection({ id: null, index: safeIndex }),
        )
    : undefined;

  useShotShortcuts({
    rootRef,
    onPrev: selectPrev,
    onNext: selectNext,
    onSave: onUpdatePrompt ? saveSelected : undefined,
    navDisabled: movePending || structurePending,
  });

  // Locate a shot (notifications, workflow progress, in-app links, Agent changes): the split layout only needs to switch the selected shot, no DOM scrolling.
  // Switching away also goes through the leave guard; auto-locate from Agent changes is already skipped by the event stream when there are unsaved changes, so editing is not interrupted.
  // 定位到分镜（通知、制作进度、应用内链接、Agent 改动）：分屏布局只需切换选中分镜，不做 DOM 滚动。
  // 切走前同样经离开拦截；Agent 改动带来的自动定位在有未保存修改时已由事件流自己略过，不会打断编辑。
  const scrollTarget = useAppStore((s) => s.scrollTarget);
  const clearScrollTarget = useAppStore((s) => s.clearScrollTarget);
  useEffect(() => {
    if (scrollTarget?.type !== "segment") return;
    const idx = ids.indexOf(scrollTarget.id);
    if (idx !== -1) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- switch the selected shot in response to a one-off locate request from an external store: switch right away if there are no changes, otherwise wait for the leave guard to allow it / 响应外部 store 的一次性定位请求切换选中分镜：没有修改时当场切换，有修改时等离开拦截放行后再切
      select(idx);
      clearScrollTarget(scrollTarget.request_id);
    } else if (Date.now() >= scrollTarget.expires_at) {
      // Current segments do not contain this shot (e.g. the event points to another episode); clear it once stale so a later segments change does not trigger it by mistake
      // 当前 segments 不含该分镜（如事件指向其他剧集），过期后清理避免下次 segments 变更误触发
      clearScrollTarget(scrollTarget.request_id);
    }
  }, [scrollTarget, ids, select, clearScrollTarget]);



  const segment = segments[safeIndex];

  return (
    <div
      ref={rootRef}
      className={cn(
        "h-full min-h-0 min-w-0",
        reorderMode
          ? "grid grid-cols-[220px_minmax(0,1fr)] grid-rows-[minmax(0,1fr)]"
          : "flex flex-col",
      )}
    >
      {reorderMode ? (
        <ShotList
          segments={segments}
          selectedIndex={safeIndex}
          onSelect={select}
          contentMode={contentMode}
          projectName={projectName}
          aspectRatio={aspectRatio}
          collapsed={false}
          onToggleCollapse={() => setReorderMode(false)}
          onAppend={handleInsertShot}
          appendDisabled={structurePending || movePending}
          onMove={handleMoveShot}
          moveDisabled={structurePending || movePending}
          footer={<ShotShortcutHint canSave={Boolean(onUpdatePrompt)} />}
        />
      ) : (
        <ShotStrip
          segments={segments}
          selectedIndex={safeIndex}
          onSelect={select}
          contentMode={contentMode}
          projectName={projectName}
          episode={episode}
          aspectRatio={aspectRatio}
          onAppend={handleInsertShot}
          appendDisabled={structurePending || movePending}
          onReorderMode={handleMoveShot ? () => setReorderMode(true) : undefined}
          onMoveStep={handleMoveStep}
          moveDisabled={structurePending || movePending}
          onGenerateStoryboard={onGenerateStoryboard}
          onGenerateVideo={onGenerateVideo}
        />
      )}
      {/* the detail fills what the strip leaves (strip mode) or its grid cell (reorder mode) */}
      <div className={cn("flex min-h-0 min-w-0 flex-col", !reorderMode && "flex-1")}>
      <RetainedEditUnit identity={segmentId ?? "missing"} value={
      segmentId !== undefined && segment ? <ShotDetail
        key={segmentId}
        segment={segment}
        segmentId={segmentId}
        contentMode={contentMode}
        aspectRatio={aspectRatio}
        projectName={projectName}
        episode={episode}
        scriptFile={scriptFile}
        selectedIndex={safeIndex}
        totalCount={segments.length}
        onPrev={selectPrev}
        onNext={selectNext}
        onUpdatePrompt={onUpdatePrompt}
        onMoveShot={handleMoveStep}
        movePending={movePending}
        onInsertShot={handleInsertShot}
        onRemoveShot={handleRemoveShot}
        structurePending={structurePending}
        onGenerateStoryboard={onGenerateStoryboard}
        onGenerateVideo={onGenerateVideo}
        onGenerateNarration={onGenerateNarration}
        onRestoreStoryboard={onRestoreStoryboard}
        onRestoreVideo={onRestoreVideo}
        generatingStoryboard={generatingStoryboard?.(segmentId)}
        generatingVideo={generatingVideo?.(segmentId)}
        generatingNarration={generatingNarration?.(segmentId)}
        durationOptions={durationOptions}
        durationEndpointFixed={durationEndpointFixed}
        lastFrame={lastFrame}
        capabilitiesLoading={capabilitiesLoading}
        durationWarningReason={durationWarningReason}
        editRef={editRef}
        // reorder mode keeps the header's move / insert / previous-next buttons; the strip covers them otherwise
        minimalHeader={!reorderMode}
      /> : null
      } message={t("dashboard:shot_externally_removed")}>
        {(detail) => detail}
      </RetainedEditUnit>
      </div>
    </div>
  );
}
