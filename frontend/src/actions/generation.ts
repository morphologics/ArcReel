/**
 * Enqueue action layer: the single entry point for all generation-type enqueue operations.
 *
 * Each action internally encapsulates three things:
 * 1. Set an optimistic occupancy mark in tasks-store **before the request is sent**; roll it back if the request fails, and fulfill it
 *    with the task_id returned by the backend on success (see {@link submit}). Occupancy therefore holds from the moment of the click,
 *    and callers need no "request in flight" flag of their own to cover the network round-trip window; batch entry points whose target set
 *    is decided by the server cannot know in advance which resources they hit, so they mark item by item from the mapping after the response (see {@link markResourcesByTaskId});
 * 2. Call the corresponding API enqueue endpoint;
 * 3. Show a toast: when the backend returns deduped=true (a task for the same resource is already in progress and this call created none),
 *    show a unified info toast; otherwise keep each operation's own success text.
 *
 * Failures are always thrown upward; the caller decides the error message. Return values are normalized to EnqueueResult,
 * hiding the shape differences of task_id / task_ids across endpoints.
 *
 * Components must not bypass this layer to call enqueue API methods directly (enforced by ESLint no-restricted-syntax).
 *
 * 入队动作层：所有生成类入队操作的唯一入口。
 *
 * 每个动作内部固定封装三件事：
 * 1. 在**请求发出前**于 tasks-store 打乐观占用标记，请求失败即回滚，成功则用后端
 *    返回的 task_id 兑现（见 {@link submit}）——占用态因此从点击那一刻起就成立，
 *    调用方无须自备「请求在途」标记来覆盖网络往返窗口；目标集合由服务端决定的批量
 *    入口无法预先知道命中哪些资源，改在响应后按映射逐项补打（见 {@link markResourcesByTaskId}）；
 * 2. 调用对应 API 入队端点；
 * 3. 弹提示：后端 deduped=true（同资源任务已在处理中，该调用未新建）时统一
 *    弹 info 提示，否则沿用各操作原有的成功文案。
 *
 * 失败一律向上抛，由调用方决定错误提示。返回值统一归一化为 EnqueueResult，
 * 屏蔽各端点 task_id / task_ids 的形状差异。
 *
 * 组件禁止绕过本层直调入队类 API 方法（ESLint no-restricted-syntax 强制）。
 */
import { API, derivativeResourceId } from "@/api";
import i18n from "@/i18n";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import type {
  AssetSheetBatchScope,
  AssetSheetBatchSubmitted,
  AuthorPromptsRequest,
  DraftDocType,
  PlanningGap,
  PlanScriptRequest,
  GenerateAdScriptRequest,
  ReferenceBatchAdmission,
  ReferenceBatchGenerateRequest,
  ReferenceGenerationRequestOptions,
  StoryboardBatchKind,
  StoryboardBatchSubmitted,
} from "@/types";
import {
  useTasksStore,
  type ImageEditResourceKind,
  type OptimisticHandle,
  type ResourceKind,
} from "@/stores/tasks-store";
import { isTerminalStatus } from "@/types/task";
import { episodeItemLabel } from "@/utils/episode-display";

/** Toasts are not limited to the episode page: entries are referred to as "title · 5". / toast 不局限在集页面：条目按「标题 · 5」指称。 */
function itemLabel(itemId: string): string {
  const { currentProjectData, currentScripts } = useProjectsStore.getState();
  return episodeItemLabel(itemId, currentScripts, currentProjectData?.episodes ?? [], i18n.t);
}

export interface EnqueueResult {
  taskIds: string[];
  deduped: boolean;
}

/**
 * When deduped, show the unified info toast; otherwise show the operation's own success text
 * (successText null means the operation is silent on success anyway; stay silent).
 *
 * deduped 时弹统一 info 提示；否则弹该操作自己的成功文案
 * （successText 为 null 表示该操作成功时本就静默，维持静默）。
 */
function notifyEnqueued(
  deduped: boolean,
  successText: string | null,
  successTone: "success" | "info" = "success",
): void {
  const { pushToast } = useAppStore.getState();
  if (deduped) {
    pushToast(i18n.t("dashboard:enqueue_deduped_toast"), "info");
  } else if (successText !== null) {
    pushToast(successText, successTone);
  }
}

