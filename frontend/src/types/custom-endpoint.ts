import type { EndpointInstallation } from "./market";
// Custom endpoint -- frontend types of endpoint definitions, split by kind into declarative and ComfyUI.
// The definition JSON itself is the single source of truth, with zero-envelope import/export: the file is the definition JSON as is.
// The backend schemas are endpoint_definition/schema.json and comfyui/schema.json under
// packages/arcreel-market-core/src/arcreel_market_core/; the final verdict is POST /custom-endpoints/validate,
// and here only the shapes the UI needs to read and write are described.
// 自定义调用端点（custom endpoint）——端点定义的前端类型，按 kind 分声明式与 ComfyUI 两种。
// 定义 JSON 本身是唯一真相源，导入导出零封套：文件即 definition 原样 JSON。
// 后端 schema 是 packages/arcreel-market-core/src/arcreel_market_core/ 下的
// endpoint_definition/schema.json 与 comfyui/schema.json，最终判定以 POST /custom-endpoints/validate 为准，
// 这里只描述 UI 需要读写的形状。

/** Source slot of a material on the ArcReel side. / 素材在 ArcReel 侧的来源槽位。 */
export type EndpointInputSource =
  | "start_image"
  | "end_image"
  | "reference_images"
  | "reference_audio_files";

/** Encoding form of a material when sent to the provider. / 素材发送到供应商时的编码形态。 */
export type EndpointInputEncoding = "data_uri" | "base64";

/** Four standard status levels, named the same as backend ProviderJobStatus. / 标准状态四档，与后端 ProviderJobStatus 同名。 */
export type EndpointStandardStatus = "queued" | "running" | "succeeded" | "failed";

export interface EndpointInputSpec {
  source: EndpointInputSource;
  encoding: EndpointInputEncoding;
  required?: boolean;
}

/** Value path item: a JSONPath string, or the JSON-in-string suffix form with json_decode. / 取值路径项：JSONPath 串，或带 json_decode 的 JSON-in-string 后缀形态。 */
export type EndpointPathItem =
  | string
  | { path: string; json_decode?: boolean; then?: string[] };

/** Value declaration: shorthand is a path priority array; the full form may also carry accept. / 取值声明：简写为路径优先级数组，全写可另带 accept。 */
export type EndpointExtractSpec =
  | EndpointPathItem[]
  | { paths: EndpointPathItem[]; accept?: "string" | "scalar" };

export interface EndpointMetaHints {
  base_url?: string;
  suggested_models?: { id: string; label?: string }[];
}

export interface EndpointMeta {
  name: string;
  author: string;
  version: string;
  description?: string;
  homepage?: string;
  /** Minimum ArcReel version (semver) required to run this definition. / 运行该定义所需的最低 ArcReel 版本（semver）。 */
  min_app_version?: string;
  hints?: EndpointMetaHints;
}

export interface EndpointAuth {
  headers?: Record<string, string>;
  query?: Record<string, string>;
}

export interface EndpointSubmitSpec {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: unknown;
  extract: {
    task_id?: EndpointExtractSpec;
    error?: EndpointExtractSpec;
  };
}

export interface EndpointPollSpec {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: unknown;
  expire_on_404?: boolean;
  extract: {
    status?: EndpointExtractSpec;
    video_url?: EndpointExtractSpec;
    image_url?: EndpointExtractSpec;
    image_b64?: EndpointExtractSpec;
    error?: EndpointExtractSpec;
    failure?: EndpointExtractSpec;
    result_id?: EndpointExtractSpec;
    usage?: Record<string, EndpointExtractSpec>;
  };
}

export interface EndpointResultSpec {
  method: string;
  url: string;
  headers?: Record<string, string>;
  extract: {
    video_url?: EndpointExtractSpec;
    image_url?: EndpointExtractSpec;
    image_b64?: EndpointExtractSpec;
    error?: EndpointExtractSpec;
    usage?: Record<string, EndpointExtractSpec>;
  };
}

