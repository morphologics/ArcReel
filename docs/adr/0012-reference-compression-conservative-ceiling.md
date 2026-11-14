---
status: accepted
---

# Reference image compression uses a conservative universal ceiling + passive 413, touching only upload copies

I2I / I2V / R2V scenarios often send several large asset images (character/scene/prop sheets, the previous storyboard image, reference images) to the provider as reference images; after base64 embedding the total request body easily exceeds each vendor's limit and the call fails. We decided to do active compression + pre-flight downgrading on **reference upload copies** at the `media_generator` choke-point layer (the single convergence point for all generation), keeping one passive 413 fallback; the compression logic is concentrated in `lib/references/reference_compression.py` and only handles temporary copies that are deleted after sending.

## Decision

- **Compress only reference upload copies**: source asset files and generation outputs keep full quality unchanged (outputs have no compression on save). So "generated 4K but didn't get 4K" cannot happen under this mechanism — reference image resolution does not determine output resolution.
- **Don't use each vendor's exact byte limit; use a single conservative universal ceiling**: by default total request ≤8MB / single image ≤4MB (clearing all built-in providers' known documented limits), stored in ConfigService and overridable per provider; **passively catch HTTP 413 → drop one tier and retry** to deal with "unknown/possibly changing limits" (e.g. relays, missing or outdated documentation).
- **Uniform JPEG q92 + 4:4:4**: every provider accepts JPEG, zero format branching; q92/4:4:4 is the "visually lossless" tier, minimising generational damage from "we compress once + the provider compresses again internally", and protecting edges of sheets with text.
- **The baseline primarily downscales resolution** (long edge ≤2048, conditional pass-through, only processed when over the limit); the downgrade ladder is likewise resolution-first, with a quality floor of q80. Research shows resolution is the first lever and low-risk; it is the high-frequency artefacts introduced by lowering quality that damage conditioning (i2v first frames are especially sensitive).
- **Differentiated by role**: multi-image reference arrays go through the full baseline + downgrade ladder; a single first/last frame is only re-encoded when over the byte limit and is **never downscaled**, to protect the sensitive first frame and avoid breaking Sora's exact pixel matching of the input image.

## Why not each vendor's exact byte limit

Hard-coding each vendor's request body limit violates the principle of "don't guess/hard-code external provider data" (most providers don't state an overall limit explicitly, and it changes often; relays each differ and can't be verified). The conservative universal ceiling is an ArcReel-side safety policy constant (analogous to a timeout default) and doesn't claim to be any vendor's real number; the passive 413 handles self-correction. The cost: occasionally "ceiling configured too high" leads to one extra failed 413 call — but a 413 is rejected at ingress and isn't billed, only adding latency, and relays that actually serve images have basically all raised their body limits, so it rarely triggers.

## Consequences

- Each backend's error path needs to let 413 be recognisable: `vidu`/`dashscope` currently wrap `httpx.HTTPStatusError` in `RuntimeError`, losing the status code, and need to be normalised to keep the status code.
- R2V's original per-task compression (`_compress_references_to_tempfiles`) and the `except RequestPayloadTooLargeError` block that was never raised — and was therefore dead code — are removed/migrated together; compression is handled uniformly by the choke-point layer, avoiding double compression.
- Sora's exact pixel matching of the input image is an independent gap that predates this requirement (the first frame isn't padded/resized to a valid tier) and is outside the scope of this design; this design guarantees it doesn't make it worse.
