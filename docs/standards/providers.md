---
paths:
  - "lib/backends/**"
  - "lib/custom_provider/**"
  - "lib/config/**"
  - "lib/billing/**"
  - "lib/agent/agent_provider_catalog.py"
  - "lib/prompts/prompt_builders*.py"
  - "agent_runtime_profile/**"
  - "docs/api-docs/**"
---

# Provider capabilities and contracts

## Capability data

### Each capability dimension has exactly one source; do not write a second copy of capability values elsewhere

Capabilities take their source per dimension (`docs/adr/0104`):

- Model facts are recorded on the model catalog entry, including the selectable durations, resolutions, the linked constraints between duration, resolution and reference images, and the text and image capability flags. When `supported_durations` is missing, raise an error directly; do not fall back implicitly (`docs/adr/0018`).
- Channel facts are declared in code by the calling channel or calling endpoint, from the same source as request construction and runtime checks (`docs/adr/0054`, `docs/adr/0056`). These facts include video input mode, reference image and reference audio limits, prompt length limit and audio track shape.
- Channel restrictions are expressed only as capability overrides on provider models the user is responsible for; the entry is not modified.

The following all write a second copy of capability values outside the source: a separate constant that duplicates a tier; a caller or configuration UI filling in its own default when the source has no value; a calling channel writing a duration or resolution check that duplicates the entry; templates in `agent_runtime_profile/` and `lib/prompts/` hard-coding durations or resolution tiers instead of having the orchestration layer inject them from the sources above. After the provider or catalog data changes, the two copies sooner or later disagree: the tiers the user can select don't match the actual request, or the agent plans with old tiers and asks for durations it cannot get at runtime.

Allowed when:

- Exceptions listed explicitly in an ADR: the Vidu endpoint-level duration correction in `docs/adr/0018`; the conservative fallback for unrecognized models and the duration presets pre-filled from the model name when creating a new entry, as specified in `docs/adr/0104` and `docs/adr/0105`.
- Copies kept during a transition period: a consistency test locks the two copies together, and the PR names the issue responsible for deleting it.
- Rule-of-thumb seconds describing pacing in prompts (e.g. "全片平均 3–5 秒/分镜" ("3–5 seconds per storyboard on average across the whole film")) are not capability tiers.