/** Shorthand for a resource-level optimistic mark (call before sending the request). / 资源粒度乐观标记的简写（请求发出前调用）。 */
function markResource(
  projectName: string,
  resourceKind: ResourceKind,
  resourceId: string,
  pendingTaskType: string,
): OptimisticHandle {
  return useTasksStore
    .getState()
    .beginOptimisticActive(projectName, resourceKind, resourceId, pendingTaskType);
}

/** Shorthand for a scriptFile-level optimistic mark (call before sending the request). / scriptFile 粒度乐观标记的简写（请求发出前调用）。 */
function markScriptFile(projectName: string, taskType: string, scriptFile: string): OptimisticHandle {
  return useTasksStore.getState().beginOptimisticActiveForScriptFile(projectName, taskType, scriptFile);
}

/**
 * Common skeleton for enqueue requests: the marks are already set in `marks`; this only sends the request and fulfills by result:
 * on failure roll back all and rethrow as-is; on success fulfill with the task_ids extracted by `taskIdsOf` (empty means roll back,
 * since without a backend task row the mark would never see a real row).
 *
 * Response parsing (`taskIdsOf`) and fulfillment share the try: in-flight marks are not cleared by any polling write-back, so
 * every exception path before fulfillment must roll back, or the mark lingers until the page is refreshed.
 *
 * 入队请求的统一骨架：标记已在 `marks` 里打好，此处只负责发请求并按结果兑现——
 * 失败全部回滚后原样抛出，成功用 `taskIdsOf` 取出的 task_id 兑现（为空即回滚，
 * 后端没建任务行时标记永远等不到真实行）。
 *
 * 响应解析（`taskIdsOf`）与兑现同在 try 内：在途标记不被任何轮询写回清除，因此
 * 兑现前的任何异常路径都必须回滚，否则标记会残留到页面刷新为止。
 */
async function submit<T>(
  marks: readonly OptimisticHandle[],
  request: () => Promise<T>,
  taskIdsOf: (res: T, markIndex: number) => string[],
): Promise<T> {
  try {
    const res = await request();
    // Each mark takes its own task row: a mark yields only once all of its task_ids are persisted; sending the whole batch list to every mark
    // would make each resource wait until the whole batch appears, and the task list snapshot keeps only the latest rows, so early rows might never appear again.
    // 逐标记取自己的任务行：标记要等它的 task_id 全部落库才让位，把整批清单发给每个标记
    // 会让每个资源都等到全批出现，而任务列表快照只保留最新若干行，早的行可能再不出现。
    marks.forEach((m, index) => m.settle(taskIdsOf(res, index)));
    return res;
  } catch (e) {
    for (const m of marks) m.rollback();
    throw e;
  }
}

function oneTaskId(res: { task_id: string }): string[] {
  return [res.task_id];
}

function manyTaskIds(res: { task_ids: string[] }): string[] {
  return res.task_ids;
}

/**
 * Set resource-level marks item by item from the backend's "resource id -> task_id" mapping.
 *
 * Used for batch entry points whose target set is decided by the server: before the request the caller does not know which resources will be hit, so marks
 * are set after the response and immediately claim their own task row; each item waits only for its own task to persist, and items not in the mapping
 * (skipped by the server or not enqueued successfully) are never marked.
 *
 * 按后端返回的「资源 id → task_id」映射逐项补打资源粒度标记。
 *
 * 用于目标集合由服务端决定的批量入口：请求发出前调用方不知道会命中哪些资源，标记
 * 因此在响应后补打并立即认领自己那条任务行——每项只等自己的任务落库，未进映射的项
 * （服务端跳过或未入队成功）一律不打标。
 */
function markResourcesByTaskId(
  projectName: string,
  resourceKind: ResourceKind,
  pendingTaskType: string,
  taskIdsByResource: Record<string, string>,
): void {
  for (const [resourceId, taskId] of Object.entries(taskIdsByResource)) {
    markResource(projectName, resourceKind, resourceId, pendingTaskType).settle([taskId]);
  }
}