/**
 * Capability fields are grouped by media type: video definitions use the same-name fields of VideoCapabilities,
 * image definitions use text_to_image, image_to_image and max_reference_images.
 *
 * 能力字段按媒体类型分集合：视频定义写 VideoCapabilities 同名字段，
 * 图片定义写 text_to_image、image_to_image 与 max_reference_images。
 */
export interface EndpointCapabilities {
  text_to_video?: boolean;
  text_to_image?: boolean;
  image_to_image?: boolean;
  first_frame?: boolean;
  last_frame?: boolean;
  max_reference_images?: number;
  reference_audio_mode?: "none" | "direct";
  max_reference_audio_count?: number;
  max_reference_audio_total_seconds?: number | null;
  reference_audio_per_image?: boolean;
  max_prompt_chars?: number | null;
  first_frame_ratio_adaptive_only?: boolean;
  audio_track?: "controllable" | "always_on" | "always_off";
  reference_route_audio_track?: "controllable" | "always_on" | "always_off" | null;
}

/** A complete declarative definition. Unknown fields are preserved as is, so the form does not drop them on round trips. / 一份完整的声明式定义。未知字段原样保留，表单不会在往返中丢弃它们。 */
export interface EndpointDefinition {
  kind: "declarative";
  schema_version: string;
  /** Media type the definition produces; video when omitted. / 定义产出的媒体类型，不写即视频。 */
  media_type?: "image" | "video";
  meta: EndpointMeta;
  auth: EndpointAuth;
  inputs?: Record<string, EndpointInputSpec>;
  enum_maps?: Record<string, Record<string, string | number | boolean>>;
  defaults?: Record<string, string | number | boolean>;
  submit: EndpointSubmitSpec;
  poll: EndpointPollSpec;
  result?: EndpointResultSpec;
  status_map?: Record<string, EndpointStandardStatus>;
  capabilities?: EndpointCapabilities;
}

/**
 * Directory of semantic key names for node bindings. Video endpoints use all eleven; image endpoints lack the four for first/last frame and timeline.
 * Same names and order as server-side `packages/arcreel-market-core/src/arcreel_market_core/comfyui/bindings.py`.
 *
 * 节点绑定的语义键名录。视频端点用全部十一个，图像端点没有首尾帧与时间轴那四个。
 * 与服务端 `packages/arcreel-market-core/src/arcreel_market_core/comfyui/bindings.py` 同名同序。
 */
export const COMFYUI_VIDEO_BINDING_KEYS = [
  "prompt",
  "negative_prompt",
  "start_image",
  "end_image",
  "reference_images",
  "width",
  "height",
  "frames",
  "fps",
  "seed",
  "output",
] as const;

export type ComfyuiBindingKey = (typeof COMFYUI_VIDEO_BINDING_KEYS)[number];

export const COMFYUI_IMAGE_BINDING_KEYS: readonly ComfyuiBindingKey[] = [
  "prompt",
  "negative_prompt",
  "reference_images",
  "width",
  "height",
  "seed",
  "output",
];

/** Semantic keys both media types must bind: without a prompt there is nothing to write, without an output there is no result to fetch. / 两种媒体类型都必须绑定的语义键：没有提示词无从下笔，没有产物取不到成片。 */
export const COMFYUI_REQUIRED_BINDING_KEYS: readonly ComfyuiBindingKey[] = ["prompt", "output"];

/** Whether a ComfyUI workflow produces images or video. / 一份 ComfyUI workflow 产出图像还是视频。 */
export type ComfyuiMediaType = "image" | "video";

/** Which input of which node the image-loading node a binding points to feeds; used to rewire the graph when there are fewer images than slots. / 一条节点绑定指向的读图节点接到谁的哪个入口；张数少于格子数时据此改图。 */
export interface ComfyuiConsumerTarget {
  node: string;
  input: string;
  class_type: string;
  title?: string;
}

