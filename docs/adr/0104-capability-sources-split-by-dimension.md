---
status: accepted
---

# Each dimension of model capability has only one source: model facts are recorded on entries, channel facts are declared by code, and channel restrictions are recorded on provider models

The model catalog delivers model capabilities with official sync, but some capabilities are not facts about the model itself; they depend on how the invocation channel packages requests, and they fork by execution sub-path: the same video model may have a different audio-track form under image-to-video and reference-to-video; Kling only enables the last frame at the pro tier. We split sources by dimension, each dimension has only one source, and neither side is used to validate the other.

- **Model facts are recorded on model entries and delivered with official sync**: for text, whether image input is accepted, whether structured output is supported, and maximum output tokens; for images, whether text-to-image and image-to-image are supported; for video, selectable durations, resolutions, and the linked constraints between duration, resolution and reference images.
- **Channel facts are declared in code by built-in invocation channels or invocation endpoints**: video input modes (text-to-video, first frame, last frame), reference image and reference audio caps, prompt length cap, audio-track form, and whether an image invocation endpoint implements text-to-image or image-to-image.

## Rules

- Capabilities are declared per model. The provider level has no capability declaration of its own; which media types and capabilities a provider supports is aggregated from its provider models.
- For images, text-to-image and image-to-image are each a dimension on both sides, with different meanings: the model entry says whether the model accepts it, the channel declaration says whether this channel implements it. The effective capability is the part both satisfy, and both the candidates for task type buckets and validation at execution use this one result.
- Invocation channels do not carry their own duration or resolution checks duplicating the model entry; durations are passed through as-is, and the upstream rejects them when out of range. The Vidu endpoint-level duration correction retained by `docs/adr/0018` is not covered by this: it corrects differences in legal durations of the same model across different API endpoints, which is a channel fact.
- When an invocation channel meets an unknown model, it first matches the capabilities of the same generation by name family, taking names from the official model name and model aliases of the entry the provider model maps to, or the API model name when no entry matches (see `docs/adr/0105`); when nothing matches, it uniformly falls back to supporting only text-to-video and first frame, no last frame, and reference image and reference audio caps of 0.
- When a channel disagrees with the model entry or the channel declaration, it is expressed with a capability override on the provider model, without modifying the model entry; the entry is modified only when a fact about the model itself is wrong. Capability overrides can override both kinds of dimension, including audio-track form; each dimension is replaced as a whole and has the highest priority.
- Only user-responsible provider models carry capability overrides, i.e. provider models of custom providers and provider models added by users under built-in providers. Officially listed provider models do not carry capability overrides (see `docs/adr/0099`).
- ComfyUI endpoint capabilities are derived only from node bindings and do not accept capability overrides (see `docs/adr/0082`). The only exception is the selectable durations of ComfyUI video models: this is a preference that `docs/adr/0082` lets users add to or remove from beyond the workflow's native duration, not a capability declaration, and it is stored as a capability override on the selectable-duration dimension; when the endpoint cannot drive duration, that dimension does not exist and the override has no effect.

## Considered Options

- **Record channel facts such as input modes on model entries as well**: a new generation of models could use all capabilities without a release. But entries cannot express forks by execution sub-path; third-party data sources also have no such data, so it would amount to manually maintaining data tightly coupled to invocation channel code.
- **Optimistically fall back to the capabilities of the same provider's newest generation for unknown models**: users could use reference images without opening them up manually, but when a new generation changes request parameters, submission would be rejected by the upstream. A conservative fallback still allows the main image-to-video route, and duration and resolution also come from the entry, unaffected by the fallback.
- **Have official data deliver capability overrides for provider models**: could express differences between official channels and let older instances use a new generation's full capabilities early. But currently the same model has identical capabilities under different built-in providers, so there is no use case; if needed later, it is just a new field.
- **Layer user capability overrides on officially listed provider models without affecting sync**: would cater to users who point built-in providers at relay services. But built-in providers represent official channels, and relay services should use custom providers (see `docs/adr/0099`).

## Consequences

- Before an invocation channel supports a new generation of video model, it has only conservative capabilities even if already in a built-in provider list. The catalog data repository should list it only after the invocation channel supports it, marked with a minimum application version (see `docs/adr/0103`).
- Provider models of custom providers no longer store selectable durations separately; selectable durations come from the corresponding entry, and channel restrictions are written into capability overrides. Presets that infer duration from the model name are used only for pre-filling when creating a model entry, and as the runtime fallback for provider models that do not match an entry (see `docs/adr/0105`).