export async function enqueueStoryboard(
  projectName: string,
  segmentId: string,
  prompt: string | Record<string, unknown>,
  scriptFile: string,
  imageModel?: string,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "storyboard", segmentId, "storyboard")],
    // the model only when one was chosen ("Generate with …"): otherwise the request is unchanged
    () =>
      imageModel
        ? API.generateStoryboard(projectName, segmentId, prompt, scriptFile, imageModel)
        : API.generateStoryboard(projectName, segmentId, prompt, scriptFile),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:storyboard_task_submitted_toast", { id: itemLabel(segmentId) }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueVideo(
  projectName: string,
  segmentId: string,
  prompt: string | Record<string, unknown>,
  scriptFile: string,
  durationSeconds?: number,
  videoModel?: string,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "video", segmentId, "video")],
    () =>
      videoModel
        ? API.generateVideo(projectName, segmentId, prompt, scriptFile, durationSeconds, videoModel)
        : API.generateVideo(projectName, segmentId, prompt, scriptFile, durationSeconds),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:video_task_submitted_toast", { id: itemLabel(segmentId) }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueNarration(
  projectName: string,
  segmentId: string,
  scriptFile: string,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "tts", segmentId, "tts")],
    () => API.generateNarrationAudio(projectName, segmentId, scriptFile),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:narration_task_submitted_toast", { id: itemLabel(segmentId) }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueEpisodeNarration(
  projectName: string,
  scriptFile: string,
): Promise<EnqueueResult> {
  const res = await API.generateEpisodeNarrationAudio(projectName, scriptFile);
  // Which segments get enqueued is filtered by the server (missing narration and has source text); they cannot be marked before the request, so mark each from the response mapping.
  // 入队哪几段由服务端筛选（缺旁白且有原文），请求发出前无从打标，故按响应映射逐段补打。
  markResourcesByTaskId(projectName, "tts", "tts", res.task_ids_by_segment);
  notifyEnqueued(
    res.deduped,
    res.task_ids.length > 0
      ? i18n.t("dashboard:narration_batch_submitted_toast", { count: res.task_ids.length })
      : i18n.t("dashboard:narration_batch_none_missing_toast"),
  );
  return { taskIds: res.task_ids, deduped: res.deduped };
}

export async function enqueueCharacter(
  projectName: string,
  name: string,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "character", name, "character")],
    () => API.generateCharacter(projectName, name),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:character_task_submitted_toast", { name }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

/**
 * Submit one character derivative sheet generation. The derivative occupancy slot is its own cell keyed by the `base/derivative` composite id, so it and the base look's
 * generation tasks do not mask each other; the instructions are recomputed by the backend from the current registration, so no prompt is passed here.
 *
 * 提交一次角色衍生资产图生成。衍生的占用槽按 `本体/衍生` 复合 id 独立成一格，与本体的
 * 生成任务互不遮蔽；指令由后端按当次登记重算，此处不传 prompt。
 */
