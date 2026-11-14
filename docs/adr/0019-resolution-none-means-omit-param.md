---
status: accepted
---

# Pure-clarity parameters such as resolution are "not sent" when unconfigured, rather than filled with our own default

Keeping our own hard-coded per-model default resolution tables (`DEFAULT_VIDEO_RESOLUTION` etc.) amounts to guessing default values on behalf of each provider, scattered and hard to maintain, and makes "unconfigured" indistinguishable from "explicitly chose a given tier". We decided to delete these default tables of ours; resolution, as a **pure clarity** parameter, is resolved by project → legacy → custom model default → None, where None means "do not include this parameter when calling the SDK" and the SDK's own default applies, rather than our fallback — handing the default back to the SDK.

## Consequences

- No system-level global default resolution is added (custom providers' model-level defaults already cover system-level semantics).
- **Boundary**: this decision only covers backends where "resolution is orthogonal to aspect ratio and can be omitted". When the size must **carry the aspect ratio** (e.g. OpenAI Sora size, image size), `aspect_size` from `docs/adr/0011` is always computed and passed down instead, never omitted as None — that path is the opposite of this decision, and the two do not conflict. Normalisation of each vendor's native naming (OpenAI size / Grok resolution / Gemini image_size) has also been folded into the centralised computation of `docs/adr/0011`, and is no longer maintained by each backend as static translation tables.
- The reference-to-video route no longer has a fallback-resolution exception; both video routes apply this decision on the same basis, see `docs/adr/0086`.
