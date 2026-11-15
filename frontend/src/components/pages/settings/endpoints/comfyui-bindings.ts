import {
  COMFYUI_IMAGE_BINDING_KEYS,
  COMFYUI_REQUIRED_BINDING_KEYS,
  COMFYUI_VIDEO_BINDING_KEYS,
  type ComfyuiBindingCandidate,
  type ComfyuiBindingKey,
  type ComfyuiBindingState,
  type ComfyuiBindingTarget,
  type ComfyuiBindings,
  type ComfyuiEndpointDefinition,
  type ComfyuiInferResponse,
  type ComfyuiMatchOrigin,
  type ComfyuiMediaType,
  type ComfyuiWorkflowNode,
} from "@/types";

// Read/write helpers for the binding editor: read hand-pickable landing points from the workflow, combine inference results and the user's choices into one set of node bindings to save,
// and compute the reasons for the save gate. Validation is solely the server's job; this only reproduces the few hard gates it would reject,
// so the user sees the reason before clicking save.
// 绑定编辑器的读写辅助：从 workflow 读出可手选的落点，把推断结果与用户的选择合成一份待保存的
// 节点绑定，并算出保存门控的原因。校验的唯一职责在服务端，这里只复现它会拒绝的那几条硬闸门，
// 让用户在点保存之前就看到原因。

/** Semantic key order is the binding table's row order; image endpoints lack the four for first/last frame and timeline. / 语义键顺序即绑定表的行顺序；图像端点没有首尾帧与时间轴那四个。 */
export function bindingKeysFor(mediaType: ComfyuiMediaType): readonly ComfyuiBindingKey[] {
  return mediaType === "image" ? COMFYUI_IMAGE_BINDING_KEYS : COMFYUI_VIDEO_BINDING_KEYS;
}

export function isRequiredBindingKey(key: ComfyuiBindingKey): boolean {
  return COMFYUI_REQUIRED_BINDING_KEYS.includes(key);
}

/** Reference images are an ordered list: list order is the reference image index, and checking or unchecking must reorder accordingly. / 参考图是一条有序列表：列表次序即参考图序号，勾选与取消都要按次序重排。 */
export function isListBindingKey(key: ComfyuiBindingKey): boolean {
  return key === "reference_images";
}

/**
 * These keys can have multiple landing points, and order carries no meaning: two consumers in one workflow each have their own width/height, likewise frame count;
 * `fps` allows multiple read-only landing points (validation only forbids their values conflicting). The construction layer writes width/height/frame count one by one and reads frame rate one by one,
 * so clicking a candidate in the editor can only add or remove that one, not push out other landing points of the same key; once pushed out, those branches
 * would never receive size and frame count again.
 *
 * 这些键可以有多个落点，且次序不带含义：一份 workflow 里两路消费者各有自己的宽高，帧数同理；
 * `fps` 允许多个只读落点（校验只禁止它们的值互相冲突）。构造层写宽高帧数时逐个写、读帧率时逐个
 * 读，因此编辑器里点中一个候选只能增删这一条，不能把同键的其余落点顶掉——顶掉之后那几路分支
 * 就再也收不到尺寸与帧数了。
 */
export function isMultiTargetBindingKey(key: ComfyuiBindingKey): boolean {
  return key === "width" || key === "height" || key === "frames" || key === "fps";
}

/** Check/uncheck for unordered multi-landing-point keys: remove if present, append if absent, leave other landing points as-is. / 无序多落点键的勾选与取消：在场即移除，不在场即追加，其余落点原样留着。 */
export function toggleSetTarget(
  current: ComfyuiBindingTarget[],
  target: ComfyuiBindingTarget,
): ComfyuiBindingTarget[] {
  return current.some((entry) => sameTarget(entry, target))
    ? current.filter((entry) => !sameTarget(entry, target))
    : [...current, target];
}

/** State of a row in the binding table. `required_unbound` is derived from a required key having no landing point; not a server inference state. / 一行在绑定表里的状态。`required_unbound` 由必填键没有落点派生，不是服务端的推断状态。 */
export type ComfyuiRowStatus =
  | "auto"
  | "manual"
  | "ambiguous"
  | "not_found"
  | "unsupported"
  | "required_unbound";

// ---------------------------------------------------------------------------
// workflow
// ---------------------------------------------------------------------------

export interface ComfyuiNodeEntry {
  id: string;
  classType: string;
  title: string;
  inputs: Record<string, unknown>;
}