export async function enqueueCharacterDerivative(
  projectName: string,
  characterName: string,
  derivativeName: string,
  imageModel?: string,
): Promise<EnqueueResult> {
  const resourceId = derivativeResourceId(characterName, derivativeName);
  const res = await submit(
    [markResource(projectName, "character_derivative", resourceId, "character_derivative")],
    () =>
      imageModel
        ? API.generateCharacterDerivative(projectName, characterName, derivativeName, imageModel)
        : API.generateCharacterDerivative(projectName, characterName, derivativeName),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, res.message);
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueCharacterVoiceSample(
  projectName: string,
  name: string,
  text: string,
  voice: string,
): Promise<EnqueueResult> {
  // The voice preview sample shares the same resource slot (kind "character") with this character's sheet generation/editing:
  // mutual exclusion is acceptable; a preview sample should only be started when the character card's other generation tasks are idle anyway.
  // 音色试听样本与该角色的资产图生成/编辑共用同一资源槽（kind "character"）：
  // 二者互斥可接受——试听样本本就该在角色卡其它生成任务空闲时才发起。
  const res = await submit(
    [markResource(projectName, "character", name, "voice_sample")],
    () => API.generateCharacterVoiceSample(projectName, name, text, voice),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:voice_sample_task_submitted_toast", { name }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

/** An asset's sheet with a model chosen for this request only ("Generate with …"). */
export async function enqueueAssetSheetWith(
  projectName: string,
  assetType: "character" | "scene" | "prop" | "product",
  name: string,
  imageModel: string,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, assetType, name, assetType)],
    () => API.generateAssetSheetWith(projectName, assetType, name, imageModel),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t(`dashboard:${assetType}_task_submitted_toast`, { name }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueScene(
  projectName: string,
  name: string,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "scene", name, "scene")],
    () => API.generateProjectScene(projectName, name),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:scene_task_submitted_toast", { name }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueProp(
  projectName: string,
  name: string,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "prop", name, "prop")],
    () => API.generateProjectProp(projectName, name),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:prop_task_submitted_toast", { name }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueProduct(
  projectName: string,
  name: string,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "product", name, "product")],
    () => API.generateProjectProduct(projectName, name),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:product_task_submitted_toast", { name }));
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueImageEdit(
  projectName: string,
  params: {
    resourceType: ImageEditResourceKind;
    resourceId: string;
    instruction: string;
    scriptFile?: string | null;
  },
): Promise<EnqueueResult> {
  // image_edit shares the same resource slot as generation tasks: kind is slotted by the edited resource's type,
  // and pendingTaskType is fixed to image_edit, consistent with taskResourceKind's normalization.
  // image_edit 与生成任务共享同一资源槽位：kind 按被编辑资源类型归槽，
  // pendingTaskType 固定 image_edit，与 taskResourceKind 的归一化保持一致。
  const res = await submit(
    [markResource(projectName, params.resourceType, params.resourceId, "image_edit")],
    () => API.editImage(projectName, params),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, res.message);
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueGrid(
  projectName: string,
  episode: number,
  scriptFile: string,
  sceneIds?: string[],
): Promise<EnqueueResult> {
  // task_ids may be an empty array (e.g. in generate-missing mode every group's storyboard images are ready, or all grid composites are waiting for grid split): then the backend creates no task row,
  // and settle([]) rolls the mark back, leaving no residue that would never see a real row.
  // task_ids 可能为空数组（如缺失即生成时各组分镜图都已就绪、或联合图都在等切分落格）：此时后端不产生任何任务行，
  // settle([]) 会把标记回滚掉，不留下永远等不到真实行的残留。
  const res = await submit(
    [markScriptFile(projectName, "grid", scriptFile)],
    () => API.generateGrid(projectName, episode, scriptFile, sceneIds),
    manyTaskIds,
  );
  // grid_id is generated on the server and does not exist before the request, so resource marks are set per grid from the response mapping after the response;
  // the round-trip window is covered by the scriptFile-level mark above.
  // grid_id 由服务端现场生成，请求发出前不存在，故按响应映射在响应后逐格补打资源标记；
  // 往返窗口由上面那个 scriptFile 粒度标记覆盖。
  markResourcesByTaskId(projectName, "grid", "grid", res.task_ids_by_grid);
  notifyEnqueued(res.deduped, res.message);
  return { taskIds: res.task_ids, deduped: res.deduped };
}

export async function enqueueGridRegenerate(
  projectName: string,
  gridId: string,
  scriptFile: string | null,
): Promise<EnqueueResult> {
  const res = await submit(
    [
      markResource(projectName, "grid", gridId, "grid"),
      ...(scriptFile ? [markScriptFile(projectName, "grid", scriptFile)] : []),
    ],
    () => API.regenerateGrid(projectName, gridId),
    oneTaskId,
  );
  // The regenerate entry is silent on success (the panel already shows status feedback); only show a toast when deduped.
  // 重生成入口成功时静默（面板内已有状态反馈），仅 deduped 时弹提示。
  notifyEnqueued(res.deduped, null);
  return { taskIds: [res.task_id], deduped: res.deduped };
}

export async function enqueueReferenceVideoUnit(
  projectName: string,
  episode: number,
  unitId: string,
  options: ReferenceGenerationRequestOptions = {},
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "reference_video", unitId, "reference_video")],
    () => API.generateReferenceVideoUnit(projectName, episode, unitId, options),
    oneTaskId,
  );
  notifyEnqueued(res.deduped, i18n.t("dashboard:reference_generate_queued"), "info");
  return { taskIds: [res.task_id], deduped: res.deduped };
}

