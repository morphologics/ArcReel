import type { PresentationResourceType } from "./presentation";

export type ProjectEventSource = "webui" | "worker" | "filesystem";

/** Locating pane of project events: assets by their asset table name (must cover the backend asset type table); script entries are uniformly episode. / 项目事件的定位窗格：资产按其资产表名（须覆盖后端资产类型表），剧本条目统一为 episode。 */
export type ProjectChangePane = "characters" | "scenes" | "props" | "products" | "episode";

export interface ProjectChangeFocus {
  pane: ProjectChangePane;
  episode?: number;
  // The three skeleton entries segment/drama_scene/shot use the timeline canvas, with anchor type uniformly segment; video_units use the reference-to-video
  // canvas, with anchor type reference_unit (aligned with WorkspaceFocusTarget["type"] and the canvas guard).
  // segment/drama_scene/shot 三种骨架条目走时间线画布，锚点类型统一为 segment；video_units 走参考
  // 生视频画布，锚点类型为 reference_unit（与 WorkspaceFocusTarget["type"] 及画布守卫对齐）。
  anchor_type?: "character" | "scene" | "prop" | "product" | "segment" | "reference_unit";
  anchor_id?: string;
  tab?: string;
}

export interface ProjectChange {
  // segment/drama_scene/shot/reference_unit are the four script skeleton entry types (narration/drama/ad/reference-to-video),
  // driving the group label mapping; drama uses drama_scene to avoid colliding with the named entity scene.
  // segment/drama_scene/shot/reference_unit 为四种剧本骨架条目类型（narration/drama/ad/参考生视频），
  // 驱动分组标签映射；drama 用 drama_scene 避免与命名实体 scene 撞组。
  entity_type:
    | "project"
    | "character"
    | "scene"
    | "prop"
    | "product"
    | "segment"
    | "drama_scene"
    | "shot"
    | "reference_unit"
    | "episode"
    | "overview"
    | "draft"
    | "grid"
    // task is not a project entity but a refresh signal of a task terminal state (important=false / focus=null):
    // only used to refetch the task list and affected canvases, not entering notifications or focus jumps.
    // task 不是项目实体，而是任务终态的刷新信号（important=false / focus=null）：
    // 只用来重拉任务列表与受影响画布，不进通知与聚焦跳转。
    | "task"
    // usage_record is likewise a refresh signal: a provider call settled and stored (success/failure/cancel),
    // used to refetch usage and cost, not entering notifications or focus jumps.
    // usage_record 同样是刷新信号：一次供应商调用结算落库（成功/失败/取消），
    // 用来重拉用量与成本，不进通知与聚焦跳转。
    | "usage_record"
    // trash is likewise a refresh signal: the project trash had additions/removals, used to refetch the file view and trash section.
    // trash 同样是刷新信号：项目回收站有增删，用来重拉文件视图与回收站分区。
    | "trash";
  action:
    | "created"
    | "updated"
    | "deleted"
    | "storyboard_ready"
    | "video_ready"
    | "grid_ready"
    | "grid_split_done"
    | "reference_video_ready"
    | "tts_ready"
    | "voice_sample_ready"
    | "task_succeeded"
    | "task_failed"
    | "task_cancelled"
    | "recorded";
  entity_id: string;
  /** Entry label rendered by the backend in the default language, only for logs and as a fallback for old publishers; UI copy follows label_key. / 后端按默认语言渲染的条目标签，只作日志与旧发布方兜底；界面文案以 label_key 为准。 */
  label: string;
  /** Stable identifier of the entry label, corresponding to `label.*` in the `events` namespace. / 条目标签的稳定标识，对应 `events` 命名空间的 `label.*`。 */
  label_key?: string;
  /** Interpolation params of label_key (e.g. id / episode). / label_key 的插值参数（如 id / episode）。 */
  label_params?: Record<string, string | number>;
  script_file?: string;
  episode?: number;
  /** Carried only when entity_type === "task": task type of the terminal task, used to decide which canvases need refetching. / 仅 entity_type === "task" 携带：终态任务的任务类型，用于判定哪类画布需重拉。 */
  task_type?: string;
  /** Carried only when entity_type === "usage_record": the terminal state this call settled into. / 仅 entity_type === "usage_record" 携带：这次调用结算成的终态。 */
  status?: string;
  focus?: ProjectChangeFocus | null;
  important: boolean;
  asset_fingerprints?: Record<string, number>;
}

export interface ProjectChangeBatchPayload {
  project_name: string;
  batch_id: string;
  fingerprint: string;
  generated_at: string;
  source: ProjectEventSource;
  changes: ProjectChange[];
}

export interface ProjectEventSnapshotPayload {
  project_name: string;
  fingerprint: string;
  generated_at: string;
}

/** Payload of the project event stream termination event -- sent after the project directory is deleted, after which the stream ends normally. / 项目事件流终止事件负载——项目目录被删除后下发，随后流正常结束。 */
export interface ProjectDeletedPayload {
  project_name: string;
}

/** Sent when an Agent session under the project starts a new round on its own without a user send (woken after a background task completes). / 项目下的 Agent 会话未经用户发送、自主开启新一轮（后台任务完成后唤醒）时下发。 */
export interface AssistantSessionResumedPayload {
  project_name: string;
  session_id: string;
  status: "running";
}

export interface WorkspaceFocusTarget {
  request_id: string;
  type: "character" | "scene" | "prop" | "product" | "segment" | "grid" | "reference_unit";
  id: string;
  route: string;
  highlight: true;
  highlight_style: "flash";
  expires_at: number;
}

export interface WorkspaceFocusTargetInput {
  request_id?: string;
  type: WorkspaceFocusTarget["type"];
  id: string;
  route?: string;
  highlight?: boolean;
  highlight_style?: WorkspaceFocusTarget["highlight_style"];
  expires_at?: number;
}

/**
 * Request to open the preview of a video unit (unit link in an Agent reply): brings the preview to the front when it is folded into a sub-tab on narrow screens,
 * and when `seconds` is not null the player starts from that time. One-shot: cleared once the player consumes it, and void if nobody consumes it for a while.
 *
 * 请求打开某个视频单元的预览（Agent 回复里的单元链接）：预览在窄屏下被收进子页签时切到前台，
 * `seconds` 不为 null 时播放器再从该时间开始播放。一次性：播放器消费后清除，一段时间内无人消费也作废。
 */
export interface PlaybackStartRequest {
  request_id: string;
  resource_type: PresentationResourceType;
  resource_id: string;
  seconds: number | null;
}

export interface WorkspaceNotificationTarget {
  type: WorkspaceFocusTarget["type"];
  id: string;
  route: string;
  highlight_style?: WorkspaceFocusTarget["highlight_style"];
}

export interface WorkspaceNotification {
  id: string;
  text: string;
  tone: "info" | "success" | "error" | "warning";
  created_at: number;
  read: boolean;
  target?: WorkspaceNotificationTarget | null;
}

export interface WorkspaceNotificationInput {
  text: string;
  tone?: WorkspaceNotification["tone"];
  target?: WorkspaceNotification["target"];
  read?: boolean;
}
