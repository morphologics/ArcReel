/**
 * Assistant / agent runtime type definitions.
 *
 * Maps to backend models in:
 * - server/agent_runtime/models.py (SessionMeta, SessionStatus)
 * - server/agent_runtime/event_log.py (TimelineEntry payload structure)
 * - server/agent_runtime/entry_pipeline.py (DraftState / DraftDeltaPayload)
 * - server/agent_runtime/service.py (SkillInfo, entry stream events)
 */

export type SessionStatus = "idle" | "running" | "completed" | "error" | "interrupted";

export interface SessionMeta {
  id: string;              // Now it is the sdk_session_id / 现在就是 sdk_session_id
  project_name: string;
  title: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
}

/** Transport form of image attachments: isomorphic with the `images` elements of `/sessions/send` and `/sessions/{id}/rewrite`. / 图片附件的传输形态：与 `/sessions/send`、`/sessions/{id}/rewrite` 的 `images` 元素同构。 */
export interface ImagePayload {
  data: string;
  media_type: string;
}

export interface ContentBlock {
  type:
    | "text"
    | "thinking"
    | "tool_use"
    | "tool_result"
    | "skill_invocation"
    | "task_progress"
    | "interrupt_notice"
    | "question_answer"
    | "agent_failure"
    | "compact_summary"
    | "image";
  text?: string;
  thinking?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  result?: string;
  is_error?: boolean;
  tool_use_id?: string;
  content?: string;
  // skill_invocation block fields (shaped at the write point: only name and arguments, no injected full text)
  // skill_invocation 块字段（写入点定型，只有名与入参，无注入全文）
  skill_name?: string;
  skill_args?: string;
  // Subagent sub-timeline: the projection groups by parent_tool_use_id and attaches it to the anchor tool_use block
  // 子智能体子时间线：投影按 parent_tool_use_id 归组后挂在锚点 tool_use 块
  sub_turns?: Turn[];
  // Subagent status/progress linked to the anchor tool_use (folded from task_progress blocks)
  // 关联到锚点 tool_use 的子智能体状态/进度（由 task_progress 块折叠而来）
  task_info?: ContentBlock;
  // image block fields
  source?: { type: "base64"; media_type: string; data: string };
  // task_progress fields
  task_id?: string;
  status?: string;
  description?: string;
  summary?: string;
  task_status?: string;
  usage?: { total_tokens?: number; tool_uses?: number; duration_ms?: number };
  // question_answer fields (AskUserQuestion reply: question -> chosen option) / question_answer fields（AskUserQuestion 答复：问题 → 所选选项）
  answers?: Record<string, string>;
  // agent_failure block (Agent failure observation shaped at the write point) / agent_failure block（写入点定型的 Agent 故障观测）
  failure?: FailureObservation;
}

export interface FailureObservation {
  version: number;
  phase: "startup" | "turn";
  timestamp: string;
  project_name: string | null;
  session_id: string | null;
  summary: {
    /**
     * Stable machine key of the one-sentence conclusion, derived only from structured evidence (see server/agent_runtime/failure_observation.py).
     * Historical events materialized before this field have no key and are shown with the generic per-stage key.
     *
     * 一句话结论的稳定机器 key，只从结构化证据派生（见 server/agent_runtime/failure_observation.py）。
     * 早于该字段物化的历史事件没有 key，按阶段的通用 key 显示。
     */
    key?: string;
    source: string;
    /** SDK / upstream may add arbitrary JSON shapes; the frontend only renders them and does not infer enums. / SDK / 上游可新增任意 JSON 形态；前端只呈现，不做枚举推断。 */
    type: unknown;
    status?: unknown;
    message: string | null;
  };
  raw: Record<string, unknown>;
}

export interface Turn {
  type: "user" | "assistant" | "system";
  content: ContentBlock[];
  uuid?: string;
  timestamp?: string;
  subtype?: string;
}

export interface PendingQuestion {
  question_id: string;
  questions: Array<{
    header?: string;
    question: string;
    options: Array<{ label: string; description: string }>;
    multiSelect: boolean;
  }>;
}

/** Session event log entry (the sole read source of the UI timeline; seq is a monotonic in-session sequence number). / 会话事件日志条目（UI 时间线唯一读源；seq 为会话内单调序号）。 */
export interface TimelineEntry {
  seq: number;
  type: "user" | "assistant" | "tool_result" | "system";
  content?: ContentBlock[] | string;
  uuid?: string;
  timestamp?: string;
  /** Carried by assistant entries, so drafts can be replaced precisely by identity. / assistant 条目携带，供 draft 按身份精确替换。 */
  message_id?: string | null;
  /** Subagent message marker: the projection groups by parent into a single collapsed card on the main timeline. / 子智能体消息标记：投影按 parent 归组为主时间线单一折叠卡片。 */
  parent_tool_use_id?: string;
  // tool_result / question_answer entry fields / tool_result / question_answer 条目字段
  tool_use_id?: string | null;
  is_error?: boolean;
  // Subtypes shaped at the write point: system entries are task_* / interrupt / skill_invocation /
  // agent_turn_failure / subagent_outcome; user entries are question_answer / compact_summary
  // 写入点定型的子类型：system 条目为 task_* / interrupt / skill_invocation /
  // agent_turn_failure / subagent_outcome；user 条目为 question_answer / compact_summary
  subtype?: string;
  task_id?: string | null;
  description?: string;
  summary?: string | null;
  task_status?: string | null;
  usage?: { total_tokens?: number; tool_uses?: number; duration_ms?: number } | null;
  // system entry fields (skill_invocation subtype: only name and arguments) / system 条目字段（skill_invocation 子类型：只记名与入参）
  skill_name?: string | null;
  skill_args?: string | null;
  // question_answer entry fields (structured answer of the AskUserQuestion reply) / question_answer 条目字段（AskUserQuestion 答复的结构化答案）
  answers?: Record<string, string> | null;
  // agent_turn_failure system entry / agent_turn_failure 系统条目
  failure?: FailureObservation;
}