/**
 * Target of one node binding. Optional keys per semantic key are constrained by the server schema: `output` has no `input`,
 * `fps`'s `direction` is always `read`, `width` / `height` / `frames` carry `step`,
 * `frames` may carry a hand-entered `fps`, `seed` carries `policy`, `reference_images` carries `consumer`.
 *
 * 一条节点绑定的落点。各语义键的可选键由服务端 schema 约束：`output` 没有 `input`，
 * `fps` 的 `direction` 恒为 `read`，`width` / `height` / `frames` 带 `step`，
 * `frames` 可带手填 `fps`，`seed` 带 `policy`，`reference_images` 带 `consumer`。
 */
export interface ComfyuiBindingTarget {
  node: string;
  input?: string;
  class_type: string;
  title?: string;
  direction?: "write" | "read";
  step?: number;
  fps?: number;
  policy?: "random" | "keep";
  consumer?: ComfyuiConsumerTarget;
}

/**
 * Semantic key -> target list. Three states: a non-empty list is bound, an empty list is explicitly unsupported, a missing key was never inferred.
 *
 * 语义键 → 目标列表。三态：非空列表是已绑定，空列表是显式不支持，键缺失是从未推断。
 */
export type ComfyuiBindings = Partial<Record<ComfyuiBindingKey, ComfyuiBindingTarget[]>>;

/**
 * A ComfyUI endpoint definition: an API-format workflow together with its node bindings.
 *
 * Capabilities are derived only from `bindings`; the definition has no `capabilities` section. `workflow` stays loosely typed: ArcReel does not rewrite
 * its structure and sends it as is on submit; the frontend only reads each node's `class_type`, `inputs` and `_meta.title`.
 *
 * 一份 ComfyUI 端点定义：API 格式 workflow 连同它的节点绑定。
 *
 * 能力只从 `bindings` 推导，定义不含 `capabilities` 节。`workflow` 保持松类型：ArcReel 不改写
 * 它的结构，提交时原样发出，前端只读每个节点的 `class_type`、`inputs` 与 `_meta.title`。
 */
export interface ComfyuiEndpointDefinition {
  kind: "comfyui";
  schema_version: string;
  meta: EndpointMeta;
  media_type: ComfyuiMediaType;
  auth?: EndpointAuth;
  workflow: Record<string, unknown>;
  bindings: ComfyuiBindings;
}

/** A node in the workflow. Values of `inputs` are either literals or links of the form `[node id, output index]`. / workflow 里的一个节点。`inputs` 的值或是字面值，或是 `[节点 id, 输出序号]` 形态的连线。 */
export interface ComfyuiWorkflowNode {
  class_type: string;
  inputs?: Record<string, unknown>;
  _meta?: { title?: string };
}

// ---------------------------------------------------------------------------
// Node binding inference (POST /custom-endpoints/comfyui/infer) / 节点绑定推断（POST /custom-endpoints/comfyui/infer）
// ---------------------------------------------------------------------------

/**
 * Inference state of one semantic key. `needs_confirmation` is re-import only: some existing entries were lost, the key re-ran
 * inference, and the user must confirm before saving.
 *
 * 一个语义键的推断状态。`needs_confirmation` 是重导入专属：既有条目有丢失的，该键重跑了
 * 推断，保存前必须由用户确认。
 */
export type ComfyuiBindingState =
  | "auto_selected"
  | "ambiguous"
  | "not_found"
  | "unsupported"
  | "needs_confirmation";

/** Where a candidate came from: this inference round, an already-saved entry kept as is, or re-matched to a new node id by type plus title. / 候选是怎么来的：本轮推断、原样沿用已保存的条目、或按类型加标题重匹配到新节点 id。 */
export type ComfyuiMatchOrigin = "inferred" | "kept" | "rematched";

/** One matched signal and its contributed weight; `message` is already rendered in the request language. / 一条命中的信号与它贡献的分量；`message` 已按请求语言渲染好。 */
export interface ComfyuiBindingSignal {
  signal: string;
  weight: number;
  message: string;
}

