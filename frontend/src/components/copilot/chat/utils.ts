import type { ImagePayload, SessionStatus, Turn } from "@/types";

// ---------------------------------------------------------------------------
// TERMINAL_SESSION_STATUSES – session statuses treated as "done" for the
// purpose of freezing running/pending indicators (subagent cards, task rows).
// ---------------------------------------------------------------------------

export const TERMINAL_SESSION_STATUSES = new Set(["completed", "error", "interrupted"]);

// ---------------------------------------------------------------------------
// composeAllTurns – merge live draft into committed turn list for rendering.
// When the user interrupts, the interrupted assistant streaming content still lives in draftTurn (unfinished
// messages don't become authoritative log entries). The turns then end with an interrupt_notice system
// turn — appending the draft at the end would render as "interrupt -> Agent reply", reversing the time
// order. Insert the draft before interrupt_notice so the UI shows
// "Agent reply -> interrupt". After a refresh the draft naturally disappears (server in-memory state, not logged).
// ---------------------------------------------------------------------------
// composeAllTurns – merge live draft into committed turn list for rendering.
//
// 当用户中断时，被中断的 assistant 流式内容仍存在 draftTurn 中（未完成的
// 消息不会形成权威日志条目）。此时 turns 末尾是 interrupt_notice 系统
// turn——若把 draft 直接附加在末尾，渲染会变成"中断 → Agent 回复"，与时间
// 顺序相反。把 draft 插到 interrupt_notice 之前，让 UI 显示成
// "Agent 回复 → 中断"。刷新后 draft 自然消失（服务端内存态，不入日志）。
// ---------------------------------------------------------------------------

export function composeAllTurns(turns: Turn[], draftTurn: Turn | null): Turn[] {
  if (!draftTurn) return turns;
  const last = turns.at(-1);
  const lastIsInterrupt = last?.type === "system"
    && (last.content ?? []).some((b) => b.type === "interrupt_notice");
  if (lastIsInterrupt && last) {
    return [...turns.slice(0, -1), draftTurn, last];
  }
  return [...turns, draftTurn];
}

// ---------------------------------------------------------------------------
// turnPlainText – the copyable / rewritable body of a turn (text blocks only).
// ---------------------------------------------------------------------------
// turnPlainText – 一个 turn 的可复制 / 可改写正文（只取 text 块）。
// ---------------------------------------------------------------------------

export function turnPlainText(turn: Turn): string {
  return (turn.content ?? [])
    .filter((block) => block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n\n");
}

// ---------------------------------------------------------------------------
// turnImageAttachments – the image attachments a turn carries, restored to the sender-side transport shape.
// Image blocks are stored verbatim as base64 in log entries; on rewrite they are taken straight from the anchor message and submitted with the new message,
// using the same request shape as a normal send with images. Block order is submission order (server: images first, text after).
// ---------------------------------------------------------------------------
// turnImageAttachments – 一个 turn 携带的图片附件，按发送侧的传输形态还原。
//
// 图片块以 base64 原样存在日志条目里，改写时直接从锚点消息取回随新消息一同提交，
// 与普通带图发送走同一条请求形态。块顺序即提交顺序（服务端图在前、文本在后）。
// ---------------------------------------------------------------------------

export function turnImageAttachments(turn: Turn): ImagePayload[] {
  return (turn.content ?? []).flatMap((block) => {
    const source = block.type === "image" ? block.source : undefined;
    if (!source?.data) return [];
    return [{ data: source.data, media_type: source.media_type }];
  });
}

// ---------------------------------------------------------------------------
// canEditUserTurn – whether this message offers the rewrite entry right now.
// A client-side prediction; the server remains the source of truth (invalid anchor 400, pending question 409).
// When not editable the entry isn't rendered, not grayed out.
// ---------------------------------------------------------------------------
// canEditUserTurn – 这条消息此刻是否给出改写入口。
//
// 客户端预判，服务端仍是判据的真相源（锚点非法 400、未决问答 409）。
// 不可编辑时入口不渲染，不做置灰。
// ---------------------------------------------------------------------------

export function canEditUserTurn(
  turn: Turn,
  context: {
    sessionStatus: SessionStatus | null;
    hasPendingQuestion: boolean;
    isSending: boolean;
    hasQueuedMessages: boolean;
  },
): boolean {
  if (turn.type !== "user") return false;
  // The rewrite anchor is the entry uuid: turns without a uuid (synthesized cards, draft) can't be anchored
  // 改写锚点就是条目 uuid：没有 uuid 的 turn（合成卡片、draft）无从锚定
  if (!turn.uuid) return false;
  // A question answer is the receipt of an Agent questionnaire, not a message the user wrote. Projected Turns carry no
  // subtype, so identify by content block type.
  // 问答答复是 Agent 问卷的回执，不是用户自己写的消息。投影产出的 Turn 不带
  // subtype，按内容块类型识别。
  if ((turn.content ?? []).some((block) => block.type === "question_answer")) return false;
  if (!turnPlainText(turn).trim() && turnImageAttachments(turn).length === 0) return false;
  if (context.sessionStatus === "running") return false;
  if (context.hasPendingQuestion) return false;
  // A send or rewrite is already in flight: letting another edit entry through now would, on click, push out the submitting editor,
  // together with its draft not yet accepted
  // 已有发送或改写在途：此刻放行别处的编辑入口，点下去会顶掉正在提交的编辑器，
  // 连同它里面还没被受理的草稿一起消失
  if (context.isSending) return false;
  // Queued messages haven't entered the conversation yet: forking now would leave them in the superseded original session
  // 排队消息还没进入对话：此刻分叉，它们会落在被取代的原会话里
  if (context.hasQueuedMessages) return false;
  return true;
}

/** Display text of a tool result: strings verbatim, anything else expanded as indented JSON. / 工具结果的展示文本：字符串原样，其余按 JSON 缩进展开。 */
export function stringifyResult(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
