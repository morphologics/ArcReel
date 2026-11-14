# ArcReel Integration Research Report for Three Vendors (Alibaba Bailian / Kling / MiniMax)

**Research date**: 2026-05-29
**Purpose**: Input material for the follow-up PRD and design documents; evaluates onboarding the three vendors as ArcReel preset providers (text / image / video)
**Currency**: Mainland China site prices in RMB are authoritative; differences on the international sites are noted
**Pricing note**: Each vendor's prices fluctuate with promotions. Prices in this report are for selection reference only, and official sources are distinguished from third-party sources; before launch, the official consoles must be treated as authoritative

**Information sources and reliability statement**:
- Alibaba Bailian: the model list has been checked item by item against the [official "Model Catalog" page](https://help.aliyun.com/zh/model-studio/models) (page updated 2026-05-21); model IDs follow the official page.
- MiniMax: the model list has been checked against the release timeline on the [official "Model Releases" page](https://platform.minimaxi.com/docs/release-notes/models); the flagship assessment is accurate.
- Kling: the model list and pricing have been verified first-hand against Kling's official documentation ([video models](https://klingai.com/document-api/apiReference/model/videoModels) / [image models](https://klingai.com/document-api/apiReference/model/imageModels)) and the [official pricing page](https://klingai.com/dev/pricing); model IDs, capability matrix, and credit unit prices (video 1 credit = ¥1, image 1 credit = ¥0.025) follow the official sources.
- Alibaba's exact per-image prices for `qwen-image-2.0` / `wan2.7-image` and the detailed tiers behind the login wall still need console confirmation; items marked "check in console" have not been confirmed first-hand.

---

## 0. Research Scope and Positioning

This report is a **research-oriented** compilation of material covering the capabilities, API protocols, and official pricing of the **current latest high-quality models** from the three vendors, focused on the practical needs of ArcReel's Chinese web-novel-to-video workflow (script → character/scene design images → storyboard images → video → compositing).

**Out of scope** (decisions for the PRD / design document stage): onboarding order, phase split, cost circuit-breaker/degradation rules, backend class design, database schema.

Model selection principle: only include the latest high-quality models suitable for ArcReel integration; outdated or superseded old versions are only marked "superseded" where necessary, without further detail.

---

## 1. Conclusion First: Recommended Models for Evaluation

| Vendor | Modality | Model | Model ID | Official pricing | Protocol |
|---|---|---|---|---|---|
| Alibaba Bailian | text | Qwen-Plus (balanced, production volume) | `qwen-plus` | Tier 0-128K: ¥0.8 in / ¥2 out (per million tokens) | OpenAI-compatible ✅ |
| Alibaba Bailian | text | Qwen3.6-Plus (latest vision-language Plus) | `qwen3.6-plus` | ¥2 in / ¥12 out; cache hit ¥0.2 | OpenAI-compatible ✅ |
| Alibaba Bailian | text | Qwen3-Max (stable flagship) | `qwen3-max` | Tier 0-32K: ¥2.5 in / ¥10 out | OpenAI-compatible ✅ |
| Alibaba Bailian | text | Qwen3.7-Max (latest flagship, agent) | `qwen3.7-max` | ¥12 in / ¥36 out (256K-1M); limited-time 50% off to ¥6/¥18 (until 2026-06-22) | OpenAI-compatible ✅ |
| Alibaba Bailian | image | Qwen-Image-2.0-Pro (officially recommended, comics/storyboards/text rendering) | `qwen-image-2.0-pro` / `qwen-image-2.0` (accelerated version) | Per image (check in console) | DashScope sync |
| Alibaba Bailian | image | Wan 2.7 Image (portrait realism/image sets) | `wan2.7-image` / `-image-pro` | Per image (check in console, failures not charged) | DashScope sync/async |
| Alibaba Bailian | video | HappyHorse 1.0 first-frame-to-video | `happyhorse-1.0-i2v` | 720P ¥0.9/s, 1080P ¥1.6/s | DashScope async |
| Alibaba Bailian | video | HappyHorse 1.0 reference-to-video (R2V) | `happyhorse-1.0-r2v` | 720P ¥0.9/s, 1080P ¥1.6/s | DashScope async |
| Alibaba Bailian | video | HappyHorse 1.0 text-to-video | `happyhorse-1.0-t2v` | 720P ¥0.9/s, 1080P ¥1.6/s | DashScope async |
| Alibaba Bailian | video | Wan 2.7 image-to-video (first/last frame/continuation/audio) | `wan2.7-i2v-2026-04-25` | Per second by resolution tier (check in console) | DashScope async |
| Kling | image | Kling Omni Image O1 (multi-reference character consistency, IP/comics/serials) | `kling-image-o1` (1-10 reference images) / `kling-v3-omni` (4K + image sets) | ¥0.2/image (1K-2K); v3-omni 4K ¥0.4 | JWT + async |
| Kling | video | Kling v3 / v3-omni (flagship, multi-shot + 4K + subject control) | `kling-v3` / `kling-v3-omni` | std ¥0.6/s, pro ¥0.8/s, 4K ¥3/s (silent, list price) | JWT + async |
| Kling | video | Kling v2-6 (the only one supporting in-video voice control) | `kling-v2-6` | pro with audio ¥1.0/s, silent ¥0.8/s | JWT + async |
| Kling | video | Kling 2.5 Turbo (value workhorse) | `kling-v2-5-turbo` | std silent ¥0.6/s, pro with audio ¥1.0/s | JWT + async |
| MiniMax | text | MiniMax-M2.7 (flagship) | `MiniMax-M2.7` | ¥2.1 in / ¥8.4 out; cache read ¥0.42 | OpenAI/Anthropic dual-compatible ✅ |
| MiniMax | image | image-01 (with character consistency) | `image-01` | ¥0.025/image (charged only on success) | Proprietary REST (single step returns URL) |
| MiniMax | video | Hailuo 2.3 (T2V + I2V, high quality) | `MiniMax-Hailuo-2.3` | 768P 6s ¥2 / 10s ¥4; 1080P 6s ¥3.5 | Proprietary REST (two-step file_id) |
| MiniMax | video | Hailuo 2.3-Fast (I2V only, half price) | `MiniMax-Hailuo-2.3-Fast` | 768P 6s ¥1.35 / 10s ¥2.25; 1080P 6s ¥2.31 | Same as above |
| MiniMax | video | S2V-01 (dedicated character-consistency R2V) | `S2V-01` | Resource pack 1.5 credits/video (about ¥3) | Same as above, `subject_reference` |

**Protocol overview**: For text, Alibaba and MiniMax both use OpenAI-compatible APIs (the existing backend can be reused); Kling has no text model. For images and video, Alibaba uses DashScope (images are mainly sync — Qwen-Image-2.0 is sync only, Wan-Image supports both sync and async; video is uniformly async tasks), Kling uses JWT, and MiniMax uses its own REST (images return a URL in one step, video uses a two-step file_id).

---

## 2. R2V (Reference-to-Video / Character Consistency) Capability Comparison

> R2V = using one or more reference images to generate video that keeps **character/subject consistency** in a new scene. It is the core capability for "the protagonist appearing across scenes" in web-novel-to-video. Field names, number of reference images, and model support differ greatly between vendors, so it is compared separately here.

### 2.1 Side-by-side Comparison

| Vendor | R2V model ID | Reference field name | Reference image limit | Consistency target | Audio | Notes |
|---|---|---|---|---|---|---|
| Alibaba HappyHorse | `happyhorse-1.0-r2v` | DashScope input reference image field | Multiple characters | Character + subject | ✅ With audio | 720P/1080P, 3-15s, native audio-video |
| Alibaba Wan 2.7 | `wan2.7-r2v` | Image N / Video N reference format | Multiple characters (mixed image + video references) | Character + subject | ✅ Can pass voice timbre/video subject | 720P/1080P, 2-10s, **the only one supporting video-referenced subjects** |
| Alibaba Wan 2.6 | `wan2.6-r2v` / `wan2.6-r2v-flash` | Same as above | Multiple characters | Character | ✅ | flash version for fast generation |
| Kling v3 / v3-omni | `kling-v3` / `kling-v3-omni` | Subject control parameters | Video character subject + multi-image subject | Character + subject | Officially marked ❌ voice | Strongest official "subject control"; v3-omni additionally supports video reference (3-10s) |
| Kling O1 | `kling-video-o1` | Multi-image subject + video reference | Multi-image subject | Character + video-driven | ❌ | Includes video reference (video editing), 3-10s |
| Kling v1-6 (old) | `kling-v1-6` | `image_list[]` | Multi-image reference-to-video (old version) | Multi-subject fusion | Depends on model | Multi-image reference + multimodal video editing |
| MiniMax S2V-01 | `S2V-01` | `subject_reference[]` | 1 image (face only) | Single-character face | — | Industry first single-image driving; environment may be slightly distorted |

### 2.2 Key Boundaries

- **Kling R2V has been upgraded to "subject control"** (official, first-hand): currently the strongest is the "subject control (video character subject + multi-image subject)" in `kling-v3` / `kling-v3-omni`; `kling-video-o1` supports multi-image subject + video reference; the multi-image reference-to-video of the old `kling-v1-6` is an early capability, now surpassed by v3/o1.
- **MiniMax Hailuo 2.3 / 2.3-Fast do not support R2V**. Hailuo 2.3 only has T2V + I2V, and 2.3-Fast is I2V only. MiniMax's character-consistency R2V requires the separate **S2V-01** model, which only supports a single face reference.
- **Alibaba has the most complete R2V capability**: both HappyHorse-1.0-r2v and Wan2.7-r2v support **multi-character** references with native audio; Wan2.7-r2v is the **only model on the Alibaba side supporting "video-referenced subjects"**, and Kling v3-omni/o1 also support video reference.

### 2.3 R2V Fit for Web-novel Scenarios

- Single protagonist across scenes: MiniMax `S2V-01` (single-image face driving, most stable consistency) or `happyhorse-1.0-r2v`
- Multiple characters interacting in the same frame: Alibaba `wan2.7-r2v` / `happyhorse-1.0-r2v` (multi-character + audio), or Kling `kling-v3-omni` (video character subject + multi-image subject)
- Video-reference driven (using a reference video to drive motion/subject): Alibaba `wan2.7-r2v`, Kling `kling-v3-omni` / `kling-video-o1`

---

## 3. Alibaba Bailian (DashScope / Model Studio)

> **Regions**: Beijing `dashscope.aliyuncs.com` / Singapore `dashscope-intl.aliyuncs.com` / Virginia `dashscope-us.aliyuncs.com`; API keys for the three regions are independent and cannot be used across regions.

### 3.1 Text: Qwen Series (including the latest Qwen3.7-Max)

#### Models and Official Pricing (Mainland China, RMB per million tokens; tiers are determined by the total input of a single request, and the whole request is billed at that tier)

| Model ID | Positioning | Input | Output | Context | Notes |
|---|---|---|---|---|---|
| `qwen3.7-max` | **Latest flagship**, aimed at the agent era, positioned against GPT-5.5 / Claude Opus 4.7 | ¥12 | ¥36 | 256K-1M | Limited-time 50% off to ¥6/¥18 (until 2026-06-22); cache-hit input ¥1.2; AA leaderboard score 56.6, fifth globally and first among Chinese models; text-only capability available |
| `qwen3-max` | Stable flagship (snapshots `qwen3-max-2025-09-23` / `-2026-01-23`) | ¥2.5 (0-32K) | ¥10 | 256K | 32-128K ¥4/¥16; 128-256K ¥7/¥28; native search agent; thinking mode multiplies output |
| `qwen3.6-plus` | Latest vision-language Plus (2026-04-02 snapshot, one of the official top Qwen recommendations) | ¥2 | ¥12 | — | Cache hit ¥0.2; code/OCR/multimodal exceed the 3.5 series |
| `qwen3.6-flash` | Latest Flash (one of the official top Qwen recommendations, high-frequency low-cost tier) | Check in console | Check in console | — | One of the three top Qwen recommendations on the official Model Catalog page; suitable for ArcReel's most frequent prompt rewriting/tag extraction |
| `qwen-plus` | Balanced for production volume (stable alias, may point to qwen3.5-plus) | ¥0.8 (0-128K) | ¥2 | 1M | 128-256K ¥2/—; top tier for low-cost ultra-long context |
| `qwen-long` | Low-cost ultra-long documents | ¥0.5 | ¥2 | 10M | Cheapest for long-chapter understanding/long-text summarisation |

> On Alibaba's official "Model Catalog" page (2026-05-21), the **three top-recommended Qwen text models** are `qwen3.7-max` / `qwen3.6-plus` / `qwen3.6-flash`; `qwen-plus` / `qwen-long` are stable-alias tiers, still usable but no longer in the top-recommended slots.

**Suggested division of work**: use `qwen3.7-max` or `qwen3-max` for flagship key plot points/complex reasoning; use `qwen-plus` or `qwen3.6-plus` (value) for high-frequency storyboard prompt rewriting, character cards, and voice-over script extraction; use `qwen-long` for whole-book understanding of ultra-long web-novel chapters. Using Qwen3.7-Max for simple tasks is "using a cannon to swat a mosquito".

**API**: OpenAI-compatible ✅ — `base_url=https://dashscope.aliyuncs.com/compatible-mode/v1`, endpoint `/chat/completions`, Bearer key. Supports streaming, `tool_calls`, `response_format={"type":"json_object"}`. Non-standard parameters `enable_thinking` / `thinking_budget` are passed through via `extra_body`. For structured output with complex Chinese schemas, forcing a tool call with `tool_choice` is more stable than `response_format`.

**Tiered billing rule**: the total input tokens of a single request determine the per-unit price tier for the whole request (it is not only the excess that costs more).

> Superseded old versions (`qwen-turbo` / Qwen2.x series) are not covered; existing stable interfaces remain usable.

#### Bailian Third-party Model Proxy (one endpoint covering multiple vendors)

Alibaba's official Model Catalog page shows that, besides Qwen, Bailian also proxies several third-party text models **in exactly the same OpenAI-compatible format as Qwen**. By onboarding a single Bailian `dashscope` provider + OpenAI backend, ArcReel gets these as well (switching the model field):

- `deepseek-v4-pro` / `deepseek-v4-flash` (DeepSeek V4)
- `kimi-k2.6` (Moonshot AI Kimi)
- `glm-5.1` (Zhipu GLM)
- `MiniMax-M2.7` (Bailian proxy path `MiniMax/MiniMax-M2.7`, with a 20% implicit-cache discount)
- `mimo-v2.5-pro` (Xiaomi MiMo)

Value for ArcReel: if we don't want to integrate the official MiniMax/DeepSeek sites separately, everything can go through a single Bailian endpoint; the trade-off is that Bailian's pricing for these third-party models may differ slightly from their official sites, and the exact price should be taken from the Bailian console.

### 3.2 Image: Qwen-Image and Wan 2.7 Image (two parallel product lines)

Alibaba has two image generation product lines with **complementary positioning**; both are worth evaluating for web-novel scenarios:

#### ① Qwen-Image Series (Qwen team, SOTA in text rendering + comic storyboards)

- **Model IDs**:
  - `qwen-image-2.0-pro` / `qwen-image-2.0-pro-2026-03-03` (**officially recommended**, fused image generation and editing model, strongest text rendering/realistic texture/semantic adherence, sync interface only)
  - `qwen-image-2.0` / `qwen-image-2.0-2026-03-03` (accelerated version, balance of quality and performance, sync interface only)
  - `qwen-image-edit` (dedicated to image editing/local modification; content contains 1-3 images + one editing instruction)
  - Previous generation `qwen-image-max` / `qwen-image-plus` (superseded by the 2.0 series, not covered)
- **Release**: Qwen-Image-2.0 was released on 2026-02-10 (the same day as ByteDance Seedream 5.0); the 2.0 iteration of Qwen's first image generation model, 7B parameters (substantially slimmed down from the original 20B MMDiT)
- **Core capabilities (high fit for web novels)**:
  - **SOTA complex text rendering**: supports long instructions of up to 1000 tokens (about 800-1000 Chinese characters), accurately handles complex layouts, multiple fonts (regular script/Slender Gold script/small regular script, etc.), and text on multiple media (glass/clothing/magazines, etc.); the official demo successfully generated an illustration containing the full 324-character text of the "Lanting Xu" (Preface to the Orchid Pavilion Poems)
  - **Multi-panel comic generation + stable cross-panel character consistency** — maps directly to the storyboard scenario
  - **Native 2K resolution** (2048×2048) output, finely rendering skin/vegetation/architectural textures; supports more than ten styles including realistic, ink wash, hand-drawn, anime, and oil painting
  - **Generation and editing in one**: a unified architecture performs text-to-image + image-to-image editing (adding inscriptions, changing backgrounds, compositing multiple images, cross-dimension fusion) in the same model; object-level editing doesn't damage surrounding detail
  - First on both the AI Arena text-to-image and image editing leaderboards; DPG-Bench score 88.32, beating FLUX.1 (12B) at 83.84
- **API**: DashScope, separate endpoints for text-to-image and image editing; the 2.0 series has sync interfaces only; image editing `messages` contain only one user object (1-3 images + one text instruction); recommended image width/height 384-3072px, single image ≤10MB; returns temporary OSS URLs
- **Pricing**: billed per successfully generated image; failures are not charged and don't consume free quota; the exact unit price should be taken from the Bailian console "Model list and pricing" (officially the commercial price is expected to be about one third of Midjourney)
- **Languages**: officially supports Simplified Chinese and English

#### ② Wan 2.7 Image (Wan team, portrait realism + image sets)

- **Model IDs**: `wan2.7-image` (standard) / `wan2.7-image-pro` (large-scale, more stable in complex scenes); released 2026-04-01
- **Core capabilities**:
  - Diverse characters with "a thousand faces for a thousand people" face customisation, no more homogeneous AI faces (strong portrait realism)
  - **Image set generation of up to 12 images per call**, keeping the same character/style/colour grading → storyboard reference
  - Ultra-long text rendering (up to 4000 characters / 3K tokens, print-grade Chinese typesetting in 12 languages)
  - Precise hex colour control; up to 9 reference images for group photos with consistent characters
  - Supports online modification of the image and plot in natural language
- **API**: DashScope sync HTTP `POST /api/v1/services/aigc/multimodal-generation/generation` (sync supported since wan2.6) or async + `X-DashScope-Async: enable`
- **Pricing**: billed per successful image, failures not charged; exact unit price per the console

**Choosing between the two lines**: for storyboards/comics/posters with text, prefer Qwen-Image-2.0-Pro (cross-panel character consistency + SOTA text + native 2K); for realistic portraits/character diversity/single-call image sets, prefer wan2.7-image. Both are billed per image and go through DashScope.

### 3.3 Video: HappyHorse 1.0 Series (primary) and Wan 2.7

> HappyHorse (快乐小马) is a family of large video models officially announced by Alibaba in 2026-04. **In the Artificial Analysis Video Arena blind tests it ranked first in both the text-to-video and image-to-video "no audio" tracks, and second globally in the audio track (just behind Seedance 2.0)**. A 15-billion-parameter single-stream Transformer with native synchronised audio-video generation; a single H100 generates 5s of 1080P in just 38s (2-3x faster than comparable models). **Fully open source and commercially usable**, which is especially friendly for the open-source ArcReel project.

#### The Full HappyHorse 1.0 Family (official pricing: 720P ¥0.9/second, 1080P ¥1.6/second)

| Model ID | Type | Features | Output spec |
|---|---|---|---|
| `happyhorse-1.0-t2v` | Text-to-video | Audio, lip sync in 7 languages, multi-shot | 720P/1080P, 3-15s, 24fps MP4 |
| `happyhorse-1.0-i2v` | First-frame-to-video | Audio, 1080P | 720P/1080P, 3-15s, 24fps MP4 |
| `happyhorse-1.0-r2v` | Reference-to-video | Audio, multi-character consistency | 720P/1080P, 3-15s, 24fps MP4 |
| `happyhorse-1.0-video-edit` | Video editing | Audio, style transfer | 720P/1080P, 3-15s, 24fps MP4 |

Lip-sync languages: Mandarin, English, Japanese, Korean, German, French, Cantonese — seven in total. Multi-shot narrative up to 15 seconds, with cinematic lighting/camera movement/character consistency.

#### Alibaba's Officially Recommended Division between HappyHorse / Wan

| Scenario | Official recommendation | Alternative |
|---|---|---|
| Text-to-video (with audio) | `happyhorse-1.0-t2v` | `wan2.7-t2v-2026-04-25` (when a custom audio file is needed) |
| First-frame-to-video | `happyhorse-1.0-i2v` | — |
| First/last frame / video continuation / chaining long videos | `wan2.7-i2v-2026-04-25` | — |
| Reference-to-video (character consistency) | `happyhorse-1.0-r2v` | `wan2.7-r2v` (when video-referenced subjects/custom voice timbre are needed) |
| Video editing | `happyhorse-1.0-video-edit` | `wan2.7-videoedit` (effects/camera-movement replication) |

#### The Full Wan 2.7 Family (720P/1080P, 2-15s, 30fps)

| Model ID | Type | Features |
|---|---|---|
| `wan2.7-t2v` / `wan2.7-t2v-2026-04-25` | Text-to-video | Audio sync, multi-shot narrative |
| `wan2.7-i2v` / `wan2.7-i2v-2026-04-25` | Image-to-video | First frame, first/last frame, video continuation, audio-driven |
| `wan2.7-r2v` | Video reference | Multi-character, Image N / Video N reference format (the only one supporting video-referenced subjects), 2-10s |
| `wan2.7-videoedit` | Video editing | Instruction editing, video transfer, effects/camera-movement replication, up to 10s |

> Wan 2.5 (`wan2.5-t2v-preview` / `wan2.5-i2v-preview`) is the only version that accepts a custom audio file (`audio_url`), 480P/720P/1080P, fixed 5s/10s tiers. Earlier versions such as Wan 2.1/2.2 have been superseded by 2.6/2.7 and are not covered.

**Wan / HappyHorse video API** (uniformly DashScope async):
- Submit: `POST /api/v1/services/aigc/video-generation/video-synthesis` + `X-DashScope-Async: enable` → `output.task_id`
- Poll: `GET /api/v1/tasks/{task_id}` → `PENDING → RUNNING → SUCCEEDED/FAILED`
- Fetch result: `output.video_url` (public OSS, valid 24h, must be re-stored immediately)

---

## 4. Kling (Kuaishou)

> **No text model**. **API**: `https://api.klingai.com/v1`, **JWT HS256** authentication (AK as iss, SK for signing, 30-minute expiry, automatic renewal required). Alibaba Cloud Bailian also proxies Kling image/video (via DashScope async, Beijing region only); if you don't want to write JWT, you can use the Bailian proxy path.
>
> The model list in this section has been verified first-hand against Kling's official documentation `klingai.com/document-api/apiReference/model/{video,image}Models`. **Exact Inspiration-credit pricing still needs to be confirmed by logging in to the app.klingai.com console** (the official docs list the capability matrix, not the credit values for each tier).

### 4.1 Image (Kolors family + new-generation Omni image)

Kling's official image model lineup (verified first-hand): `kling-v1` / `kling-v1-5` / `kling-v2` / `kling-v2-new` / `kling-v2-1` / `kling-v3` / `kling-v3-omni` / `kling-image-o1`.

#### The Two Most Worth Evaluating for Web-novel Scenarios

- **`kling-image-o1` (Kling Omni Image O1, strongly recommended)**: Kling's new-generation multi-reference image model, based on the MVL (multimodal visual language) framework, **supports 1-10 reference images as simultaneous input** with cross-image character consistency. Its official positioning is "IP character design, comics/serials, brand materials" — **this is currently Kling's strongest image model for web-novel characters across scenes/storyboards**, far beyond the old Kolors. Supports custom aspect ratios (1K/2K) + smart aspect ratio, text-to-image/image-to-image/subject control (multi-image subject). Training data up to 2025-12.
- **`kling-v3-omni` (image capability)**: custom aspect ratios 1K/2K/**4K** + smart aspect ratio, text-to-image/image-to-image/**image set generation**/subject control (multi-image subject). It is the image component of the 3.0 unified multimodal architecture; 4K + image sets are valuable for batch storyboard output.

#### Old Versions (as needed, superseded by o1/v3)

- `kling-v1`: text-to-image/image-to-image general reference-image (垫图) generation, 1K, 8 aspect ratios
- `kling-v1-5`: image-to-image with **character features + facial likeness** preservation, 1K
- `kling-v2` / `kling-v2-1`: include multi-image reference generation, style transfer; v2-1 is the most complete (general reference image + character features + facial likeness + multi-image reference + style transfer)
- Chinese lettering is a traditional strength of the Kolors family (ChatGLM3 text encoder); the new-generation o1/v3-omni further improve text rendering

**API**: JWT + async, `POST /v1/images/generations` → `GET /v1/images/generations/{task_id}` (image_url valid 24h)
**Key parameters**: `model_name`, `prompt`, `negative_prompt`, `image`, `image_fidelity`, `human_fidelity`, `n`, `aspect_ratio`; multi-image reference uses an image array (o1 up to 10 images)

**Official image pricing (first-hand, `klingai.com/dev/pricing`, image credits 1 credit = ¥0.025)**:

| Model | Capability | Spec | Price/image |
|---|---|---|---|
| `kling-image-o1` | Text-to-image/image-to-image/image editing | All aspect ratios | ¥0.2 (8 credits) |
| `kling-v3-omni` | Text-to-image/image-to-image/image editing | 1K/2K | ¥0.2 |
| `kling-v3-omni` | Same as above | 4K | ¥0.4 (16 credits) |
| `kling-v3` | Text-to-image/image-to-image | 1K/2K | ¥0.2 |
| `kling-v2-1` | Text-to-image | — | ¥0.1 (4 credits) |
| `kling-v2-1` | Image-to-image | — | ¥0.2 |
| `kling-v2` | Text-to-image | — | ¥0.1 |
| `kling-v2` | Image-to-image, multi-image reference | — | ¥0.4 (16 credits) |
| `kling-v1-5` | Image-to-image, character features/facial likeness | — | ¥0.2 |
| `kling-v1` | Text-to-image/image-to-image | — | ¥0.025 (1 credit, cheapest) |
| Utility model | Outpainting | — | ¥0.2 |
| Utility model | Smart subject-image completion | — | ¥0.5 (20 credits) |

So `kling-image-o1` / `kling-v3-omni` (recommended) cost ¥0.2/image (1K-2K), which is expensive compared with MiniMax image-01 at ¥0.025/image, but their multi-image reference consistency is stronger.

### 4.2 Video

Kling's official video model lineup (verified first-hand): `kling-v1` / `kling-v1-5` / `kling-v1-6` / `kling-v2-master` / `kling-v2-1` / `kling-v2-1-master` / `kling-v2-5-turbo` / `kling-v2-6` / `kling-v3` / `kling-v3-omni` / `kling-video-o1`.

**Official video pricing (first-hand, `klingai.com/dev/pricing`, 1 credit = ¥1 list price, billed by combination of dimensions in ¥/second)**:

| Spec | Silent | With audio | With reference video (silent) |
|---|---|---|---|
| Standard std × 1s | ¥0.6 | ¥0.8 | ¥0.9 |
| High quality pro × 1s | ¥0.8 | ¥1.0 | ¥1.2 |
| 4K × 1s | ¥3.0 | ¥3.0 | — |

That is, Kling video is billed by a four-dimensional combination of "mode × duration × reference video or not × audio or not"; for example, pro with audio 5s = ¥1.0×5 = ¥5; std silent 5s = ¥3. Resource pack tiers: trial ¥0.7/credit (30% off first purchase), standard ¥1/credit, bulk ¥0.9/credit.

Community evaluations: Kling is in the global top tier for motion physics, camera stability, and breaking down complex instructions. On the Artificial Analysis T2V (with-audio) Leaderboard (2026-05-28), Kling 3.0 Omni 1080p Pro has Elo 1099 and ranks fourth (the top three are Dreamina Seedance 2.0, HappyHorse-1.0, Veo 3.1).

> Platform capabilities independent of model version (official): digital human (a presenter video generated from a single photo), lip sync (mouth movement driven by text/audio), video-to-sound-effects (adding sound effects to Kling-generated or user-uploaded videos).

#### ① kling-v3 / kling-v3-omni (flagship, multi-shot + 4K + subject control)

- **Capabilities (official, first-hand)**: std/pro/**4K**, duration 3-15s; text-to-video includes **single-shot + multi-shot video generation**; image-to-video includes single-shot (first frame only) + multi-shot + **first/last frame (one continuous take)** + **subject control (video character subject + multi-image subject)**; v3-omni additionally supports video reference (3-10s only; the 4K tier does not support video reference); v3 also supports motion control (std/pro, not 4K)
- **Important correction**: in the official capability table, **"sound control (voice control)" for v3 / v3-omni is marked ❌**. This is **inconsistent** with some third-party claims of "v3 native audio-video sync" — follow the official source: the voice control capability of v3/v3-omni video itself is, per the official marking, not supported; sound effects can be supplemented via the platform-level "video-to-sound-effects" capability. Be sure to double-check the official docs before integration.
- **Subject control is key for web novels**: the "video character subject + multi-image subject" control of v3/v3-omni is a stronger character-consistency capability than the old v1-6 multi-image reference

#### ② kling-v2-6 (the only video version explicitly supporting voice control)

- **Capabilities (official, first-hand)**: std/pro, 5s/10s + other durations; text-to-video/image-to-video (std silent video only, pro includes audio); first/last frame (pro, silent only); **sound control (voice control) supported only in pro ✅**; motion control (other-duration tiers)
- It is the **only version among the official video models explicitly marked "sound control ✅" in the capability table** (pro tier); prioritise evaluating it when in-video voice is needed

#### ③ kling-v2-5-turbo (value workhorse)

- **Capabilities (official, first-hand)**: std/pro, 5s/10s; text-to-video + image-to-video (all tiers); first/last frame (pro only); resolution pro 1080p, 24fps
- **Official pricing**: per the dimension combination in the table above, std 5s silent = ¥3, pro 5s with audio = ¥5 (pro 1080p 24fps); a Kuaishou IR announcement on 2025-09-24 stated that each 5s 1080P clip is about 30% cheaper than 2.1
- No advanced capabilities such as multi-image subject/sound control; purely a value tier for volume

#### ④ kling-video-o1 (dedicated to video reference + subject control)

- **Capabilities (official, first-hand)**: std/pro, 3-10s (text-to-video/image-to-video only 5s, 10s); image-to-video includes first/last frame (one continuous take) + **subject control (multi-image subject only)** + **video reference (including video editing)**; sound control ❌
- Suited to scenarios that need "driving by a reference video" or multi-image subject consistency

#### ⑤ kling-v1-6 / v1-5 / v1 (old versions, as needed)

- `kling-v1-6`: text-to-video/image-to-video all tiers, first/last frame (pro), **multi-image reference-to-video** + multimodal video editing + video continuation + two-image effects (hug/kiss/heart gesture); pro 1080p
- `kling-v1-5`: mainly image-to-video, first/last frame/last frame only/motion brush (pro); includes video continuation
- `kling-v1`: text-to-video/image-to-video, camera control, first/last frame, motion brush, video continuation, two-image effects; 720p
- `kling-v2-master` / `kling-v2-1` / `kling-v2-1-master`: the v2-generation tiers; v2-1 image-to-video includes first/last frame (pro)

**Quick reference of key capability distribution** (official, first-hand):
- Multi-shot narrative: only `v3` / `v3-omni`
- In-video voice control: only `kling-v2-6` (pro)
- Subject control (character consistency): `v3` / `v3-omni` (video subject + multi-image subject, strongest), `o1` (multi-image subject + video reference), `v1-6` (multi-image reference-to-video)
- 4K: only `v3` / `v3-omni`
- Video reference (including editing): `v3-omni` (3-10s), `o1`

**Kling video integration notes** (JWT):

```python
import jwt, time


def kling_token(ak, sk):
    return jwt.encode(
        {"iss": ak, "exp": int(time.time()) + 1800, "nbf": int(time.time()) - 5},
        sk,
        algorithm="HS256",
        headers={"alg": "HS256", "typ": "JWT"},
    )
```

Recommend wrapping JWT authentication in a class that reuses the token within 30 minutes and refreshes automatically 60 seconds before expiry.

---

## 5. MiniMax Hailuo

> Cheapest across all modalities, with the most consistent protocol. All APIs go through `https://api.minimaxi.com/v1` (China) / `https://api.minimax.io/v1` (international), Bearer key; text is compatible with both the OpenAI and Anthropic SDKs.

### 5.1 Text: MiniMax-M2.7

- **Model IDs**: `MiniMax-M2.7` (flagship) / `MiniMax-M2.7-highspeed` (2x speed, 2x price); Bailian proxy `MiniMax/MiniMax-M2.7`
- **Release**: 2026-03-18, the current latest flagship (supersedes M2.5 / abab 7)
- **Capabilities**: SWE-Pro 56.22% (officially described as matching GPT-5.3-Codex); MoE 230B total params/10B active; 200K context; top tier for Chinese literary writing; thinking mode + Function Calling + Toolathon 46.3%
- **API**: OpenAI-compatible `base_url=https://api.minimaxi.com/v1`; or via the Bailian proxy with a 20% implicit-cache discount; the Anthropic SDK is also compatible
- **Official pricing** (China site, RMB per million tokens, source platform.minimaxi.com pricing-paygo):

  | Model | Input | Output | Cache-hit read | Cache write |
  |---|---|---|---|---|
  | M2.7 | ¥2.1 | ¥8.4 | ¥0.42 (20%) | ¥2.625 (125%) |
  | M2.7-highspeed | ¥4.2 | ¥16.8 | ¥0.42 | ¥2.625 |

- **International site**: M2.7 input $0.30/M, output $1.20/M (about 1/8 of the Claude Sonnet price)
- **Positioning**: the second text model paired with Qwen — M2.7 for persona/emotionally nuanced text and Chinese creative writing, Qwen-Plus for structured JSON

> Old versions such as abab 6.5 / M2.5 / abab 7 can still be called but have been superseded by M2.7; not covered.

### 5.2 Image: image-01

- **Model ID**: `image-01`
- **Capabilities**: unified text-to-image + image-to-image; `subject_reference` with a single face reference drives multiple scenes (character card → multi-scene character art); `aspect_ratio` or `width`/`height` (512-2048, multiples of 8); `prompt_optimizer`; `n` 1-9
- **API**: `POST https://api.minimaxi.com/v1/image_generation`, Bearer + JSON, returns `url` (24h) or base64 directly in a single step
- **Official pricing**: ¥0.025/image (charged only on success); international site about $0.0035-0.005/image
- **Style**: SOTA for cinematic portraits + realistic materials, the most stable character consistency of the three; slightly weaker than Kolors / Qwen-Image for pure anime

### 5.3 Video: Hailuo 2.3 Series

> Capability boundary: Hailuo 2.3 = T2V + I2V; Hailuo 2.3-Fast = I2V only; character-consistency R2V uses the separate S2V-01 (single face image only).

#### ① MiniMax-Hailuo-2.3 (T2V + I2V, high quality)

- **Model ID**: `MiniMax-Hailuo-2.3` (released 2025-10-28, the current flagship video model)
- **Capabilities**: T2V + I2V; native 1080P output; excellent physics (NCR architecture); 85% response to complex instructions; camera control (embedded in the prompt as `[左摇,上升]` (pan left, rise), etc., ≤3 combined); **good at stylised anime/illustration/game CG** (fits web novels)
- **Supported tiers**: 6s (768P/1080P), 10s (768P; 10s does not support 1080P)
- **Official pricing** (China, RMB per video): 768P 6s ¥2 / 10s ¥4; 1080P 6s ¥3.5
- **International site**: 768P $0.045/s, 1080P Pro $0.08/s

#### ② MiniMax-Hailuo-2.3-Fast (I2V only, about half price)

- **Model ID**: `MiniMax-Hailuo-2.3-Fast`
- **Capabilities**: image-to-video (I2V) only, no T2V; same generation quality at about half the price; for fast I2V iteration
- **Official pricing**: 768P 6s ¥1.35 / 10s ¥2.25; 1080P 6s ¥2.31

#### ③ S2V-01 (dedicated character-consistency R2V)

- **Model ID**: `S2V-01`
- **Capabilities**: MiniMax's pioneering Subject-to-Video; a single face reference image drives character consistency across a whole video (face/pose/expression/lighting can be adjusted independently via the prompt); especially suited to multiple storyboard shots/recurring characters
- **Parameters**: `subject_reference=[{"type":"character","image":["url"]}]`
- **Limitations**: single face image only; the environment may be slightly distorted; prompt adherence is slightly lower than T2V/I2V
- **Pricing**: resource pack deducts 1.5 credits/video (about ¥3)

> Hailuo-02 (`MiniMax-Hailuo-02`) keeps a 512P draft tier (512P 6s ¥0.6); the rest is superseded by 2.3. Old versions such as T2V-01-Director are only kept for backward compatibility and are not covered.

**MiniMax video integration notes** (two steps to get the URL):
1. `POST /v1/video_generation` → `task_id`
2. Poll `GET /v1/query/video_generation?task_id=xxx` → `status=Success` returns `file_id`
3. `GET /v1/files/retrieve?file_id=xxx` → `download_url` (short-lived; recommend configuring `callback_url`, which must first respond to the challenge verification)

---

## 6. Integration Feasibility Assessment

### 6.1 Protocol Mapping

| Modality / vendor | Alibaba Bailian | Kling | MiniMax |
|---|---|---|---|
| Text | ✅ OpenAI-compatible, reuse the existing backend | ❌ None | ✅ OpenAI/Anthropic dual-compatible |
| Image | DashScope sync/async | JWT + async | Proprietary REST (single step returns URL) |
| Video | DashScope async (unified endpoint) | JWT + async (multiple endpoints) | Proprietary REST (two-step file_id) |

### 6.2 ArcReel Backend Workload

| Reuse level | Models |
|---|---|
| Reuse the OpenAI text backend | `qwen3.7-max`, `qwen3.6-plus`, `qwen3.6-flash`, `qwen3-max`, `qwen-plus`, `MiniMax-M2.7`; plus the Bailian-proxied `deepseek-v4-pro/flash`, `kimi-k2.6`, `glm-5.1`, `mimo-v2.5-pro` (all in the same OpenAI-compatible format) |
| New DashScope backend (one covering everything, must support both sync and async task call paths) | Images `qwen-image-2.0-pro` / `qwen-image-2.0` / `qwen-image-edit` (sync), `wan2.7-image` (sync/async); video `happyhorse-1.0-*` / `wan2.7-*` / `wan2.5-*` (async); the Bailian Kling proxy |
| New Kling JWT backend | Video `kling-v3` / `kling-v3-omni` / `kling-v2-6` / `kling-v2-5-turbo` / `kling-video-o1`; image `kling-image-o1` / `kling-v3-omni` (unified `/v1/images` and `/v1/videos` endpoint families + JWT renewal) |
| New MiniMax backend (two steps to get the URL) | `MiniMax-Hailuo-2.3/-Fast`, `S2V-01`; `image-01` is simpler with a single step |

### 6.3 Authentication Specifics

- Alibaba: Bearer + keys for different regions cannot be mixed; async requires the `X-DashScope-Async: enable` header
- Kling: JWT HS256, 30-minute expiry; needs token caching + early refresh
- MiniMax: simple Bearer; video uses a two-step file_id download or a configured callback_url (must first respond to the challenge)

### 6.4 Fit Scores for Web-novel Scenarios

| Dimension | Alibaba Bailian | Kling | MiniMax |
|---|---|---|---|
| Chinese script understanding/structured output | ★★★★★ | — | ★★★★★ |
| Chinese text rendering (covers/posters) | ★★★★★ (Qwen-Image-2.0) | ★★★★ (image-o1/v3-omni improved text rendering) | ★★★ |
| Comic/storyboard cross-panel character consistency (image) | ★★★★★ (Qwen-Image-2.0 multi-panel comics) | ★★★★★ (image-o1 up to 10 reference images, built for comics/serials) | ★★★★ (image-01) |
| Character consistency (video R2V) | ★★★★★ (happyhorse-r2v / wan2.7-r2v multi-character + audio + video reference) | ★★★★★ (v3-omni video subject + multi-image subject, o1 video reference) | ★★★★ (S2V-01 single face image, most stable but single person only) |
| Video physics | ★★★★★ (HappyHorse first/second on both leaderboards) | ★★★★★ (Omni physics engine) | ★★★★ (NCR native 1080P, strong at anime) |
| Video multi-shot narrative | ★★★★★ (wan2.7 + happyhorse multi-shot subject consistency) | ★★★★★ (v3/v3-omni official multi-shot video generation) | ★★★ |
| Integrated audio-video | ★★★★★ (HappyHorse native audio-video, lip sync in 7 languages / wan2.5 audio_url) | ★★★ (officially only v2-6 pro supports voice control; v3/v3-omni marked ❌, sound effects via platform-level "video-to-sound-effects") | ★★★ (requires external TTS) |
| Price/value | ★★★★ (HappyHorse 720P ¥0.9/s) | ★★★ (video from std ¥0.6/s, pro with audio ¥1.0/s, 4K ¥3/s; images ¥0.2/image) | ★★★★★ (Hailuo-2.3-Fast 768P 6s ¥1.35) |

---

## 7. Key Risks and Uncertainties

1. **Price volatility**: all three have limited-time discounts (Qwen3.7-Max 50% off until 2026-06-22, all Qwen3.6 models 55% off, HappyHorse limited-time 20% off, Kling package promotions). Price fields in ArcReel's `PROVIDER_REGISTRY` should be configurable, not hard-coded.
2. **Official prices verified**: the full HappyHorse 1.0 family 720P ¥0.9/s, 1080P ¥1.6/s (Alibaba Cloud launch announcement); Qwen series tiered prices and Qwen3.7-Max ¥12/¥36 (Alibaba Cloud pricing page/product page); MiniMax M2.7 ¥2.1/¥8.4, image-01 ¥0.025/image, Hailuo 2.3 tiers (platform.minimaxi.com); **Kling credit unit prices for all video/image models** (official pricing page klingai.com/dev/pricing: video 1 credit = ¥1, billed by the four dimensions "mode × duration × reference video × audio"; image 1 credit = ¥0.025, image-o1/v3-omni ¥0.2/image).
3. **Still to be checked in the console**: exact per-image RMB prices for `qwen-image-2.0-pro/2.0` and `wan2.7-image`, exact token prices for `qwen3.6-flash` (Alibaba's official pricing page does not directly list per-image/per-tier values); Bailian-side pricing for the Bailian-proxied third-party models (DeepSeek/Kimi/GLM/MiMo). Kling pricing has been verified first-hand and needs no further checking.
4. **Model ID updates**: Qwen publishes snapshots monthly; prefer stable aliases + regular regression testing; Alibaba's official top Qwen text recommendations are `qwen3.7-max`/`qwen3.6-plus`/`qwen3.6-flash`, and for image/video `wan2.7-image-pro`/`qwen-image-2.0-pro`/`happyhorse-1.0-*` (all per the official Model Catalog page, 2026-05-21); MiniMax currently M2.7 / Hailuo 2.3 / image-01 (per the official releases page).
5. **Capability boundaries (official, first-hand)**: Kling's strongest character consistency is currently the "subject control (video character subject + multi-image subject)" of `kling-v3`/`v3-omni`, not the old v1-6 multi-image reference; Kling `v3`/`v3-omni` video "voice control" is officially marked ❌, and only `kling-v2-6` pro supports in-video voice. Hailuo 2.3 does not support R2V (T2V + I2V only), 2.3-Fast is I2V only, and MiniMax R2V goes through S2V-01 (single face image only).
6. **Alibaba's two image product lines**: Qwen-Image-2.0 (text rendering/comic storyboards) and Wan-Image (portrait realism) are positioned differently and must be evaluated separately per scenario; they are not substitutes.
7. **HappyHorse is open source and commercially usable**: base model/distillation/super-resolution/inference code are all open source, which is friendly for ArcReel; however, there are many third-party sites online with the same name, so integrate only with the official Alibaba Cloud Bailian API.
8. **International site differences**: MiniMax minimaxi.com (China) vs minimax.io (international) keys are not interchangeable; Alibaba's three regions have independent keys; Kling app.klingai.com (China) vs klingai.com (global).
9. **Treat leaderboards with caution**: Artificial Analysis updates quickly, and a Chinese model's advantage on Chinese-specific tasks does not equal leadership on global general benchmarks; base selection on hands-on "Chinese + web novel" testing.
10. **Kling's model list has been verified first-hand, but pricing still needs the console**: Kling's video/image model lists have been checked item by item against the official docs (v3/v3-omni/v2-6/v2-5-turbo/o1/image-o1, etc.), and the capability matrix follows the official source. Two points to note: ① the official capability table marks **v3/v3-omni video "sound control (voice control)" as ❌**, and only `kling-v2-6` pro supports in-video voice, which conflicts with some third-party claims of "v3 native audio-video" — follow the official source; ② the exact Inspiration-credit values for each tier of each model are not listed in the official docs and must be confirmed by logging in to the app.klingai.com console.

---

## 8. References (first-hand official sources)

- Alibaba Cloud Bailian Model Catalog (top recommendations, updated 2026-05-21): https://help.aliyun.com/zh/model-studio/models
- Alibaba Cloud Bailian video model catalog: https://help.aliyun.com/zh/model-studio/video-generate-edit-model/
- Alibaba Cloud Bailian model pricing: https://help.aliyun.com/zh/model-studio/model-pricing
- Qwen Qwen-Image text-to-image API: https://help.aliyun.com/zh/model-studio/qwen-image-api
- Qwen Qwen-Image-Edit image editing: https://help.aliyun.com/zh/model-studio/qwen-image-edit-guide
- Tongyi Wanxiang text-to-video API: https://help.aliyun.com/zh/model-studio/text-to-video-api-reference
- Qwen large model product page (Qwen3.7-Max / Qwen-Image / Wan2.7): https://www.aliyun.com/product/tongyi
- Kling official video model list (first-hand): https://klingai.com/document-api/apiReference/model/videoModels
- Kling official image model list (first-hand): https://klingai.com/document-api/apiReference/model/imageModels
- Kling official pricing page (first-hand): https://klingai.com/dev/pricing
- Kling open platform developer docs: https://app.klingai.com/cn/dev/document-api
- MiniMax pricing (pricing-paygo): https://platform.minimaxi.com/docs/guides/pricing-paygo
- MiniMax model release updates: https://platform.minimaxi.com/docs/release-notes/models
- MiniMax image-to-video API: https://platform.minimaxi.com/docs/api-reference/video-generation-i2v
- MiniMax S2V-01: https://platform.minimax.io/docs/api-reference/video-generation-s2v
- MiniMax Hailuo 2.3 release: https://www.minimax.io/news/minimax-hailuo-23

---

**Architecture alignment**: ArcReel `PROVIDER_REGISTRY` preset providers + `lib/backends/{text,image,video}_backends/`
