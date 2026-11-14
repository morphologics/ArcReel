# ArcReel TTS (Narration Voice-over) Selection and Integration Research Report

> Research date: 2026-06-02. All unit prices / model names / API details were fetched on that date; TTS pricing changes frequently, so re-check the live docs before making engineering decisions.
> Uncertain items are always marked **UNVERIFIED** and never invented (following the "don't guess external provider data" principle).
> The integration decisions for this phase that correspond to this report are in `docs/adr/0010-tts-audio-through-generation-queue.md` and `GLOSSARY.md` "Media types and voice-over (TTS)".

## 0. Scope and positioning

Selecting TTS for ArcReel (Chinese novel → short video), **used in this phase for narration voice-over in storytelling (narration) mode**.

- **Primary selection criterion**: quality of Chinese narration (tone / pauses / emotion / continuity in read-aloud long-form text). The architecture is not tied to a language; language coverage is noted per provider, and the final choice is left to the user.
- **Both integration paths are covered**: A. cloud API providers (main integration path); B. self-hosted / on-device open-source models (privacy / cost comparison).
- **ArcReel integration constraints**: ① custom provider credentials are fixed to a single `api_key` field + `base_url` (`docs/adr/0008`); multi-field auth is only possible via a built-in preset; ② OpenAI-compatible `/v1/audio/speech` can reuse the existing OpenAI client factory and go through relays; ③ declarative pricing is declared per `kind` (`docs/adr/0009`); ④ sync vs. async decides whether the backend mimics `text_backends` or `video_backends`; ⑤ if TTS can reuse the credentials of an already integrated provider (Gemini / Volcengine Ark / OpenAI / Grok / Vidu / Alibaba Bailian DashScope), integration cost is lowest.

Method: two rounds of adversarial deep-research (109 + 9 agents) + targeted gap-filling (duration mismatch / specs of already integrated providers / watermarks). Conclusions are labelled with confidence and votes (e.g. 3-0 = confirmed by three votes, 2-1 = confirmed by majority, 0-3/1-2 = rejected).

## 1. Conclusions first

### 1.1 Tiered recommendations

| Tier | First choice | Rationale | Cost |
|---|---|---|---|
| **Lowest integration cost** | **Alibaba DashScope Qwen-TTS** | Reuses ArcReel's existing single-field sk- DashScope credential (zero new config forms); best Chinese in the low-friction tier; Qwen-TTS synchronous HTTP fits a synchronous backend | TTS uses the native `/api/v1` path (not OpenAI-compatible), so a synchronous adapter must be written; unit price UNVERIFIED |
| **Extensible / self-service** | **Custom provider OpenAI-compatible audio path** | Single field + genuinely OpenAI-compatible; users can hook up Fish / self-hosted IndexTTS (behind a shim) / relays themselves, so ArcReel doesn't have to write code per provider | Requires a new audio `EndpointSpec` + `CustomAudioBackend` + an audio mode in `infer_endpoint` |
| **Best Chinese quality** | **Volcengine Doubao large-model speech synthesis** / ElevenLabs | Doubao: 325 voices, native Chinese dialects, Voice Cloning 2.0, emotional continuity; ElevenLabs: Multilingual v2/v3 has a good reputation for Chinese | Doubao uses multi-field auth (appid+access_token+resource_id) so it needs a built-in preset; **the Ark key cannot be reused** (Seed Speech is a separate service); neither is OpenAI-compatible |
| **Self-hosted privacy** | **CosyVoice2** / **VoxCPM-0.5B** | Both Apache 2.0, commercially usable; VoxCPM's Chinese CER of 0.93% is the best among open-source models | Needs a GPU; no official OpenAI-compatible wrapper; VRAM/RTF UNVERIFIED |

### 1.2 Decision for this phase (v1)

**DashScope Qwen-TTS preset (reused credential, per-character billing, synchronous native adapter) + custom provider OpenAI-compatible audio path (`openai-tts` → `/v1/audio/speech` + `CustomAudioBackend`).** Fish / self-hosted IndexTTS (behind a shim) / relays go through the custom path. See `docs/adr/0010` for details.

## 2. Cloud API provider comparison

### 2.1 Main comparison table

