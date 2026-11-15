import { uid } from "@/utils/id";
import { parseDurationInput } from "@/utils/duration_format";
import { parseMultiplier } from "@/utils/multiplier";
import type {
  CapabilityOverrides,
  CustomProviderInfo,
  CustomProviderModelInput,
  DiscoveredModel,
  EndpointDescriptor,
  EndpointKey,
  FetchedProviderModel,
  MatchedCatalogEntry,
  MediaType,
  PathOverrides,
  ProviderMultiplierInfo,
} from "@/types";
import { isComfyuiEndpoint, withDurationsOverride, type DiscoveryFormat } from "./customProviderHelpers";
import type { FetchSelection } from "./FetchModelsDialog";

// ---------------------------------------------------------------------------
// Edit unit content of the custom provider form: saved providers and unsaved changes are both compared in this shape. / 自定义供应商表单的编辑单元内容：已保存的供应商与未保存修改都按这份形状比较。
// ---------------------------------------------------------------------------

export interface ModelRow {
  /** Stable row identifier, randomly generated. / 行的稳定标识，随机生成。 */
  key: string;
  model_id: string;
  display_name: string;
  endpoint: EndpointKey;
  is_default: boolean;
  is_enabled: boolean;
  resolution: string; // empty string = null / 空串 = null
  // Raw text of available durations, editable only on ComfyUI video rows: on submit written back to the durations key of the capability overrides; empty string = remove the key,
  // following the workflow's native durations. On load taken from the overrides; other rows just carry it as-is and it isn't read on submit.
  // 可选时长的原始文本，只有 ComfyUI 视频行可编辑：提交时写回能力覆盖的 durations 键，空串 = 去掉该键、
  // 跟随 workflow 原生时长。加载时取自覆盖，其余行只是原样带着，提交时不读它。
  durations_text: string;
  // Raw text of the provider model multiplier, parsed before submit; must be a non-negative finite number.
  // 供应商模型倍率的原始文本，提交前 parse；必须是非负有限数。
  multiplier_text: string;
  // Entry binding "model vendor/official model name": only present when a matched model was chosen for an unrecognized model while fetching models.
  // 条目绑定「模型厂商/官方模型名」：获取模型时为未识别的模型选了对应模型才有。
  entry_binding: string | null;
  // Entry the server matched by (model_id, media type, entry binding); null = no match (unpriced),
  // undefined = not yet known (manually added rows, rows with a chosen matched model, or with changed model ID and endpoint; known only after saving).
  // 服务端按 (model_id, 媒体类型, 条目绑定) 对应到的条目；null = 对应不上（未定价），
  // undefined = 还不知道（手动新增、选了对应模型或改过模型 ID 与端点的行，保存后才知道）。
  matched_entry: MatchedCatalogEntry | null | undefined;
  capability_overrides: CapabilityOverrides | null;
  // Request address edits (request name -> path); null = all use endpoint defaults. / 请求地址修改（请求名 → 路径）；null = 全部按端点默认。
  path_overrides: PathOverrides | null;
  // Model ID and endpoint at row creation: the matched entry the server gave while fetching models is only valid for this combination; changing either is treated as "not yet known".
  // 行创建时的模型 ID 与端点：获取模型时服务端给出的对应条目只对这一组合有效，改过任一项即按「还不知道」处理。
  original_model_id: string;
  original_endpoint: EndpointKey;
}

export interface CustomProviderFormValue {
  displayName: string;
  /** The protocol the user chose. "Not chosen yet" and "chose openai" are not the same: only the former lets an endpoint wired in decide the protocol. / 用户选过的协议。「还没选过」与「选了 openai」不是一回事：只有前者才让接线过来的端点定协议。 */
  pickedFormat: DiscoveryFormat | null;
  baseUrl: string;
  /** Newly entered key; when editing an existing provider, empty means keep the existing key. / 新输入的密钥；编辑已有供应商时留空表示保留现有密钥。 */
  apiKey: string;
  /** "This provider needs no key": keyless APIs such as local deployments save an empty key, and the required-field check on create is lifted too. / 「该供应商无需密钥」：本地部署等无凭证接口保存空密钥，同时解除新建时的必填校验。 */
  noApiKey: boolean;
  models: ModelRow[];
  imageMaxWorkers: string;
  videoMaxWorkers: string;
  audioMaxWorkers: string;
  /** Provider multiplier; empty string = default (submits null on save). / 供应商倍率；空串 = 默认值（保存时提交 null）。 */
  providerMultiplier: string;
}

