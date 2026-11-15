import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { Download, Loader2, RotateCcw, Trash2 } from "lucide-react";

import { API } from "@/api";
import { TruncatedText } from "@/components/shared/TruncatedText";
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
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { episodeDisplayName, episodeItemLabel, type EpisodeLedger } from "@/utils/episode-display";
import type { RestoreResult, TrashListing, TrashRecord } from "@/types";
import { errMsg } from "@/utils/async";
import { formatShortDateTime } from "@/utils/date-format";
import { downloadBlob } from "@/utils/download";
import { formatFileSize } from "@/utils/file-size";

type ListingState = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; listing: TrashListing };

const NO_EPISODES: EpisodeLedger = [];

const ASSET_TYPES = ["character", "scene", "prop", "product"] as const;

function isAssetType(value: unknown): value is (typeof ASSET_TYPES)[number] {
  return ASSET_TYPES.includes(value as (typeof ASSET_TYPES)[number]);
}

/**
 * Shots and video units are referred to by the name snapshot taken at deletion: "Episode name · 5", with the episode name from the current ledger.
 * Old records without a snapshot return null, and the caller falls back to showing the number.
 *
 * 分镜与视频单元按删除时的称呼快照指称：「集名 · 5」，集名取当前账本。
 * 没有快照的旧记录返回 null，由调用方退回编号显示。
 */
function itemAtDeletion(t: TFunction<"dashboard">, object: TrashRecord["object"], episodes: EpisodeLedger) {
  if (typeof object.episode !== "number" || typeof object.item_position !== "number") return null;
  return `${episodeDisplayName(episodes, object.episode, t)} · ${object.item_position}`;
}

/** The name is followed by the body excerpt at deletion, which distinguishes several records of the same episode; without an excerpt only the name. / 名称后接删除时的正文摘录，同一集的多条记录凭它区分；没有摘录时只有名称。 */
function withExcerpt(t: TFunction<"dashboard">, name: string, object: TrashRecord["object"]): string {
  const excerpt = object.item_excerpt;
  return typeof excerpt === "string" && excerpt !== "" ? t("trash_object_with_excerpt", { name, excerpt }) : name;
}

/** Object name: shown by object type; objects involving only files use the deleted file's path. / 对象名：按对象类型显示，只涉及文件的对象取被删文件的路径。 */
function objectTitle(
  t: TFunction<"dashboard">,
  object: TrashRecord["object"],
  episodes: EpisodeLedger,
): string | undefined {
  if (object.kind === "edit_timeline" && typeof object.name === "string") {
    return t("trash_object_edit_timeline", { name: object.name, episode: object.episode });
  }
  if (object.kind === "episode") {
    return typeof object.title === "string" && object.title !== ""
      ? t("trash_object_episode", { name: object.title })
      : t("trash_object_episode_untitled", { episode: object.id });
  }
  if (object.kind === "grid") return t("trash_object_grid", { episode: object.episode });
  if (object.kind === "end_frame") {
    const shot = itemAtDeletion(t, object, episodes);
    return shot === null
      ? t("trash_object_end_frame", { shot: object.shot_id })
      : withExcerpt(t, t("trash_object_end_frame", { shot }), object);
  }
  if (object.kind === "reference_audio") return t("trash_object_reference_audio", { name: object.character });
  if (object.kind === "asset" && isAssetType(object.asset_type) && typeof object.name === "string") {
    return t(`trash_object_asset.${object.asset_type}`, { name: object.name });
  }
  if (object.kind === "asset_derivative" && typeof object.name === "string") {
    return t("trash_object_asset_derivative", { name: object.name, owner: object.owner });
  }
  if (object.kind === "source_file" && typeof object.path === "string") {
    return t("trash_object_source_file", { name: object.path.split("/").pop() });
  }
  if (object.kind === "planning_draft") return t("trash_object_planning_draft", { episode: object.episode });
  if (object.kind === "script_entry") {
    const unit = object.item_kind === "video_units";
    const item = itemAtDeletion(t, object, episodes);
    if (item === null) {
      return t(unit ? "trash_object_script_unit" : "trash_object_script_entry", { id: object.id, episode: object.episode });
    }
    return withExcerpt(t, t(unit ? "trash_object_script_unit_at" : "trash_object_script_entry_at", { item }), object);
  }
  return object.path;
}

function recordTitle(t: TFunction<"dashboard">, record: TrashRecord, episodes: EpisodeLedger): string {
  return objectTitle(t, record.object, episodes) ?? record.files[0] ?? record.id;
}

