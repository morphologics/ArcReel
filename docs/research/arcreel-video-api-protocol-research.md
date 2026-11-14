# ArcReel Video API Protocol Adaptation Research Report

**Research cut-off date**: 2026-05-27
**Purpose**: Input material for writing the follow-up PRD and design documents
**Author**: Research assistance (Claude)
**Related background**: Expansion of ArcReel's video provider system, integration with the custom provider ecosystem

---

## 0. Research Scope and Positioning

This report is a **research-oriented** compilation of material and **does not include** a concrete directory structure, Adapter class design, or implementation plan. Those should be produced in the later PRD and design document stages, based on ArcReel's current `lib/backends/video_backends/` + `lib/custom_provider/` architecture.

Questions covered by the research:
1. What de facto standards exist for relay-station (API proxy) video API endpoint formats
2. API specifications of the mainstream official video generation platforms
3. The capability support matrix for each protocol (text-to-video / image-to-video / first and last frame / reference-to-video / video continuation / audio / lip sync / character consistency)
4. Feasibility assessment and priority recommendations for each protocol
5. Full API specification details for the highest-priority protocols
6. Choice of approach for custom provider integration (plugin vs declarative)
7. Feasibility and design options for a runtime plugin mechanism (supporting the planned community protocol-sharing feature)

Out of scope:
- ArcReel code-level directory organisation / class inheritance / file naming
- Database schema changes / Alembic migrations
- Frontend UI changes
- A concrete implementation timeline

---

## 1. ArcReel Architecture Research Baseline (important: aligned with the current state)

> This section is the **prerequisite for understanding the later research conclusions**; every protocol adaptation recommendation must fit within this architecture.

### 1.1 Video Backend Abstraction Layer

ArcReel has already built a mature video generation abstraction in `lib/backends/video_backends/`:

| Key element | Location | Description |
|---|---|---|
| `VideoBackend` Protocol | `lib/backends/video_backend_contract.py` | Duck-typed contract requiring `name` / `model` / `capabilities` / `generate()` |
| `VideoCapability` enum | `lib/backends/video_backend_contract.py` | Capability bitmap: `TEXT_TO_VIDEO` / `IMAGE_TO_VIDEO` / `GENERATE_AUDIO` / `NEGATIVE_PROMPT` / `VIDEO_EXTEND` / `SEED_CONTROL` / `FLEX_TIER` |
| `VideoGenerationRequest` / `VideoGenerationResult` | Same as above | Unified request/response data classes |
| `register_backend(name, factory)` | `lib/backends/video_backends/registry.py` | Registration mechanism |
| Existing backend implementations | `gemini.py` / `ark.py` / `grok.py` / `openai.py` / `newapi.py` / `vidu.py` | 6 providers |

**ArcReel glossary convention**: **backend** (the client object constructed per provider + model that actually calls the API) is used to refer to generation backends; the glossary's `_Avoid` marks the word `adapter` as one to avoid. In essence it plays the Adapter role of the Ports & Adapters paradigm, but ArcReel calls it backend throughout to stay consistent with the provider-derivation semantics, the parallel with frontend, and the naming of the three media backends (consistent with the convention of SQLAlchemy / Django naming the same kind of role "backend"). For the discussion of architectural alignment see 9.1.

### 1.2 Custom Provider System

`lib/custom_provider/` already lets users connect any OpenAI/Google-compatible relay station:

| Key element | Role |
|---|---|
| `CustomProvider` ORM (DB table) | `discovery_format` ∈ {openai, google} + `base_url` + `api_key` |
| `CustomProviderModel` ORM (DB table) | Each model is attached to one `endpoint` (an ENDPOINT_REGISTRY key) |
| `ENDPOINT_REGISTRY` | Single source of truth for protocol ownership |
| `infer_endpoint()` | Heuristically infers the endpoint from model_id |
| `create_custom_backend()` factory | Dispatches to the matching VideoBackend by endpoint |
| Frontend UI | CRUD on the settings page, automatic `/v1/models` discovery |

**The current ENDPOINT_REGISTRY already has 6 entries** (excluding ones not covered in this research round, such as anthropic-messages):