/** A hint. It does not block saving, but without it users would not know how a given shape behaves. / 一条提示。不拦保存，但有它用户才知道某个形态会怎么表现。 */
export interface ComfyuiInferenceNote {
  code: string;
  message: string;
}

export interface ComfyuiBindingCandidate {
  /** Entry that can be written into `bindings` directly, with `step` / `policy` / `direction` / `consumer` already filled. / 可直接写进 `bindings` 的条目，`step` / `policy` / `direction` / `consumer` 都已填好。 */
  target: ComfyuiBindingTarget;
  score: number;
  signals: ComfyuiBindingSignal[];
  selected: boolean;
  origin: ComfyuiMatchOrigin;
  /** Upstream chain length of the output candidate, used to explain "why this one"; null for other semantic keys. / 产物候选的上游链长度，用来解释「为什么是这一个」；其余语义键为 null。 */
  depth: number | null;
}

export interface ComfyuiKeyInference {
  state: ComfyuiBindingState;
  candidates: ComfyuiBindingCandidate[];
  notes: ComfyuiInferenceNote[];
}

export interface ComfyuiInferResponse {
  media_type: ComfyuiMediaType;
  /** Whether saving this result directly would pass validation; the real gate is still the validator at save time. / 照这份结果直接落盘能不能过校验；真闸门仍是保存时的校验器。 */
  savable: boolean;
  bindings: Partial<Record<ComfyuiBindingKey, ComfyuiKeyInference>>;
  notes: ComfyuiInferenceNote[];
  import_shape: EndpointImportShape;
  /** Wrapped result of a raw API workflow; null when the payload was already a definition. / 原始 API workflow 的包装结果；载荷本就是定义时为 null。 */
  wrapped_definition: ComfyuiEndpointDefinition | null;
}

/** Definitions flowing through import, validation and saving: either kind is possible. / 导入、校验与保存这条路上流过的定义：两种 kind 都可能。 */
export type AnyEndpointDefinition = EndpointDefinition | ComfyuiEndpointDefinition;

// ---------------------------------------------------------------------------
// CRUD / validate
// ---------------------------------------------------------------------------

export interface CustomEndpointInfo {
  installation: EndpointInstallation | null;
  id: number;
  /** System-assigned `ce-<id>`; meaningless to users, not shown in the UI. / 系统分配的 `ce-<id>`；对用户无意义，界面不展示。 */
  key: string;
  display_name: string;
  kind: string;
  schema_version: string;
  media_type: string;
  /** Either kind is possible: ComfyUI definitions have no `submit` / `poll`; narrow with `isDeclarativeDefinition` before reading them. / 两种 kind 都可能：ComfyUI 定义没有 `submit` / `poll`，读它之前先用 `isDeclarativeDefinition` 收窄。 */
  definition: AnyEndpointDefinition;
  created_at: string | null;
  updated_at: string | null;
}

/** Model rows actually referencing it, returned by the server when deleting an endpoint is refused. / 删除端点被拒时，服务端返回的实际引用模型行。 */
export interface EndpointReference {
  provider_id: number;
  provider_display_name: string;
  model_id: string;
  model_display_name: string;
}

export interface EndpointDefinitionIssue {
  /** Locator string within the definition JSON, rooted at `$`, e.g. `poll.extract.video_url[0]`. / 定义 JSON 内的定位串，根为 `$`，如 `poll.extract.video_url[0]`。 */
  path: string;
  code: string;
  /** Explanation already rendered by the server per Accept-Language; the frontend shows it directly. / 服务端已按 Accept-Language 渲染好的说明，前端直接展示。 */
  message: string;
}

/** Existing definition with the same author and name, judged by meta.author + meta.name on import. / 导入时按 meta.author + meta.name 判定的同作者同名的既有定义。 */
export interface EndpointDuplicateDescriptor {
  id: number;
  key: string;
  display_name: string;
  version: string;
  relation: "newer" | "same" | "older";
}

