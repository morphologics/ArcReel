/**
 * Session event log projection — entries -> Turn[].
 *
 * Log entries are already shaped at the server write point (tool_result as separate entries, interrupt / task notifications /
 * AskUserQuestion answers as typed entries, subagent entries carrying parent_tool_use_id,
 * stream_event not logged); this module only does render grouping: merge consecutive assistant entries,
 * backfill tool_result by tool_use_id, update tasks in place by task_id; compaction continuation summaries
 * are projected as compact_summary blocks, and inferred final states of anchorless subagents hang on synthesized cards.
 * No content sniffing, no content-comparison dedup, no message synthesis.
 *
 * The projection has two layers:
 * - internal accumulated state (fold): consumes entries one by one, only appending and backfilling by id, never replaying backward;
 *   entry blocks are deep-copied exactly once at this layer.
 * - display view (display): derived per turn from the accumulated state — task folding, deriving completion of lingering subagents,
 *   and mounting subagent sub-timelines are all recomputed here; cached by turn version, only turns touched by new
 *   entries rebuild their view, untouched turns stay reference-stable.
 *
 * `projectEntriesToTurns` is the pure-function entry (a blank projector replaying everything);
 * `createTimelineProjector` is the incremental entry of the same implementation; their results are always identical.
 *
 * 会话事件日志投影 — entries → Turn[]。
 *
 * 日志条目已在服务端写入点定型（tool_result 独立条目、interrupt / task 通知 /
 * AskUserQuestion 答复为 typed 条目、子智能体条目带 parent_tool_use_id、
 * stream_event 不入日志），本模块只做渲染归组：连续 assistant 条目合并、
 * tool_result 按 tool_use_id 回填、task 按 task_id 就地更新；压缩续接摘要
 * 投影为 compact_summary 块，缺锚点子智能体的推断终态挂到合成卡片上。
 * 不做内容嗅探、不做内容比对去重、不合成消息。
 *
 * 投影分两层：
 * - 内部累积态（fold）：逐条消费条目，只做追加与按 id 回填，永不回溯重放；
 *   条目块在此层做唯一一次深拷贝。
 * - 展示视图（display）：由累积态按 turn 派生——task 折叠、滞留子智能体推导
 *   完成、子智能体子时间线挂载都在这层重算；按 turn 版本缓存，只有被新
 *   条目触达的 turn 重建视图，未触达的 turn 保持引用稳定。
 *
 * `projectEntriesToTurns` 是纯函数入口（空投影器全量重放）；
 * `createTimelineProjector` 是同一实现的增量入口，二者结果恒等。
 */

import type {
  ContentBlock,
  DraftDeltaPayload,
  DraftState,
  TimelineEntry,
  Turn,
} from "@/types";

function entryBlocks(entry: TimelineEntry): ContentBlock[] {
  const content = entry.content;
  if (Array.isArray(content)) return content;
  if (typeof content === "string" && content) return [{ type: "text", text: content }];
  return [];
}

function blocksText(blocks: ContentBlock[]): string {
  return blocks
    .filter((b) => b.type === "text")
    .map((b) => b.text ?? "")
    .join("\n");
}

function cloneBlock(block: ContentBlock): ContentBlock {
  return structuredClone(block);
}

/**
 * Merge two sets of log entries by seq (union, ascending, dedup by seq). The log is append-only and entries
 * are immutable by seq, so any source (cold-read full frame / SSE live / send response) can be safely unioned in any arrival order,
 * with no overwrite semantics.
 *
 * 按 seq 合并两组日志条目（并集、升序、seq 去重）。日志 append-only 且条目
 * 按 seq 不可变，任一来源（冷读整帧 / SSE 直播 / 发送响应）先到后到均可安全
 * 并集，不存在覆盖语义。
 */