// Default endpoint for new model rows; when the protocol switches away from ComfyUI, rows that can't attach also fall back to it.
//: 新模型行默认挂的端点；协议从 ComfyUI 切走时，挂不住的行也退回它。
export const DEFAULT_ENDPOINT = "openai-chat" as EndpointKey;

// "This row has no endpoint yet". When switching into the ComfyUI protocol with no ComfyUI endpoint yet, rows that can't attach stay here:
// the selector shows nothing selected and saving is blocked until the user imports an endpoint and picks one for it.
//: 「这一行还没有端点」。切进 ComfyUI 协议而一个 ComfyUI 端点都还没有时，挂不住的行停在这里：
//: 选择器显示未选择，保存被拦下，直到用户导入端点并为它选一个。
export const UNSET_ENDPOINT = "" as EndpointKey;

export function newModelRow(partial?: Partial<ModelRow>): ModelRow {
  const base = {
    key: uid(),
    model_id: "",
    display_name: "",
    endpoint: DEFAULT_ENDPOINT,
    is_default: false,
    is_enabled: true,
    resolution: "",
    durations_text: "",
    multiplier_text: "1",
    entry_binding: null,
    matched_entry: undefined,
    capability_overrides: null,
    path_overrides: null,
    ...partial,
  };
  return { ...base, original_model_id: base.model_id, original_endpoint: base.endpoint };
}

/**
 * Turns a model checked in the "Fetch models" dialog into a row of the new-provider form. Rows with a chosen matched model are stored as entry bindings; the match result is computed by the server after saving,
 * so treat it as "not yet known"; `isDefault` is given by the caller per media type.
 *
 * 「获取模型」对话框里勾选的模型变成新建表单的一行。选了对应模型的行存为条目绑定，对应结果要等保存后由服务端算，
 * 按「还不知道」处理；`isDefault` 由调用方按媒体类型给出。
 */
export function fetchedToRow(selection: FetchSelection, isDefault: boolean): ModelRow {
  const { model, endpoint, binding } = selection;
  return newModelRow({
    model_id: model.model_id,
    display_name: model.model_id,
    endpoint: endpoint ?? DEFAULT_ENDPOINT,
    is_default: isDefault,
    is_enabled: true,
    entry_binding: binding ? `${binding.maker}/${binding.name}` : null,
    matched_entry: binding ? undefined : model.matched_entry,
  });
}

/** Turns the new-provider form's model discovery results into rows of the "Fetch models" dialog; for custom providers, a name matching an entry counts as recognized. / 新建表单的模型发现结果转成「获取模型」对话框的行；自定义供应商以名字对应上条目为已识别。 */
export function discoveredToFetched(m: DiscoveredModel, mediaOf: (endpoint: EndpointKey) => MediaType | undefined): FetchedProviderModel {
  return {
    model_id: m.model_id,
    display_name: m.display_name,
    media_type: m.media_type ?? mediaOf(m.endpoint) ?? "text",
    recognized: m.recognized ?? !!m.matched_entry,
    model_key: null,
    matched_entry: m.matched_entry ?? null,
    endpoint: m.endpoint,
    endpoint_by_media_type: m.endpoint_by_media_type ?? null,
    is_default: m.is_default,
  };
}

// Concurrency limit: number inputs store a controlled string; empty string = unset (null, uses the global default).
// 并发上限：number 输入用受控字符串存储；空串 = 未设置（null，走全局默认）。
function workersToStr(n?: number | null): string {
  return n != null ? String(n) : "";
}

/** Value when a custom provider has no multiplier set; the new-provider form can't get the server's default yet, so it is displayed with this. / 自定义供应商没有设置倍率时的值；新建表单还取不到服务端的默认值，按它显示。 */
export const DEFAULT_PROVIDER_MULTIPLIER: ProviderMultiplierInfo = { multiplier: 1, default: 1 };

/**
 * A new provider has no id yet and can't get the server's default multiplier, so the same default is given by protocol: 0 for ComfyUI, 1 for others
 * (consistent with default_provider_multiplier in lib/billing/multipliers.py).
 *
 * 新建供应商还没有 id、取不到服务端的默认倍率，按协议给出同一份默认值：ComfyUI 协议为 0，其余为 1
 * （与 lib/billing/multipliers.py 的 default_provider_multiplier 一致）。
 */
export function newProviderDefaultMultiplier(isComfyui: boolean): number {
  return isComfyui ? 0 : DEFAULT_PROVIDER_MULTIPLIER.default;
}

// Provider multiplier: shown empty when equal to the default, meaning "follow default"; saving an empty string restores the default.
// 供应商倍率：等于默认值时显示为空，表示「跟随默认」；保存空串即恢复默认。
function providerMultiplierToStr(info: ProviderMultiplierInfo): string {
  return info.multiplier === info.default ? "" : String(info.multiplier);
}