/** Node ids in ComfyUI are numeric strings, sorted by value; non-numeric ones come after in lexical order. / 节点 id 在 ComfyUI 里是数字串，按数值排序；非数字的排在其后按字典序。 */
function nodeOrder(id: string): [number, string] {
  const digits = /^(\d+)/.exec(id);
  return digits ? [Number(digits[1]), id] : [Number.MAX_SAFE_INTEGER, id];
}

function compareNodeIds(a: string, b: string): number {
  const [an, as] = nodeOrder(a);
  const [bn, bs] = nodeOrder(b);
  return an === bn ? as.localeCompare(bs) : an - bn;
}

function asNode(value: unknown): ComfyuiWorkflowNode | null {
  if (typeof value !== "object" || value === null) return null;
  const node = value as ComfyuiWorkflowNode;
  return typeof node.class_type === "string" ? node : null;
}

/** Reads all nodes of the workflow, ascending by node id; the binding table and manual-pick dropdown share this order. / 读出 workflow 的全部节点，按节点 id 升序——绑定表与手选下拉共用这一个顺序。 */
export function workflowNodes(workflow: Record<string, unknown>): ComfyuiNodeEntry[] {
  const entries: ComfyuiNodeEntry[] = [];
  for (const [id, raw] of Object.entries(workflow)) {
    const node = asNode(raw);
    if (!node) continue;
    entries.push({
      id,
      classType: node.class_type,
      title: typeof node._meta?.title === "string" ? node._meta.title : "",
      inputs: node.inputs ?? {},
    });
  }
  return entries.sort((a, b) => compareNodeIds(a.id, b.id));
}

/** How many times each `class_type` appears in the workflow, descending by count, ties by name. / 每个 `class_type` 在 workflow 里出现了几次，按出现次数降序、同次数按名称排序。 */
export function classTypeCounts(nodes: ComfyuiNodeEntry[]): { classType: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const node of nodes) counts.set(node.classType, (counts.get(node.classType) ?? 0) + 1);
  return [...counts]
    .map(([classType, count]) => ({ classType, count }))
    .sort((a, b) => (b.count === a.count ? a.classType.localeCompare(b.classType) : b.count - a.count));
}

/**
 * Whether an input is a link. ComfyUI links look like `[upstream node id, output index]`; `{"__value__": [...]}`
 * is the ecosystem's wrapper for array literals, a literal array the author deliberately declared, so it can be bound.
 *
 * Check the wrapper before the shape, same order as the server's `workflow.is_link`: unwrapping first would judge
 * `{"__value__": ["4", 0]}` as a link, and an input that can really take a value would vanish from the manual-pick dropdown.
 *
 * 一个输入是不是连线。ComfyUI 的连线形如 `[上游节点 id, 输出序号]`；`{"__value__": [...]}`
 * 是生态里给数组字面值加的包装，作者刻意声明的字面数组，因此绑得上去。
 *
 * 先看包装再判形状，与服务端的 `workflow.is_link` 同序：反过来先解包会把
 * `{"__value__": ["4", 0]}` 判成连线，于是一个真能填值的输入在手选下拉里不见了。
 */
export function isLinkInput(value: unknown): boolean {
  if (isWrappedValue(value)) return false;
  return Array.isArray(value) && value.length === 2 && typeof value[1] === "number";
}

function isWrappedValue(value: unknown): value is { __value__: unknown } {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.keys(value).length === 1 &&
    "__value__" in value
  );
}

function unwrapValue(value: unknown): unknown {
  return isWrappedValue(value) ? value.__value__ : value;
}

/** Current literal value of the input, for the manual-pick dropdown; links and absent inputs give no value. / 该输入当前的字面值，供手选下拉展示；连线与缺席都得不到值。 */
export function literalInputText(node: ComfyuiNodeEntry, input: string): string {
  const value = unwrapValue(node.inputs[input]);
  if (value === undefined) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}

/**
 * Full set of hand-pickable landing points. The output is node-level (no `input`); other semantic keys can only land on literal-value inputs;
 * filling a value into a link would be overwritten by upstream, and the server validator rejects it outright.
 *
 * 可手选的落点全集。产物是节点级的（没有 `input`），其余语义键只能落在字面值输入上——
 * 往连线上填值会被上游覆盖，服务端的校验器直接拒。
 */
export function manualTargets(nodes: ComfyuiNodeEntry[], key: ComfyuiBindingKey): ComfyuiBindingTarget[] {
  const targets: ComfyuiBindingTarget[] = [];
  for (const node of nodes) {
    if (key === "output") {
      targets.push(withTitle({ node: node.id, class_type: node.classType }, node.title));
      continue;
    }
    for (const [input, value] of Object.entries(node.inputs)) {
      if (isLinkInput(value)) continue;
      const target: ComfyuiBindingTarget = { node: node.id, input, class_type: node.classType };
      if (key === "fps") target.direction = "read";
      targets.push(withTitle(target, node.title));
    }
  }
  return targets;
}

