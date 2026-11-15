import { createContext, useContext } from "react";

/**
 * uuids of turn-failure entries that arrived while viewing, provided by MessageFlow: only these failure cards are announced; failures already present when the session opened
 * show as usual but aren't announced. Subagent cards use this to judge failures in the sub-timeline; existing failures seen when expanding a card aren't announced.
 *
 * 查看期间新到达的轮次失败条目的 uuid，由 MessageFlow 提供：只为这些失败卡片播报，打开会话时已有的失败
 * 照常显示但不播报。子智能体卡片据此判断子时间线里的失败，展开卡片看到的既有失败不播报。
 */
export const ArrivedFailuresContext = createContext<ReadonlySet<string>>(new Set());

export function useArrivedFailures(): ReadonlySet<string> {
  return useContext(ArrivedFailuresContext);
}