export interface EndpointSchemaVersionInfo {
  file: string | null;
  current: string;
  level: "direct" | "warning" | "confirm";
}

/** Comparison of meta.min_app_version against the current app version; failing it only warns and does not block import. / meta.min_app_version 与当前应用版本的比对；不满足只提示，不拦导入。 */
export interface EndpointMinAppVersionInfo {
  required: string;
  current: string;
  satisfied: boolean;
}

/**
 * What the server took this payload as.
 *
 * The file users most often have is a workflow exported by ComfyUI itself rather than an endpoint definition, and its export menu has two items:
 * `Export (API)` can be submitted, while the canvas archive of `Export` cannot.
 *
 * 服务端把这份载荷当成什么收的。
 *
 * 用户手上最常见的文件是 ComfyUI 自己导出的 workflow 而不是端点定义，导出菜单又有两项：
 * `Export (API)` 能提交、`Export` 的画布存档不能。
 */
export type EndpointImportShape = "endpoint_definition" | "comfyui_api_workflow" | "comfyui_ui_workflow";

export interface EndpointValidateResponse {
  errors: EndpointDefinitionIssue[];
  warnings: EndpointDefinitionIssue[];
  duplicates: EndpointDuplicateDescriptor[];
  hints: EndpointMetaHints | null;
  schema_version: EndpointSchemaVersionInfo;
  /** Null when the definition declares no threshold or the app version cannot be read. / 定义未声明门槛或应用版本读不出时为 null。 */
  min_app_version: EndpointMinAppVersionInfo | null;
  import_shape: EndpointImportShape;
  /** Wrapped result of a raw API workflow; null for the other two shapes, where the client keeps using its own copy. / 原始 API workflow 的包装结果；另两种形状为 null，客户端继续用自己手上那份。 */
  wrapped_definition: ComfyuiEndpointDefinition | null;
}

// ---------------------------------------------------------------------------
// Three endpoint test modes / 端点测试三模式
// ---------------------------------------------------------------------------

export interface EndpointTestParameters {
  model: string;
  prompt?: string;
  duration_seconds?: number;
  aspect_ratio?: string;
  resolution?: string | null;
  generate_audio?: boolean;
}

/** Credential source, one of two: a saved custom provider, or a temporary entry for this test. / 凭证来源二选一：已保存的自定义供应商，或本次测试临时输入。 */
export interface EndpointTestCredentials {
  provider_id?: string;
  base_url?: string;
  api_key?: string;
}

/** Multipart materials for endpoint tests; keys match the server form field names. / 端点测试 multipart 素材；键与服务端表单字段同名。 */
export type EndpointTestAssets = Partial<Record<EndpointInputSource, File[]>>;