function withTitle(target: ComfyuiBindingTarget, title: string): ComfyuiBindingTarget {
  return title ? { ...target, title } : target;
}

/**
 * Check/uncheck for ordered list keys. Selected items are ordered by the candidate list (the inference side's tie order is node order);
 * entries picked manually that aren't in the candidate list come after, keeping insertion order; list order is the reference image index.
 *
 * 有序列表键的勾选与取消。选中项按候选列表的次序排（推断侧的并列次序就是节点序），
 * 手选进来、不在候选列表里的条目排在其后并保持加入顺序——列表顺序即参考图序号。
 */
export function toggleListTarget(
  candidates: ComfyuiBindingCandidate[],
  current: ComfyuiBindingTarget[],
  target: ComfyuiBindingTarget,
): ComfyuiBindingTarget[] {
  const next = current.some((entry) => sameTarget(entry, target))
    ? current.filter((entry) => !sameTarget(entry, target))
    : [...current, target];
  const rank = (entry: ComfyuiBindingTarget): number => {
    const index = candidates.findIndex((candidate) => sameTarget(candidate.target, entry));
    return index < 0 ? candidates.length : index;
  };
  return next
    .map((entry, index) => ({ entry, index }))
    .sort((a, b) => rank(a.entry) - rank(b.entry) || a.index - b.index)
    .map((item) => item.entry);
}

/** Two targets point to the same landing point: output compares the node, others compare node plus field. / 两条目标指向同一个落点：产物比节点，其余比节点加字段。 */
export function sameTarget(a: ComfyuiBindingTarget, b: ComfyuiBindingTarget): boolean {
  return a.node === b.node && (a.input ?? null) === (b.input ?? null);
}

/** Which slot in the workflow a target writes to; read-only `fps` writes no value, so it occupies no slot. / 一条目标写进 workflow 的哪一格；只读的 `fps` 不写值，因此不占格子。 */
function targetLanding(target: ComfyuiBindingTarget): string | null {
  if (target.direction === "read") return null;
  return target.input === undefined ? `#${target.node}` : `#${target.node}.${target.input}`;
}

// ---------------------------------------------------------------------------
// Inference result -> node bindings to save / 推断结果 → 待保存的节点绑定
// ---------------------------------------------------------------------------

/**
 * Persisted form of the inference result: only keys with a "unique top score" carry an entry automatically; ambiguous and pending ones carry none,
 * since those are exactly for the user to decide. Explicit unsupported keeps its empty list; other keys are absent (never inferred).
 *
 * 推断结果的落盘形态：只有「唯一最高分」的键会自动带上条目，歧义与待确认一条也不带——
 * 那正是要用户来定的。显式不支持沿用它的空列表，其余键缺席（从未推断）。
 */
export function bindingsFromInference(
  inference: ComfyuiInferResponse,
  mediaType: ComfyuiMediaType,
): ComfyuiBindings {
  const bindings: ComfyuiBindings = {};
  for (const key of bindingKeysFor(mediaType)) {
    const result = inference.bindings[key];
    if (!result) continue;
    if (result.state === "unsupported") {
      bindings[key] = [];
      continue;
    }
    if (result.state !== "auto_selected") continue;
    const selected = result.candidates.filter((candidate) => candidate.selected).map((c) => c.target);
    if (selected.length > 0) bindings[key] = selected;
  }
  return bindings;
}

/** After the media type changes, out-of-range semantic keys are removed along with their entries; they would never be filled anyway. / 媒体类型改了之后，越界的语义键连同它的条目一起摘掉——它本就不会被填值。 */
export function pruneBindings(bindings: ComfyuiBindings, mediaType: ComfyuiMediaType): ComfyuiBindings {
  const allowed = new Set<string>(bindingKeysFor(mediaType));
  const next: ComfyuiBindings = {};
  for (const [key, targets] of Object.entries(bindings)) {
    if (allowed.has(key)) next[key as ComfyuiBindingKey] = targets;
  }
  return next;
}

/** Set of indices in the candidate list of the key's currently selected entries. / 该键当前选中的条目在候选列表里的下标集合。 */
export function selectedCandidateIndexes(
  candidates: ComfyuiBindingCandidate[],
  targets: ComfyuiBindingTarget[] | undefined,
): Set<number> {
  const chosen = new Set<number>();
  if (!targets) return chosen;
  candidates.forEach((candidate, index) => {
    if (targets.some((target) => sameTarget(target, candidate.target))) chosen.add(index);
  });
  return chosen;
}