/**
 * Batch video generation: one request with all-or-nothing admission; the server evaluates all target units.
 *
 * All three outcomes are successful evaluations, and only `admitted` creates tasks; `confirmation_required` and
 * `blocked` create no task, so the optimistic occupancy marks are rolled back as a whole and the caller shows the confirmation or gaps from the verdict.
 * When the request body omits unit_ids (generate missing) the target set is decided by the server and the frontend cannot mark, so nothing is marked.
 *
 * 批量视频生成：一次请求走全有或全无准入，由服务端评估全部目标单元。
 *
 * 三种结局都是评估成功，只有 `admitted` 建了任务——`confirmation_required` 与
 * `blocked` 一个任务也没建，故乐观占用标记随即整批回滚，由调用方按结论展示确认或缺口。
 * 请求体省略 unit_ids 时（缺失即生成）目标集合由服务端决定，前端无从打标，此时不打标。
 */
export async function enqueueReferenceVideoBatch(
  projectName: string,
  episode: number,
  payload: ReferenceBatchGenerateRequest,
): Promise<ReferenceBatchAdmission> {
  const unitIds = payload.unit_ids ?? [];
  const marks = unitIds.map((unitId) =>
    markResource(projectName, "reference_video", unitId, "reference_video"),
  );
  const res = await submit(
    marks,
    () => API.generateReferenceVideoBatch(projectName, episode, payload),
    (admission, index) => {
      if (admission.decision !== "admitted") return [];
      const taskId = admission.task_ids_by_unit[unitIds[index]];
      return taskId === undefined ? [] : [taskId];
    },
  );
  if (res.decision === "admitted") {
    notifyEnqueued(
      res.deduped,
      // If even the first target was not enqueued, no task was created, and "0 submitted" would only clash with the interruption message below.
      // 首个目标就没入队时一个任务也没建，「已提交 0 个」只会和下面那句中断提示打架。
      res.task_ids.length > 0
        ? i18n.t("dashboard:reference_batch_queued", { count: res.task_ids.length })
        : null,
      "info",
    );
    // An enqueue interruption does not undo tasks already created, so "how many were created" and "which were not" must be said together: reporting only the success count
    // would make the user think the whole batch is running, then later find some missing without knowing why.
    // 入队中断不撤销已建的任务，所以「建了几个」与「哪些没建」要一起说：只报成功数
    // 会让用户以为整批都在跑，回头发现少了几条却不知道为什么。
    if (res.enqueue_failures.length > 0) {
      useAppStore
        .getState()
        .pushToast(
          i18n.t("dashboard:reference_batch_enqueue_interrupted", {
            count: res.enqueue_failures.length,
          }),
          "error",
        );
    }
  }
  return res;
}

/**
 * Batch generation of storyboard images / shot videos for one episode: the target set is decided by the server (shots in this episode without a usable artifact),
 * and occupancy marks are set item by item after the response from "shot -> task_id".
 *
 * When the shot-video batch fails admission no task is created; no success toast, the caller states the verdict.
 *
 * 一集分镜图 / 分镜视频的批量生成：目标集合由服务端决定（本集没有可用产物的分镜），
 * 占用标记在响应后按「分镜 → task_id」逐项补打。
 *
 * 分镜视频整批准入未通过时一个任务也没建，不提示成功，由调用方陈述结论。
 */
export async function enqueueStoryboardBatch(
  projectName: string,
  episode: number,
  kind: StoryboardBatchKind,
): Promise<StoryboardBatchSubmitted> {
  const res = await API.submitStoryboardBatch(projectName, episode, kind);
  if (res.admission && res.admission.decision !== "admitted") return res;
  const taskType = kind === "storyboards" ? "storyboard" : "video";
  markResourcesByTaskId(projectName, taskType, taskType, res.task_ids_by_unit);
  const queued = Object.keys(res.task_ids_by_unit).length;
  notifyEnqueued(
    false,
    queued > 0 ? i18n.t(`dashboard:storyboard_batch_submitted.${kind}`, { count: queued }) : null,
  );
  if (res.enqueue_failures.length > 0) {
    useAppStore
      .getState()
      .pushToast(
        i18n.t("dashboard:storyboard_batch_enqueue_failed", { count: res.enqueue_failures.length }),
        "warning",
      );
  }
  return res;
}