export interface PreviewedRequest {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

/**
 * "How was this request computed". A ComfyUI endpoint's size, frame count, seed and graph rewiring are all computed; a workflow JSON of dozens of
 * nodes alone cannot answer "what pixel size did my chosen 720p actually become".
 *
 * `width` / `height` / `frames` / `seed` null means that dimension does not drive this workflow (unbound, or size
 * bound on only one side and thus judged fixed); the literal in the workflow is kept as is.
 *
 * 「这份请求是怎么算出来的」。ComfyUI 端点的尺寸、帧数、种子与改图都是算出来的，光看一份几十个
 * 节点的 workflow JSON 答不出「我选的 720p 到底变成了多少像素」。
 *
 * `width` / `height` / `frames` / `seed` 为 null 表示这一维没有驱动这份 workflow（未绑定，或尺寸
 * 只绑了一侧因而判为固定），workflow 里的字面值原样保留。
 */
export interface ComfyuiRequestConversions {
  /** Fingerprint of the workflow actually sent, the same value recorded in the clip version metadata. / 实发 workflow 的指纹，与成片版本元数据里记的是同一个值。 */
  workflow_sha256: string;
  aspect_ratio: string;
  resolution: string | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  frames: number | null;
  seed: number | null;
  negative_prompt: string;
  /** Nodes removed this time when rewiring the graph by reference image count. / 这次按参考图张数改图删掉的节点。 */
  dropped_nodes: string[];
}

export interface EndpointPreviewResponse {
  submit: PreviewedRequest;
  poll: PreviewedRequest;
  result: PreviewedRequest | null;
  /** Declarative endpoint requests all come straight from template filling with no conversion to explain, so null. / 声明式端点的请求全部来自模板直填，没有可说明的换算，为 null。 */
  conversions: ComfyuiRequestConversions | null;
}

export type EndpointTestStage = "submit" | "poll" | "result";

export interface EndpointExtractionAttempt {
  path: string;
  json_decode: boolean;
  matched: boolean;
  value: unknown;
}

export interface EndpointExtractionField {
  key: string;
  value: unknown;
  attempts: EndpointExtractionAttempt[];
}

/** Value report of a single stage; the check-response response has the same shape as the extractions of a trial-run result. / 单个阶段的取值报告，check-response 的响应与试跑结果的 extractions 同形。 */
export interface EndpointStageReport {
  stage: EndpointTestStage;
  fields: EndpointExtractionField[];
  task_id?: string | null;
  raw_status?: unknown;
  status?: EndpointStandardStatus | null;
  video_url?: string | null;
  image_url?: string | null;
  /** Decoded byte size of the image taken via `image_b64`; null when no image can be decoded. / `image_b64` 取到的图片解码后的字节数；解不出图片为 null。 */
  image_bytes?: number | null;
  error?: string | null;
  result_id?: string | null;
  duration_seconds?: number | null;
}

export type TrialRunStatus = "queued" | "running" | "succeeded" | "failed";

/** The four segments of a connection test: submit, poll, extract result, fetch artifact. / 测试连接的四段：提交、轮询、取得结果、取回产物。 */
export const TRIAL_RUN_STAGES = ["submit", "poll", "result", "artifact"] as const;

export type TrialRunStage = (typeof TRIAL_RUN_STAGES)[number];

/** Reached segments are done; unreached ones are skipped in a terminal state and pending while running. It does not mark "which segment the failure fell in". / 到达过的段是 done；没到达的段终态上是 skipped、运行中是 pending。不标「失败落在哪一段」。 */
export type TrialRunStageState = "done" | "pending" | "skipped";

export interface TrialRunInfo {
  id: string;
  status: TrialRunStatus;
  provider: string;
  model: string;
  /** Whether this run produced video or images; read it to decide how to display the artifact. / 这一笔产的是视频还是图像；产物按哪种形态展示读它。 */
  media_type: ComfyuiMediaType;
  /** Epoch seconds. / epoch 秒。 */
  created_at: number;
  finished_at: number | null;
  api_call_id: number | null;
  /** The provider's id for this run (ComfyUI's `prompt_id`); null before submit or when unrecognized. / 供应商给这一笔的 id（ComfyUI 即 `prompt_id`）；提交之前或认不出时为 null。 */
  provider_job_id: string | null;
  stages: Partial<Record<TrialRunStage, TrialRunStageState>>;
  request: PreviewedRequest | null;
  submit_response: unknown;
  poll_responses: unknown[];
  result_response: unknown;
  extractions: Partial<Record<EndpointTestStage, EndpointStageReport>>;
  video_url: string | null;
  duration_seconds: number | null;
  error: string | null;
  /** Stable failure code behind `error`; bare exception text has no code, so null. / `error` 背后那个稳定失败码；裸异常文本没有码，为 null。 */
  error_code: string | null;
  /** What this failure code should prompt the user to do, using the same `GenerationAction` set as generation failures on the project page. / 这条失败码该让用户去做什么，取值同项目页生成失败那一套 `GenerationAction`。 */
  error_action: string | null;
  has_artifact: boolean;
}

export interface TrialRunModelRef {
  provider_id: string;
  model_id: string;
}