/**
 * State of a row. Changed by the user this round, or entries from a saved definition (kept and rematched alike), both count as manually specified;
 * per `docs/adr/0082`, every saved node binding was already confirmed by the user.
 *
 * 一行的状态。用户本轮改过、或条目来自已保存的定义（沿用与重匹配都是），都算手动指定——
 * 按 `docs/adr/0082`，保存下来的每一条节点绑定本来就是用户确认过的。
 */
export function rowStatus(
  key: ComfyuiBindingKey,
  state: ComfyuiBindingState | undefined,
  targets: ComfyuiBindingTarget[] | undefined,
  options: { touched: boolean; candidates: ComfyuiBindingCandidate[] },
): ComfyuiRowStatus {
  if (targets !== undefined && targets.length === 0) {
    return isRequiredBindingKey(key) ? "required_unbound" : "unsupported";
  }
  if (targets === undefined) {
    // Tied candidates take priority over "required unbound": when candidates are there, the thing to do is pick one, not search from scratch.
    // 并列候选优先于「必填未绑定」：有候选摆在那里时，该做的事是选一个，不是从头找。
    if (state === "ambiguous" || state === "needs_confirmation") return "ambiguous";
    return isRequiredBindingKey(key) ? "required_unbound" : "not_found";
  }
  if (options.touched) return "manual";
  const origins = boundOrigins(options.candidates, targets);
  return origins.length > 0 && origins.every((origin) => origin === "inferred") ? "auto" : "manual";
}

/** How each entry of this row came about; entries picked manually that aren't in the candidate list aren't included. / 这一行的条目各自是怎么来的；手选进来、不在候选列表里的条目不在其中。 */
function boundOrigins(
  candidates: ComfyuiBindingCandidate[],
  targets: ComfyuiBindingTarget[],
): ComfyuiMatchOrigin[] {
  return candidates
    .filter((candidate) => targets.some((target) => sameTarget(target, candidate.target)))
    .map((candidate) => candidate.origin);
}

/**
 * Mark left on this row by reimport: if any entry was migrated by type plus title, report "rematched", the kind that more needs
 * the user's review; when all were kept as-is, report "kept"; rows newly inferred or hand-picked this round have no mark.
 *
 * 重导入在这一行留下的记号：只要有一条是按类型加标题迁移过来的就报「已重匹配」——它是更需要
 * 用户过目的那一种；全都原样沿用时报「沿用」；本轮新推断或手选的行没有记号。
 */
export function rowOrigin(
  candidates: ComfyuiBindingCandidate[],
  targets: ComfyuiBindingTarget[] | undefined,
): ComfyuiMatchOrigin | null {
  if (targets === undefined || targets.length === 0) return null;
  const origins = boundOrigins(candidates, targets);
  if (origins.includes("rematched")) return "rematched";
  return origins.length > 0 && origins.every((origin) => origin === "kept") ? "kept" : null;
}

/** The four counts on the right of the table header. / 表头右侧的四类计数。 */
export function statusTally(statuses: ComfyuiRowStatus[]): {
  bound: number;
  ambiguous: number;
  notFound: number;
  unsupported: number;
} {
  return {
    bound: statuses.filter((s) => s === "auto" || s === "manual").length,
    ambiguous: statuses.filter((s) => s === "ambiguous").length,
    notFound: statuses.filter((s) => s === "not_found" || s === "required_unbound").length,
    unsupported: statuses.filter((s) => s === "unsupported").length,
  };
}

// ---------------------------------------------------------------------------
// Save gate / 保存门控
// ---------------------------------------------------------------------------

/** A reason blocking save: `key` renders the semantic key name, `code` decides which sentence to say. / 一条挡住保存的原因：`key` 用来渲染语义键名，`code` 决定说哪一句。 */
export interface ComfyuiSaveBlocker {
  code: "required_unbound" | "needs_choice" | "needs_binding" | "target_taken" | "placeholder_name";
  key?: ComfyuiBindingKey;
  /** Only for `target_taken`: the other semantic key colliding on the same slot. / `target_taken` 专属：撞在同一格子上的另一个语义键。 */
  otherKey?: ComfyuiBindingKey;
}