/** Restore result notice: on degradation explain what was recovered and what is download-only; give the new name when the object was renamed and the real location when a file moved. / 恢复结果的提示：降级时说明找回了什么、哪些只能下载；对象换了名时给出新名，文件换了位置时给出真实落点。 */
function restoredMessage(t: TFunction<"dashboard">, record: TrashRecord, result: RestoreResult): string {
  const { currentScripts, currentProjectData } = useProjectsStore.getState();
  const episodes = currentProjectData?.episodes ?? [];
  const name = recordTitle(t, record, episodes);
  if (result.degraded) {
    return result.download_only.length > 0
      ? t("trash_restored_degraded", { name, count: result.restored.length })
      : t("trash_restored_degraded_files", { name, count: result.restored.length });
  }
  if (record.object.kind === "episode") return t("trash_restored_episode", { name });
  if (record.object.kind === "script_entry") {
    const label = episodeItemLabel(String(result.object.id), currentScripts, episodes, t);
    return t("trash_restored_object", { name: label });
  }
  // Named objects (edit timelines) explain renaming by name; others explain by file location. / 带名字的对象（剪辑时间线）按名字说明是否改名；其余按文件落点说明。
  if (record.has_snapshot && typeof record.object.name === "string") {
    const landed = result.object.name;
    return typeof landed === "string" && landed !== record.object.name
      ? t("trash_restored_object_renamed", { name, landed })
      : t("trash_restored_object", { name });
  }
  if (result.restored.some((item) => item.as_history)) return t("trash_restored_as_history", { name });
  const renamed = result.restored.find((item) => item.path !== item.original);
  return renamed ? t("trash_restored_renamed", { name, path: renamed.path }) : t("trash_restored", { name });
}

/** Result whose target was deleted when generation finished: nobody deleted it, so its origin is explained separately. / 生成完成时目标已删除的结果：不是谁删的，单独说明来历。 */
function recordMetaKey(record: TrashRecord): string {
  return record.object.note === "target_deleted_during_generation"
    ? "trash_record_meta_target_deleted"
    : `trash_record_meta_${record.origin}`;
}

interface PurgeRequest {
  /** The record being permanently deleted; empty when emptying the trash, which conflicts with all records being restored. / 彻底删除的那条记录；清空时为空，与所有恢复中的记录冲突。 */
  recordId?: string;
  title: string;
  description: string;
  confirmLabel: string;
  run: () => Promise<unknown>;
}

/**
 * "Trash" section of the files view: lists deletion records, which can be restored, permanently deleted or emptied.
 * Restore can be deleted again, so it runs directly; permanent delete and empty are irreversible and are confirmed first with AlertDialog.
 *
 * 文件视图的「回收站」分区：列出删除记录，可以恢复、彻底删除或清空。
 * 恢复可以再删回去，直接执行；彻底删除与清空不可逆，先用 AlertDialog 确认。
 */