/** Queued message status: `queued` handed to the Agent, awaiting acceptance; `unsent` still queued when the Agent process exited, the user must decide to send, edit or delete it. / 排队消息的状态：`queued` 已交给 Agent、等待被接纳；`unsent` Agent 进程退出时仍在排队，需由用户决定发送、编辑或删除。 */
export type QueuedMessageState = "queued" | "unsent";

/**
 * Queued message: a user message already sent that the Agent has not yet accepted into the conversation (server in-memory state, not in the event log).
 * `id` is the uuid of the timeline user entry once accepted.
 *
 * 排队消息：已发出、Agent 尚未接纳进对话的用户消息（服务端内存态，不入事件日志）。
 * `id` 即被接纳后时间线用户条目的 uuid。
 */
export interface QueuedMessage {
  id: string;
  content: ContentBlock[];
  timestamp?: string | null;
  state: QueuedMessageState;
}

/** Intent of withdrawing a queued message: `edit` returns the content to the input, `delete` discards it. / 撤回排队消息的意图：`edit` 把内容退回输入框，`delete` 直接丢弃。 */
export type QueuedMessageWithdrawal = "edit" | "delete";

/** Response of editing / deleting a queued message. / 编辑 / 删除排队消息的响应。 */
export interface QueuedMessageWithdrawalResponse {
  session_id: string;
  id: string;
  /** `withdrawn`: withdrawn and removed from the queue; `accepted`: the Agent already received it, the message enters the conversation as usual. / `withdrawn`：已撤回并移出排队；`accepted`：Agent 已接收，消息照常进入对话。 */
  outcome: "withdrawn" | "accepted";
  /** The message to return to the input when editing and withdrawal succeeded, otherwise null. / 编辑且撤回成功时为要退回输入框的消息，否则为 null。 */
  message: QueuedMessage | null;
}

/** Response of resending an "unsent" message: the same queued message, now back to `queued`. / 重新发送「未发送」消息的响应：同一条排队消息，已回到 `queued`。 */
export interface QueuedMessageResendResponse {
  session_id: string;
  id: string;
  queued_message: QueuedMessage;
}

/** Response of sending a queued message immediately. / 立即发送排队消息的响应。 */
export interface QueuedMessageSendNowResponse {
  session_id: string;
  id: string;
  /** `sent`: re-delivered, the Agent interrupts the current turn to handle it first and the message stays in the tray until accepted; `accepted`: the Agent already received it, no resend. / `sent`：已重新送入，Agent 打断当前轮先处理它，消息留在托盘里直到被接纳；`accepted`：Agent 已接收，不再重发。 */
  outcome: "sent" | "accepted";
}

/** Acceptance response of the send and rewrite endpoints: exactly one of queued message and authoritative entry. / 发送与改写端点的受理响应：排队消息与权威条目二者恰有其一。 */
export interface AcceptedMessageResponse {
  status: string;
  session_id: string;
  /** First message of a new session, or a retry with the same client_key hitting an already accepted message. / 新会话首条消息，或同一 client_key 的重试命中已被接纳的消息。 */
  entry: TimelineEntry | null;
  /** Message to an existing session: awaiting Agent acceptance. / 已有会话的消息：等待 Agent 接纳。 */
  queued_message: QueuedMessage | null;
}

/** Snapshot of the server streaming preview state (identity is message_id, not logged). / 服务端流式预览态快照（身份为 message_id，不入日志）。 */
export interface DraftState {
  message_id: string;
  parent_tool_use_id?: string | null;
  content: ContentBlock[];
  rev?: number;
  /** Raw partial JSON accumulated for each tool_use block (the prefix for continuing input_json_delta after reconnect). / 各 tool_use 块已累积的原始 partial JSON（重连后续拼 input_json_delta 的前缀）。 */
  tool_json?: Record<number, string>;
}

/** delta SSE event payload (references message_id + block index; rev is monotonic for filtering on reconnect). / delta SSE 事件载荷（引用 message_id + block index，rev 单调用于重连过滤）。 */
export interface DraftDeltaPayload {
  message_id: string;
  parent_tool_use_id?: string | null;
  delta_type: "block_start" | "text_delta" | "thinking_delta" | "input_json_delta";
  block_index: number;
  rev: number;
  block?: ContentBlock;
  text?: string;
  thinking?: string;
  partial_json?: string;
}

/** GET /sessions/{id}/entries response. / GET /sessions/{id}/entries 响应。 */
export interface EntriesResponse {
  session_id: string;
  status: SessionStatus;
  entries: TimelineEntry[];
  draft: DraftState | null;
  draft_rev: number;
}

export interface SkillInfo {
  name: string;
  description: string;
  scope: "project" | "user";
  path: string;
  // Backend hint of a Lucide icon id; display name lives in i18n (dashboard:skill_name_<id>).
  icon?: string;
}

export interface TodoItem {
  content: string;
  activeForm: string;
  status: "pending" | "in_progress" | "completed";
}
