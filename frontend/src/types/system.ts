export interface SystemConfigSettings {
  default_video_backend: string;
  default_video_backend_i2v?: string;
  default_video_backend_r2v?: string;
  default_image_backend: string;
  default_image_backend_t2i?: string;
  default_image_backend_i2i?: string;
  default_text_backend: string;
  default_audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
  text_backend_simple: string;
  text_backend_complex: string;
  video_generate_audio: boolean;
  video_poll_timeout_seconds: number;
  anthropic_api_key: { is_set: boolean; masked: string | null };
  anthropic_base_url: string;
  anthropic_model: string;
  anthropic_default_haiku_model: string;
  anthropic_default_opus_model: string;
  anthropic_default_sonnet_model: string;
  claude_code_subagent_model: string;
  agent_session_cleanup_delay_seconds: number;
  agent_max_concurrent_sessions: number;
  /** GitHub raw proxy prefix for market sources; empty string means direct connection. / 市场源 GitHub raw 代理前缀；空串即直连。 */
  market_github_proxy_prefix?: string;
}

export interface SystemConfigOptions {
  video_backends: string[];
  image_backends: string[];
  text_backends: string[];
  audio_backends?: string[];
  provider_names?: Record<string, string>;
  /** "provider_id/model_id" -> model name written per Accept-Language; a missing key falls back to the model id. / "provider_id/model_id" → 按 Accept-Language 成文的模型名；缺键即退回 model id。 */
  model_names?: Record<string, string>;
}

/** Prefilled values for new TTS projects; audio_backend is null when no audio provider is configured. / 新建 TTS 项目的预填值；未配置任何音频供应商时 audio_backend 为 null。 */
export interface NarrationDefaultsResponse {
  audio_backend: string | null;
  narration_voice: string;
  narration_speed: number | null;
}

export interface TtsModelCapabilitiesResponse {
  supports_speed: boolean;
}

export interface GetSystemConfigResponse {
  settings: SystemConfigSettings;
  options: SystemConfigOptions;
}

/** Task type bucket keys (docs/adr/0054). Internal code term, not shown directly in UI copy. / 任务类型桶键（docs/adr/0054）。代码内部术语，界面文案不直接呈现。 */
export type GenerationTypeBucket = "t2i" | "i2i" | "i2v" | "r2v";

/**
 * Model (provider/model) the backend auto-selects for each purpose when both project and global layers are empty, on the same basis as execution-time resolution;
 * null when no provider is available. Auto-inference is the same for all text tiers, so they are merged into one item.
 *
 * 各用途在项目与全局各层都留空时后端自动选中的模型（provider/model），与执行期解析同口径；
 * 没有可用供应商时为 null。文本各档位的自动推断相同，合为一项。
 */
export type AutoBackendsResponse = Record<GenerationTypeBucket | "text", string | null>;

/** Candidates for a single media_type: the full default layer + per task type bucket subsets filtered by capability. / 单一 media_type 的候选：默认层全量 + 各任务类型桶按能力过滤后的子集。 */
export interface MediaCandidates {
  default: string[];
  buckets: Partial<Record<GenerationTypeBucket, string[]>>;
}

export interface ModelCandidatesResponse {
  image: MediaCandidates;
  video: MediaCandidates;
  /** Provider id -> name written per Accept-Language; same basis for built-in and custom. / 供应商 id → 按 Accept-Language 成文的名字；内置与自定义同一口径。 */
  provider_names: Record<string, string>;
  /** "provider_id/model_id" -> model name written per Accept-Language; covers candidates of all media types. / "provider_id/model_id" → 按 Accept-Language 成文的模型名；覆盖全部媒体类型的候选。 */
  model_names: Record<string, string>;
}

export interface SystemVersionReleaseInfo {
  version: string;
  tag_name: string;
  name: string;
  body: string;
  html_url: string;
  published_at: string;
}

export interface GetSystemVersionResponse {
  current: { version: string };
  latest: SystemVersionReleaseInfo | null;
  has_update: boolean;
  checked_at: string;
  update_check_error: string | null;
}

/** "Already seen" state of the first-use tour -- instance-level; unset counts as not seen. / 首次使用引导的「已看过」状态 —— 实例级，未设置视为未看过。 */
export interface OnboardingStatus {
  seen: boolean;
}

/**
 * Options saved for the studio on the server (`/user-options`), shared by the member's devices. Only keys the member
 * has set are present; the option registry in `preferences/user-options.ts` holds the defaults. `null` in an update
 * removes the key (back to its default).
 */
export interface StudioUserOptions {
  default_editor?: "arcreel" | "openreel";
  mode?: "easy" | "advanced";
  /** The YouReel Editor's "Include motion transitions" (set in the Editor; kept here for the member). */
  motion_transitions?: "on" | "off";
  /** The YouReel Editor's "Keep messages open until closed" (set in the Editor; kept here for the member). */
  keep_toasts?: "on" | "off";
}

export type StudioUserOptionsPatch = { [K in keyof StudioUserOptions]?: StudioUserOptions[K] | null };

export interface SystemConfigPatch {
  default_video_backend?: string;
  default_video_backend_i2v?: string;
  default_video_backend_r2v?: string;
  default_image_backend?: string;
  default_image_backend_t2i?: string;
  default_image_backend_i2i?: string;
  default_text_backend?: string;
  default_audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
  text_backend_simple?: string;
  text_backend_complex?: string;
  video_generate_audio?: boolean;
  video_poll_timeout_seconds?: number;
  anthropic_api_key?: string;
  anthropic_base_url?: string;
  anthropic_model?: string;
  anthropic_default_haiku_model?: string;
  anthropic_default_opus_model?: string;
  anthropic_default_sonnet_model?: string;
  claude_code_subagent_model?: string;
  agent_session_cleanup_delay_seconds?: number;
  agent_max_concurrent_sessions?: number;
  market_github_proxy_prefix?: string;
}