export function TrashSection({ projectName, refreshKey }: { projectName: string; refreshKey: number }) {
  const { t } = useTranslation("dashboard");
  const trashRevision = useAppStore((s) => s.trashRevision);
  const episodes = useProjectsStore((s) => s.currentProjectData?.episodes) ?? NO_EPISODES;
  const [state, setState] = useState<ListingState>({ status: "loading" });
  // Several records can be restored at once: each record holds its own lock and releases only itself when done.
  // 可以同时恢复多条记录：每条记录各自占用，完成时只释放自己。
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(new Set());
  const [purge, setPurge] = useState<PurgeRequest | null>(null);

  // Auto refetch and "Retry" share one controller: each load cancels the previous one, so old responses do not overwrite the new list.
  // 自动重拉与「重试」共用一个控制器：每次加载都取消上一次，旧响应不会覆盖新列表。
  const controllerRef = useRef<AbortController | null>(null);
  const load = useCallback(() => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const { signal } = controller;
    API.getTrash(projectName, { signal })
      .then((listing) => {
        if (!signal.aborted) setState({ status: "ready", listing });
      })
      .catch((error: unknown) => {
        if (!signal.aborted) setState({ status: "error", message: errMsg(error) });
      });
  }, [projectName]);

  useEffect(() => {
    load();
    const controllers = controllerRef;
    return () => controllers.current?.abort();
  }, [load, trashRevision, refreshKey]);

  const afterChange = () => useAppStore.getState().invalidateTrash();

  /**
   * Records the server confirmed removed are taken off the list immediately without waiting for a refetch: otherwise restored or deleted records would linger, released, and could be acted on again.
   * An in-flight list request may predate this change, so cancel it first; the following refetch defers to the server.
   *
   * 服务端已确认移除的记录立即从列表里拿掉，不等重拉：否则已恢复或已删除的记录还留在列表里、又解除了占用，可以被再操作一次。
   * 在途的列表请求可能早于这次变更，先取消；随后的重拉以服务端为准。
   */
  const dropRecords = (keep: (record: TrashRecord) => boolean) => {
    controllerRef.current?.abort();
    setState((current) => {
      if (current.status !== "ready") return current;
      const records = current.listing.records.filter(keep);
      const totalSize = records.reduce((total, record) => total + record.size, 0);
      return { status: "ready", listing: { ...current.listing, records, total_size: totalSize } };
    });
  };

  const restore = async (record: TrashRecord) => {
    setBusyIds((current) => new Set(current).add(record.id));
    try {
      const result = await API.restoreTrashRecord(projectName, record.id);
      // When degradation leaves download-only structure files the record remains; the following refetch turns it into the download-only form
      // 降级后留下只能下载的结构文件时记录还在，由随后的重拉换成只能下载的样子
      if (result.download_only.length === 0) dropRecords((item) => item.id !== record.id);
      if (record.object.kind === "script_entry" && !result.degraded) {
        if (await refreshAfterWrite(projectName, t) !== "success") return;
      }
      useAppStore.getState().pushToast(restoredMessage(t, record, result), result.degraded ? "warning" : "success");
    } catch (error) {
      useAppStore.getState().pushToast(t("trash_action_failed", { message: errMsg(error) }), "error");
    } finally {
      setBusyIds((current) => {
        const next = new Set(current);
        next.delete(record.id);
        return next;
      });
      afterChange();
    }
  };

  if (state.status === "loading") {
    return <p className="px-6 py-4 text-sm text-muted-foreground">{t("file_view_loading")}</p>;
  }
  if (state.status === "error") {
    return (
      <div className="flex flex-col items-start gap-2 px-6 py-4 text-sm text-destructive">
        <p className="wrap-break-word">{t("trash_load_failed", { message: state.message })}</p>
        <Button variant="outline" size="sm" onClick={() => load()}>
          {t("file_view_retry")}
        </Button>
      </div>
    );
  }

  const { records, total_size: totalSize, retention_days: retentionDays } = state.listing;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* The empty button aligns left: on narrow screens the Agent panel floats over the right of the canvas and would cover right-aligned controls / 清空按钮靠左：窄屏下 Agent 面板浮在画布右侧，靠右的控件会被盖住 */}
      <div className="flex shrink-0 flex-col items-start gap-2 border-b border-border px-6 py-3">
        <p className="min-w-0 text-sm text-subtle-foreground">
          {t("trash_summary", { total: records.length, size: formatFileSize(totalSize), days: retentionDays })}
        </p>
        <Button
          variant="destructive"
          size="sm"
          disabled={records.length === 0 || busyIds.size > 0}
          onClick={() =>
            setPurge({
              title: t("trash_empty_title"),
              description: t("trash_empty_description", { total: records.length }),
              confirmLabel: t("trash_empty"),
              run: () => API.emptyTrash(projectName),
            })
          }
        >
          <Trash2 data-icon="inline-start" aria-hidden />
          {t("trash_empty")}
        </Button>
      </div>
      {records.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <Trash2 aria-hidden />
              </EmptyMedia>
              <EmptyTitle>{t("trash_empty_state_title")}</EmptyTitle>
              <EmptyDescription>{t("trash_empty_state_description", { days: retentionDays })}</EmptyDescription>
            </EmptyHeader>
          </Empty>
        </div>
      ) : (
        <ul className="relative flex min-h-0 flex-1 flex-col overflow-y-auto px-4 py-2 [scrollbar-gutter:stable]">
          {records.map((record) => {
            const busy = busyIds.has(record.id);
            return (
              <li key={record.id} className="flex items-center gap-3 rounded-md px-2 py-2 hover:bg-muted/40">
                <div className="flex min-w-0 flex-1 flex-col">
                  {itemAtDeletion(t, record.object, episodes) === null ? (
                    <TruncatedText text={recordTitle(t, record, episodes)} className="text-sm" />
                  ) : (
                    <span className="wrap-break-word text-sm">{recordTitle(t, record, episodes)}</span>
                  )}
                  <span className="text-xs text-muted-foreground">
                    {t(recordMetaKey(record), {
                      time: formatShortDateTime(record.deleted_at) ?? record.deleted_at,
                    })}
                    {" · "}
                    <span className="num">{formatFileSize(record.size)}</span>
                  </span>
                  {record.download_only && (
                    <DownloadOnlyFiles projectName={projectName} record={record} />
                  )}
                </div>
                {!record.download_only && (
                  <Button variant="outline" size="sm" disabled={busy} onClick={() => void restore(record)}>
                    {busy ? (
                      <Loader2 data-icon="inline-start" aria-hidden className="animate-spin" />
                    ) : (
                      <RotateCcw data-icon="inline-start" aria-hidden />
                    )}
                    {t("trash_restore")}
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    setPurge({
                      recordId: record.id,
                      title: t("trash_purge_title"),
                      description: t("trash_purge_description", { name: recordTitle(t, record, episodes) }),
                      confirmLabel: t("trash_purge"),
                      run: () => API.purgeTrashRecord(projectName, record.id),
                    })
                  }
                >
                  {t("trash_purge")}
                </Button>
              </li>
            );
          })}
        </ul>
      )}
      <PurgeConfirmDialog
        request={purge}
        onSucceeded={(request) => dropRecords(request.recordId ? (item) => item.id !== request.recordId : () => false)}
        // Restores started after opening also block confirmation: check the current locks again on submit / 打开后才开始的恢复同样挡住确认：提交时再按当前占用判一次
        blocked={purge !== null && (purge.recordId ? busyIds.has(purge.recordId) : busyIds.size > 0)}
        onClose={() => setPurge(null)}
        onDone={afterChange}
      />
    </div>
  );
}

