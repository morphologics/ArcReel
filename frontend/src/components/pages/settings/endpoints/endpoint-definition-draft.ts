import type {
  AnyEndpointDefinition,
  ComfyuiEndpointDefinition,
  EndpointDefinition,
  EndpointExtractSpec,
  EndpointInputEncoding,
  EndpointInputSource,
  EndpointPathItem,
  EndpointStandardStatus,
} from "@/types";

// Read/write helpers for definition drafts. The form and JSON view share the same EndpointDefinition object;
// this only provides conversions for "read out the shape the UI needs" and "keep unknown fields when writing back", without validation;
// validation is solely the job of the server's POST /custom-endpoints/validate.
// 定义草稿的读写辅助。表单与 JSON 视图共享同一份 EndpointDefinition 对象，
// 这里只提供「读出 UI 需要的形状」与「写回时保留未知字段」的转换，不做校验——
// 校验是服务端 POST /custom-endpoints/validate 的唯一职责。

/** Media type of a declarative definition; a definition without `media_type` is video. / 声明式定义的媒体类型；定义不写 `media_type` 即视频。 */
export type EndpointMediaType = "image" | "video";

export function definitionMediaType(definition: EndpointDefinition): EndpointMediaType {
  return definition.media_type ?? "video";
}

/** Output value key: the item in the poll or second-fetch section that reads out the output (URL or inline base64). / 产物取值键：轮询或二次取件节里读出产物（地址或内联 base64）的那一项。 */
export type EndpointArtifactKey = "video_url" | "image_url" | "image_b64";

/**
 * Form differences for one media type, corresponding to the backend validator's MEDIA_TYPE_RULES:
 * selectable material sources, output value keys (in runtime lookup order), generation parameter variables insertable into the request body.
 *
 * 一种媒体类型在表单上的差异，与后端校验器的 MEDIA_TYPE_RULES 对应：
 * 可选的素材来源、产物取值键（按运行时取用的先后排列）、可插入请求体的生成参数变量。
 */
export interface MediaTypeFormProfile {
  inputSources: EndpointInputSource[];
  artifactKeys: EndpointArtifactKey[];
  /** i18n keys for the variable name and description text. / 变量名与说明文案的 i18n key。 */
  generationVariables: { name: string; descKey: string }[];
}

const variable = (name: string, descKey = `ce_var_${name}`) => ({ name, descKey });

export const MEDIA_TYPE_FORM_PROFILES: Record<EndpointMediaType, MediaTypeFormProfile> = {
  video: {
    inputSources: ["start_image", "end_image", "reference_images", "reference_audio_files"],
    artifactKeys: ["video_url"],
    generationVariables: [
      variable("prompt"),
      variable("model"),
      variable("duration"),
      variable("resolution"),
      variable("aspect_ratio"),
      variable("width"),
      variable("height"),
      variable("generate_audio"),
    ],
  },
  image: {
    inputSources: ["reference_images"],
    artifactKeys: ["image_url", "image_b64"],
    generationVariables: [
      variable("prompt"),
      variable("model"),
      variable("resolution", "ce_var_resolution_image"),
      variable("aspect_ratio"),
      variable("width"),
      variable("height"),
      variable("seed"),
    ],
  },
};

export const INPUT_ENCODINGS: EndpointInputEncoding[] = ["data_uri", "base64"];

export const STANDARD_STATUSES: EndpointStandardStatus[] = [
  "queued",
  "running",
  "succeeded",
  "failed",
];

export const HTTP_METHODS = ["GET", "POST", "PUT"];

/** Form section ids, also the basis for diagnostics card navigation and vertical rail numbering. / 表单分节 id，同时是诊断卡定位与竖轨编号的依据。 */
export type EndpointFormSection =
  | "meta"
  | "auth"
  | "inputs"
  | "submit"
  | "poll"
  | "status"
  | "capabilities"
  | "test";

/** Prefill for a new definition: method and identity status mapping have common answers; capabilities and value paths vary widely by vendor and are left empty. / 新建定义的预填：方法与恒等状态映射有通用答案，能力与取值路径各家差异大，留空。 */
export function newEndpointDefinition(author: string): EndpointDefinition {
  return {
    kind: "declarative",
    schema_version: "1.2.0",
    meta: { name: "", author, version: "1.0.0" },
    auth: { headers: { Authorization: "Bearer {{ api_key }}" } },
    inputs: { first_frame: { source: "start_image", encoding: "data_uri" } },
    submit: {
      method: "POST",
      url: "{{ base_url }}/",
      body: {},
      extract: { task_id: [] },
    },
    poll: {
      method: "GET",
      url: "{{ base_url }}/",
      extract: { status: [], video_url: [] },
    },
    status_map: {
      queued: "queued",
      processing: "running",
      succeeded: "succeeded",
      failed: "failed",
    },
  };
}

// ---------------------------------------------------------------------------
// Value paths / 取值路径
// ---------------------------------------------------------------------------

/** Reads the path array from a value declaration; both short and full forms are normalized, absent becomes an empty array. / 读出取值声明中的路径数组；两种简写/全写形态归一，缺席为空数组。 */
export function readPaths(spec: EndpointExtractSpec | undefined): EndpointPathItem[] {
  if (spec === undefined) return [];
  if (Array.isArray(spec)) return spec;
  return spec.paths ?? [];
}

/** Writes back the path array, keeping the original form's accept; an empty array is written as an empty short form rather than deleting the key, so the field doesn't silently disappear. / 写回路径数组，保留原形态的 accept；空数组写成空简写而非删键，避免字段静默消失。 */
export function writePaths(
  spec: EndpointExtractSpec | undefined,
  paths: EndpointPathItem[],
): EndpointExtractSpec {
  if (spec !== undefined && !Array.isArray(spec)) {
    return spec.accept === undefined ? { paths } : { paths, accept: spec.accept };
  }
  return paths;
}