/**
 * List of save gate reasons; an empty list means it can be saved. All three are hard gates of the server validator: prompt and output must be bound,
 * tied candidates must be decided by someone, and two semantic keys can't write the same field; plus one for renaming: auto-wrapped `meta` is a placeholder,
 * and two unrelated workflows would be judged duplicates for having the same author and name.
 *
 * 保存门控的原因清单，空表即可以保存。三条都是服务端校验器的硬闸门：提示词与产物必须有着落、
 * 并列候选必须有人来定、两个语义键不能写同一个字段；另加一条改名——自动包装的 `meta` 是占位值，
 * 两份不相干的 workflow 会因同作者同名被判成重复。
 */
export function saveBlockers(
  bindings: ComfyuiBindings,
  inference: ComfyuiInferResponse,
  definition: ComfyuiEndpointDefinition,
  placeholderName: string,
): ComfyuiSaveBlocker[] {
  const blockers: ComfyuiSaveBlocker[] = [];
  const name = definition.meta.name.trim();
  if (name === "" || name === placeholderName) blockers.push({ code: "placeholder_name" });
  for (const key of bindingKeysFor(definition.media_type)) {
    const targets = bindings[key];
    if (isRequiredBindingKey(key) && (targets === undefined || targets.length === 0)) {
      blockers.push({ code: "required_unbound", key });
      continue;
    }
    if (targets !== undefined) continue;
    const result = inference.bindings[key];
    if (result === undefined) continue;
    if (result.state !== "ambiguous" && result.state !== "needs_confirmation") continue;
    // When reimport lost the entries and rerunning inference found nothing, this row has no candidates at all: the thing to do is pick manually or mark unsupported.
    // 重导入把条目判丢、重跑推断又一无所获时，这一行一个候选也没有：该做的是手选或标为不支持。
    blockers.push({ code: result.candidates.length > 0 ? "needs_choice" : "needs_binding", key });
  }
  const seen = new Map<string, ComfyuiBindingKey>();
  for (const key of bindingKeysFor(definition.media_type)) {
    for (const target of bindings[key] ?? []) {
      const landing = targetLanding(target);
      if (landing === null) continue;
      const taken = seen.get(landing);
      if (taken !== undefined) blockers.push({ code: "target_taken", key, otherKey: taken });
      else seen.set(landing, key);
    }
  }
  return blockers;
}

/**
 * Does this definition fail even the endpoint test?
 *
 * Of the save gates only these two are also hard gates of the server **validator** (`comfyui_binding_required` and
 * `comfyui_target_collision`); previewing or testing the connection with them only gets a 422 back. The other three aren't: undecided tied
 * candidates and invalidated original bindings just mean this dimension isn't bound, and the preview still renders; the "doesn't drive this workflow" in the conversion note
 * is exactly how it should look; the placeholder name only blocks saving.
 *
 * 这份定义连端点测试都过不去吗。
 *
 * 保存门控里只有这两条同时是服务端**校验器**的硬闸门（`comfyui_binding_required` 与
 * `comfyui_target_collision`），带着它们去预览或测试连接只会换回一条 422。另外三条不是：并列
 * 候选未定与原绑定失效只是这一维没绑上，预览照样渲得出来——换算说明里那个「未驱动这份 workflow」
 * 正是它该有的样子；占位名只挡保存。
 */
export function testRefused(blockers: readonly ComfyuiSaveBlocker[]): boolean {
  return blockers.some((blocker) => blocker.code === "required_unbound" || blocker.code === "target_taken");
}

/**
 * Canonical form of a definition for "has it changed" comparison.
 *
 * Comparing strings directly would judge two definitions with the same content as different: an empty title on the manual-pick side is "don't write the `title` key"
 * (see `withTitle`), while the server's rematch returns `"title": ""`; key order also follows each source.
 * Only by sorting recursively by key name and treating empty `title` as absent are both sides on the same ruler. This only affects button text and
 * clickability; what's persisted is still the original `draft`.
 *
 * 一份定义用于「改过没有」比对的规范形。
 *
 * 直接串比会把两份内容相同的定义判成不同：空标题在手选那一侧是「不写 `title` 这个键」
 * （见 `withTitle`），服务端重匹配回来的却是 `"title": ""`；键的次序也各按各的来源。
 * 递归按键名排序、并把空 `title` 一律当作缺省，两侧才在同一把尺子上。这只影响按钮文案与
 * 可点性，落盘的仍是原样的 `draft`。
 */
export function definitionFingerprint(definition: object): string {
  return JSON.stringify(canonical(definition));
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value === null || typeof value !== "object") return value;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([key, raw]) => !(key === "title" && raw === ""))
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return Object.fromEntries(entries.map(([key, raw]) => [key, canonical(raw)]));
}