| Provider | Chinese | OpenAI-compatible `/v1/audio/speech` | Sync/async | Auth | Billing unit | Already integrated in ArcReel | Confidence |
|---|---|---|---|---|---|---|---|
| **OpenAI** tts-1 / tts-1-hd / gpt-4o-mini-tts | Strong in English, weak in Chinese | ✅ Yes (the industry-standard shape) | Synchronous, returns bytes directly + chunked streaming | Single field | Per character (tts-1 $15, tts-1-hd $30 /1M chars) | Text/image integrated (TTS not) | 3-0; unit price 2-1 |
| **ElevenLabs** Multilingual v2/v3, Flash/Turbo | Excellent | ❌ Proprietary | Synchronous (`/stream` chunked, still a one-shot request) | Single field | Per character (Flash/Turbo $0.05, Multilingual $0.10 /1K chars) | No | 3-0 |
| **Alibaba DashScope** Qwen-TTS / CosyVoice | Strong | ❌ TTS is **not** on the compatible path (compatible path is chat only) | Qwen-TTS synchronous HTTP; CosyVoice realtime = WebSocket | Single-field Bearer (sk-) | Per character / per 10K characters (CosyVoice ~¥2/10K chars **UNVERIFIED**) | ✅ Credential reusable | 3-0; unit price UNVERIFIED |
| **Volcengine Doubao** large-model speech synthesis (Seed-TTS) | Strong (native dialects) | ❌ Proprietary (openspeech.bytedance.com) | Synchronous HTTP + WSS streaming + **async long text** (submit→query ≤100K characters, audio kept for 7 days) | **Multi-field** (appid+access_token+resource_id) | Per character / character bundles + voices billed yearly | ❌ Ark key not reusable | 3-0 |
| **MiniMax** T2A async | Strong | ❌ Proprietary (`t2a_async_v2`) | **Async** (create task → poll → fetch file_id, URL expires in 9h) | Single field **rejected** (1-2, do not assume it can use the custom path) | Credit subscription tiers **rejected** (0-3) | No | API 3-0 |
| **Fish Audio** (OpenAudio S1) | Good | ✅ Yes (native `/v1/audio/speech`) | Synchronous streaming (TTFB<150ms) | Single-field Bearer | Per **UTF-8 byte** ($15/1M bytes) | No (best candidate for the custom path) | Gap-fill, single round |
| **Gemini** native audio TTS | Good (cmn) | ❌ `generateContent` inline base64, **no streaming** | Synchronous | Single field (x-goog-api-key) | Per **token** | ✅ Credential reusable | Gap-fill, single round |
| **Google Cloud TTS** (Chirp3/Neural2/WaveNet) | Good (cmn-CN) | ❌ `synthesize` REST | Synchronous; long audio via async LRO | **Multi-field** (GCP service-account) | Per character ($4–$160/1M) | Same credential family as Vertex (API must be enabled) | Gap-fill, single round |
| **Azure AI Speech** | Good (zh-CN + dialects) | ❌ | Synchronous realtime + async batch | **Multi-field** (subscription key + region) | Per character ($16/1M std) | No | Gap-fill, single round |
| **Tencent Cloud TTS** | Strong | ❌ Signed API | Synchronous + streaming | **Multi-field** (AppID+SecretId+SecretKey signature) | Per character, tiered | No | Gap-fill, single round |
| **iFlytek TTS** | Top-tier | ❌ WSS | Streaming WebSocket | **Multi-field** (APPID+APIKey+APISecret HMAC signature) | Per character / bundles | No | Gap-fill, single round |

### 2.2 Key boundaries

- **OpenAI billing is split**: the main pricing page has moved to per-token realtime pricing; the dedicated per-character TTS models (tts-1 $15, tts-1-hd $30 /1M chars) are **not in the main table** — check `developers.openai.com/api/docs/models/tts-1`. The conclusion "pricing has moved to tokens" was judged slightly overstated by the verifiers (2-1).
- **DashScope credential is reusable but TTS is not on the compatible path**: the single-field Bearer can be reused, but the OpenAI-compatible interface only exposes chat completions; TTS uses the native `/api/v1/services/aigc/multimodal-generation/generation` — it cannot reuse the OpenAI client factory / relays, so a native adapter is needed.
- **Doubao "credential trap"**: Doubao Speech (Seed-TTS) is a service separate from Ark with multi-field auth; **ArcReel's existing Ark key cannot be reused** — it needs a built-in preset + a new multi-field credential form.
- **Two MiniMax claims were rejected**: single-field auth (1-2) and credit subscription tiers (0-3) both failed verification — **do not use them to assert that MiniMax can go through the custom provider path**.
- **Declarative pricing kind**: across vendors this splits into three categories: **per character** (OpenAI/ElevenLabs/DashScope/GCloud/Azure/Tencent/iFlytek), **per token** (Gemini), and **per UTF-8 byte** (Fish). The `docs/adr/0009` declarative pricing needs at least a `per_character` kind in this phase.

## 3. Self-hosted / on-device open-source TTS (privacy tier, including licensing traps)

