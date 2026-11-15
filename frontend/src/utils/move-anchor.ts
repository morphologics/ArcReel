/**
 * Anchor conversion for reordering: the backend move API moves an entry after the anchor, or to the front when the anchor is null.
 * Returns undefined when the operation doesn't move.
 *
 * 改序的锚点换算：后端 move 接口把条目移到锚点之后，锚点为 null 时移到最前。
 * 返回 undefined 表示这次操作不移动。
 */

/** Drag item source onto item target and release: dragging down lands after the target, dragging up lands before the target (i.e. after the one before it). / 把第 source 条拖到第 target 条上松手：向下拖落在目标之后，向上拖落在目标之前（即目标前一条之后）。 */
export function dropAnchor<T>(ids: readonly T[], source: number, target: number): T | null | undefined {
  if (source === target || source < 0 || target < 0) return undefined;
  if (source < target) return ids[target];
  return target > 0 ? ids[target - 1] : null;
}

/** Move one position earlier or later: earlier lands after the item two before (moving the second item earlier moves it to the front), later lands after the next item. / 前移或后移一位：前移落到前两条之后（第二条前移即移到最前），后移落到下一条之后。 */
export function stepAnchor<T>(
  ids: readonly T[],
  index: number,
  direction: "earlier" | "later",
): T | null | undefined {
  if (index < 0 || index >= ids.length) return undefined;
  if (direction === "earlier") return index > 0 ? (ids[index - 2] ?? null) : undefined;
  return index < ids.length - 1 ? ids[index + 1] : undefined;
}