/** Structure files left after a degraded restore: explain why they are download-only and offer each for download. / 降级恢复后留下的结构文件：说明为什么只能下载，逐个提供下载。 */
function DownloadOnlyFiles({ projectName, record }: { projectName: string; record: TrashRecord }) {
  const { t } = useTranslation("dashboard");
  const [downloading, setDownloading] = useState<string | null>(null);

  const download = async (path: string) => {
    const name = path.split("/").pop() ?? path;
    setDownloading(path);
    try {
      downloadBlob(await API.downloadTrashRecordFile(projectName, record.id, path), name);
    } catch (error) {
      useAppStore.getState().pushToast(t("file_view_download_failed", { name, message: errMsg(error) }), "error");
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="mt-1 flex min-w-0 flex-col items-start gap-1">
      <p className="text-xs text-warn">{t("trash_download_only_hint")}</p>
      <ul className="flex min-w-0 max-w-full flex-col items-start">
        {record.files.map((path) => (
          <li key={path} className="max-w-full">
            <Button
              variant="link"
              size="xs"
              className="max-w-full"
              disabled={downloading !== null}
              aria-label={t("trash_download_file", { path })}
              onClick={() => void download(path)}
            >
              {downloading === path ? (
                <Loader2 data-icon="inline-start" aria-hidden className="animate-spin" />
              ) : (
                <Download data-icon="inline-start" aria-hidden />
              )}
              <TruncatedText text={path} focusable={false} className="font-mono text-xs" />
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Confirmation for permanent delete and empty: irreversible. Closed when `request` is null. / 彻底删除与清空的确认：不可恢复。`request` 为 null 时关闭。 */
function PurgeConfirmDialog({
  request,
  blocked,
  onSucceeded,
  onClose,
  onDone,
}: {
  request: PurgeRequest | null;
  blocked: boolean;
  onSucceeded: (request: PurgeRequest) => void;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation("dashboard");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Keep the previous content during the close animation; clear the previous error when the request changes / 关闭动画期间沿用上一次的内容；换了请求时清掉上一次的错误
  const [shown, setShown] = useState(request);
  if (request !== null && request !== shown) {
    setShown(request);
    setError(null);
  }

  const confirm = async () => {
    if (!shown || running || blocked) return;
    setRunning(true);
    setError(null);
    try {
      await shown.run();
      onSucceeded(shown);
      onDone();
      onClose();
    } catch (err) {
      setError(t("trash_action_failed", { message: errMsg(err) }));
    } finally {
      setRunning(false);
    }
  };

  return (
    <AlertDialog
      open={request !== null}
      onOpenChange={(next) => {
        // Ignore Esc while submitting so the dialog does not disappear while the request is in flight / 提交中不响应 Esc，避免请求还在途时对话框先消失
        if (!next && !running) onClose();
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{shown?.title}</AlertDialogTitle>
        </AlertDialogHeader>
        <AlertDialogBody>
          <div className="flex flex-col gap-3">
            <AlertDialogDescription>{shown?.description}</AlertDialogDescription>
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
          </div>
        </AlertDialogBody>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={running}>{t("common:cancel")}</AlertDialogCancel>
          <AlertDialogAction variant="destructive" disabled={running || blocked} onClick={() => void confirm()}>
            {running && <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />}
            {shown?.confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
