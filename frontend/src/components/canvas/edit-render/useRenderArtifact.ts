import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "@/api";
import { submitRender } from "@/actions/render";
import { errMsg } from "@/utils/async";
import { isTerminalStatus } from "@/types";
import type { FinalCutStatus, JianyingDraftStatus, RenderKind, RenderOptions, SubtitleMode, TaskItem } from "@/types";
import type { TimelineNarration } from "@/types/edit-timeline";

/** Polling interval for render tasks: progress is fetched by task_id while the dialog is open, more timely than the task list's fallback polling. / 出片任务的轮询间隔：对话框打开期间按 task_id 拉取进度，比任务列表的兜底轮询更及时。 */
export const RENDER_TASK_POLL_MS = 2000;

const RENDER_TASK_TYPE: Record<RenderKind, string> = {
  final_cut: "render_final_cut",
  jianying_draft: "render_jianying_draft",
};

export type RenderArtifactView = FinalCutStatus | JianyingDraftStatus;

export interface RenderArtifactState {
  /** Artifact state of the current deliverable; null before the first load completes. / 当前交付物的产物现状；首次加载完成前为 null。 */
  artifact: RenderArtifactView | null;
  loading: boolean;
  /** Reason reading the state failed. / 读取现状失败的原因。 */
  loadError: string | null;
  /** The task submitted this time; null when not submitted, kept after the terminal state to show the result (the failure reason is on the task). / 本次提交的任务；未提交时为 null，终态后保留以展示结果（失败原因在任务上）。 */
  task: TaskItem | null;
  /** true from submit until the task's terminal state. / 从提交到任务终态都为 true。 */
  submitting: boolean;
  /** true while the submit request is in flight (no task_id yet); picking up an existing task skips this phase. / 提交请求在途（还没拿到 task_id）时为 true；接回已有任务时不经过这一段。 */
  requesting: boolean;
  /** Reason the submit request itself failed (e.g. the pre-enqueue check did not pass). / 提交请求本身失败的原因（如入队前检查不通过）。 */
  submitError: string | null;
  /** Submit one render task and start polling. / 提交一次出片任务并开始轮询。 */
  submit: () => Promise<void>;
}

function fetchArtifact(
  projectName: string,
  timelineId: string,
  kind: RenderKind,
  options: RenderOptions,
  signal: AbortSignal,
): Promise<RenderArtifactView> {
  return kind === "final_cut"
    ? API.getFinalCutStatus(projectName, timelineId, { ...options, signal })
    : API.getJianyingDraftStatus(projectName, timelineId, { narration: options.narration, signal });
}