export function mergeEntriesBySeq(
  existing: TimelineEntry[],
  incoming: TimelineEntry[],
): TimelineEntry[] {
  if (existing.length === 0) return [...incoming].sort((a, b) => a.seq - b.seq);
  if (incoming.length === 0) return existing;
  const bySeq = new Map<number, TimelineEntry>();
  for (const entry of existing) bySeq.set(entry.seq, entry);
  for (const entry of incoming) {
    if (!bySeq.has(entry.seq)) bySeq.set(entry.seq, entry);
  }
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

// ---------------------------------------------------------------------------
// Incremental projector
// ---------------------------------------------------------------------------
// 增量投影器
// ---------------------------------------------------------------------------

/** A turn in the internal accumulated state: content is internal blocks that can be backfilled in place; version is the display cache key. / 内部累积态的 turn：content 为可就地回填的内部块，version 是展示缓存键。 */
interface InternalTurn {
  type: Turn["type"];
  content: ContentBlock[];
  uuid?: string;
  timestamp?: string;
  version: number;
}

/** Registered location of a tool_use block: cross-turn backfill and subagent anchoring both locate by it in O(1). / tool_use 块的登记位置：跨 turn 回填与子智能体锚定都按此 O(1) 定位。 */
interface ToolUseSite {
  fold: Fold;
  turn: InternalTurn;
  block: ContentBlock;
}

/** Fold state of one flat timeline (the main timeline or a subagent group). / 一条平铺时间线（主时间线或某个子智能体组）的折叠状态。 */
interface Fold {
  committed: InternalTurn[];
  cursor: InternalTurn | null;
  /** The owning subagent group (null for the main timeline), used to propagate dirty flags up the anchor chain. / 所属子智能体组（主时间线为 null），脏标记沿锚链向上传播用。 */
  group: SubagentGroup | null;
  version: number;
  displayVersion: number;
  display: Turn[];
}

interface SubagentGroup {
  id: string;
  fold: Fold;
  /** Location of the anchor tool_use; null means not yet anchored (rendered as a synthesized card at top level). / 锚点 tool_use 的位置；null 表示尚未锚定（顶层渲染合成卡片）。 */
  anchor: ToolUseSite | null;
}

export interface TimelineProjector {
  /**
   * Project entries -> Turn[], value-equal to `projectEntriesToTurns(entries)`.
   * entries must follow the event log contract: ascending by seq, entries with the same seq immutable, and between two adjacent
   * calls the set only grows (append-only union). When the prefix is unchanged only the new suffix is folded,
   * otherwise everything is replayed.
   *
   * 投影 entries → Turn[]，结果与 `projectEntriesToTurns(entries)` 逐值相等。
   * entries 须遵循事件日志契约：按 seq 升序、同 seq 条目不可变、相邻两次
   * 调用之间集合只增不减（append-only 并集）。前缀未变时只折叠新增后缀，
   * 否则整体重放。
   */
  project(entries: TimelineEntry[]): Turn[];
  /** Number of entries consumed incrementally (cache size observation, for tests). / 已增量消费的条目数（缓存规模观测，测试用）。 */
  readonly size: number;
}

export function createTimelineProjector(): TimelineProjector {
  // Prefix fingerprint: source is the entries reference from the last project call, count the number of folded entries
  // 前缀指纹：source 为上次 project 的 entries 引用，count 为已折叠条目数
  let source: TimelineEntry[] = [];
  let count = 0;

  let versionCounter = 0;
  let main: Fold = newFold();
  let groups = new Map<string, SubagentGroup>();
  // Groups not yet anchored: compose() only needs to scan this set rather than all historical groups, so in long sessions
  // subagent groups already anchored don't drag the synthesized-card scan for every new message, as the session grows, to
  // O(total historical subagents).
  // 尚未锚定的组：compose() 只需扫这个集合而非全部历史 groups，避免长会话
  // 里已锚定完成的子智能体组随会话增长把每条新消息的合成卡片扫描拖成
  // O(历史子智能体总数)。
  let pendingGroups = new Set<SubagentGroup>();
  // Inferred description and final state of anchorless subagents (inferred at the write point from the sub-timeline), shown by synthesized cards.
  // 缺锚点子智能体的推断描述与终态（写入点按子时间线推断），合成卡片据此显示。
  let subagentOutcomes = new Map<string, ContentBlock>();
  let toolUseSites = new Map<string, ToolUseSite>();
  let turnViewCache = new WeakMap<InternalTurn, { version: number; turn: Turn }>();
  let composed: Turn[] = [];
  let composedDirty = true;

  function newFold(): Fold {
    return { committed: [], cursor: null, group: null, version: 0, displayVersion: -1, display: [] };
  }

  function reset(): void {
    versionCounter = 0;
    main = newFold();
    groups = new Map();
    pendingGroups = new Set();
    subagentOutcomes = new Map();
    toolUseSites = new Map();
    turnViewCache = new WeakMap();
    composed = [];
    composedDirty = true;
    count = 0;
  }

  /**
   * Whether the anchor chain of site's timeline (walking up existing anchor ownership) loops back to targetFold.
   * Used as a pre-anchoring check: in real data subagent nesting is an acyclic DAG (parent_tool_use_id
   * always points at an earlier, still-open ancestor call); cycles can only come from malformed/self-referencing entries
   * (e.g. parent_tool_use_id equal to its own tool_use id). Once anchoring forms a cycle, that group's
   * display views depend on each other and can never be reached from the main timeline, so the whole timeline gets
   * silently dropped empty by compose() — not anchoring and falling back to the existing "synthesized card" is safer than dropping it.
   *
   * site 所在时间线的锚链（沿既有 anchor 归属向上走）是否会绕回 targetFold。
   * 用于锚定前判定：真实数据里子智能体嵌套是无环 DAG（parent_tool_use_id
   * 恒指向更早、仍开放的祖先调用），环只可能来自畸形/自指条目
   * （如 parent_tool_use_id 等于自身 tool_use id）。一旦锚定成环，该组的
   * 展示视图会互相依赖、永远无法从主时间线触达，导致整段时间线被
   * compose() 静默丢空——不锚定、退回既有的"合成卡片"兜底比丢空更安全。
   */
  function anchorCreatesCycle(anchorFold: Fold, targetFold: Fold): boolean {
    const seen = new Set<Fold>();
    let current: Fold | null = anchorFold;
    while (current) {
      if (current === targetFold) return true;
      if (seen.has(current)) return false;
      seen.add(current);
      current = current.group?.anchor?.fold ?? null;
    }
    return false;
  }

  /** After an entry touches a turn: invalidate its display cache, and invalidate ancestor turns level by level along the subagent anchor chain. / 条目触达 turn 后：失效其展示缓存，并沿子智能体锚链逐层失效祖先 turn。 */
  function touch(fold: Fold, turn: InternalTurn): void {
    composedDirty = true;
    turn.version = ++versionCounter;
    const seen = new Set<Fold>();
    let current: Fold | null = fold;
    while (current && !seen.has(current)) {
      seen.add(current);
      current.version = ++versionCounter;
      const anchor: ToolUseSite | null = current.group?.anchor ?? null;
      if (!anchor) break;
      anchor.turn.version = ++versionCounter;
      current = anchor.fold;
    }
  }

  function startTurn(fold: Fold, type: Turn["type"], content: ContentBlock[], entry: TimelineEntry): InternalTurn {
    if (fold.cursor) fold.committed.push(fold.cursor);
    const turn: InternalTurn = { type, content, uuid: entry.uuid, timestamp: entry.timestamp, version: 0 };
    fold.cursor = turn;
    touch(fold, turn);
    return turn;
  }

  /** Append a system-injected block: merge into the current assistant turn if possible, otherwise open a system turn. / 追加系统注入块：优先并入当前 assistant turn，否则开 system turn。 */
  function attachSystemBlock(fold: Fold, entry: TimelineEntry, block: ContentBlock): void {
    if (fold.cursor && fold.cursor.type === "assistant") {
      fold.cursor.content.push(block);
      touch(fold, fold.cursor);
      return;
    }
    startTurn(fold, "system", [block], entry);
  }

  function findTaskBlock(content: ContentBlock[], taskId: string): ContentBlock | null {
    for (const block of content) {
      if (block.type === "task_progress" && block.task_id === taskId) return block;
    }
    return null;
  }

  /**
   * Locate an existing task block by task_id: check the current turn first (the common path), and on miss fall back to
   * flushed committed turns (newest first) — task updates arriving after a turn boundary
   * must still update the original block in place, otherwise it stays forever in the unfinished task_started state. Returns
   * the turn holding the hit block, so the caller touches the right cache invalidation range (possibly not
   * fold.cursor).
   *
   * 按 task_id 定位既有 task 块：先查当前 turn（常见路径），未命中回退
   * 已 flush 的 committed turns（新近优先）——turn 边界之后到达的 task 更新
   * 仍要归属原块就地更新，否则原块永久停在 task_started 未完成态。返回
   * 命中块所在的 turn，供调用方 touch 到正确的缓存失效范围（可能不是
   * fold.cursor）。
   */
  function findTaskSite(fold: Fold, taskId: string): { turn: InternalTurn; block: ContentBlock } | null {
    if (fold.cursor) {
      const found = findTaskBlock(fold.cursor.content, taskId);
      if (found) return { turn: fold.cursor, block: found };
    }
    for (let i = fold.committed.length - 1; i >= 0; i -= 1) {
      const turn = fold.committed[i];
      const found = findTaskBlock(turn.content, taskId);
      if (found) return { turn, block: found };
    }
    return null;
  }

  function applyTaskBlock(fold: Fold, entry: TimelineEntry, taskBlock: ContentBlock, updateOnly: boolean): void {
    const taskId = taskBlock.task_id;
    if (taskId && updateOnly) {
      const site = findTaskSite(fold, taskId);
      if (site) {
        const existing = site.block;
        existing.status = taskBlock.status;
        if (taskBlock.summary) existing.summary = taskBlock.summary;
        if (taskBlock.task_status) existing.task_status = taskBlock.task_status;
        if (taskBlock.usage) existing.usage = taskBlock.usage;
        touch(fold, site.turn);
        return;
      }
    }
    attachSystemBlock(fold, entry, taskBlock);
  }

  /** Register a tool_use block's location (first registration of an id wins), and anchor waiting subagent groups. / 登记 tool_use 块位置（同 id 首次登记生效），并锚定等待中的子智能体组。 */
  function registerToolUse(fold: Fold, turn: InternalTurn, block: ContentBlock): void {
    const id = block.id;
    if (!id) return;
    if (!toolUseSites.has(id)) toolUseSites.set(id, { fold, turn, block });
    const group = groups.get(id);
    if (group && !group.anchor) {
      const site = toolUseSites.get(id);
      if (site && !anchorCreatesCycle(site.fold, group.fold)) {
        group.anchor = site;
        pendingGroups.delete(group);
        // The synthesized card merges back into its anchor: the anchor turn's display needs rebuilding, and the top-level synthesized card disappears
        // 合成卡片并回锚点：锚点 turn 展示需重建，顶层合成卡片消失
        touch(site.fold, site.turn);
      }
    }
  }

  /** Get the sub-timeline fold state for parent_tool_use_id, attempting anchoring when the group first appears. / 取 parent_tool_use_id 对应的子时间线折叠状态，组首现时尝试锚定。 */
  function groupFoldFor(parentId: string): Fold {
    let group = groups.get(parentId);
    if (!group) {
      const fold = newFold();
      group = { id: parentId, fold, anchor: null };
      fold.group = group;
      groups.set(parentId, group);
      const site = toolUseSites.get(parentId);
      if (site && !anchorCreatesCycle(site.fold, fold)) {
        group.anchor = site;
        touch(site.fold, site.turn);
      } else {
        pendingGroups.add(group);
      }
      composedDirty = true;
    }
    return group.fold;
  }

  function appendOne(entry: TimelineEntry): void {
    const fold = entry.parent_tool_use_id ? groupFoldFor(entry.parent_tool_use_id) : main;

    if (entry.type === "assistant") {
      const blocks = entryBlocks(entry).map(cloneBlock);
      let turn: InternalTurn;
      if (fold.cursor && fold.cursor.type === "assistant") {
        fold.cursor.content.push(...blocks);
        turn = fold.cursor;
        touch(fold, turn);
      } else {
        turn = startTurn(fold, "assistant", blocks, entry);
      }
      for (const block of blocks) {
        if (block.type === "tool_use") registerToolUse(fold, turn, block);
      }
      return;
    }

    if (entry.type === "tool_result") {
      const resultText = typeof entry.content === "string" ? entry.content : blocksText(entryBlocks(entry));
      // The backfill anchor is located in O(1) by the registration index, restricted to the same timeline: a tool_result arriving after a turn boundary
      // (e.g. an interrupt flushed that turn first) must still backfill the matching
      // tool_use, otherwise its result status hangs forever in the unfinished state (same rule as question_answer
      // backfill).
      // 回填锚点按登记索引 O(1) 定位，限定同一时间线：turn 边界之后到达的
      // tool_result（如中断先把该 turn flush 出去）仍要能回填对应
      // tool_use，否则其结果状态永久悬挂在未完成态（与 question_answer
      // 回填口径一致）。
      let site: ToolUseSite | null = null;
      if (entry.tool_use_id) {
        const hit = toolUseSites.get(entry.tool_use_id);
        if (hit && hit.fold === fold) site = hit;
      }
      if (site) {
        site.block.result = resultText;
        site.block.is_error = Boolean(entry.is_error);
        touch(site.fold, site.turn);
      } else {
        attachSystemBlock(fold, entry, {
          type: "tool_result",
          tool_use_id: entry.tool_use_id ?? undefined,
          content: resultText,
          is_error: Boolean(entry.is_error),
        });
      }
      return;
    }

    if (entry.type === "system") {
      if (entry.subtype === "agent_turn_failure" && entry.failure) {
        startTurn(
          fold,
          "system",
          [{ type: "agent_failure", failure: structuredClone(entry.failure) }],
          entry,
        );
        return;
      }
      if (entry.subtype === "skill_invocation") {
        // The chip's render anchor is the Skill tool_use block (input is the name and arguments); when the tool_use block
        // is already registered, don't append again, to avoid two chips for one call. Look up via toolUseSites
        // rather than only fold.cursor — registration happens when the tool_use block itself is appended, which is more
        // durable than cursor; if that turn was flushed out of the current turn early by entries like interrupt,
        // it is still correctly judged as anchored (restricted to the same timeline, same rule as question_answer
        // backfill).
        // 芯片渲染锚点是 Skill tool_use 块（input 即名与入参）；tool_use 块
        // 已登记时不再追加，避免同一调用出现两枚芯片。按 toolUseSites 查找
        // 而非只看 fold.cursor——登记发生在 tool_use 块本身追加时，比
        // cursor 更持久，该 turn 若被 interrupt 等条目提前 flush 出当前
        // turn，仍能正确判定为已锚定（限定同一时间线，与 question_answer
        // 回填口径一致）。
        const site = entry.tool_use_id != null ? toolUseSites.get(entry.tool_use_id) : null;
        const anchored = site != null && site.fold === fold;
        if (!anchored) {
          attachSystemBlock(fold, entry, {
            type: "skill_invocation",
            skill_name: entry.skill_name ?? undefined,
            skill_args: entry.skill_args ?? undefined,
            tool_use_id: entry.tool_use_id ?? undefined,
          });
        }
        return;
      }
      if (entry.subtype === "interrupt") {
        startTurn(fold, "system", [{ type: "interrupt_notice" }], entry);
        return;
      }
      if (entry.subtype === "subagent_outcome") {
        if (entry.tool_use_id) {
          subagentOutcomes.set(entry.tool_use_id, {
            type: "task_progress",
            status: "task_notification",
            description: entry.description ?? "",
            summary: entry.summary ?? undefined,
            task_status: entry.task_status ?? undefined,
            tool_use_id: entry.tool_use_id,
          });
          composedDirty = true;
        }
        return;
      }
      if (entry.subtype !== "task_started" && entry.subtype !== "task_progress" && entry.subtype !== "task_notification") {
        return;
      }
      applyTaskBlock(
        fold,
        entry,
        {
          type: "task_progress",
          task_id: entry.task_id ?? undefined,
          status: entry.subtype,
          description: entry.description ?? "",
          summary: entry.summary ?? undefined,
          task_status: entry.task_status ?? undefined,
          usage: entry.usage ?? undefined,
          tool_use_id: entry.tool_use_id ?? undefined,
        },
        entry.subtype !== "task_started",
      );
      return;
    }

    // entry.type === "user"
    if (entry.subtype === "compact_summary") {
      startTurn(fold, "system", [{ type: "compact_summary", text: blocksText(entryBlocks(entry)) }], entry);
      return;
    }
    if (entry.subtype === "question_answer") {
      const resultText = typeof entry.content === "string" ? entry.content : blocksText(entryBlocks(entry));
      const answers = entry.answers ?? undefined;
      // Backfill the AskUserQuestion tool_use block that asked: the result status doesn't hang, and the question card can mark the choice.
      // Located in O(1) by the registration index, restricted to the same timeline (if an entry like interrupt flushes the turn early
      // during a pending question, the anchor must still be found across turns).
      // 回填提问的 AskUserQuestion tool_use 块：结果状态不悬挂，问题卡可标记所选。
      // 按登记索引 O(1) 定位，限定同一时间线（pending 提问期间若有 interrupt 之类
      // 的条目提前把该 turn flush 出去，锚点仍要能跨 turn 找回）。
      let site: ToolUseSite | null = null;
      if (entry.tool_use_id) {
        const hit = toolUseSites.get(entry.tool_use_id);
        if (hit && hit.fold === fold) site = hit;
      }
      if (site) {
        site.block.result = resultText;
        site.block.is_error = Boolean(entry.is_error);
        if (answers) site.block.answers = { ...answers };
        touch(site.fold, site.turn);
      }
      if (entry.is_error) {
        // Rejected/interrupted questions have no user answer; with nowhere to backfill, keep the orphan result block
        // 被拒/中断的提问没有用户答复；无处回填时保留孤立结果块
        if (!site) {
          attachSystemBlock(fold, entry, {
            type: "tool_result",
            tool_use_id: entry.tool_use_id ?? undefined,
            content: resultText,
            is_error: true,
          });
        }
        return;
      }
      startTurn(fold, "user", [{ type: "question_answer", answers, text: resultText }], entry);
      return;
    }

    startTurn(fold, "user", entryBlocks(entry).map(cloneBlock), entry);
  }

  // -------------------------------------------------------------------------
  // Display view — derived from the accumulated state, cached by turn version
  // -------------------------------------------------------------------------
  // 展示视图 — 由累积态派生，按 turn 版本缓存
  // -------------------------------------------------------------------------

  function foldDisplay(fold: Fold): Turn[] {
    if (fold.displayVersion === fold.version) return fold.display;
    const internal = fold.cursor ? [...fold.committed, fold.cursor] : [...fold.committed];
    fold.display = internal.map(turnView);
    fold.displayVersion = fold.version;
    return fold.display;
  }

  function turnView(turn: InternalTurn): Turn {
    const cached = turnViewCache.get(turn);
    if (cached && cached.version === turn.version) return cached.turn;
    const built = buildTurnView(turn);
    turnViewCache.set(turn, { version: turn.version, turn: built });
    return built;
  }

  /**
   * Build one turn's display view: mount subagent sub-timelines, derive lingering task_started of completed `Agent` calls
   * as completed, and fold task progress into the anchor tool_use (subagent
   * cards show status and progress in place without separate progress rows; anchorless task blocks stay as-is).
   * Mutable blocks (tool_use / task_progress) are shallow-copied each time, so the accumulated state isn't polluted by the display layer,
   * and repeated builds of the same version converge to the same result.
   *
   * 构建单个 turn 的展示视图：挂载子智能体子时间线、把已完成 `Agent` 调用
   * 的滞留 task_started 推导为完成、将 task 进度折叠进锚点 tool_use（子智能体
   * 卡片就地显示状态与进度，不渲染独立进度行；无锚点的 task 块保持原样）。
   * 可变块（tool_use / task_progress）逐次浅拷，累积态不被展示层污染，
   * 同一版本重复构建收敛到同一结果。
   */
  function buildTurnView(turn: InternalTurn): Turn {
    const content: ContentBlock[] = [];
    let toolUseById: Map<string, ContentBlock> | null = null;
    let completedAgentIds: Set<string> | null = null;
    for (const block of turn.content) {
      if (block.type === "tool_use") {
        const view = { ...block };
        if (block.id) {
          const group = groups.get(block.id);
          if (group && group.anchor?.block === block) view.sub_turns = foldDisplay(group.fold);
          (toolUseById ??= new Map()).set(block.id, view);
          if (view.name === "Agent" && view.result !== undefined) (completedAgentIds ??= new Set()).add(block.id);
        }
        content.push(view);
      } else if (block.type === "task_progress") {
        content.push({ ...block });
      } else {
        content.push(block);
      }
    }
    // When the Agent tool_use matching a task_started block already has a result, derive it as completed
    // task_started 块对应的 Agent tool_use 已有 result 时推导为已完成
    if (completedAgentIds) {
      const completed = completedAgentIds;
      for (const block of content) {
        if (
          block.type === "task_progress" &&
          block.status === "task_started" &&
          block.tool_use_id &&
          completed.has(block.tool_use_id)
        ) {
          block.status = "task_notification";
          block.task_status = "completed";
        }
      }
    }
    let finalContent = content;
    if (toolUseById) {
      const anchors = toolUseById;
      finalContent = content.filter((block) => {
        if (block.type !== "task_progress" || !block.tool_use_id) return true;
        const anchor = anchors.get(block.tool_use_id);
        if (!anchor) return true;
        anchor.task_info = block;
        return false;
      });
    }
    return { type: turn.type, content: finalContent, uuid: turn.uuid, timestamp: turn.timestamp };
  }

  function compose(): Turn[] {
    if (!composedDirty) return composed;
    const out = [...foldDisplay(main)];
    // Groups still without an anchor (e.g. leftover lazily created groups, groups refused anchoring for forming a cycle): form their own card with a synthesized anchor,
    // without losing the sub-timeline. Scan only pendingGroups (groups currently unanchored), not all historical
    // groups — anchored groups never revert to pending, so the scan doesn't grow with the total subagents in the session.
    // 仍无锚点的组（如懒生成残余组、成环被拒锚的组）：以合成锚点独立成卡，
    // 不丢子时间线。只扫 pendingGroups（当前仍未锚定的组），不扫全部历史
    // groups——已锚定的组不会再变回待锚定，扫描量不随会话内子智能体总数增长。
    for (const group of pendingGroups) {
      const card: ContentBlock = { type: "tool_use", id: group.id, name: "Agent", input: {}, sub_turns: foldDisplay(group.fold) };
      const outcome = subagentOutcomes.get(group.id);
      if (outcome) card.task_info = { ...outcome };
      out.push({ type: "system", content: [card], uuid: `subagent-${group.id}` });
    }
    composed = out;
    composedDirty = false;
    return out;
  }

  return {
    project(entries: TimelineEntry[]): Turn[] {
      // Prefix invariance is judged by endpoint reference: the log contract guarantees entries are immutable by seq and the set only grows,
      // so the same endpoint reference means the prefix is element-wise the same reference (an insertion in the middle necessarily shifts the endpoint index).
      // 前缀不变性按端点引用判定：日志契约保证条目按 seq 不可变、集合只增
      // 不减，端点同引用即前缀逐元素同引用（中部插入必然移动端点索引）。
      const extendsPrefix =
        entries.length >= count &&
        (count === 0 || (entries[0] === source[0] && entries[count - 1] === source[count - 1]));
      if (!extendsPrefix) reset();
      for (let i = count; i < entries.length; i++) appendOne(entries[i]);
      source = entries;
      count = entries.length;
      return compose();
    },
    get size(): number {
      return count;
    },
  };
}

/**
 * Full projection pure-function entry (entries -> Turn[]): a blank projector replays once.
 * The incremental path `createTimelineProjector` always yields the same result as this function.
 *
 * 全量投影纯函数入口（entries → Turn[]）：空投影器一次性重放。
 * 增量路径 `createTimelineProjector` 与本函数结果恒等。
 */
export function projectEntriesToTurns(entries: TimelineEntry[]): Turn[] {
  return createTimelineProjector().project(entries);
}

// ---------------------------------------------------------------------------
// Draft projection and incremental application
// ---------------------------------------------------------------------------
// draft 投影与增量应用
// ---------------------------------------------------------------------------

/** Collect the message_id set of persisted authoritative assistant entries (a derived index for judging draft replacement). / 收集已落权威 assistant 条目的 message_id 集合（draft 替换判定的派生索引）。 */
export function collectCommittedMessageIds(entries: TimelineEntry[]): Set<string> {
  const ids = new Set<string>();
  for (const entry of entries) {
    if (entry.type === "assistant" && entry.message_id != null) ids.add(entry.message_id);
  }
  return ids;
}

/**
 * Whether the draft has been replaced by an authoritative entry: compared by identity (an assistant entry with the same message_id
 * being persisted counts as replacement), no content comparison. The set is maintained incrementally by the caller; the check is O(1).
 *
 * draft 是否已被权威条目替换：按身份比对（同 message_id 的 assistant 条目
 * 已落库即视为替换），不做内容比对。集合由调用方增量维护，判定 O(1)。
 */
export function isDraftReplaced(
  draft: DraftState | null,
  committedMessageIds: ReadonlySet<string>,
): boolean {
  if (!draft || !draft.message_id) return false;
  return committedMessageIds.has(draft.message_id);
}

/**
 * draft -> Turn. Returns null when replaced is true (already replaced by an authoritative entry);
 * subagent streaming drafts don't enter the main timeline: the main line only shows the collapsed card,
 * whose content updates with authoritative entries (near real time).
 *
 * draft → Turn。replaced 为真（已被权威条目替换）时返回 null；
 * 子智能体的流式草稿不进主时间线：主线只显示折叠卡片，
 * 卡片内容随权威条目（近实时）更新。
 */
export function buildDraftTurn(draft: DraftState | null, replaced: boolean): Turn | null {
  if (!draft || !draft.message_id) return null;
  if (draft.parent_tool_use_id) return null;
  if (replaced) return null;
  const content = (draft.content ?? []).filter(Boolean);
  if (content.length === 0) return null;
  return {
    type: "assistant",
    content,
    uuid: `draft-${draft.message_id}`,
  };
}

/** Full draft -> Turn entry: derives the replacement check from entries (use buildDraftTurn on hot paths). / draft → Turn 全量入口：从 entries 派生替换判定（热路径请用 buildDraftTurn）。 */
export function projectDraftToTurn(
  draft: DraftState | null,
  entries: TimelineEntry[],
): Turn | null {
  return buildDraftTurn(draft, isDraftReplaced(draft, collectCommittedMessageIds(entries)));
}

/** Client draft mirror: content is indexed by block_index, toolJson accumulates unclosed JSON. / 客户端 draft 镜像：content 以 block_index 为数组下标，toolJson 累积未闭合 JSON。 */
export interface DraftMirror extends DraftState {
  toolJson: Record<number, string>;
}

/**
 * Apply one delta (pure function, returns a new object). Callers must filter by the rev threshold first;
 * when message_id changes start a new draft (an identity switch means the previous one was closed by an authoritative entry).
 *
 * 应用一条 delta（纯函数，返回新对象）。调用方须先按 rev 门槛过滤；
 * message_id 变化时另起新 draft（身份切换即上一条已由权威条目收尾）。
 */
export function applyDraftDelta(
  draft: DraftMirror | null,
  payload: DraftDeltaPayload,
): DraftMirror {
  const base: DraftMirror =
    draft && draft.message_id === payload.message_id
      ? { ...draft, content: [...draft.content], toolJson: { ...draft.toolJson } }
      : {
          message_id: payload.message_id,
          parent_tool_use_id: payload.parent_tool_use_id ?? null,
          content: [],
          toolJson: {},
        };
  const index = payload.block_index;

  if (payload.delta_type === "block_start") {
    base.content[index] = payload.block ? cloneBlock(payload.block) : { type: "text", text: "" };
    return base;
  }

  if (payload.delta_type === "text_delta") {
    const block = base.content[index]?.type === "text" ? { ...base.content[index] } : { type: "text" as const, text: "" };
    block.text = `${block.text ?? ""}${payload.text ?? ""}`;
    base.content[index] = block;
    return base;
  }

  if (payload.delta_type === "thinking_delta") {
    const block =
      base.content[index]?.type === "thinking" ? { ...base.content[index] } : { type: "thinking" as const, thinking: "" };
    block.thinking = `${block.thinking ?? ""}${payload.thinking ?? ""}`;
    base.content[index] = block;
    return base;
  }

  if (payload.delta_type === "input_json_delta") {
    const block =
      base.content[index]?.type === "tool_use"
        ? { ...base.content[index] }
        : { type: "tool_use" as const, id: undefined, name: "", input: {} };
    const updated = `${base.toolJson[index] ?? ""}${payload.partial_json ?? ""}`;
    base.toolJson[index] = updated;
    try {
      const parsed: unknown = JSON.parse(updated);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        block.input = parsed as Record<string, unknown>;
      }
    } catch {
      // Unclosed JSON: keep accumulating / 未闭合 JSON：继续累积
    }
    base.content[index] = block;
    return base;
  }

  return base;
}
