---
status: accepted
---

# Media size: aspect ratio is determined solely by aspect_ratio and always takes priority; resolution only determines clarity

The output size (width×height) of storyboard images/videos was previously derived from each backend's own static "(resolution tier, aspect ratio) → pixels" table, or by passing the "resolution value" down directly as the size. The problem is that **a size carries its own aspect ratio**: as soon as a resolution value carries aspect ratio information (wrong values in a hard-coded table, or a custom value entered by the user such as `1920x1080`), it overrides the project's `aspect_ratio`, producing images with the wrong aspect ratio. Layer-by-layer investigation (8 image backends + 8 video backends + custom resolution values) confirmed this is not a single-point bug but a **mechanism defect**:

- OpenAI image `OPENAI_IMAGE_SIZE_MAP` wrongly assigned 1K `3:4`/`4:3` the pixels of the neighbouring ratios 9:16/16:9 (right orientation, wrong magnitude by ±25~31%); the I2I path (`images.edit`) doesn't pass size at all, so the aspect ratio is decided by the upstream default (the user's real-world test of wrong output aspect ratio was exactly this path).
- DashScope qwen's registry `resolutions` are pixel values carrying an aspect ratio (e.g. 16:9's `2688*1536`); when the project was set to 9:16, the old logic `if explicit: return explicit` passed it down as-is and the aspect ratio was lost.
- The OpenAI/NewAPI video table contained dall-e-style 4:7 values and 1080, which isn't evenly divisible, and unknown/custom values passed through as-is could be invalid.

The established product-level principle: **`aspect_ratio` is the sole source of truth for output aspect ratio and always takes priority; resolution (a preset tier or a custom value) only determines the scale of clarity, not the aspect ratio; achieve as exact an aspect ratio as possible within each backend's size constraints; when resolution is missing but a size is required to control the aspect ratio, fall back to a default of 720P. Outputs with the wrong aspect ratio are unusable.**

## Considered Options

- **Fix each backend's own static table** — rejected: wrong values are scattered in many places, adding aspect ratios/backends would still require re-copying tables, and it doesn't fix the root cause of "resolution values carrying an aspect ratio override aspect_ratio".
- **A unified size mechanism `lib/backends/aspect_size.py`** — adopted. Compute the exact size from the aspect ratio and the short edge: valid size = `(aw·round_to·t, ah·round_to·t)` (`aw:ah` being the reduced ratio), which naturally has zero aspect ratio deviation + is divisible by `round_to`; each backend clamps by its own pixel constraints (`max_long_edge` / `max_total_pixels`). Resolution is uniformly normalised to a "short edge": tiers by table lookup, custom `WxH` takes `min` to strip the aspect ratio, `None` falls back to 720.

## Consequences

- **① Custom resolution values have their aspect ratio stripped**: if the user enters `1920x1080` (16:9) but the project wants 9:16, only `min=1080` is taken as the short edge and the aspect ratio still follows 9:16. The frontend `ResolutionPicker` still allows free input; normalisation is in the backend (the sole enforcement point).
- **② The unified mechanism replaces each backend's static tables**: `OPENAI_IMAGE_SIZE_MAP` and DashScope's `_SIZE_BY_RATIO`/`_WAN_PIXELS_BY_BUDGET`/`_EDIT_SIZE_BY_RATIO` are deprecated; backends that accept arbitrary pixels (gpt-image-2, DashScope qwen-image-2.0 fusion series and wan method-two pixels, qwen-edit-plus/max) have zero aspect ratio deviation. OpenAI image I2I and T2I pass size down symmetrically.
- **③ Billing is decoupled from output size**: the main OpenAI image path is billed by token (covering the vast majority), and the `(resolution, aspect_ratio) → size` reverse lookup in the billing fallback is removed. **Cost**: the fallback when the SDK doesn't return usage from now on loses size-differentiated cost (landing on the default `1024x1024` tier) — we deliberately don't introduce an extra size source of truth for the fallback; the main path is unaffected.
- **④ Unified short-edge tiers (2K=1440, 4K=2160) change the existing output of several pixel backends** (all exact aspect ratios):
  - OpenAI image 2K: 9:16 from about `2048×3584` (4:7) → `1440×2560`, roughly half the pixels, lower cost;
  - DashScope wan 4K: 9:16 from `3072×5376` (long edge exceeds 4K) → `2160×3840` (a long edge of 3840 is genuine 4K), roughly half the pixels;
  - DashScope wan/qwen 2K: 9:16 from `1536×2688` (4:7 deviation) → `1440×2560` (exact, slightly smaller);
  - DashScope wan 1K: 9:16 from `768×1344` → `1008×1792` (larger).
- **⑤ Some backends have inherent constraints that cannot be eliminated, treated as legitimate exceptions**:
  - **sora video**: size is a fixed tier enum and **does not accept arbitrary WxH**. So video doesn't go through `aspect_size` and instead **snaps to the valid tier with the closest aspect ratio**. Valid tiers are graded by model (confirmed from the official model page; the openai SDK `VideoSize` Literal lags behind): sora-2 (base) only has 720p exact tiers (`720x1280`/`1280x720`), with clarity capped at the 720 level (clarity yields to aspect ratio, in line with the principle); sora-2-pro additionally has 1080p exact tiers (`1080x1920`/`1920x1080`). The dall-e-style 4:7 tiers (`1024x1792`/`1792x1024`) violate aspect-ratio-first and have been removed (`lib/backends/video_backends/openai.py`). NewAPI video is a generic width/height aggregation port and goes through `aspect_size` (aligned to multiples of 8, giving standard sizes such as `1920×1080`).
  - **ark Seedream 2K**: because of a lower bound on total pixels (≥3.68M) it keeps about 0.1% aspect ratio deviation; the existing exact table is kept (migrating would regress precision).
  - **DashScope qwen classic series** (`qwen-image`/`-plus`/`-max`) only has 5 fixed tiers and doesn't accept arbitrary pixels: no preset registered, not recommended officially, and not covered by this mechanism.
- **String backends with an independent aspect ratio need no size logic changes**: Gemini/Grok/Vidu/ark/DashScope video pass `aspect_ratio`/`ratio` down as a separate SDK field, orthogonal to `resolution` (verified via context7 that Veo's `aspect_ratio` and `resolution` are separate fields; Grok has the same shape); they don't squeeze the aspect ratio into a pixel size, so they never had the size bug; only aspect-ratio independence guard tests and comments are added.

Belongs to the same category as `docs/adr/0001`, "generation parameters resolved at execution time" — size is likewise derived precisely in the execution layer from aspect ratio + resolution, not frozen in advance.