/** Find an in-flight render task for the same edit timeline, deliverable and version as the artifact; a failed lookup is treated as none. / 查找与产物同一剪辑时间线、同一交付物与版本的在途出片任务；查询失败时按没有处理。 */
async function findActiveRenderTask(
  projectName: string,
  kind: RenderKind,
  artifact: RenderArtifactView,
): Promise<TaskItem | null> {
  try {
    const pages = await Promise.all(
      (["running", "queued"] as const).map((status) =>
        API.listTasks({ projectName, taskType: RENDER_TASK_TYPE[kind], status }),
      ),
    );
    return (
      pages
        .flatMap((page) => page.items)
        .find(
          (task) =>
            task.payload.timeline_id === artifact.timeline_id &&
            task.payload.narration === artifact.narration &&
            (!("subtitles" in artifact) || task.payload.subtitles === artifact.subtitles),
        ) ?? null
    );
  } catch {
    return null;
  }
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(signal.reason instanceof Error ? signal.reason : new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

/**
 * Render state of one deliverable and version of an edit timeline: artifact state, submit and task progress.
 * The version is determined by the narration version and whether subtitles are burned in; JianYing drafts ignore ``subtitles``.
 *
 * The cancellation scope is "project x edit timeline x deliverable x version": any change or unmount invalidates in-flight reads and polling.
 * Callers mount the consuming component keyed by these, so switching remounts it as a whole and local state does not carry over across versions.
 * After reading the artifact state, if the same artifact already has an in-flight task (e.g. submitted before the dialog closed, or by the Agent), keep tracking it.
 * After the task succeeds, fetch the artifact state once more; on failure show the task's failure reason.
 *
 * 一个剪辑时间线某种交付物、某个版本的出片状态：产物现状、提交与任务进度。
 * 版本由旁白版本与是否烧入字幕决定；剪映草稿不看 ``subtitles``。
 *
 * 取消域是「项目 × 剪辑时间线 × 交付物 × 版本」：任一变化或卸载时作废在途的读取与轮询。
 * 调用方以这些为 key 挂载使用方组件，切换时整体重新挂载，本地状态不跨版本残留。
 * 读到产物现状后，若同一产物已有在途任务（如对话框关闭前提交的，或由 Agent 提交的），接着跟踪它。
 * 任务成功后补拉一次产物现状，失败时展示任务的失败原因。
 */
export function useRenderArtifact(
  projectName: string,
  timelineId: string,
  kind: RenderKind,
  narration: TimelineNarration,
  subtitles: SubtitleMode,
): RenderArtifactState {
  const [artifact, setArtifact] = useState<RenderArtifactView | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [task, setTask] = useState<TaskItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const scopeRef = useRef<AbortController | null>(null);

  const load = useCallback(
    async (signal: AbortSignal): Promise<RenderArtifactView | null> => {
      setLoading(true);
      setLoadError(null);
      try {
        const next = await fetchArtifact(projectName, timelineId, kind, { narration, subtitles }, signal);
        if (signal.aborted) return null;
        setArtifact(next);
        return next;
      } catch (err) {
        if (signal.aborted) return null;
        setLoadError(errMsg(err));
        return null;
      } finally {
        // react-doctor-disable-next-line react-doctor/no-loading-flag-reset-outside-finally -- already reset in finally; when aborted, the new load round that takes over is responsible for resetting / 已在 finally 中复位；被 abort 时由接管的新一轮加载负责复位
        if (!signal.aborted) setLoading(false);
      }
    },
    [projectName, timelineId, kind, narration, subtitles],
  );

  const poll = useCallback(
    async (taskId: string, signal: AbortSignal) => {
      for (;;) {
        const current = await API.getTask(taskId);
        if (signal.aborted) return;
        setTask(current);
        if (isTerminalStatus(current.status)) {
          if (current.status === "succeeded") await load(signal);
          return;
        }
        await sleep(RENDER_TASK_POLL_MS, signal);
      }
    },
    [load],
  );

  /** submitting holds from getting the task_id until the task's terminal state; failures getting the task_id or polling are recorded as submit failures. / 从取得 task_id 到任务终态都处于 submitting；取 task_id 或轮询失败时记为提交失败。 */
  const follow = useCallback(
    async (start: () => Promise<string>, signal: AbortSignal) => {
      setSubmitting(true);
      setSubmitError(null);
      setTask(null);
      try {
        const taskId = await start();
        if (signal.aborted) return;
        await poll(taskId, signal);
      } catch (err) {
        if (signal.aborted) return;
        setSubmitError(errMsg(err));
      } finally {
        // react-doctor-disable-next-line react-doctor/no-loading-flag-reset-outside-finally -- already reset in finally; when aborted, the cancellation scope is invalidated and the takeover is responsible for resetting / 已在 finally 中复位；被 abort 时取消域已作废，由接管方负责复位
        if (!signal.aborted) setSubmitting(false);
      }
    },
    [poll],
  );

  useEffect(() => {
    const controller = new AbortController();
    scopeRef.current = controller;
    void (async () => {
      const loaded = await load(controller.signal);
      if (loaded === null || controller.signal.aborted) return;
      const active = await findActiveRenderTask(projectName, kind, loaded);
      if (active === null || controller.signal.aborted) return;
      await follow(() => Promise.resolve(active.task_id), controller.signal);
    })();
    return () => controller.abort();
  }, [load, follow, projectName, kind]);

  const submit = useCallback(async () => {
    const signal = scopeRef.current?.signal;
    if (!signal || signal.aborted) return;
    await follow(async () => {
      setRequesting(true);
      try {
        return (await submitRender(projectName, timelineId, kind, { narration, subtitles })).task_id;
      } finally {
        if (!signal.aborted) setRequesting(false);
      }
    }, signal);
  }, [projectName, timelineId, kind, narration, subtitles, follow]);

  return { artifact, loading, loadError, task, submitting, requesting, submitError, submit };
}
