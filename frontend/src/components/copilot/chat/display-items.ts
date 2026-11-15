import type { ContentBlock, Turn } from "@/types";
import { composeAllTurns } from "./utils";

// ---------------------------------------------------------------------------
// Display layer of the message area: turns the projected turn list into display items rendered one by one.
// The projection splits by SDK message, so one reply is often split into several assistant turns, many holding only signature-only empty thinking blocks;
// the streaming draft is a separate one too. The display layer merges consecutive assistant turns between two user messages into one round;
// work and copy operate per round, and turns and blocks with no visible content are skipped, with no empty items or extra spacing rendered for them.
// ---------------------------------------------------------------------------
// 消息区的显示层：把投影出的 turn 列表整理成逐条渲染的显示项。
//
// 投影按 SDK 消息切分，一次回复常被拆成多条 assistant turn，其中不少只含签名的空思考块；
// 流式草稿也是单独的一条。显示层把两条用户消息之间连续的 assistant turn 合成一轮，
// 工序与复制都以整轮为单位，并跳过没有可见内容的 turn 与块，不为它们渲染空项与多余间距。
// ---------------------------------------------------------------------------

export interface DisplayItem {
  /** Identity for rendering and scroll positioning. A reply round's identity comes from the item before it, unchanged before and after the streaming draft is persisted. / 渲染与滚动定位用的身份。一轮回复的身份取自它前面那一项，流式草稿落库前后保持不变。 */
  key: string;
  /** The merged turn: a round's content blocks concatenated in time order, timestamp taken from the last segment. / 合并后的 turn：一轮回复的内容块按时间顺序拼接，时间取最后一段。 */
  turn: Turn;
  /** This item contains the streaming draft; its last block is still being generated. / 这一项包含流式草稿，末尾块仍在生成。 */
  streaming: boolean;
}

export function buildDisplayItems(turns: Turn[], draftTurn: Turn | null): DisplayItem[] {
  const items: DisplayItem[] = [];
  composeAllTurns(turns, draftTurn).forEach((composed, index) => {
    const streaming = composed === draftTurn;
    if (!streaming && !hasVisibleContent(composed)) return;
    // Persisted replies drop invisible blocks: a signature-only empty thinking block left in would form its own empty work segment, adding an extra gap
    // 已落库的回复去掉看不见的块：签名空思考块留在里面会自成一段空工序，多出一道段距
    const turn = composed.type === "assistant" && !streaming
      ? { ...composed, content: composed.content.filter(isVisibleBlock) }
      : composed;

    const previous = items.at(-1);
    if (turn.type === "assistant" && previous?.turn.type === "assistant") {
      previous.turn = {
        ...previous.turn,
        content: [...previous.turn.content, ...turn.content],
        timestamp: turn.timestamp ?? previous.turn.timestamp,
      };
      previous.streaming ||= streaming;
      return;
    }

    // A reply round follows a user message or system event and is named after the preceding item as anchor: after the draft is persisted the first turn switches to
    // the entry uuid; if the identity changed with it, the whole round would remount and expanded work rows would collapse
    // 一轮回复跟在用户消息或系统事件之后，以前一项为锚命名：草稿落库后首条 turn 换成
    // 条目 uuid，身份若跟着变，整轮会重新挂载，展开中的工序随之收起
    const key = turn.type === "assistant"
      ? `${previous?.key ?? "start"}:reply`
      : (turn.uuid ?? `turn-${index}`);
    items.push({ key, turn, streaming });
  });
  return items;
}

function hasVisibleContent(turn: Turn): boolean {
  return (turn.content ?? []).some(isVisibleBlock);
}

function isVisibleBlock(block: ContentBlock): boolean {
  switch (block.type) {
    case "text":
      return Boolean(block.text?.trim());
    case "thinking":
      return Boolean(block.thinking?.trim());
    case "image":
      return Boolean(block.source?.data);
    case "agent_failure":
      return Boolean(block.failure);
    default:
      return true;
  }
}