| Endpoint Key | media_type | family | Status |
|---|---|---|---|
| `openai-chat` | text | openai | Implemented |
| `gemini-generate` | text | google | Implemented |
| `openai-images` | image | openai | Implemented |
| `gemini-image` | image | google | Implemented |
| `openai-video` | video | openai | Implemented (**OpenAI Sora `/v1/videos` compatible**) |
| `newapi-video` | video | newapi | Implemented (**NewAPI's own `/v1/video/generations` protocol**) |

### 1.3 Preset Providers

`PROVIDER_REGISTRY` in `lib/config/registry.py` already has 5 presets:

- `gemini-aistudio` / `gemini-vertex` — the full Veo 3.1 family
- `ark` — Volcano Engine Ark (Seedance 1.5 Pro / 2.0 / 2.0 Fast registered)
- `grok` — xAI Grok Imagine Video
- `openai` — Sora 2 / Sora 2 Pro
- `vidu` — the full Vidu Q3 family (including reference-to-video)

### 1.4 Protocols Bound at the Model Level (architectural decision)

ArcReel has explicitly confirmed in `2026-04-26-custom-provider-model-endpoint-design`:

> One relay station = one provider; different models under the same provider may use completely different protocols;
> protocol ownership is pushed down to the model layer (the `CustomProviderModel.endpoint` field);
> the provider-level `discovery_format` is only used for model discovery and is decoupled from the calling protocol.

**All subsequent protocol adaptation designs must respect this architectural constraint.**

---

## 2. Relay-station Protocol Ecosystem Landscape (summary of de facto standards)

After covering 15+ mainstream relay stations (the main NewAPI repository, AiHubMix, Qiniu Cloud, Wisdom Gate, AI/ML API, APIMart, EvoLink, CometAPI, Kie.ai, PiAPI, useapi.net, kazhang.ai, apiyi, burn.hair, closeai, etc.), the research concludes that **four major schools coexist**:

### 2.1 School A: OpenAI Sora `/v1/videos`

**Endpoints**: `POST /v1/videos` + `GET /v1/videos/{video_id}` + `GET /v1/videos/{video_id}/content`
**Format**: multipart/form-data or JSON
**Parameters**: `model` + `prompt` + `seconds` (string) + `size` ("1280x720") + `input_reference`
**Auth**: `Authorization: Bearer`
**Representative instances**: OpenAI official, AiHubMix, the Qiniu Cloud sora path, Wisdom Gate, Azure OpenAI, ArcReel's existing `openai-video` endpoint

**Key facts**:
- OpenAI has officially announced that **Sora 2 / Sora 2 Pro will be retired on 2026-09-24** (developer notification sent 2026-03-24)
- The path will remain a de facto standard; relay stations will keep using it out of inertia to host other models
- The Qiniu Cloud compatible path returns `task_result.videos[0].url` directly in the query response, whereas OpenAI official requires a second GET to `/content`

### 2.2 School B: NewAPI's Own `/v1/video/generations`

**Endpoints**: `POST /v1/video/generations` (note the plural) + `GET /v1/video/generations/{task_id}`
**Format**: pure JSON
**Parameters**: `model` + `prompt` + `image` + `duration` (number) + `width`/`height` + `metadata` (object)
**Auth**: `Authorization: Bearer`
**Representative instances**: all relay stations deployed on NewAPI / OneAPI (dozens, including DMXAPI, closeai, burn.hair); ArcReel's existing `newapi-video` endpoint

**Key facts**:
- The `metadata{}` field is a black box for vendor-specific pass-through (the only ones explicitly listed in the NewAPI docs are Kling's `image_tail`/`negative_prompt`/`seed` and Jimeng's `req_key`/`image_urls`/`aspect_ratio`; everything else depends entirely on the relay station's middleware)
- Completeness of metadata pass-through varies greatly between relay stations; the same Kling `camera_control` works on DMXAPI but is lost on some self-deployed NewAPI instances
- State machine: `queued / in_progress / completed / failed`

### 2.3 School C: v2 Generic Generations (one endpoint for all models)

**Endpoints**: `POST /v2/video/generations` or `/v2/videos/generations` (subtle differences between vendors)
**Format**: pure JSON
**Core feature**: **single endpoint + model field switching**, hosting dozens to hundreds of models (Kling/Veo/Sora/Hailuo/Wan/Seedance all under one URL)
**Auth**: `Authorization: Bearer`
**Representative instances**:
- **AI/ML API** (aimlapi.com) `/v2/video/generations` — the most typical representative of School C
- **getimg.ai** `/v2/videos/generations`
- **APIMart** `/v1/videos/generations`
- **EvoLink** `/v1/videos/generations`
- **Qiniu Cloud veo** path `/v1/videos/generations`
- **xAI official** `/v1/videos/generations` (**the official API itself has this shape**)
- **CometAPI** `/volc/v3/contents/generations/tasks` (path details differ but the idea is the same)

**Key facts**:
- It is effectively a **discriminated union by model**: a single URL accepts 60+ model IDs, but each model's set of optional fields is completely different
- Example: under the same endpoint Kling takes `cfg_scale` / `negative_prompt` / `image_url` / `last_image_url`; Veo 3.1 takes `image_urls[]`; Seedance takes `reference_images[]` / `reference_audios[]` / `reference_videos[]` / `generate_audio`; Sora takes `resolution` / `image_url`
- Model naming is badly fragmented: AIMLAPI uses `kling-video/v1/standard/text-to-video`, APIMart uses `sora-2-vip`, getimg.ai uses `happyhorse-1` (its own brand name)
- **Adopted by many relay stations**; it is the mainstream de facto standard besides School B
- **Not yet covered** in ArcReel's current ENDPOINT_REGISTRY

### 2.4 School D: Verb-style create/submit

**Endpoints**: `POST /api/v1/jobs/createTask`, `POST /api/v1/task`, `POST /v1/videos/create`, etc. (split into create + query)
**Format**: pure JSON, parameters nested in an `input{}` sub-object
**Representative instances**:
- **Kie.ai** `/api/v1/jobs/createTask` + `/api/v1/jobs/recordInfo`
- **PiAPI** `/api/v1/task` + `/api/v1/task/{task_id}`
- **useapi.net** `/v1/{vendor}/videos/create`
- **Suchuang (速创)** `/api/sora2/submit`
- **laozhang.ai** `/veo/v1/api/video/submit`

**Key facts**:
- School D is not internally consistent; paths and parameter details differ hugely between vendors, so a single backend cannot be reused
- PiAPI has non-standard status codes (outer `status: "completed"` string + inner `output.status: 99` integer meaning done), requiring a two-level check

### 2.5 Observations on School Distribution

Counted by the number of relay stations covered in the research (**not strict market share**):

- School A (OpenAI Sora compatible): high coverage, essential for overseas/compliance-sensitive customers
- School B (NewAPI's own): **de facto monopoly among self-deployed relay stations in China**, the highest coverage
- School C (v2 generic generations): **adopted by many relay stations**, used by overseas aggregators and xAI official, second in coverage
- School D (verb style): fragmented internally, mostly vendor-specific, limited coverage per vendor

---

## 3. Research on Mainstream Official Video Platform APIs

### 3.1 Platforms Already Integrated into ArcReel

| Platform | Current status | Protocol | Notes |
|---|---|---|---|
| Google Veo 3.1 | Implemented | Vertex AI + AI Studio dual backend | Text-to-video/image-to-video/continuation/audio/negative prompt |
| Volcano Ark Seedance 2.0 | Implemented | Ark SDK | 2.0 is registered but **multimodal reference and video extension are not enabled** |
| xAI Grok Imagine | Implemented | xAI SDK | Text-to-video/image-to-video/reference-to-video |
| OpenAI Sora 2 | Implemented | openai SDK | Includes `create_and_poll` |
| Vidu Q3 | Implemented | httpx | Text-to-video/image-to-video/**reference-to-video** (already used in reference-to-video mode) |

### 3.2 Official Platforms Researched but Not Yet Integrated

#### Kling Official

- **Base URL**: `https://api.klingai.com`
- **Auth**: JWT HS256, payload `{iss: ak, exp: now+1800, nbf: now-5}`, 30-minute expiry
- **Endpoints**: multiple endpoints by intent — `/v1/videos/text2video`, `/image2video`, `/multi-image2video`, `/video-extend`, `/lip-sync`
- **Capabilities**: T2V / I2V / FLF (pro mode `image_tail`) / R2V (`elements[]` in 1.6) / Extend / Audio (2.6 pro `enable_audio`) / LipSync
- **Key pitfalls**:
  - The status string is `succeed`, not `succeeded`
  - The video URL is at `$.data.task_result.videos[0].url`
  - 500 means a content moderation rejection (check the error field, not message)
  - `kling-2.1-master` does not support the `mode` field; 2.x models don't support `cfg_scale`
- **Value for web-novel scenarios**: one of the strongest video models in China; an essential direct-connection option for self-deployed studios

#### Alibaba DashScope Tongyi Wanxiang

- **Base URL (Beijing)**: `https://dashscope.aliyuncs.com/api/v1`
- **Base URL (Singapore)**: `https://dashscope-intl.aliyuncs.com/api/v1`
- **Auth**: `Authorization: Bearer` + mandatory `X-DashScope-Async: enable` header
- **Endpoints**: `POST /services/aigc/video-generation/video-synthesis` (wan2.5+ multimodal) / `POST /services/aigc/image2video/video-synthesis/` (wan2.1 / wan2.2-s2v) + `GET /tasks/{task_id}`
- **Capabilities**: T2V / I2V / FLF (wan2.1-kf2v) / R2V (`ref_image_urls[]`) / Extend / Audio / digital human LipSync (wan2.2-s2v)
- **One protocol covering multiple models**: wan2.7-t2v / wan2.6-i2v-flash / wan2.1-kf2v-plus / wan2.2-s2v
- **Key pitfalls**:
  - Omitting `X-DashScope-Async: enable` makes the call block synchronously until it dies
  - wan2.7 uses `resolution`+`ratio`, wan2.6 uses `size` ("1280*720", with an asterisk)
  - The state machine is all upper case: `PENDING / RUNNING / SUCCEEDED / FAILED / CANCELED / SUSPENDED / UNKNOWN`
  - Temporary OSS URLs are valid for **24h**, and so is the task_id
  - `IPInfringementSuspect` / `DataInspectionFailed` are content moderation errors
- **Value for web-novel scenarios**:
  - wan2.7 allows extremely long Chinese prompts; a single call can hold a complete storyboard script (5 shots, 1500 characters is fine)
  - `shot_type: multi` native multi-shot narrative

#### MiniMax Hailuo Official

- **Base URL (global)**: `https://api.minimax.io/v1`
- **Base URL (China)**: `https://api.minimaxi.com/v1`
- **Auth**: `Authorization: Bearer`
- **Endpoints**: `POST /video_generation` + `GET /query/video_generation?task_id={id}` + **`GET /files/retrieve?file_id={id}`** (two steps to get the URL)
- **Capabilities**: T2V / I2V / FLF / R2V (S2V-01's `subject_reference`) / Audio (Hailuo 2.3) / Director models support shot instructions embedded in the prompt
- **Key pitfalls**:
  - **Two steps to get the URL**: the query does not return the video URL, only a `file_id`; the File API must then be called
  - **The download URL is valid for only 9 hours** (32,400 seconds, per the official announcement) — the shortest of all platforms
  - Status values are capitalised: `Preparing / Queueing / Processing / Success / Fail`
  - Webhook registration must echo `challenge`, with a 3-second timeout
  - China / global endpoints differ, and API keys are independent
  - `base_resp.status_code != 0` indicates a business error (even an HTTP 200 may have failed internally)
- **Value for web-novel scenarios**: king of value, strong physics

#### Runway Gen-3 / Gen-4 / Gen-4.5

- **Base URL**: `https://api.dev.runwayml.com/v1`
- **Auth**: `Authorization: Bearer` + mandatory `X-Runway-Version: 2024-11-06` header
- **Endpoints**: `/image_to_video` / `/text_to_video` / `/video_to_video` + `/tasks/{id}`
- **Capabilities**: T2V (gen4.5) / I2V / R2V (gen4_image `@mention`) / Aleph video editing / Act-Two LipSync
- **Key pitfalls**:
  - No webhook; polling is mandatory
  - File uploads use an ephemeral upload URI
  - duration only `5 | 10`
- **Business value**: strong demand from overseas advertising/e-commerce; Gen-4.5 is #1 in the Arena

#### Luma Dream Machine (Ray-2)

- **Base URL**: `https://api.lumalabs.ai/dream-machine/v1`
- **Auth**: `Authorization: Bearer`
- **Endpoints**: **a single `POST /generations`** + `GET /generations/{id}`
- **Capabilities**: T2V / I2V / FLF / Extend / Loop (**elegantly expresses every intent with the two `keyframes.frame0/frame1` slots**)
- **Key design**: keyframes union type `{type: "image"|"generation", url|id}`; using a generation id in frame0 means Extend
- **Key pitfall**: the keyframes union type must be modelled specially and cannot reuse the `image_url` field

#### Pika 2.2

- **The official API is not self-service**; it is only open to B2B partners
- **The only integration path**: via fal.ai `queue.fal.run/fal-ai/pika/v2.2/{capability}`
- **Exclusive capabilities**: Pikaframes (2-5 keyframe multi-frame interpolation), Pikascenes (multi-image fusion, `ingredients_mode: creative|precise`)

#### PixVerse

- **Base URL**: `https://app-api.pixverse.ai`
- **Auth**: custom `API-KEY:` header + mandatory `Ai-trace-id` idempotency key
- **Endpoints**: `/openapi/v2/video/{text|img|transition|extend|fusion|lipsync}/generate`
- **Capabilities**: T2V / I2V / FLF (Transition) / R2V (Fusion) / Extend / Audio (V5.5+) / LipSync
- **NewAPI has no native channel**; must connect directly to the official API

#### ByteDance Jimeng (Volcano Engine CV)

- **Base URL**: `https://visual.volcengineapi.com`
- **Auth**: **AWS V4-style signing** (Volcano SigV4)
- **Endpoints**: `?Action=CVSync2AsyncSubmitTask` / `?Action=CVSync2AsyncGetResult`
- **Extremely complex**; recommend going through a relay layer rather than connecting directly

#### fal.ai

- **Base URL**: `queue.fal.run/{vendor}/{model}/{capability}`
- **Auth**: fal.ai key
- **Features**: native webhook support (`?fal_webhook=URL`) + JWKS signature verification
- **Value**: the only viable integration path for Pika 2.2; can also connect Luma / Veo / Kling and other models

---

## 4. Capability Matrix (side-by-side comparison)

### 4.1 Capability Dimension Definitions

- **T2V**: text-to-video, prompt only
- **I2V first frame**: image-to-video (first-frame constraint)
- **FLF**: first and last frame (First-and-Last-Frame)
- **R2V**: reference-to-video (multiple reference images to keep character/object consistency, **not a first frame**)
- **Extend**: video continuation/extension
- **Audio**: native joint audio-video generation
- **LipSync**: lip sync
- **Character**: character consistency (named entity registration)

### 4.2 Relay-station Schools × Capabilities

✅ Native ｜ 🟡 Partial support/specific models ｜ ❌ Not supported

| School / platform | T2V | I2V | FLF | R2V | Extend | Audio | LipSync | Character |
|---|---|---|---|---|---|---|---|---|
| **School A** OpenAI Sora `/v1/videos` | ✅ | ✅ | ❌ | 🟡 characters API | ✅ extensions | ✅ | ❌ | ✅ |
| **School B** NewAPI `/v1/video/generations` | ✅ | ✅ | 🟡 metadata.image_tail | 🟡 metadata pass-through | ❌ | 🟡 pass-through | ❌ | ❌ |
| **School C** AIMLAPI `/v2/video/generations` | ✅ | ✅ | ✅ | ✅ | 🟡 vendor-specific | ✅ | ❌ | ✅ |
| **School C** APIMart `/v1/videos/generations` | ✅ | ✅ | ❌ | 🟡 prompt-mention | 🟡 sora ref | ✅ Sora2 | ❌ | 🟡 prompt-mention |
| **School D** Kie.ai `jobs/createTask` | ✅ | ✅ | 🟡 model-specific | ✅ character_id_list ≤5 | 🟡 veo extend | ✅ veo/sora2/wan2.7 | ❌ | ✅ |
| **School D** PiAPI `task` | ✅ | ✅ | ✅ image_tail_url | ✅ elements | ✅ task_type=extend | ✅ Seedance 2.0 | ✅ separate task | ✅ |

### 4.3 Official Platforms × Capabilities

| Platform | T2V | I2V | FLF | R2V | Extend | Audio | LipSync | Character |
|---|---|---|---|---|---|---|---|---|
| OpenAI Sora 2 | ✅ | ✅ | ❌ | 🟡 characters | ✅ 6×20s=120s | ✅ | ❌ | ✅ |
| Google Veo 3.1 | ✅ | ✅ | ✅ | ✅ | 🟡 /extend | ✅ 48kHz | ❌ | 🟡 |
| Volcano Seedance 2.0 | ✅ | ✅ first_frame | ✅ last_frame | ✅ 9 images + 3 videos + 3 audios | ✅ | ✅ | ✅ phoneme, 8 languages | ✅ omni-ref |
| Kling 2.6 / 3.0 | ✅ | ✅ | ✅ pro mode | ✅ elements ≤4 | ✅ extend | ✅ 2.6 pro | ✅ lip-sync task | ✅ multi-image |
| MiniMax Hailuo 02 / 2.3 | ✅ | ✅ | ✅ | ✅ subject_reference | ❌ | ✅ 2.3 | ❌ | ✅ S2V-01 |
| PixVerse V5.5 / V6 | ✅ | ✅ | ✅ Transition | ✅ Fusion | ✅ extend | ✅ V5.5+ | ✅ | ✅ ≤3-image fusion |
| Vidu Q3 | ✅ | ✅ | ✅ | ✅ ref_image_urls ≤7 | 🟡 | ✅ + bgm | ❌ | ✅ |
| Runway Gen-4.5 | ✅ | ✅ | ❌ | ✅ gen4_image @mention ≤3 | 🟡 expand | ❌ | ✅ Act-Two | ✅ |
| Pika 2.2 | ✅ | ✅ | ✅ Pikaframes | ✅ Pikascenes ≤5 | 🟡 | ❌ | ❌ | ✅ |
| Luma Ray-2 | ✅ | ✅ keyframes.frame0 | ✅ frame0+frame1 | ❌ | ✅ generation id | ❌ | ❌ | 🟡 chain |
| Alibaba Wan 2.7 | ✅ | ✅ | ✅ | ✅ Image1..5 placeholders | ✅ | ✅ | ✅ s2v digital human | ✅ |
| xAI Grok Imagine | ✅ | ✅ image:{url} | ❌ | ✅ reference_images ≤7 | ✅ extensions | ❌ | ❌ | 🟡 |

### 4.4 Key Observations

- **R2V field names come in 7 variants**: `characters` / `reference_images` / `image_urls` / `reference_image_urls` / `elements` / Wan 2.7 prompt placeholders / `subject_reference`
- **FLF field names come in 4 variants**: `last_frame_image` / `image_tail` / `last_image_url` / `keyframes.frame1`
- **Duration is expressed** inconsistently across platforms: string `"5"` / integer `5` / frame count / enum `6|10` / Luma `"5s"` / Sora `"4"|"8"|"12"|"16"|"20"` string enum
- **Synchronised audio-video generation** has overlapping semantics: joint generation (Veo/Sora/Seedance/Vidu Q3) vs explicit switch (Kling 2.6 pro `enable_audio` / Vidu `generate_audio`) vs automatic (Hailuo 2.3)

---

## 5. Parameter Alignment Table (15 dimensions side by side)

| Dimension | OpenAI Sora 2 | xAI Grok | Kling | MiniMax | Veo 3.1 | Wan 2.7 | Runway | Luma | Vidu Q3 | Pika 2.2 | Seedance 2.0 | NewAPI School B | aimlapi School C | Kie.ai School D | PiAPI School D |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **prompt** | `prompt` | `prompt` | `prompt` | `prompt` | `prompt` | `prompt` | `promptText` | `prompt` | `prompt` | `prompt` | `content[].text` | `prompt` | `prompt` | `input.prompt` | `input.prompt` |
| **negative_prompt** | — | — | `negative_prompt` | — | `negative_prompt` | `negative_prompt` ≤500 | — | — | — | `negative_prompt` | — | `metadata.negative_prompt` | `negative_prompt` | `input.negative_prompt` | `input.negative_prompt` |
| **duration** | `seconds: "4\|8\|12\|16\|20"` | `duration: 1-15` | `duration: "5"\|"10"` | `duration: 6\|10` | `seconds: "8"` | `duration: 2-15` | `duration: 5\|10` | `duration: "5s"\|"9s"` | `duration: 1-16` | `duration: 5\|10` | `duration: 4\|5\|6\|8\|10\|12\|15` | `duration: 5` int | `duration` string | `input.n_frames`/`input.duration` | `input.duration: 5` |
| **resolution** | `size: "1280x720"` | `resolution: "720p"` | `aspect_ratio` | `resolution: "1080P"` | `aspectRatio` | `resolution: "720P"` + `ratio` | `ratio: "1280:720"` | `resolution: "540p..4k"` | `resolution: "540p"\|"720p"\|"1080p"` | `aspect_ratio` + `resolution` | `ratio: "16:9"` + `resolution` | `size: "1920x1080"` | model decides | `input.size: "standard"\|"hd"` | `input.aspect_ratio` |
| **seed** | — | **none** | (old versions) | — | — | `seed` | `seed` | — | `seed: -1` random | `seed` | `seed` | `metadata.seed` | `seed` | — | `input.seed` |
| **first frame** | `input_reference` | `image:{url}` | `image_url` | `first_frame_image` | `image_url` | `first_frame_url` | `promptImage` | `keyframes.frame0` | `image` | `image_url` | `first_frame_url` | `image` | `image_url` | `input.image_urls[0]` | `input.image_url` |
| **last frame** | ❌ | ❌ | `image_tail` | `last_frame_image` | ❌ | `last_frame_url` | ❌ | `keyframes.frame1` | `last_image` | `images[1]` (Pikaframes) | `last_frame_url` | `metadata.image_tail` | `last_image_url` | (some models) | `input.image_tail_url` |
| **reference array** | `characters:[{id}]` | `reference_images:[{url}]` ≤7 | `elements:[]` ≤4 | `subject_reference` single | `image_urls:[]` | `Image1..5` placeholders | `referenceImages` ≤3 | — | `reference_image_urls:[]` ≤7 | `images:[]` ≤5 | `reference_images:[]` ≤9 | `metadata.image_urls:[]` | `image_urls`/`reference_images` | `input.character_id_list` | `input.elements:[]` |
| **audio switch** | (automatic) | — | `enable_audio` (2.6 pro) | (2.3 automatic) | (automatic) | (automatic) | — | — | `generate_audio` + `bgm` | — | `generate_audio` | (pass-through) | (model decides) | `input.sound` (2.6) | `input.enable_audio` |
| **callback** | — polling | — polling | polling | polling | LRO | DashScope async | polling | polling | polling | polling | `callback_url` | `webhook_url` (some) | polling | `callBackUrl` | `config.webhook_config` |
| **mode/quality** | model decides | — | `mode: "std"\|"pro"` | `prompt_optimizer` | model variant | — | model variant | model variant | `movement_amplitude` | `ingredients_mode` | model variant `-fast` | — | model id contains mode | `input.size: "standard"\|"hd"` | `input.mode: "std"\|"pro"` |
| **CFG** | — | — | `cfg_scale: 0-1` (v1) | — | — | — | — | — | — | `cfg_scale` (legacy) | — | (pass-through) | `cfg_scale` | — | `input.cfg_scale` |
| **special header** | — | — | JWT 30min | — | OAuth2 | `X-DashScope-Async` | `X-Runway-Version` | — | — | — | — | — | — | — | — |
| **task_id path** | `$.id` | `$.request_id` | `$.data.task_id` | `$.task_id` | LRO operation | `$.output.task_id` | `$.id` | `$.id` | `$.task_id` | `$.request_id` | `$.id` | `$.task_id` | `$.id` | `$.data.taskId` | `$.task_id` |
| **video URL path** | second GET /content | `$.video.url` | `$.data.task_result.videos[0].url` | second step via file_id | `$.video.uri` | `$.output.video_url` | `$.output[0]` | `$.assets.video` | `$.creations[0].url` | `$.video.url` | `$.content.video_url` | `$.url` | `$.video.url` / `$.assets.video` | `$.data.response.resultUrls[0]` | `$.output.works[0].video.url` |

### 5.1 State Machine Mapping Differences

Terminal-state strings are completely inconsistent between vendors and must be mapped per vendor:

| Platform | queued | running | succeeded | failed |
|---|---|---|---|---|
| OpenAI Sora | `queued` | `in_progress` | `completed` | `failed` |
| NewAPI School B | `queued` | `in_progress` | `completed` | `failed` |
| Alibaba DashScope | `PENDING` (**all upper case**) | `RUNNING` | `SUCCEEDED` | `FAILED` |
| Volcano Ark Seedance | `queued` | `running` | `succeeded` | `failed` |
| Kling | `submitted` | `processing` | **`succeed` (not succeeded)** | `failed` |
| MiniMax Hailuo | `Queueing` / `Preparing` | `Processing` | **`Success` (capitalised)** | `Fail` |
| PiAPI inner level | — | — | `output.status: 99` (**integer**) | `output.status: <99` |
| Runway | `PENDING` | `RUNNING` | `SUCCEEDED` | `FAILED` |

### 5.2 Video URL Expiry (temporary OSS links)

**All official platforms use temporary URLs, which must be re-stored immediately**:

| Platform | Expiry | Risk level |
|---|---|---|
| **MiniMax Hailuo** | **9 hours** (32,400 seconds) | 🔴 Shortest of all platforms |
| OpenAI Sora 2 | Docs and tests disagree (docs say 24h, some users report 1h) | 🟠 |
| Vertex AI Veo | Short period after the LRO operation completes | 🟠 |
| Volcano Ark Seedance | 24 hours (TOS temporary URL) | 🟡 |
| Alibaba DashScope Wan | 24 hours | 🟡 |
| Runway | 24-48 hours | 🟡 |
| Luma Ray-2 | Relatively long | 🟢 |
| Kling | 30 days (inferred from the lip-sync endpoint constraints) | 🟢 |
| Qiniu Cloud sora-compatible | 7 days | 🟢 |

---

## 6. Protocol Priority Recommendations

> Priority scoring dimensions (1-5 each): relay-station ecosystem coverage / model capability coverage / documentation completeness / implementation complexity (simpler scores higher) / long-term stability / demand in ArcReel's web-novel scenarios

### 6.1 Implemented Protocols (keep)

| Protocol | ArcReel endpoint | Status | Notes |
|---|---|---|---|
| OpenAI Sora compatible | `openai-video` | ✅ Implemented | Mind the 2026-09-24 deprecation |
| NewAPI's own | `newapi-video` | ✅ Implemented | metadata pass-through needs an extended vendor mapping table |
| Full Veo 3.1 family | gemini-* provider | ✅ Implemented | — |
| Seedance 1.5/2.0 | ark provider | ✅ Implemented | 2.0 multimodal reference + Extend not enabled |
| Grok Imagine | grok provider | ✅ Implemented | — |
| Full Vidu Q3 family | vidu provider | ✅ Implemented | reference-to-video mode is live |

### 6.2 P0 — Recommended to Fill in for v1.0

| Protocol | Reason for P0 | Complexity |
|---|---|---|
| **School C `/v2/video/generations`** | **Adopted by many relay stations** (AIMLAPI / xAI official / getimg.ai / APIMart / EvoLink / Qiniu veo / CometAPI, etc.); the second-largest de facto standard after School B | Medium (discriminated union by model, needs model-specific schema branches) |
| **Kling official protocol** | The preferred model for Chinese novel-to-video users; self-deployed studios need a direct connection for stability; special JWT auth | Medium-high (JWT HS256 + 30min token cache, multiple endpoints by intent) |
| **Alibaba DashScope (Tongyi Wanxiang Wan)** | **Unique value** for web-novel scenarios: wan2.7 long Chinese prompts + native `shot_type: multi` multi-shot narrative; one protocol covers wan2.7/2.6/2.1-kf2v/2.2-s2v | Medium (X-DashScope-Async + input/parameters nesting + multi-model routing) |
| **MiniMax Hailuo official** | Strong physics, king of value; the two-step File API URL model differs from other platforms; the 9h URL makes re-storage most urgent | Medium-high (two-step File API flow + webhook challenge) |

### 6.3 P1 — Additions for v1.x

| Protocol | Reason |
|---|---|
| **Kie.ai School D** `/api/v1/jobs/createTask` | Representative of the overseas verb style; can cover some niche relay stations |
| **Runway Gen-3/4 official** | Advertising/e-commerce demand; `X-Runway-Version` header; no webhook, polling required |
| **Seedance 2.0 multimodal reference extension** | Model is registered but reference_images / video_extend are not enabled; improves the existing ark provider's capability bitmap |

### 6.4 P2 — Wait for User Feedback

| Protocol | Reason |
|---|---|
| PixVerse official | NewAPI has no native channel, direct official connection required; custom `API-KEY` header |
| fal.ai queue API | The only integration path for Pika 2.2; value of a unified queue protocol |
| Luma Dream Machine | Unique keyframes union type, but low demand in Chinese scenarios |

### 6.5 P3 — Not Proactively Pursued

| Protocol | Reason |
|---|---|
| ByteDance Jimeng direct connection | Volcano SigV4 signing is extremely complex; recommend going through a relay layer (commonly proxied by Schools B/C) |
| Separate PiAPI adaptation | High overlap with Kie.ai; covering up to P1 is enough |
| Replicate predictions | Low overlap with ArcReel's user base |
| Together AI | Same as above |

---

## 7. Choice of Custom Provider Integration Approach (conclusion)

> **Revision note (2026-08)**: the choice in this chapter has been superseded by `docs/adr/0067`: custom call endpoints use declarative JSON definitions, positioned as the unified form for the "JSON in/out + submit/poll" school — covering long-tail aggregators, and absorbing the HTTP-style built-in backends that can be expressed as a whole into declarative definitions shipped with releases (first batch newapi / v2_video_generations / minimax); protocols that cannot be expressed, such as signature-based auth and SDK-style integration, remain as built-in Python endpoints. The conclusions of 7.1–7.2, which used "expressing every protocol" as the yardstick, are no longer a basis for decisions; the runtime plugin route in 7.5 is unaffected (belongs to #872). The body of this chapter is kept as it was.

### 7.1 Comparison

After detailed evaluation, **the plugin approach (Python code extension) is better than the declarative YAML approach**:

| Dimension | Python plugin | Declarative YAML | Winner |
|---|---|---|---|
| Protocol coverage | 100% (fallback) | 50-60% (JWT/SigV4/multipart/two-step flows cannot be expressed) | plugin |
| Integration difficulty | Write a backend class + tests | Write YAML + debug templates | A tie on the surface; in practice plugins are easier to debug |
| ArcReel maintenance cost | Each backend is independent, existing system | Needs new Jinja2 + JSONPath + state mapping + probe + docs | plugin (major advantage) |
| Debugging experience | Standard Python traceback | YAML schema errors / template rendering errors / field path errors | plugin wins by far |
| AI-assisted code generation | Cursor/Claude Code produces a complete backend in 30 seconds | Must first understand a home-grown YAML schema | plugin (the key reversal since 2024) |
| Fit with ArcReel's user profile | All developers who know Python | Suited to non-programmers | plugin |

### 7.2 Key Decision Basis

**The video API ecosystem has 10 kinds of capability that must be implemented in Python** (cannot be expressed declaratively):

1. Kling official JWT signing (HS256, 30-minute expiry, needs a token cache)
2. Volcano Ark SigV4-like signing (AKSK + canonical request + HMAC)
3. DashScope two-step + regional base URL switching (Beijing/Singapore/US West credentials are not interchangeable)
4. OpenAI Sora multipart upload (when local files are base64)
5. MiniMax two-step download (`file_id → /files/{file_id}` to get the download_url)
6. Luma keyframes union type (`type: image|generation`)
7. Vertex AI Veo (service account JSON + OAuth2 token refresh + LRO double polling)
8. Runway header version validation
9. OpenAI Sora content download split into variants (`?variant=video|thumbnail|spritesheet`)
10. PiAPI non-standard status codes (outer string + inner `output.status: 99` integer)

**The scenarios declarative YAML can cover** are exactly the ones ArcReel already covers in its V1 built-in backends (pure JSON in/out schools such as NewAPI, OpenAI-compatible, Kie.ai), which removes users' motivation to integrate declaratively.

### 7.3 Fit with ArcReel's Current State

ArcReel's current **custom provider integration flow already exists and does not need to be redesigned**:

1. User → settings page → add custom provider
2. Fill in `display_name` + `discovery_format` + `base_url` + `api_key`
3. Click [Fetch model list] → calls `/api/v1/custom-providers/discover`
4. The UI infers the endpoint automatically
5. The user ticks models to enable / manually adjusts the endpoint / enters prices
6. Saved to the `custom_provider` + `custom_provider_model` tables

**Engineering steps for adding a new protocol (e.g. Kling official, DashScope)**:
1. Create a new backend class in `lib/backends/video_backends/` (implementing the `VideoBackend` Protocol)
2. Register the new endpoint key and build_backend closure in `ENDPOINT_REGISTRY`
3. Add heuristic rules to `infer_endpoint()`
4. Add `endpoint_xxx_display` in three languages to the i18n files
5. Add unit tests with mocked httpx

**Users don't need to write code** or modify any configuration file; they just attach the corresponding endpoint when adding a custom provider in the frontend UI.

### 7.4 Distinguishing Two "New Backend" Scenarios (prerequisite concept)

Two user scenarios must be strictly distinguished; their integration paths are completely different:

| Scenario | Description | Current support |
|---|---|---|
| **Scenario A: protocol already supported, connecting a new relay station** | The user finds a new NewAPI / School C aggregator / OpenAI-compatible site | ✅ Pure UI operation, zero code (described in 7.3) |
| **Scenario B: protocol not supported, a new protocol adaptation is needed** | The user wants to connect Kling official (JWT) / DashScope (special header) / some brand-new private relay protocol | ⚠️ Currently requires changing the `lib/backends/video_backends/` + `ENDPOINT_REGISTRY` source |

**Scenario A accounts for the vast majority of user needs**, and the experience is already smooth. **Scenario B currently has a gap in the experience** — connecting a new protocol requires changing source code, rebuilding, and going through a PR or a local fork. The runtime plugin feature is meant precisely to close the Scenario B gap and support community protocol sharing.

---

## 7.5 Runtime Plugin Mechanism Research (supporting material for feature planning)

> This section provides research on feasibility and design options for ArcReel's planned "runtime plugin" feature. Goal: let users load third-party-contributed video protocol backends without modifying ArcReel source or rebuilding the image, thereby supporting community sharing and lowering the barrier to integrating custom protocols. This section **does not make decisions for the PRD**; it only summarises viable paths, industry practice, points of fit with ArcReel's existing architecture, and open problems.

### 7.5.1 Current State of ArcReel's Registration Mechanism (the starting point for runtime plugins)

The current state confirmed after studying ArcReel's source (these are the facts a runtime plugin design must build on):

1. **Backend registration is an in-process static dictionary**. The three media backends (`video_backends` / `image_backends` / `text_backends`) each have a `registry.py` with exactly the same pattern:

   ```python
   _BACKEND_FACTORIES: dict[str, Callable[..., VideoBackend]] = {}


   def register_backend(name: str, factory: Callable[..., VideoBackend]) -> None:
       _BACKEND_FACTORIES[name] = factory


   def create_backend(name: str, **kwargs) -> VideoBackend:
       if name not in _BACKEND_FACTORIES:
           raise ValueError(f"Unknown video backend: {name}")
       return _BACKEND_FACTORIES[name](**kwargs)
   ```

2. **Registration happens at module import**. `lib/backends/video_backends/__init__.py` explicitly calls `register_backend(PROVIDER_GROK, GrokVideoBackend)` etc. at load time, registering all built-in backends into the dictionary. `register_backend()` itself is already a public API, and **calling it again at runtime is perfectly legal** — this is the natural entry point for runtime plugins.

3. **Protocol ownership lives in `ENDPOINT_REGISTRY` (a static dictionary)**. The `CustomProviderModel.endpoint` field can only take keys already registered in `ENDPOINT_REGISTRY`. This is currently the biggest obstacle to runtime extension: **even if a user registers a new backend at runtime, they cannot register a new endpoint key for models to attach to**.

4. **Backend constructor parameters are injected from DB configuration**. Factories such as `create_text_backend_for_task()` read the DB via `ConfigResolver.provider_config()` to get `api_key` etc. and pass them explicitly to the backend constructor, no longer relying on environment variable fallback (`2026-05-12-agent-sandbox-design` removed all env fallbacks). Runtime plugin backends must likewise accept explicit configuration injection and must not read environment variables.

**Conclusion**: ArcReel's `register_backend()` + duck-typed Protocol design is **naturally suited to runtime extension**. The core change is turning `ENDPOINT_REGISTRY` from a static dictionary into a two-layer "built-in + dynamically injected plugin" structure, and adding plugin discovery, loading, security, and lifecycle management.

### 7.5.2 Benchmarking Industry Runtime Plugin Mechanisms

| Approach | Discovery mechanism | Registration | What suits ArcReel | What doesn't |
|---|---|---|---|---|
| **Python entry_points** (PEP 621) | `importlib.metadata.entry_points(group=...)` | Automatically visible after the user runs `pip install arcreel-plugin-xxx` | Standard, zero third-party dependencies, fits the "community sharing of pip packages" vision | Must be packaged as a wheel, bare local files don't work; Docker users must rebuild the image or mount to add packages |
| **Directory scan + importlib** | Scan `*_backend.py` in a given directory and load dynamically with `importlib.util.spec_from_file_location` | Takes effect as soon as the file lands; combined with `__subclasses__()` for automatic capture | Self-deployed users can load by mounting a volume without rebuilding the image; friendly for local development | Must handle duplicate loading, name conflicts, and error isolation ourselves |
| **pluggy** (pytest plugin system) | Hook specs + setuptools entry_points | Hook multicast (1:N) | Mature and stable | ArcReel routes 1:1 (one model → one backend) and doesn't need hook multicast; pluggy lacks lifecycle management (token cache/connection pool), so it would be over-engineering |
| **LiteLLM `custom_provider_map`** | Declare `{provider, custom_handler}` in a config file | Module path + instance variable | Directly matches the "user registers a custom provider" scenario | LiteLLM's video endpoints currently don't support CustomLLM (even embeddings are still at the issue stage), so its video path can't be reused directly |

**Industry consensus**: a dual track of entry_points (published plugins) + directory scanning (local development/self-deployment) is the mainstream combination for plugin discovery in modern Python applications. ArcReel's Docker deployment form means **directory scanning + volume mounting** is friendlier for self-deployed users, while entry_points better suit mature plugins "published to PyPI for one-click community installation".

**LiteLLM's evolution (direct reference value)**: LiteLLM originally only supported manual runtime assignment `litellm.custom_provider_map = [{"provider": ..., "custom_handler": ...}]`; in issue #7733 the community asked for entry_points so third-party packages could register automatically (at the time only a `.pth` hack worked), and PR #15881 then implemented declaring CustomLLM subclasses via `[project.entry-points.litellm]` in `pyproject.toml`, discovered and registered automatically by `importlib.metadata`. This "manual registration → automatic entry_points discovery" path closely matches ArcReel's current state (manual `register_backend()` calls) and can serve as a direct blueprint for a phased implementation.

**A known LiteLLM defect that must be avoided**: issue #23352 reports that when a model name registered by a plugin (after stripping the prefix) collides with a known model of a built-in provider, requests are **silently routed to the built-in provider instead of the plugin handler**, without any error. Lesson for ArcReel: dispatch priority and namespace isolation for plugin endpoints must be defined at design time; **explicitly registered plugins should take priority over heuristic inference**, to avoid silent hijacking by identically named models.

### 7.5.3 Design Questions Runtime Plugins Must Resolve (pending PRD decisions)

The following is a list of questions, derived from the existing architecture, that runtime plugins must answer, as input to the PRD:

**A. Discovery and loading**
- Plugin source: PyPI packages (entry_points) / local directory (volume mount) / support both?
- Load timing: load once at process start, or support hot loading (adding plugins while running without a restart)?
- Load failure isolation: an exception from a single plugin must not bring down the whole backend registry; needs try-except wrapping + degraded logging (following the current design philosophy where a missing key in `register_backend` doesn't affect startup).

**B. Making ENDPOINT_REGISTRY extensible (core change)**
- Can plugins register new endpoint keys? If so, `ENDPOINT_REGISTRY` needs to change from a module-level static dictionary to one that supports runtime injection.
- Namespace isolation for new endpoint keys: should plugin endpoints get a prefix (e.g. `plugin:kling-official`) to prevent conflicts with built-in keys?
- How the `infer_endpoint()` heuristics accommodate plugin endpoints: can a plugin declare its own heuristic rules (model_id match pattern → its own endpoint)?

**C. Plugin contract (backend interface constraints)**
- Plugin backends must implement the `VideoBackend` Protocol (`name` / `model` / `capabilities` / `generate()`); the existing Protocol is ready for this.
- Do plugins need to declare metadata: supported model list, capability bitmap, required credential fields (`required_keys` / `secret_keys`, see `ProviderMeta`), display name (i18n)?
- Plugin configuration injection: how does a plugin backend get the api_key / base_url the user entered in the UI? Needs to reuse the existing `CustomProvider` credential storage + `mask_secret()` masking.

**D. Security (the core risk of executing third-party code at runtime)**
- A plugin is arbitrary Python code running inside the ArcReel process with full file system/network access.
- Is a sandbox needed? ArcReel already has an agent sandbox (bubblewrap/seatbelt, see `2026-05-12-agent-sandbox-design`), but that isolates the Agent SDK's Bash subprocesses; **backend plugins run in the main process** and cannot directly reuse the same sandbox.
- **An in-process Python sandbox is not feasible (a clear research conclusion)**: RestrictedPython states officially that it "is not a sandbox system or a secured environment"; the industry consensus is that an in-process CPython sandbox can hardly be made truly secure because of Python's dynamic features (`__import__` abuse, introspection escapes, deserialisation attacks) (the pysandbox author declared this path a dead end). Real isolation can only come from process/container boundaries (seccomp, namespaces, micro-VMs such as Firecracker, WASM compilation), but these all conflict with the form of "a plugin called as a backend in the main process". **Therefore ArcReel's plugin security should not rely on a code-level sandbox, but follow a "source trust" route.**
- Mitigation directions (source trust route): plugin source review (trust only signed PyPI packages / only allow-listed GitHub repositories), manual confirmation at install time, a community rating mechanism, an officially maintained list of reviewed plugins, static code scanning before install (dependency/dangerous-call detection) — the specific trade-offs are pending PRD decisions.
- Credential leakage risk: a malicious plugin could read other providers' api_keys. Need to evaluate whether to restrict plugins to accessing only the credentials attached to them.

**E. Lifecycle management**
- Connection pools / token caches: how is the lifecycle of plugin instances managed for stateful resources such as Kling's 30-minute JWT expiry and connection pool reuse (singleton vs new per request)? Existing backends reuse instances via a `(provider_name, model)` caching strategy, which plugins should follow.
- Uninstall / update: when a user disables or updates a plugin, how are the registered backends and endpoints cleaned up?

**F. Distribution and community**
- Form of community sharing: GitHub repositories / PyPI packages / an official ArcReel plugin marketplace?
- Version compatibility: plugins declare the range of ArcReel versions they are compatible with (compatibility strategy when the Protocol interface changes).
- Docs and scaffolding: provide an `arcreel backend scaffold`-style tool + AI prompt templates so users can quickly generate a Protocol-compliant plugin skeleton with Cursor/Claude Code (7.1 already argued that AI-assisted generation is the key advantage of the plugin approach over the declarative one).

### 7.5.4 Assessment of Fit with the Existing Architecture

| Change point | Existing foundation | Amount of change |
|---|---|---|
| Runtime backend registration | `register_backend()` is already a public API, duck-typed Protocol ready | Small (just add plugin loader calls) |
| Extensible ENDPOINT_REGISTRY | Currently a static dictionary | **Medium** (needs a two-layer structure + namespace isolation) |
| `infer_endpoint()` accommodating plugins | Existing heuristics are hard-coded | Medium (needs an interface for plugins to declare heuristic rules) |
| Credential storage/masking | `CustomProvider` + `mask_secret()` ready | Small (plugins just reuse it) |
| i18n endpoint display names | `endpoint_xxx_display` three-language mechanism ready | Small (plugins must provide their own display names, may need an English fallback) |
| Security model | The agent sandbox doesn't cover main-process backends; an in-process Python sandbox has been shown infeasible | **Large/unknown** (can't rely on a code sandbox, only source trust + review, which is a product/security decision) |
| Lifecycle/caching | `(provider, model)` instance cache ready | Small (plugins follow it) |

**Overall assessment**: the `register_backend()` + Protocol design gives a high fit and a manageable amount of change for the **functional implementation** of runtime plugins; the real difficulty is concentrated in two places — **making ENDPOINT_REGISTRY dynamic** (an engineering problem, medium) and **the security model for executing third-party code in the main process** (a product + security decision that the PRD must argue in depth). Since the research has shown a code-level sandbox to be infeasible, the focus of the security design should be "source trust + review + community governance" rather than "technical isolation".

### 7.5.5 Possible Incremental Rollout Approaches (for the PRD to choose from, not final)

Several incremental paths found in the research, ordered from smallest to largest change:

1. **Minimum viable (self-deployment only)**: directory scanning + volume mounting, only supporting self-deployed users loading plugins they wrote themselves in a trusted environment, without addressing security (the user trusts themselves). Smallest change, allows the mechanism to be validated quickly.
2. **Community sharing (PyPI + review)**: entry_points + PyPI package publishing, combined with an officially maintained "list of reviewed plugins"; community contributions must be reviewed before entering the recommended list. Balances openness and security.
3. **Official plugin marketplace**: ArcReel maintains a plugin registry + version compatibility declarations + community ratings, and users install with one click in the UI. Best experience, but needs marketplace infrastructure and ongoing operational investment.

These three paths are not mutually exclusive and can serve as three stages of the feature's evolution.

---

## 8. Summary of Key Risks and Pitfalls

### 8.1 OpenAI Sora 2 Deprecation

- Official notice issued 2026-03-24: **Sora 2 / Sora 2 Pro and the Videos API will be shut down on 2026-09-24**
- Affected model IDs: `sora-2`, `sora-2-pro`, `sora-2-2025-10-06`, `sora-2-2025-12-08`, `sora-2-pro-2025-10-06`
- ArcReel must complete a migration assessment to sora-3 or an alternative model before 2026-Q3
- The `/v1/videos` path will remain a de facto standard; relay stations will keep using it out of inertia

### 8.2 NewAPI metadata Pass-through Completeness Is Uncontrollable

- The same Kling `camera_control` works on DMXAPI but may be lost on some self-deployed NewAPI instances
- ArcReel's `newapi-video` backend needs to note, at the channel configuration or documentation level, the set of fields verified to pass through
- For some advanced capabilities (motion brush / camera control), recommend marking in the UI that they "may not be available on some relay stations"

### 8.3 The Universal Video URL Expiry Trap

- All platforms use temporary URLs; the most dangerous, MiniMax, lasts only **9 hours**
- ArcReel must **start re-storing within 10 seconds** of SUCCEEDED, to local or object storage
- Don't rely on vendor URLs as frontend display links

### 8.4 Status String Differences

- Kling `succeed` (not `succeeded`)
- MiniMax `Success` (capitalised)
- DashScope all upper case `SUCCEEDED`
- PiAPI inner `output.status: 99` (integer)
- Each backend must maintain its own status mapping table; they cannot be reused

### 8.5 Fragmented Model ID Naming

In School C `/v2/video/generations`, the same model is named completely differently on different relay stations:
- AIMLAPI: `kling-video/v1/standard/text-to-video`
- APIMart: `sora-2-vip`
- getimg.ai: `happyhorse-1` (its own brand name)
- xAI official: `grok-imagine-video`

ArcReel needs to maintain a model name alias mapping at the channel configuration layer, or accept manual user correction in the `infer_endpoint()` heuristics.

### 8.6 Discovery Limitations of School C v2

The `/v1/models` list cannot reliably distinguish whether the target endpoint is `/v1/video/generations` or `/v2/video/generations`, because model ID naming is identical for both.

**Automatic heuristics can only provide a default** (suggest `newapi-video`, which is more common); users switch manually in the UI according to the actual relay station's docs. This is an inherent limitation of the NewAPI / OneAPI-derived relay ecosystem, not a design flaw of ArcReel.

### 8.7 Seedance 2.0 Model IDs Differ Between China and Overseas

- China: `doubao-seedance-2-0-260128`
- Overseas: `dreamina-seedance-2-0-260128`
- Cross-region calls will always return 404
- ArcReel's existing ark / ark-agent-plan dual-provider design can serve as a reference

### 8.8 Content Moderation Error Codes

Moderation rejection error codes differ completely between platforms:
- OpenAI Sora: `error.code: "moderation_blocked"`
- Alibaba DashScope: `IPInfringementSuspect` / `DataInspectionFailed`
- Kling: HTTP 500 + `error: "...violate the community guidelines (CM_EXT.POther)"` (**use the error field, not message**)

These should not be treated as retryable errors and need special recognition at the backend layer.

---

## 9. Questions for the Follow-up PRD / Design Documents

As input material, this report leaves the following questions for the PRD stage to settle. They are grouped into three sets: "architecture layer → protocol implementation layer → runtime plugin layer".

### 9.1 Architecture Alignment Assessment (before decisions on specific protocol integrations)

> This group consists of **decisions at a higher level than integrating any single protocol**: before adding protocols at scale, first assess whether the current `lib/backends/video_backends/` + `lib/custom_provider/` architecture needs adjusting to align with mature industry implementations. **Important precondition: don't be misled by naming.**

**On naming (clarified first to avoid misdirection)**:
- ArcReel's current `VideoBackend` Protocol + the various `XxxVideoBackend` implementations **are in essence already the industry-favoured Ports & Adapters (hexagonal architecture) paradigm** — the Protocol is the Port and each backend is an Adapter. The architectural skeleton is already aligned with best practice.
- The glossary (GLOSSARY.md) chose `backend` rather than `adapter` because: backend fits the "derivation" semantics with provider (one provider derives multiple backends), parallels frontend, and keeps naming consistent across the video/image/text media backends. **Mature projects such as SQLAlchemy / Django likewise name the same kind of role backend rather than adapter**, so the naming itself is not a case of "not aligned with industry".
- **Leaning conclusion**: refactoring with "align on the adapter name" as the goal is not recommended; naming is superficial, and what really needs assessing are the capability gaps below.

**Architectural gaps the PRD really needs to assess (unrelated to naming)**:

1. **Whether a unified async task abstraction should be extracted as a first-class citizen**. This research confirmed: 9 official platforms + 4 major relay schools **all use an async task model** (submit → poll → status mapping → URL re-storage), and the state-machine strings, URL expiry windows, two-step URL retrieval, etc. all differ.
   - **Real-signal check**: are the current backends each re-implementing the poll loop / status mapping / URL re-storage? If duplication is high, an `AsyncVideoTask` abstraction should be extracted above `VideoBackend` (the fal.ai queue model can serve as a reference). If shared poll infrastructure already exists, nothing needs to change.
   - This is the substantive point to align with industry, unrelated to naming.

2. **Coupling review for adding protocols**. Ideal state: adding a new protocol (e.g. Kling) only requires a new backend class + one registration line in `ENDPOINT_REGISTRY`.
   - **Real-signal check**: if adding a new protocol requires changing `resolver` / `cost_calculator` / `media_generator` / several enums at the same time, coupling is too tight and needs decoupling — but the decoupling approach is unrelated to naming.

3. **Making the ENDPOINT_REGISTRY static dictionary extensible**. This is a hard blocker for runtime plugins (see 7.5.1) and the main gap versus industry extensible registration mechanisms (entry_points / pluggy). It is a point that clearly should be aligned (see 9.3).

**Guiding principle**: let real pain points (adding a protocol touches many files, duplicated async polling code, plugins blocked by the static dictionary) drive refactoring, not the observation that "the industry calls it adapter". Without the substantive pain points above, keep the current architecture and only extend it incrementally.

### 9.2 Protocol Implementation Layer (concrete questions for P0 protocol integration)

1. **The concrete integration order for P0 protocol adaptations** (suggest School C v2-video-generations first → Kling official → DashScope → MiniMax)
2. **The key name for each new endpoint in `ENDPOINT_REGISTRY`** (e.g. `v2-video-generations` / `kling-official` / `dashscope-async` / `minimax-video`)
3. **Extension rules for the `infer_endpoint()` heuristics** (how to distinguish new endpoints from existing ones)
4. **Design of a video URL re-storage middleware** (for short-lived URLs such as MiniMax 9h, Sora 1h)
5. **How the MiniMax File API two-step flow is encapsulated inside the backend** (exposing a unified video_url externally)
6. **Kling JWT token caching strategy** (asyncio.Lock + dict TTL)
7. **DashScope region configuration** (binding between base_url and api_key)
8. **Design of metadata profiles for the NewAPI backend** (field mapping tables maintained per vendor)
9. **A unified async task state-machine abstraction** (introduce it at the VideoBackend layer or implement per backend; decided together with item 1 of 9.1)
10. **Response to the Sora 2 deprecation** (migration assessment window and fallback strategy)

### 9.3 Runtime Plugin Layer

1. **Approach for making ENDPOINT_REGISTRY dynamic for runtime plugins** (static dictionary → built-in + plugin two-layer structure; same root as item 3 of 9.1)
2. **The security model for runtime plugins** (a code-level sandbox has been shown infeasible; focus on source trust + review)
3. **Choice of discovery and loading mechanism for runtime plugins** (entry_points / directory scanning / both)
4. **Contract definition for runtime plugins** (metadata declaration, credential injection, i18n, heuristic rules)
5. **Phasing of the incremental rollout of runtime plugins** (minimum viable → community sharing → official marketplace)

---

## 10. References

### First-hand API Documentation

- OpenAI Sora 2: https://platform.openai.com/docs/guides/video-generation
- OpenAI Deprecations: https://developers.openai.com/api/docs/deprecations
- NewAPI docs: https://doc.newapi.pro/api/generate-video/ and https://doc.newapi.pro/api/kling-jimeng/
- Kling: https://app.klingai.com/cn/dev/document-api
- Volcano Ark Seedance: https://www.volcengine.com/docs/82379
- Alibaba DashScope: https://help.aliyun.com/zh/model-studio/text-to-video-api-reference
- MiniMax Hailuo: https://platform.minimax.io/docs/api-reference/video-generation-t2v
- AI/ML API: https://docs.aimlapi.com/api-references/video-models
- xAI Grok: https://docs.x.ai/developers/model-capabilities/video/generation
- Kie.ai: https://docs.kie.ai/market/
- PiAPI: https://piapi.ai/docs/
- Runway: https://docs.dev.runwayml.com/
- Luma: https://docs.lumalabs.ai/docs/video-generation
- PixVerse: https://docs.platform.pixverse.ai/

### Existing ArcReel Design Documents (background alignment)

- `docs/adr/` — architectural decisions such as VideoBackend layering, custom providers and endpoints, the duration source of truth
- `GLOSSARY.md` — glossary (backend rather than adapter)

### Relay-station Documentation (sources of de facto standards)

- AiHubMix: https://docs.aihubmix.com/en/api/Video-Gen
- APIMart: https://docs.apimart.ai/en/api-reference/videos/
- Qiniu Cloud AI inference: https://developer.qiniu.com/aitokenapi
- useapi.net Kling: https://useapi.net/docs/api-kling-v1/
- fal.ai: https://fal.ai/models/

---

**Report version**: v1 (final research version)
**Architecture alignment**: ArcReel `lib/backends/video_backends/` + `lib/custom_provider/` + `ENDPOINT_REGISTRY`
**Next step**: write the PRD and design documents for specific endpoint integrations based on this report