/**
 * Batch sheet generation: the target set is decided by the server by scope, and occupancy marks are set per member after the response;
 * derivative members occupy the `base/derivative` composite slot (character_derivative), not masking the base look.
 * When a task for the same resource already exists (deduped) show the unified dedup toast, otherwise report the number enqueued; members with no task created are not marked.
 *
 * 资产图批量生成：目标集合由服务端按范围决定，占用标记在响应后按成员逐项补打；
 * 衍生成员占 `本体/衍生` 复合槽（character_derivative），与本体互不遮蔽。
 * 已有同资源任务（deduped）时弹统一去重提示，否则报入队张数；未建出任务的成员不打标。
 */
export async function enqueueAssetSheetBatch(
  projectName: string,
  scope: AssetSheetBatchScope,
): Promise<AssetSheetBatchSubmitted> {
  const res = await API.submitAssetSheetBatch(projectName, scope);
  let queued = 0;
  for (const member of res.members) {
    if (!member.task_id) continue;
    queued += 1;
    const [kind, id]: [ResourceKind, string] = member.derivative
      ? ["character_derivative", derivativeResourceId(member.name, member.derivative)]
      : [member.asset_type, member.name];
    markResource(projectName, kind, id, kind).settle([member.task_id]);
  }
  notifyEnqueued(
    res.members.some((member) => member.deduped),
    queued > 0 ? i18n.t("assets:sheet_batch_submitted", { count: queued }) : null,
  );
  return res;
}

/** Task IDs of members in a text task batch that had a task created. / 文本任务批次里已建出任务的成员的任务 ID。 */
function memberTaskIds(batch: { members: ReadonlyArray<{ task_id?: string | null }> }): string[] {
  return batch.members.flatMap((member) => (member.task_id ? [member.task_id] : []));
}

/** Occupancy slot for prompt authoring tasks: one text task per episode; resource_id matches the server's `episode-{N}`. / 提示词编写任务的占用槽：一集一个文本任务，resource_id 与服务端 `episode-{N}` 一致。 */
export function promptAuthoringResourceId(episode: number): string {
  return `episode-${episode}`;
}

/**
 * Submit prompt authoring ("AI write / AI rewrite"). When an explicit rewrite needs overwrite confirmation the server returns 409; the error is thrown as-is,
 * and the caller reads `diagnostic.prompt_overwrite`, shows a confirmation dialog, then retries with the token.
 *
 * 提交提示词编写（「AI 编写 / AI 重写」）。显式重写需要确认覆盖时服务端 409，错误原样抛出，
 * 由调用方读 `diagnostic.prompt_overwrite` 弹确认框后带令牌重试。
 */
export async function enqueuePromptAuthoring(
  projectName: string,
  episode: number,
  request: AuthorPromptsRequest,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "text_episode_script", promptAuthoringResourceId(episode), "text_episode_script")],
    () => API.authorPrompts(projectName, episode, request),
    (response) => memberTaskIds(response.batch),
  );
  const taskIds = memberTaskIds(res.batch);
  const deduped = res.batch.members.some((member) => member.deduped === true);
  notifyEnqueued(
    deduped,
    i18n.t(request.rewrite ? "dashboard:prompt_authoring_rewrite_queued" : "dashboard:prompt_authoring_queued"),
    "info",
  );
  if (!deduped) watchPromptAuthoringNoop(projectName, taskIds);
  return { taskIds, deduped };
}