| Model | Chinese quality | License / commercial use | Notes | Confidence |
|---|---|---|---|---|
| **CosyVoice2-0.5B** | Strong (CER 1.38%) | ✅ Apache 2.0, both code and weights commercially usable, royalty-free | FunAudioLLM; voice cloning | 3-0 |
| **VoxCPM-0.5B** | **Best open-source (CER 0.93%)** | ✅ Apache 2.0, commercially usable | Tokenizer-free, 1.8M hours of Chinese and English; beats IndexTTS2/CosyVoice2 | 3-0 (self-reported by the authors in an arXiv preprint, not peer-reviewed) |
| **IndexTTS2** | Strong (CER 1.03%, second among open-source) | ⚠️ Commercial use of weights requires bilibili authorization (v1 requires written authorization; **v2 uses a threshold**: a separate license is needed only above 100M MAU or above RMB 1B annual revenue, so small and medium commercial use is most likely exempt) | bilibili; strong emotion control; the paper has **duration control** but GitHub notes "not yet enabled in this version"; **no official API server / no OpenAI-compatible endpoint**, so connecting it to ArcReel requires your own shim; GPU VRAM/RTF UNVERIFIED | License 2-1 |
| **F5-TTS** | Good | ⚠️ Base weights are **CC-BY-NC, no commercial use** (Emilia dataset; still prohibited after fine-tuning; code is MIT); commercial use requires training from scratch on your own commercial data | Commercial-use trap | 3-0 |

**Conclusion**: for the self-hosted privacy tier, first choice is **CosyVoice2 / VoxCPM** (both Apache 2.0, cleanly commercially usable). IndexTTS / F5-TTS have commercial licensing traps; check the license version by version before choosing them. The clean way to connect any self-hosted model to ArcReel = put an OpenAI-compatible `/v1/audio/speech` service in front of it when self-hosting, and plug it in through the custom provider audio path.

## 4. Key risks and unknowns

### 4.1 Duration mismatch (most critical; deferred in this phase but the solution is recorded)

The real TTS speech duration ≠ the narration script's `NarrationSegment.duration_seconds` (this preset duration drives both the video clip length and the subtitle timeline). **Industry-standard solution: invert the contract — the audio drives the timeline, and `duration_seconds` is demoted to a generation hint.**

- ArcReel's existing `server/services/presentation/jianying_draft_service.py` **already lays things out by real material duration** (both video and subtitle `trange` use `actual_duration_us = material.duration`); "the longest real material wins" is already the existing design.
- Audio longer than video → **stretch the video** (`setpts`/`tpad` freeze frame, no quality loss), **never speed up the narration**.
- Subtitles are **generated from the real audio**: prefer provider character-level timestamps (ElevenLabs `with-timestamps`, works for Chinese, zero extra infrastructure); fallback is WhisperX forced alignment (for Chinese only the community model `jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn`, with weaker accuracy).
- ⚠️ **Don't count on "isochronous TTS"** (a target_duration parameter): no commercial Chinese cloud API exposes it; it exists only at research level (Amazon ICASSP'22, VideoDubber AAAI'23 explicitly say Chinese is harder because token count ≠ speech duration). The self-hosted IndexTTS2 paper has duration control but the current version doesn't enable it.
- ⚠️ Controlling duration via the speed parameter is unreliable: ranges differ by vendor (ElevenLabs 0.7–1.2, OpenAI 0.25–4.0, Doubao 0.8–2.0, MiniMax 0.5–2.0), and **OpenAI `gpt-4o-mini-tts` has historically ignored speed** (officially called a documentation bug); it can only be used for secondary ±10% fine-tuning. `ffmpeg atempo` is clean only at 0.5–2.0x, and stretching speech beyond ~10–15% causes audible degradation.

> Decision for this phase: **simply attach a Jianying (CapCut) audio track (each segment independent, not aligned, user fine-tunes manually)**; no automatic re-timing / subtitle reflow (deferred). The solution above is for reference when this is enabled later.

### 4.2 Other

- **Long text**: MiniMax inline text is capped at 50K characters, the file path allows up to 1M (2-1); Doubao async long text ≤100K characters. Per-segment narration is short, so no chunking is needed in this phase.
- **Voice consistency**: a single fixed voice is enough for cross-segment consistency; voice cloning (Doubao 2.0 / CosyVoice) can lock the voice further (this phase uses preset voices; cloning is left for later).
- **Streaming vs. whole file**: ArcReel needs to land a versionable audio file + feed the Jianying (CapCut) audio track, **so receiving the whole file is enough; streaming only adds complexity** (the synchronous backend fetches the bytes in one go).
- **Watermark / compliance**: **none** of the selected providers forces a non-removable watermark on preset voices (MiniMax `aigc_watermark` defaults to false). ArcReel is an open-source project; AI-labelling compliance obligations do not fall on this project (user decision; not expanded on in this report).

## 5. Integration decisions and implementation surface for this phase (summary)

The full decision record is in `docs/adr/0010` and `GLOSSARY.md`. Key points:

