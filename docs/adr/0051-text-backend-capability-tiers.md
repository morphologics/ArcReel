---
status: accepted
---

# Text backends are configured per task tier: simple / complex tiers + default fallback

The call sites of the text pipeline (TextGenerator) cover script generation, episode planning, script_plan content extraction / splitting (drama / narration / reference_video), project overview, and style-image analysis, and new ones keep being added as features evolve. If model settings mapped one-to-one to call sites, the configuration surface would grow linearly with call sites, and each new call site would either need another setting or fall outside existing settings and become a model source invisible to the user. We decided to configure by **task tier**: each call site is fixed in code to the **simple** or **complex** tier (simple = project overview, style-image analysis; complex = script generation, episode planning, all script_plan), and the user only configures "which text backend each tier uses" + one "default model" as a fallback; an unset tier falls back to the default model, and an unset default model goes to automatic inference. Resolution order is **project first**: project tier > project default model > global tier > global default model > automatic inference — "project default model" reads as "this project uses it overall"; it can be overridden by the more specific project tier, but it shadows global configuration.

## Explicitly not adopted

- **Per-key configuration by task type**: settings get out of control as call sites multiply; in practice, new call sites don't get settings added in step, eventually drifting into some call sites having unconfigurable, invisible model sources.
- **Simple / medium / complex three tiers**: existing call sites have no stable distinction between "medium" and "complex", and two tiers already cover all actual decision needs; the number of tiers is itself configuration surface, so err on the tight side.
- **Tiers also mapping the Claude Agent SDK's model**: the Agent SDK accepts only Anthropic-protocol-compatible credentials, while tiers can point at any text_backends provider; merging the two would create the illegal state of "a tier selected a model the agent can't use". The model for agent conversations and subagent reasoning is still determined by the agent provider configuration (see `docs/adr/0017`), and the settings page notes this boundary next to the text tiers.
- **Dimension-first resolution order** (project tier > global tier > project default > global default): once the global tiers are fully configured, the project default model would never take effect, making the field useless.

## Consequences

- The tier mapping is a code fact that users cannot change: new text call sites must be explicitly assigned a tier (the TextTaskType → tier mapping has an exhaustiveness check), and no text-generation call bypassing tier resolution is allowed.
- The simple tier carries style-image analysis, so its model must support image input (vision); if the capability isn't met at resolution time, it errors directly with no silent model switch.
- The list of call sites covered by each tier is shown on the settings page as a persistent description (the same component in three places: global settings / project settings / creation wizard); changes in coverage must update the description copy in step (zh / en / vi).