/**
 * Show a toast when prompt authoring ends as a no-op (no pending-authoring entries in scope, the text model was not called), explaining that checking overwrite allows a rewrite.
 * The authoring entry closes after submitting, so the task result can only be tracked in the enqueue layer; tracking stops once all tasks reach a terminal state, or the task list no longer tracks this project
 * (switched to another project, left the studio); on return, stale toasts are not shown retroactively.
 *
 * 提示词编写以空操作收尾（范围内没有待编写的条目，未调用文本模型）时弹提示，说明勾选覆盖可以重写。
 * 编写入口提交后即关闭，只能在入队层跟踪任务结果；任务都到达终态，或任务列表不再跟踪本项目
 * （切到别的项目、离开工作室）后停止跟踪，回来时不补弹过期的提示。
 */
function watchPromptAuthoringNoop(projectName: string, taskIds: readonly string[]): void {
  const pending = new Set(taskIds);
  if (pending.size === 0) return;
  const check = (state: ReturnType<typeof useTasksStore.getState>): void => {
    if (state.refreshScope?.projectName !== projectName) {
      unsubscribe();
      return;
    }
    for (const task of state.tasks) {
      if (!pending.has(task.task_id) || !isTerminalStatus(task.status)) continue;
      pending.delete(task.task_id);
      if (task.status === "succeeded" && task.result?.no_pending_authoring === true) {
        useAppStore.getState().pushToast(i18n.t("dashboard:prompt_authoring_nothing_pending"), "info");
      }
    }
    if (pending.size === 0) unsubscribe();
  };
  const unsubscribe = useTasksStore.subscribe(check);
  check(useTasksStore.getState());
}

/** Occupancy slot for script plan tasks: one text task per episode; resource_id matches the server's `episode-{N}`. / 脚本规划任务的占用槽：一集一个文本任务，resource_id 与服务端 `episode-{N}` 一致。 */
export function scriptPlanResourceId(episode: number): string {
  return `episode-${episode}`;
}

/**
 * Submit AI script planning. The new plan replaces this episode's existing plan and drafts wholesale; confirming before replacement is the caller's job;
 * when admission fails or the episode already has a plan in progress, the server's error is thrown as-is.
 *
 * 提交 AI 规划脚本。新的规划整份替换本集现有的规划与草稿，替换前的确认由调用方负责；
 * 准入不成立或本集已有进行中的规划时，服务端的错误原样抛出。
 */
export async function enqueueScriptPlan(
  projectName: string,
  episode: number,
  request: PlanScriptRequest,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "text_script_plan", scriptPlanResourceId(episode), "text_script_plan")],
    () => API.planScript(projectName, episode, request),
    (response) => memberTaskIds(response.batch),
  );
  const taskIds = memberTaskIds(res.batch);
  const deduped = res.batch.members.some((member) => member.deduped === true);
  notifyEnqueued(deduped, null);
  return { taskIds, deduped };
}

/**
 * Submit the ad/short "AI generate script"; it occupies the same slot as prompt authoring (same server task type, `episode-{N}`).
 * When a full redo needs overwrite confirmation the server returns 409; the error is thrown as-is, and the caller reads `diagnostic.script_overwrite`, shows a confirmation dialog, then retries with the token.
 *
 * 提交广告/短片「AI 生成脚本」，占用与提示词编写同一个槽（服务端同一任务类型、`episode-{N}`）。
 * 整份重做需要确认覆盖时服务端 409，错误原样抛出，由调用方读 `diagnostic.script_overwrite` 弹确认框后带令牌重试。
 */
export async function enqueueAdScript(
  projectName: string,
  episode: number,
  request: GenerateAdScriptRequest,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "text_episode_script", promptAuthoringResourceId(episode), "text_episode_script")],
    () => API.generateAdScript(projectName, episode, request),
    (response) => memberTaskIds(response.batch),
  );
  const taskIds = memberTaskIds(res.batch);
  const deduped = res.batch.members.some((member) => member.deduped === true);
  notifyEnqueued(deduped, i18n.t("dashboard:ad_script_queued"), "info");
  return { taskIds, deduped };
}

/** The two occupancy slots of episode planning, matching the server: the first window takes the first one, then each window alternates between the two slots. / 分集规划的两个占用槽，与服务端一致：首窗占前一个，之后逐窗在两槽间交替。 */
export const EPISODE_PLANNING_SLOTS = ["episode-planning", "episode-planning-next"] as const;

