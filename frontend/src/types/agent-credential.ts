/**
 * Types for Agent Anthropic credentials + preset provider catalog.
 *
 * Aligned with the Pydantic models in backend server/routers/agent_config.py.
 *
 * Agent Anthropic 凭证 + 预设供应商目录类型。
 *
 * 与后端 server/routers/agent_config.py 的 Pydantic 模型对齐。
 */

export interface PresetProvider {
  id: string;
  display_name: string;
  icon_key: string;
  messages_url: string;
  discovery_url: string | null;
  default_model: string;
  suggested_models: string[];
  docs_url: string | null;
  api_key_url: string | null;
  notes: string | null;
  api_key_pattern: string | null;
  is_recommended: boolean;
}

export interface PresetProvidersResponse {
  providers: PresetProvider[];
  custom_sentinel_id: string;
}

/** Model tiers of an Agent credential: the main model and four routes haiku, sonnet, opus, subagent. / Agent 凭证的模型档位：主模型与 haiku、sonnet、opus、子智能体四个路由。 */
export type AgentCredentialTier = "model" | "haiku" | "sonnet" | "opus" | "subagent";

/** Pricing status of one tier, looked up by the server against the current model catalog. / 一个档位的定价状态，由服务端按当前模型目录现查。 */
export interface AgentCredentialTierStatus {
  tier: AgentCredentialTier;
  /** Model name the tier actually calls; null means the tier is empty and Claude Code decides which model to call. / 档位实际调用的模型名；null 表示档位留空，由 Claude Code 决定调用哪个模型。 */
  model_name: string | null;
  /** Display name of the matched model entry; null when no entry matches. / 对应到的模型条目显示名；对应不上条目时为 null。 */
  entry_display_name: string | null;
  /** The match comes from the model entry the user chose for the tier, not automatic matching by model name. / 对应关系取自用户为档位选择的模型条目，而不是按模型名自动对应。 */
  manually_bound: boolean;
  /** The tier stores a binding but the bound entry is no longer in the model catalog: matching has fallen back to model name; the binding can still be removed. / 档位存着绑定、但所绑条目已不在模型目录里：对应关系已回到按模型名，绑定仍可解除。 */
  binding_stale: boolean;
  priced: boolean;
}

export interface AgentCredential {
  id: number;
  preset_id: string;
  display_name: string;
  icon_key: string | null;
  base_url: string;
  api_key_masked: string;
  model: string | null;
  haiku_model: string | null;
  sonnet_model: string | null;
  opus_model: string | null;
  subagent_model: string | null;
  /** Agent credential multiplier, applied to the reference cost of each call with this credential; 0 means not billed. / Agent 凭证倍率，乘在这条凭证每次调用的参考费用上；0 表示不计费用。 */
  multiplier: number;
  /** Pricing status of the five tiers in fixed order: main model, haiku, sonnet, opus, subagent. / 五个档位的定价状态，顺序固定：主模型、haiku、sonnet、opus、子智能体。 */
  tiers: AgentCredentialTierStatus[];
  is_active: boolean;
  created_at: string | null;
}

export interface CreateAgentCredentialRequest {
  preset_id: string;
  display_name?: string | null;
  base_url?: string | null;
  /** Mutually exclusive with from_custom_provider_id. / 与 from_custom_provider_id 二选一。 */
  api_key?: string;
  /** When given, the server copies the key from that custom provider (the request must not also carry api_key). / 给出时由服务端从该自定义供应商复制密钥（请求中不得再带 api_key）。 */
  from_custom_provider_id?: number;
  model?: string | null;
  haiku_model?: string | null;
  sonnet_model?: string | null;
  opus_model?: string | null;
  subagent_model?: string | null;
  multiplier?: number;
  /** Tier -> model entry ("model maker/official model name"); updates merge per tier, null removes that tier's binding. / 档位 → 模型条目（「模型厂商/官方模型名」）；更新时按档位合并，null 解除该档位的绑定。 */
  tier_bindings?: Partial<Record<AgentCredentialTier, string | null>>;
  activate?: boolean | null;
}

export type UpdateAgentCredentialRequest = Partial<
  Omit<CreateAgentCredentialRequest, "preset_id" | "activate" | "from_custom_provider_id">
>;

export interface ProbeResult {
  success: boolean;
  status_code: number | null;
  latency_ms: number | null;
  error: string | null;
}

export type DiagnosisCode =
  | "openai_compat_only"
  | "auth_failed"
  | "model_not_found"
  | "model_required"
  | "rate_limited"
  | "network"
  | "timeout"
  | "unknown";

export interface SuggestionAction {
  kind: "check_api_key" | "run_discovery" | "see_docs";
  suggested_value: string | null;
}

export interface TestConnectionResponse {
  overall: "ok" | "fail";
  messages_probe: ProbeResult;
  diagnosis: DiagnosisCode | null;
  suggestion: SuggestionAction | null;
  /** Full address actually requested this time; same value as the preview below the input. / 本次实际请求的完整地址；与输入框下方的预览同值。 */
  messages_url: string;
}

export interface TestConnectionRequest {
  preset_id?: string | null;
  base_url?: string | null;
  api_key: string;
  model?: string | null;
}