/** Form content for a saved provider. Saved models are saved one by one in the model list and model Sheet, not in the form. / 已保存的供应商对应的表单内容。已保存的模型在模型列表与模型 Sheet 里逐个保存，不进表单。 */
export function formFromProvider(
  provider: CustomProviderInfo,
  multiplier: ProviderMultiplierInfo = DEFAULT_PROVIDER_MULTIPLIER,
): CustomProviderFormValue {
  return {
    displayName: provider.display_name,
    pickedFormat: provider.discovery_format,
    baseUrl: provider.base_url,
    apiKey: "",
    noApiKey: false,
    models: [],
    imageMaxWorkers: workersToStr(provider.image_max_workers),
    videoMaxWorkers: workersToStr(provider.video_max_workers),
    audioMaxWorkers: workersToStr(provider.audio_max_workers),
    providerMultiplier: providerMultiplierToStr(multiplier),
  };
}

/** Initial content of the new-provider form; when wired in from "Call endpoint", prefill the API address and start one model row with the chosen endpoint. / 新建表单的初始内容；从「调用端点」接线过来时预填接口地址，并按所选端点起一行模型。 */
export function newProviderForm(initialBaseUrl?: string, initialEndpoint?: EndpointKey): CustomProviderFormValue {
  return {
    displayName: "",
    pickedFormat: null,
    baseUrl: initialBaseUrl ?? "",
    apiKey: "",
    noApiKey: false,
    models: initialEndpoint ? [newModelRow({ endpoint: initialEndpoint })] : [],
    imageMaxWorkers: "",
    videoMaxWorkers: "",
    audioMaxWorkers: "",
    providerMultiplier: "",
  };
}

// Empty string = unset (null); otherwise must be a positive integer (>= 1). Returns undefined for invalid
// input (0, decimals, scientific notation, minus sign, non-digit characters), which the save blocks with a hint; no longer uses parseInt
// to silently truncate ("1.5"->1, "1e3"->1) and write invalid values as wrong config. 0 is not valid user input.
// 空串 = 未设置（null）；否则必须是正整数（≥1）。返回 undefined 表示非法
// 输入（0、小数、科学计数、负号、含非数字字符），由保存拦截并提示——不再用 parseInt
// 静默截断（"1.5"→1、"1e3"→1）把非法值写成错误配置。0 不是合法用户输入。
export function parsePositiveInt(s: string): number | null | undefined {
  const trimmed = s.trim();
  if (!trimmed) return null;
  if (!/^\d+$/.test(trimmed)) return undefined;
  const n = Number(trimmed);
  return Number.isSafeInteger(n) && n >= 1 ? n : undefined;
}

/** Provider multiplier: empty string = restore default (null); otherwise same as parseMultiplier, invalid input returns undefined. / 供应商倍率：空串 = 恢复默认（null）；否则同 parseMultiplier，非法输入返回 undefined。 */
export function parseOptionalMultiplier(s: string): number | null | undefined {
  return s.trim() ? parseMultiplier(s) : null;
}

/**
 * The entry a row matches under current edits. Entries are matched by the server from the model ID and the endpoint's media type; for rows where these two changed
 * the match must be recomputed, and the frontend doesn't guess but treats it as "not yet known" (undefined); changing back to the original values brings back the result from load.
 *
 * 行在当前编辑下对应到的条目。条目由服务端按模型 ID 与端点的媒体类型对应，改过这两项的行
 * 对应结果要重新算，前端不猜，按「还不知道」（undefined）处理；改回原值即取回加载时的结果。
 */
export function matchedEntryFor(row: ModelRow): MatchedCatalogEntry | null | undefined {
  return row.model_id === row.original_model_id && row.endpoint === row.original_endpoint ? row.matched_entry : undefined;
}

export interface RowToInputOptions {
  /** This row's available durations are editable (ComfyUI video endpoint, duration options derivable): in that case durations_text is written back to the overrides. / 这一行的可选时长可编辑（ComfyUI 视频端点、档位给得出来）：此时由 durations_text 写回覆盖。 */
  durationsEditable: boolean;
}