/**
 * Submit AI episode planning: plan window by window from the planning start point to the end of the whole source. When episode planning is already in progress or admission fails,
 * the server's error is thrown as-is. Instructions go only with this submission and are not written to the project.
 *
 * 提交 AI 规划分集：从规划起点逐窗规划到整本源文结尾。已有进行中的分集规划或准入不成立时，
 * 服务端的错误原样抛出。附加指令只随本次提交，不写进项目。
 */
export async function enqueueEpisodePlanning(
  projectName: string,
  instructions: string | null,
  gap: PlanningGap | null = null,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "text_episode_plan", EPISODE_PLANNING_SLOTS[0], "text_episode_plan")],
    () => API.planEpisodes(projectName, instructions, gap),
    (response) => memberTaskIds(response.batch),
  );
  const taskIds = memberTaskIds(res.batch);
  const deduped = res.batch.members.some((member) => member.deduped === true);
  notifyEnqueued(deduped, null);
  return { taskIds, deduped };
}

/**
 * Start a replan: generate a "new episode plan" window by window starting from this episode, occupying the same pair of slots as episode planning. When a plan or episode planning is already in progress,
 * the server's error is thrown as-is. Instructions are saved with the plan.
 *
 * 发起重新规划：从这一集开始逐窗生成「新的分集方案」，与分集规划占同一对槽。已有方案或分集规划在进行时，
 * 服务端的错误原样抛出。附加指令随方案保存。
 */
export async function enqueueEpisodeReplan(
  projectName: string,
  episode: number,
  instructions: string | null,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "text_episode_plan", EPISODE_PLANNING_SLOTS[0], "text_episode_plan")],
    () => API.startEpisodeReplan(projectName, episode, instructions),
    (response) => memberTaskIds(response.batch),
  );
  const taskIds = memberTaskIds(res.batch);
  const deduped = res.batch.members.some((member) => member.deduped === true);
  notifyEnqueued(deduped, null);
  return { taskIds, deduped };
}

/** Continue generating a new episode plan that stopped midway, occupying the same pair of slots as episode planning. Server rejections are thrown as-is. / 接着生成中途停止的新的分集方案，与分集规划占同一对槽。服务端的拒绝原样抛出。 */
export async function enqueueEpisodeReplanContinue(projectName: string, candidateId: string): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "text_episode_plan", EPISODE_PLANNING_SLOTS[0], "text_episode_plan")],
    () => API.continueEpisodeReplan(projectName, candidateId),
    (response) => memberTaskIds(response.batch),
  );
  const taskIds = memberTaskIds(res.batch);
  const deduped = res.batch.members.some((member) => member.deduped === true);
  notifyEnqueued(deduped, null);
  return { taskIds, deduped };
}

/** Occupancy slot for draft repair tasks: one per draft; resource_id matches the server's `episode-{N}-{doc_type}`. / 草稿 AI 修复任务的占用槽：一份草稿一个，resource_id 与服务端 `episode-{N}-{doc_type}` 一致。 */
export function draftRepairResourceId(episode: number, docType: DraftDocType): string {
  return `episode-${episode}-${docType}`;
}

/**
 * Submit draft repair for an invalid draft. The repair reads the saved draft at version `baseRevision`; server errors such as the draft having changed or this draft
 * already having a repair in progress are thrown as-is.
 *
 * 提交待修复草稿的 AI 修复。修复读取的是 `baseRevision` 那一版已保存的草稿；草稿已变、本份草稿
 * 已有进行中的修复等服务端错误原样抛出。
 */
export async function enqueueDraftRepair(
  projectName: string,
  episode: number,
  docType: DraftDocType,
  baseRevision: string,
  instructions: string | null,
): Promise<EnqueueResult> {
  const res = await submit(
    [markResource(projectName, "text_draft_repair", draftRepairResourceId(episode, docType), "text_draft_repair")],
    () => API.repairEpisodeDraft(projectName, episode, docType, baseRevision, instructions),
    (response) => memberTaskIds(response.batch),
  );
  const taskIds = memberTaskIds(res.batch);
  const deduped = res.batch.members.some((member) => member.deduped === true);
  notifyEnqueued(deduped, i18n.t("dashboard:draft_repair_queued"), "info");
  return { taskIds, deduped };
}