/** Whether a path item can be edited directly in the form; the json_decode form is only edited in the JSON view. / 路径项是否可在表单里直接编辑；json_decode 形态只在 JSON 视图编辑。 */
export function isPlainPath(item: EndpointPathItem): item is string {
  return typeof item === "string";
}

export function pathItemText(item: EndpointPathItem): string {
  return isPlainPath(item) ? item : item.path;
}

// ---------------------------------------------------------------------------
// Diagnostics navigation / 诊断定位
// ---------------------------------------------------------------------------

const SECTION_BY_ROOT: Record<string, EndpointFormSection> = {
  meta: "meta",
  auth: "auth",
  inputs: "inputs",
  submit: "submit",
  poll: "poll",
  result: "poll",
  status_map: "status",
  capabilities: "capabilities",
};

/**
 * Maps a diagnostic's location string (e.g. `poll.extract.video_url[0]`) to its form section.
 * Fields like `enum_maps`, `defaults`, `schema_version` have no form control and return null,
 * meaning they can only be handled in the JSON view; assigning them to a section would send the user somewhere the field can't be found.
 *
 * 把诊断的定位串（如 `poll.extract.video_url[0]`）映射到所属表单分节。
 * `enum_maps`、`defaults`、`schema_version` 这类字段表单里没有控件，返回 null
 * 表示只能在 JSON 视图处理——归给某个分节会把用户送到一个找不到该字段的地方。
 */
export function sectionOfIssuePath(path: string): EndpointFormSection | null {
  const root = path.replace(/^\$\.?/, "").split(/[.[]/, 1)[0];
  return SECTION_BY_ROOT[root] ?? null;
}

// ---------------------------------------------------------------------------
// Immutable writes / 不可变写入
// ---------------------------------------------------------------------------

/** Rebuilds the object in key order so renaming a key doesn't change field order; order changes would make the JSON view jump needlessly. / 按键序重建对象，让重命名键不改变字段顺序——顺序变化会让 JSON 视图无谓地跳动。 */
export function renameKey<T>(
  record: Record<string, T>,
  from: string,
  to: string,
  value: T,
): Record<string, T> {
  if (from !== to && to in record) return record;
  const next: Record<string, T> = {};
  for (const [k, v] of Object.entries(record)) {
    if (k === from) next[to] = value;
    else next[k] = v;
  }
  if (!(from in record)) next[to] = value;
  return next;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Whether this definition is declarative. ComfyUI definitions only have `workflow` and `bindings`; the `submit` / `poll` that the detail form and header dereference
 * don't exist on it at all, so the two can only split on the same check.
 *
 * 这份定义是不是声明式的。ComfyUI 定义只有 `workflow` 与 `bindings`，详情表单与头部直接解引用的
 * `submit` / `poll` 在它上面根本不存在，两者只能在同一个判据上分道。
 */
export function isDeclarativeDefinition(value: AnyEndpointDefinition): value is EndpointDefinition {
  return value.kind === "declarative";
}

/**
 * Structural gate before the JSON view writes back to the draft: the form and header don't access defensively (the draft is the definition),
 * they dereference the meta, submit.extract and poll.extract containers directly, and missing any one makes it unrenderable.
 * Field-level gaps (e.g. meta.name) only affect a single control's value and are left to server validation diagnostics.
 *
 * JSON 视图写回草稿前的结构闸：表单与头部不做防御性访问（草稿即定义），
 * 直接解引用 meta、submit.extract、poll.extract 这几个容器，缺任一即不可渲染。
 * 字段级缺失（如 meta.name）只影响单个控件的取值，交给服务端校验诊断。
 */
export function isRenderableDefinition(value: unknown): value is EndpointDefinition {
  return (
    isRecord(value) &&
    isRecord(value.meta) &&
    isRecord(value.submit) &&
    isRecord(value.submit.extract) &&
    isRecord(value.poll) &&
    isRecord(value.poll.extract)
  );
}

/**
 * Structural gate for ComfyUI definitions, same purpose as {@link isRenderableDefinition}: only checks the two containers dereferenced on display,
 * meta and workflow; field-level problems are left to server validation.
 *
 * ComfyUI 定义的结构闸，与 {@link isRenderableDefinition} 同一用途：只认展示时会解引用的
 * meta 与 workflow 两个容器，字段级问题交给服务端校验。
 */
export function isRenderableComfyuiDefinition(value: unknown): value is ComfyuiEndpointDefinition {
  return isRecord(value) && value.kind === "comfyui" && isRecord(value.meta) && isRecord(value.workflow);
}

/**
 * Derives the market entry slug from the endpoint name, rule `^[a-z0-9][a-z0-9-]{0,63}$`: strip accents, lowercase, fold other characters into hyphens;
 * empty string when the name has no usable ASCII characters.
 *
 * 由端点名称派生市场条目 slug，规则 `^[a-z0-9][a-z0-9-]{0,63}$`：去重音、转小写，其余字符折成连字符；
 * 名称里没有可用 ASCII 字符时为空串。
 */
export function slugFromName(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+/, "")
    .slice(0, 64)
    .replace(/-+$/, "");
}

/** Export file name: endpoints with an install record use the record's slug directly, otherwise derived from meta.name, falling back to `endpoint` when derivation is empty. / 导出文件名：有安装记录的端点直接用记录里的 slug，否则由 meta.name 派生，派生为空时退化为 `endpoint`。 */
export function definitionFileName(definition: AnyEndpointDefinition, installationSlug?: string | null): string {
  if (installationSlug) return `${installationSlug}.json`;
  return `${slugFromName(definition.meta?.name ?? "") || "endpoint"}.json`;
}