- **Media type**: add a 4th `media_type` = `audio` (capability = `text_to_speech`), on a par with image/video/text.
- **Scheduling**: goes through GenerationQueue/Worker (audio lane), like image/video; **the backend is synchronous** (mimics `text_backends`, returns in seconds, no submit-poll-resume). `enqueue_tts(segment_ids?)`.
- **Backend**: add `lib/backends/audio_backends/`; DashScope synchronous native adapter + custom OpenAI-compatible audio path.
- **Voice**: configurable string id (global default + `project.json` `settings.narration_voice` override, no built-in catalog, optional speech rate).
- **Versioning**: yes (resource_paths/VersionManager, directory `audio/`, file `segment_{id}.mp3`).
- **Data model**: `GeneratedAssets.narration_audio: str | None` (project.json, no DB table); text source = `NarrationSegment.novel_text` as is.
- **Billing**: `per_character` pricing kind (DashScope preset); custom providers use the unit price the user enters in the DB.

## 6. Must verify before implementation (UNVERIFIED list)

External provider data — **don't guess, don't hard-code**; check the primary docs item by item before coding:

1. DashScope TTS **exact model ids** (qwen-tts / qwen3-tts-flash / cosyvoice-v2?) and which one is **purely synchronous HTTP** (CosyVoice realtime is WebSocket, which doesn't fit a synchronous backend → leaning towards Qwen-TTS).
2. DashScope TTS **synchronous REST endpoint + request/response schema**, **per-character unit price + currency**, **available voice ids**, output format/sample rate.
3. OpenAI-compatible `/v1/audio/speech` request schema (model/voice/input/response_format/speed), for the custom audio `EndpointSpec`.
4. Whether the custom provider `price_unit` supports a per-character unit (audio).
5. Whether Fish Audio forces a watermark on cloud OpenAudio S1 (UNVERIFIED).
6. Self-hosted model VRAM/RTF/whether there is an official OpenAI-compatible wrapper (CosyVoice2 / VoxCPM / IndexTTS2).

## 7. References (primary official sources, fetched 2026-06-02)

- OpenAI TTS guide: https://developers.openai.com/api/docs/guides/text-to-speech
- OpenAI tts-1 model/pricing: https://developers.openai.com/api/docs/models/tts-1
- ElevenLabs pricing: https://elevenlabs.io/pricing/api ; streaming: https://elevenlabs.io/docs/api-reference/text-to-speech/stream ; character-level timestamps: https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps
- Volcengine Doubao large-model speech synthesis: https://www.volcengine.com/docs/6561/1257543 ; async long text: https://www.volcengine.com/docs/6561/1829010 ; Voice Cloning 2.0: https://www.volcengine.com/docs/6561/1305191
- Alibaba DashScope OpenAI compatibility: https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope ; Qwen-TTS: https://www.alibabacloud.com/help/en/model-studio/qwen-tts ; pricing: https://help.aliyun.com/zh/model-studio/model-pricing
- MiniMax T2A async: https://platform.minimax.io/docs/guides/speech-t2a-async
- Fish Audio pricing/rate limits: https://docs.fish.audio/developer-guide/models-pricing/pricing-and-rate-limits
- Gemini speech generation: https://ai.google.dev/gemini-api/docs/speech-generation
- Google Cloud TTS pricing: https://cloud.google.com/text-to-speech/pricing
- Azure Speech pricing: https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/
- Tencent Cloud TTS: https://cloud.tencent.com/document/product/1073/37995 ; iFlytek TTS: https://www.xfyun.cn/doc/tts/online_tts/API.html
- CosyVoice LICENSE: https://github.com/FunAudioLLM/CosyVoice/blob/main/LICENSE
- VoxCPM paper: https://arxiv.org/html/2509.24650v1 ; weights: https://huggingface.co/openbmb/VoxCPM-0.5B
- IndexTTS: https://github.com/index-tts/index-tts ; license: https://huggingface.co/spaces/IndexTeam/IndexTTS-2-Demo/blob/main/INDEX_MODEL_LICENSE_EN.txt ; license discussion: https://github.com/index-tts/index-tts/issues/228
- F5-TTS commercial-use restriction: https://github.com/SWivid/F5-TTS/discussions/997
- Duration alignment: WhisperX https://github.com/m-bain/whisperX ; duration modelling for automatic dubbing https://arxiv.org/html/2211.16934v2 ; from speech translation to automatic dubbing https://aclanthology.org/2020.iwslt-1.31/ ; ffmpeg atempo https://ayosec.github.io/ffmpeg-filters-docs/7.1/Filters/Audio/atempo.html
- Pattern for wrapping self-hosted models in an OpenAI-compatible shim: https://github.com/matatonic/openedai-speech