export function rowToInput(r: ModelRow, { durationsEditable }: RowToInputOptions): CustomProviderModelInput {
  let overrides = r.capability_overrides;
  if (durationsEditable) {
    // On failure throw DurationParseError directly; saving should have blocked before the call, so this only honestly converts the string
    // to list[int] without silently degrading (treating invalid input as an empty string would quietly switch the user's durations back to native).
    // 失败时直接抛 DurationParseError；保存在调用前应已拦截，故此处只负责诚实地把字符串
    // 转成 list[int] 而不静默降级（无效输入被当成空串，会把用户的档位悄悄换回原生时长）
    overrides = withDurationsOverride(overrides, parseDurationInput(r.durations_text) ?? undefined);
  }
  return {
    model_id: r.model_id,
    display_name: r.display_name || r.model_id,
    endpoint: r.endpoint,
    is_default: r.is_default,
    is_enabled: r.is_enabled,
    ...(r.resolution ? { resolution: r.resolution } : { resolution: null }),
    // Other rows' overrides (including durations and max_output_tokens written by upgrade migrations) are passed back as-is: omitting them in a whole-table PUT clears them
    // 其余行的覆盖（含升级迁移写入的 durations、max_output_tokens）原样回传：整表 PUT 省略即清空
    capability_overrides: overrides,
    path_overrides: r.path_overrides,
    // Invalid multipliers were blocked before saving, so this fallback value never takes effect / 保存前已拦截非法倍率，这里的回退值不会生效
    multiplier: parseMultiplier(r.multiplier_text) ?? 1,
    entry_binding: r.entry_binding,
  };
}

/**
 * Whether a row can attach to the current protocol is derived along the way, not by rewriting rows once at the moment the protocol switches: ComfyUI endpoints only attach to
 * ComfyUI providers and vice versa (two-way pairing in docs/adr/0081), and this check needs the endpoint catalog, which is fetched asynchronously;
 * at the moment of switching it may not be back yet, and once it is, nobody recomputes. Keeping an old value that can't attach, that row is hidden in the endpoint selector
 * (the selector filters by protocol), the user can't see it, and only gets a 422 on save.
 *
 * Where to re-attach: switching into ComfyUI takes the first ComfyUI endpoint, switching away falls back to the new-row default endpoint; when there's no ComfyUI endpoint
 * at all there's nowhere to go, and the row lands on UNSET_ENDPOINT, which the user can see and saving can block, better than a hidden old endpoint.
 * Deriving rather than rewriting also brings one more thing: switch away and back, and the originally hand-picked endpoint comes back by itself; models always stores the user's
 * last explicit choice. While the catalog isn't back yet (`catalog` is null), no row can be decided, so return as-is.
 *
 * 行挂不挂得住当前协议是一路派生下来的，不是切协议那一刻改写一遍行就算数：ComfyUI 端点只挂得上
 * ComfyUI 供应商，反之亦然（docs/adr/0081 的双向配对），而这份判断要查端点目录——目录是异步取的，
 * 切协议那一刻它可能还没回来，回来之后也不会有人再重算一遍。留着挂不住的旧值，那一行在端点选择器
 * 里是隐着的（选择器按协议过滤），用户看不见它，保存时才吃一个 422。
 *
 * 改挂的去处：切进 ComfyUI 取第一个 ComfyUI 端点，切走退回新行的默认端点；一个 ComfyUI 端点都
 * 还没有时没有去处，行落在 UNSET_ENDPOINT 上——用户看得见、保存拦得住，比留个隐形的旧端点强。
 * 派生而非改写还带来一点：切走再切回来，原先手选的那个端点自己回来了，models 里存的始终是用户
 * 最后一次显式选择。目录还没回来时（`catalog` 为 null）一行都判不了，原样返回。
 */
export function effectiveModelRows(
  models: ModelRow[],
  catalog: EndpointDescriptor[] | null,
  isComfyui: boolean,
): ModelRow[] {
  if (catalog === null) return models;
  const firstComfyui = catalog.find(isComfyuiEndpoint)?.key;
  return models.map((row) => {
    const descriptor = catalog.find((item) => item.key === row.endpoint);
    if (descriptor !== undefined && isComfyuiEndpoint(descriptor) === isComfyui) return row;
    const endpoint = (isComfyui ? firstComfyui : DEFAULT_ENDPOINT) ?? UNSET_ENDPOINT;
    // Switched to another path, so the default flag, capability overrides and request address edits are invalidated: override validity is tied to (endpoint, model_id) anyway,
    // and the ComfyUI protocol disables overrides entirely (server _check_protocol_constraints), so keeping them would surely be rejected.
    // 换了一路，默认标记、能力覆盖与请求地址修改随之作废：覆盖的合法性本就绑在 (endpoint, model_id) 上，
    // 而 ComfyUI 协议整个关闭覆盖（服务端 _check_protocol_constraints），留着必被拒。
    return { ...row, endpoint, is_default: false, capability_overrides: null, path_overrides: null };
  });
}
