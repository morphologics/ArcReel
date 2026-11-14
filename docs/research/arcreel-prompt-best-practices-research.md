# Research Report: Official Best Practices for Image/Video Generation Prompts

> Purpose: drive iteration of the script-generation writing guidance in ArcReel (novel → short video, i2v image-to-video pipeline), i.e. the guidance the LLM follows to produce `image_prompt` / `video_prompt`.
> Corresponding issue: https://github.com/ArcReel/ArcReel/issues/1092 (the design-alignment resolutions are in that issue's comments).
> Scope: the 8 providers ArcReel actually integrates; only **official** documentation sites / official blogs / model cards / official Help Centers are accepted. Where no official guide could be found, this is recorded truthfully as "no official guide found", and not substituted with third-party tutorials or model memory.
> Research date: 2026-07-13
> Hard-constraint compliance: every conclusion in this report carries an official source URL; anything the vendor does not state explicitly is marked "no official guide found / no explicit official statement". A small amount of third-party mirrored content appears only as side notes, clearly marked "unverified", and does not enter the conclusions.

---

## 1. Cross-provider common conclusions (organised by the 4 research questions)

### Q1 General advice for writing image prompts

**Consensus 1: layering the elements is a universal paradigm, and the layers are highly convergent across vendors.** Almost every vendor with an official guide breaks an image prompt into the hierarchy "subject → environment/scene → lighting → camera language (shot size / angle / camera position / lens) → style/atmosphere".
- Google: template `shot size + subject + environment + lighting + camera position + lens` (https://ai.google.dev/gemini-api/docs/image-generation ).
- Alibaba Wanxiang: advanced formula = `subject + scene + style + camera language + atmosphere words + detail modifiers`, plus a five-dimension vocabulary for "shot size / angle / shot type / style / lighting" (https://help.aliyun.com/zh/model-studio/text-to-image-prompt ).
- Volcengine Seedream: `subject + action + environment`, with style/colour/light and shadow/composition as optional additions (https://www.volcengine.com/docs/82379/1829186?lang=zh ).
- MiniMax image-01: the official example layers as `subject + clothing → shot size/angle → environment → photographic style/era → medium/texture → degree of realism` (https://platform.minimax.io/docs/api-reference/image-generation-t2i ).
- OpenAI GPT-image: recommends a fixed order `scene/background → subject → key details → constraints`, and stating the intended use (https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide ).
- Kling: images go through MVL (natural language + image), guided by "scene-director-style writing"; the face-reference formula includes `facial features + hairstyle + clothing + action + background + lighting + atmosphere` (https://kling.ai/quickstart/klingai-image-3-model-user-guide ).

**Consensus 2: vendors consistently advocate "concrete and visual", and oppose vagueness and meaningless piling-up of quality words.** Several use almost the same do/don't pattern: replace abstract words with visible visual details.
- Google: `Be specific: More details give you more control`; "fantasy armor" → "ornate elven plate armor, etched with silver leaf patterns…" (https://cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-image-generation-best-practices ).
- Alibaba: `提示词描述越完整、精确和丰富，生成的图像品质越高` ("the more complete, precise and rich the prompt description, the higher the quality of the generated image") (https://help.aliyun.com/zh/model-studio/text-to-image-prompt ).
- Kling: replace "magic" with "swirling blue energy particles with an ethereal glow" (https://kling.ai/blog/kling-ai-prompt-guide ).
- Vidu (for video reference images): `cinematic dramatic beautiful high quality` is officially called "model noise" and should be replaced with actionable visual constraints (https://www.vidu.com/blog/image-prompt-generator-ai-video ).

**Consensus 3: narrative natural-language sentences > stacked keywords/tags.**
- Volcengine Seedream has the most explicit do/don't example: recommended "一个穿着华丽服装的女孩，撑着遮阳伞走在林荫道上，莫奈油画风格" ("a girl in gorgeous clothing, holding a parasol, walking along a tree-lined avenue, in the style of a Monet oil painting"); avoid "一个女孩，撑伞，林荫街道，油画般的细腻笔触" ("a girl, umbrella, tree-lined street, delicate oil-painting-like brushstrokes") (https://www.volcengine.com/docs/82379/1829186?lang=zh ).
- The official example prompts from Google, xAI and MiniMax are all complete narrative sentences (see the URLs cited above and https://docs.x.ai/developers/model-capabilities/images/generation ).
- Exception / more neutral: OpenAI says explicitly that "minimal, narrative paragraphs, JSON, instruction-style and tag-style all work, as long as intent and constraints are clear", without mandating any one style (https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide ).

**Consensus 4: almost no vendor gives a quantitative "word count / length" recommendation.** The level of detail is qualitative (the more specific, the more controllable), not numeric.
- No quantitative length advice: Google, Volcengine, MiniMax, Kling, OpenAI (all "no numeric length recommendation found"; see the per-vendor excerpts).
- The only official signal "in favour of long prompts": OpenAI states explicitly that long prompts work (`Long prompts can work well`) and notes that GPT-image-2 is good at handling long structured descriptions; Alibaba's official qwen-image-2.0-pro examples follow a "very long structured description of several hundred characters" approach (https://help.aliyun.com/zh/model-studio/text-to-image ).
- The only concrete word-count range seen comes from Vidu's "Q3 official prompt guide (50–150 characters)" — but it **appears only on CSDN/gitcode mirrors; the original official URL could not be located, so no conclusion is drawn from it** (see divergence point 5).

> **Implications for ArcReel (stating common facts only, no rewrite)**: officials broadly support "layered elements + concrete visuals + narrative sentences", and broadly oppose piling up vague quality words. "Too terse" (e.g. a scene missing the lighting/atmosphere layer) runs counter to the mainstream official position; filling in the subject/environment/lighting/camera language/atmosphere layers has cross-vendor official backing. On length, vendors set almost no hard metrics, leaning towards "be specific where it matters".

---

### Q2 General advice for i2v image-to-video prompts (first frame provided)

**Consensus 1 (strongest, most consistent): the first frame already provides subject/scene/style, so the prompt should focus on motion/change and camera movement, avoiding a restatement of the static image; add environmental dynamics or style transitions where needed.** This is the most consistent point across vendors in this research, and it directly hits ArcReel's i2v path.
- Google: `Prompt for motion only. Your source image already provides the subject, scene, and style. Focus your prompt on the motion…`; and explicitly "don't re-describe the person/background/lighting — redundancy confuses the model"; refer to people generically as "the subject/the woman/he/she" (https://cloud.google.com/vertex-ai/generative-ai/docs/video/best-practice ).
- Alibaba Wanxiang: `图像已经确定了主体、场景与风格，因此提示词主要描述动态过程及运镜需求` ("the image has already fixed the subject, scene and style, so the prompt mainly describes the dynamic process and camera movement requirements"); image-to-video formula = `motion + camera movement` (https://help.aliyun.com/zh/model-studio/text-to-video-prompt ).
- MiniMax: basic I2V formula = `first-frame subject + motion/change`; `prompt describes how the scene evolves from this static image into motion` (https://platform.minimax.io/docs/guides/video-prompt , https://platform.minimax.io/docs/guides/video-generation ).
- Vidu: i2v formula = `subject motion + camera movement + scene change + style`; the core is "describe the motion, don't restate the image" (https://www.vidu.com/ai-image-to-video ).
- OpenAI Sora: the image is the first-frame anchor, `The model uses the image as an anchor for the first frame, while your text prompt defines what happens next` (https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide ).
- Kling: the official i2v example prompt is "镜头拉远，女生微笑" ("camera pulls back, the girl smiles") (camera movement + subject action); the scene is given by the image (https://www.klingai.com/document-api/api/video/3-0-omni/image-to-video ).
- Volcengine Seedance: the dedicated i2v article gives "character-action prompt = subject + detailed description of the subject's action change from first frame to last frame + camera movement" (https://www.volcengine.com/docs/82379/1951250?lang=zh ).
- xAI (weak signal): one sentence in the official blog, `Give it a starting image, describe the motion…`; the examples focus on motion and camera movement and don't restate the image (https://x.ai/news/grok-imagine-video-1-5 ).

**Consensus 2: camera movement should be explicit and "preferably one at a time"; several vendors provide official camera-movement vocabularies/commands.**
- MiniMax: the i2v API has 15 bracketed `[command]` camera-movement commands ([Push in]/[Pull out]/[Pan left]…/[Static shot], etc.), applicable to Hailuo 2.3 / 2.3-Fast; at most 3 within the same [] is recommended (https://platform.minimax.io/docs/api-reference/video-generation-i2v ).
- Kling: a natural-language camera-movement table (push-in/pan/tilt/tracking…) + the structured `camera_control` six-axis protocol (horizontal/vertical/pan/tilt/roll/zoom, [-10,10]) (https://kling.ai/blog/kling-ai-prompt-guide , https://www.klingai.com/document-api/api/video/3-0-omni/image-to-video ).
- Alibaba: control with "镜头推进/镜头左移" ("camera pushes in / camera moves left"); when no change is wanted, write "固定镜头" ("fixed shot"); wan2.7 has also dropped the `shot_type` parameter in favour of natural-language control (https://help.aliyun.com/zh/model-studio/text-to-video-prompt ).
- Volcengine Seedance 2.0: `一个镜头里尽量只指定 1 种运镜方式，不要同时要求推拉摇移` ("specify only one camera movement per shot where possible; don't ask for push, pull, pan and truck at the same time") (https://www.volcengine.com/docs/82379/2222480?lang=zh ).
- Vidu: adding camera movement feels more cinematic, but "avoid stacking several simultaneous motion commands in one sentence"; use directional language + intensity qualifiers (subtle/gentle/minimal) (https://www.vidu.com/blog/image-to-video-ai ).

**Consensus 3: match duration and amount of action — vendors broadly advocate "a short shot focuses on a single, completable action"; "slow, gentle, continuous action" is a specific recommendation from some providers (e.g. Volcengine Seedance), not a universal position; duration is set by API parameters and cannot be changed by prompt text.**
- Volcengine Seedance 2.0 (most explicit): `优先选用缓慢、轻柔、连贯的细微动作，尽量规避狂奔、大跳、剧烈翻滚等高爆发、大动态动作` ("prefer slow, gentle, continuous subtle actions; avoid high-burst, highly dynamic actions such as sprinting, big jumps and violent tumbling"); also "the model's support for precise timing (e.g. 0–3 seconds) is unstable", so it recommends describing events in order with "shot 1/2/3" storyboards rather than hard second-level timing (https://www.volcengine.com/docs/82379/2222480?lang=zh ).
- Google: `For short videos, dedicate each prompt to a single, focused moment`; don't chain A→B→C in one prompt (https://cloud.google.com/vertex-ai/generative-ai/docs/video/best-practice ).
- OpenAI: actions should be "simple, single, completable", with beats given in seconds ("takes four steps… in the final second"); duration/resolution are "container" parameters and `will not change based on prose like 'make it longer'`; a 4-second shot fits about 1–2 lines of dialogue (https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide ).
- Vidu: i2v is most stable at 4–6 seconds; pushing to 8 seconds+ with a complex source image tends to drift; use minimal-motion prompts for face-heavy images (https://www.vidu.com/blog/image-to-video-ai ).
- MiniMax: the official tutorial recommends keeping a single camera movement within a reasonable 5–6 seconds (https://platform.minimax.io/docs/guides/video-prompt ).

**Consensus 4: t2v and i2v guidance is clearly separated on most official sites.** Google, Alibaba, MiniMax, Volcengine, Vidu and Kling all have official content that maps to i2v (see the notes in the per-vendor excerpts); only xAI has no systematic guide (just one sentence in a blog).

> **Implications for ArcReel (stating common facts only)**: "video_prompt describes the action/camera movement within the shot's duration" is consistent with the mainstream official position of "a short shot focuses on a single, completable action + explicit camera movement"; "slow and continuous" is only a specific recommendation from some providers (e.g. Volcengine Seedance), and "ambient sound" applies only where a provider explicitly supports it — neither should be treated as the mainstream position of all vendors. The problem of "a 9-second shot whose action is only 28 characters and lacks an environmental-dynamics layer" can be measured against the official "motion + camera movement + (environment/atmosphere dynamics)" layering — vendors oppose both restating the static image and stacking several simultaneous violent actions.

---

### Q3 Official positions on negative phrasing

This is the question with the greatest divergence between vendors; the official approaches fall into three types:

**Type A: no separate negative channel; vendors state explicitly "write only what you want, replace negation with positive description".**
- Google image (Gemini/Nano Banana, exactly what ArcReel uses for first frames): the verbatim advice is `Describe what you want, not what you don't`; the example rewrites "no cars" as "an empty, deserted street with no signs of traffic" (https://cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-image-generation-best-practices ).
- Vidu: none of the generation APIs has a `negative_prompt` field; the official approach is "positive, specific, restrained + explicitly declare what stays/is still" (e.g. "background remains static") (https://platform.vidu.com/docs/image-to-video , https://www.vidu.com/blog/image-prompt-generator-ai-video ).
- MiniMax: none of the image/i2v/s2v schemas checked has a negative parameter; the tutorials consistently layer positive descriptions (https://platform.minimax.io/docs/api-reference/video-generation-i2v ).
- OpenAI Sora: no negative parameter; core position: "details you don't describe, Sora will invent on its own", i.e. rely on positive description (https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide , https://developers.openai.com/api/reference/resources/videos/methods/create ).

**Type B: a separate negative channel is provided, but vendors require "descriptive wording inside the channel too, avoid no/don't", and lean towards writing negations as positives.**
- Google video (Veo): has a separate `negativePrompt` parameter, but the official guidance is to fill in the entity nouns to exclude ("wall, frame") rather than "no walls", and states explicitly `Not recommended: using instructive language or words such as "no" or "don't"` (https://cloud.google.com/vertex-ai/generative-ai/docs/video/video-gen-prompt-guide ).
- Kling: has a `negative_prompt` field (≤2500 characters), but the official guidance also recommends "supplementing negative information through negative sentences in the positive prompt" (https://www.klingai.com/document-api/api/video/3-0-omni/image-to-video ).
- Alibaba: **split across models** — the qwen-image series and wan2.7 video **support** `negative_prompt` (≤500 characters, officially defined as "only for assisting in optimising generation quality"); but wan2.7-image-pro/image **do not support it**, and the official guidance is to "describe it in the positive prompt (不要出现 xxx — 'xxx must not appear')" (https://help.aliyun.com/zh/model-studio/text-to-image , https://help.aliyun.com/zh/model-studio/text-to-video-api-reference ).

**Type C: vendors encourage writing "prohibit/exclude" constraint sentences directly in the body (negation as a first-class tool).**
- Volcengine Seedance 2.0: has a dedicated "constraint words" subsection, with examples including "保持无字幕/避免生成任何文字或字幕/不要生成Logo/不要生成水印" ("keep without subtitles / avoid generating any text or subtitles / do not generate a logo / do not generate a watermark"), and even "视频全程禁止出现…同款分身、双胞胎效果" ("throughout the video, … identical doubles or twin effects are prohibited"); at the same time it admits this "cannot be avoided 100%, only made less likely" (https://www.volcengine.com/docs/82379/2222480?lang=zh ).
- OpenAI image (GPT-image): officially **encourages** writing exclusions/invariants explicitly, `State exclusions and invariants explicitly (e.g., 'no watermark,' 'no extra text,' 'no logos')`, and for edits uses "change only X + keep everything else the same" (https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide ).

**Common implicit baseline: negation (through whichever channel) is not a cure-all, and several vendors state explicitly that "constraints are not 100% controllable" (Volcengine says so outright; Vidu/Google image avoid it by "rewriting as positive", Google video uses a descriptive negativePrompt).** xAI has no official position on negation at all (no negative channel in the API, and no related guidance text).

> **Direct relevance to ArcReel's three frozen decisions (providing factual basis only, no rewrite)**:
> - Prohibition sentences of the "don't mix in past/future events" kind — the official evidence is polarised: on the image side (Google/Vidu/MiniMax/Sora) the mainstream is "rewrite as positive description, use fewer negation words"; but Volcengine and OpenAI image say explicitly that "exclusions may be written into the body". No vendor treats "narrative chronology / memory flashbacks" as something a prompt can control; this kind of problem is fundamentally "a single static frame cannot render chronology", and has nothing to do with the negative channel.
> - Blacklist of counter-example word families — no vendor uses a "word-family enumeration blacklist" mechanism; the mainstream is "describe the image you want" (positive substitution), or "write the entity nouns to exclude" (Veo-style). Word-family enumeration blocking has no precedent in official guides.
> - Whether to keep negative sentences — depends on the target model: if ArcReel's first frame uses Gemini/Seedream-type models, vendors lean towards positive rewriting; if it uses a model that supports negative_prompt (Kling/qwen-image/wan2.7 video), the separate channel can be used, but "descriptive" wording is still preferable.

---

### Q4 Prompt language (Chinese vs. English)

**Consensus: almost no vendor officially states "one language works better".** Vendors broadly "support Chinese and English / multiple languages", but are basically silent on "which one to write prompts in".
- Declare "supports Chinese and English / multiple languages" without taking a position on which is better: Alibaba (image/video prompts "支持中英文" — "support Chinese and English", with side-by-side Chinese and English versions; HappyHorse "支持任何语言输入" — "supports input in any language") (https://help.aliyun.com/zh/model-studio/text-to-video-api-reference , https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference ); Volcengine Seedance "支持中英文" ("supports Chinese and English") (https://www.volcengine.com/docs/82379/2168087?lang=zh ); MiniMax has Chinese and English sites, no language restriction on prompts (no position taken); Kling uses Chinese and English examples side by side (no position taken); Vidu has Chinese and English sites, prompts in both are first-class (no position taken).
- The only one with an explicit "language mechanism" statement is Google's old Imagen line: `Imagen models support only English natively`; non-English is translated to English, and zh/zh-CN/zh-TW etc. can be set explicitly; **but this is a deprecated old line whose migration deadline has passed** (Google officially marks it as deprecated and recommended migrating to gemini-2.5-flash-image before 2026-06-30); neither the new Gemini 3 image line nor Veo has an explicit statement on Chinese vs. English (https://cloud.google.com/vertex-ai/generative-ai/docs/image/set-text-prompt-language ).
- OpenAI's general position (Help Center, not specific to GPT-image/Sora): `optimized for English, but trained on multilingual data`; the target language can be used, but no conclusion is given on Chinese vs. English (https://help.openai.com/en/articles/6742369 ).
- Easily confused with "prompt language" but to be kept separate is "output language / dialogue language": Kling dialogue supports 5 languages, Chinese/English/Japanese/Korean/Spanish (https://kling.ai/blog/kling-ai-prompt-guide ); Vidu Q3 audio output supports English/Japanese/Chinese (https://www.vidu.com/vidu-q3 ); Volcengine Seedance 2.0 requires "a consistent dialogue language, avoid mixing Chinese and English" (https://www.volcengine.com/docs/82379/2222480?lang=zh ). These are all **the language of speech inside the video**, not advice on the language to write prompts in.

> **Implications for ArcReel**: at the official level there is no citable verdict on "Chinese vs. English, which is better". Official docs for Chinese models (Volcengine/Alibaba/Kling/MiniMax/Vidu) give equal weight to Chinese with plenty of Chinese examples; overseas models (Google/OpenAI/xAI) mostly use English examples but don't mandate it. ArcReel's choice of prompt language lacks a hard official basis and is something it can decide for itself.

---

## 2. Per-vendor excerpts (official sources + key conclusions)

### 1. Google (Gemini 3 Pro/3.1 Flash Image; Veo 3.1)

Official sources:
- Image prompting: https://ai.google.dev/gemini-api/docs/image-generation
- Image best practices: https://cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-image-generation-best-practices
- Language setting (old Imagen line): https://cloud.google.com/vertex-ai/generative-ai/docs/image/set-text-prompt-language
- Veo prompt guide: https://cloud.google.com/vertex-ai/generative-ai/docs/video/video-gen-prompt-guide
- Veo best practices (with a separate i2v chapter): https://cloud.google.com/vertex-ai/generative-ai/docs/video/best-practice
- Veo 3.1 blog: https://cloud.google.com/blog/products/ai-machine-learning/ultimate-prompting-guide-for-veo-3-1

Key points: image = layered elements + concreteness + narrative sentences, "more details = more control", no numeric length advice. There is an official separate i2v chapter, stating explicitly "describe only the motion, don't restate the image, refer to people generically"; camera movement is split into three categories: Camera/Subject/Environmental. Negation: **images have no negative channel, write positively only**; **video has `negativePrompt`, but write the entity nouns to exclude and avoid no/don't**. Language: only the old Imagen line has an explicit "natively English only + translation" statement; the new lines make no claim about which is better.
Not found: quantitative word-count/length advice for images; explicit statements on Chinese vs. English for the new Gemini 3 image line and Veo; a quantitative duration-to-amount-of-action mapping.

### 2. Volcengine Ark / Doubao (Seedream 4.0/4.5/5.0; Seedance 1.5 Pro/2.0)

Official sources:
- Seedream image prompt guide: https://www.volcengine.com/docs/82379/1829186?lang=zh
- Seedance 1.5 Pro prompt guide: https://www.volcengine.com/docs/82379/2168087?lang=zh
- Seedance 2.0 prompt guide: https://www.volcengine.com/docs/82379/2222480?lang=zh
- Seedance 2.0 tutorial (i2v first frame / first-and-last frame / role configuration): https://www.volcengine.com/docs/82379/2291680?lang=zh
- Best practice for using Seedream to help Seedance generate video (dedicated i2v article): https://www.volcengine.com/docs/82379/1951250?lang=zh

Key points: image = `subject + action + environment` + optional aesthetics; narrative sentences beat keywords (with do/don't examples); newer models need less description, "concise and precise beats ornate stacking". The dedicated i2v article's formula = `subject + action change from first frame to last frame + camera movement`, and camera movement "only needs a brief description"; 2.0 stresses "prefer slow, continuous, small actions; avoid high-burst, highly dynamic ones", "only 1 camera movement per shot", and using "shot 1/2/3" instead of second-level timecodes. Negation = **write "constraint words" in the body** (keep without subtitles / no logo / prohibit twins), while admitting this isn't 100% controllable. Language: supports Chinese and English, no position on which is better; dialogue must use a consistent language.
Not found: explicit statement on image language preference; a separately named "Seedance 2.0 Pro" guide (the official names are 2.0/2.0 Fast/2.0 Mini).
Note: the Volcengine docs site is JS-rendered; the content was read after rendering in a browser.

### 3. xAI (Grok Imagine Image/Video)

Official sources (all checked; confirmed there is no prompt guide):
- Imagine overview: https://docs.x.ai/developers/model-capabilities/imagine
- Image generation: https://docs.x.ai/developers/model-capabilities/images/generation
- Image-to-video i2v: https://docs.x.ai/developers/model-capabilities/video/image-to-video
- Video generation: https://docs.x.ai/developers/model-capabilities/video/generation
- Blog Video 1.5: https://x.ai/news/grok-imagine-video-1-5

**Conclusion: xAI has not published any dedicated prompt-writing guide**; docs.x.ai is entirely API reference. The only usable official signals: one i2v sentence, `Give it a starting image, describe the motion…`, + official English narrative example sentences (oriented on motion/camera movement/atmosphere); hard parameter constraints (duration 1–15s, aspect ratio, resolution). **Negation: no negative channel in the API, and no related official guidance text at all. Language: zero official advice (all examples are in English).** Structure formulas / negative usage circulating online all come from third-party mirrors and were not accepted.

### 4. OpenAI (GPT-image-2; Sora 2 / 2 Pro)

Official sources:
- GPT Image prompting guide: https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide
- Image API guide: https://developers.openai.com/api/docs/guides/image-generation
- Sora 2 prompting guide: https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide
- Video generation guide: https://developers.openai.com/api/docs/guides/video-generation
- create video API: https://developers.openai.com/api/reference/resources/videos/methods/create
- Multilingual use (Help Center): https://help.openai.com/en/articles/6742369

Key points: image = fixed order `scene → subject → details → constraints` + state the intended use; minimal/narrative/JSON/tag-style all work; "long prompts work", but in production prefer clear templates + small iterative steps. i2v = the image is the first-frame anchor, and the prompt defines "what happens next" (image passed via `input_reference`); actions should be simple + given second-level beats; **short prompt = more creative freedom, long prompt = stronger control**; duration is a "container" parameter that prose cannot change, maximum 20s. Negation: **for images, vendors encourage explicitly writing exclusions such as no watermark/no logos**; Sora relies on positive description, no negative parameter. Language: only the general Help Center "optimised for English + multilingual"; no Chinese-vs-English conclusion specific to GPT-image/Sora.

### 5. Shengshu Technology Vidu (Q1/Q2 image; Q3 turbo/pro, 2.0 video)

Official sources:
- Image-to-video API: https://platform.vidu.com/docs/image-to-video
- Reference-to-image API: https://platform.vidu.com/docs/reference-to-image
- Official i2v motion prompt blog: https://www.vidu.com/ai-image-to-video
- Image prompt workflow blog: https://www.vidu.com/blog/image-prompt-generator-ai-video
- i2v practice blog: https://www.vidu.com/blog/image-to-video-ai
- Q3 model page: https://www.vidu.com/vidu-q3

Key points: Vidu has **no centralised official Prompt Guide**; guidance is scattered across API model cards + official blogs. Image (as a video reference image) = make face/clothing/lighting direction concrete; opposes piling up meaningless quality words ("model noise"). i2v formula = `subject motion + camera movement + scene change + style`; describe motion rather than the image, keep the prompt simple and focused, avoid multiple simultaneous actions; 4–6s is most stable, use minimal motion for face-heavy images. Negation = **no negative channel**; go positive + explicitly declare what stays still. Language: Chinese and English sites, both first-class, no claim about which is better (Q3 "multilingual" refers to audio output). Parameter note: `movement_amplitude` has no effect on Q2/Q3, so motion amplitude has to be controlled via prompt text.
Unverified side note: the "Vidu Q3 official prompt guide (structure formula + 50–150 characters + sound effects at sentence end)" circulating in Chinese appears only on CSDN/gitcode mirrors; the original official URL could not be located, so no conclusion is drawn from it.

### 6. Alibaba DashScope / Bailian (Qwen-image 2.0; Wanxiang wan2.7; HappyHorse 1.0)

Official sources:
- Text-to-image prompt guide: https://help.aliyun.com/zh/model-studio/text-to-image-prompt
- Text/image-to-video prompt guide (with i2v formula): https://help.aliyun.com/zh/model-studio/text-to-video-prompt
- qwen-image/Wanxiang text-to-image usage: https://help.aliyun.com/zh/model-studio/text-to-image
- Wanxiang 2.7 image API: https://help.aliyun.com/zh/model-studio/wan-image-generation-and-editing-api-reference
- Wanxiang 2.7 text-to-video API: https://help.aliyun.com/zh/model-studio/text-to-video-api-reference
- Wanxiang 2.7 image-to-video API (i2v): https://help.aliyun.com/zh/model-studio/image-to-video-general-api-reference
- HappyHorse i2v API: https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference

Key points: image basic `subject + scene + style` / advanced `+ camera language + atmosphere + details` + five-dimension vocabulary; "the more complete, precise and rich, the higher the quality"; qwen-image-2.0-pro uses very long structured descriptions. i2v formula = `motion + camera movement` ("the image has already fixed the subject/scene/style"); when nothing should change, write "固定镜头" ("fixed shot"); wan2.7 dropped shot_type in favour of natural language; `duration` 2–15s, aspect ratio follows the first frame. Negation = **per model**: qwen-image/wan2.7 video support `negative_prompt` (for assistive optimisation); wan2.7-image-pro/image don't, and require writing "不要出现 xxx" ("xxx must not appear") in the body; HappyHorse has no negative. Language: image/video prompts "支持中英文" ("support Chinese and English"), side-by-side Chinese and English versions are given, HappyHorse "支持任何语言" ("supports any language"), but **no official wording of "optimised for / better in Chinese" was found**.

### 7. MiniMax (image-01; Hailuo 2.3/Fast; S2V-01)

Official sources:
- Image generation guide: https://platform.minimax.io/docs/guides/image-generation
- Image t2i API: https://platform.minimax.io/docs/api-reference/image-generation-t2i
- Video generation guide: https://platform.minimax.io/docs/guides/video-generation
- Image-to-video i2v API (15 camera-movement commands): https://platform.minimax.io/docs/api-reference/video-generation-i2v
- S2V API: https://platform.minimax.io/docs/api-reference/video-generation-s2v
- Official Hailuo prompt tutorial: https://platform.minimax.io/docs/guides/video-prompt

Key points: image = layer descriptive elements (subject + shot size + environment + style + medium + degree of realism); image-01 limit 1500 characters, `prompt_optimizer` defaults to false for images. i2v basic `first-frame subject + motion/change`, precise `+ camera movement + aesthetic atmosphere`; **15 official bracketed `[command]` camera-movement commands** apply to Hailuo 2.3, ≤3 within the same []; keep a single camera movement within 5–6s; i2v limit 2000 characters, `prompt_optimizer` defaults to true for video. Negation = **no negative channel, layer positive descriptions** (the only "prohibition" is the 1026 block for sensitive content). Language: Chinese and English sites, no claim about which is better.

### 8. Kuaishou Kling (Image O1, v3 Omni Image; Video v2.5T/v2.6/v3/v3 Omni/O1)

Official sources:
- Official video prompt guide: https://kling.ai/blog/kling-ai-prompt-guide
- Image 3.0 user guide (Prompt Handbook): https://kling.ai/quickstart/klingai-image-3-model-user-guide
- Image 3.0 Omni guide: https://kling.ai/quickstart/klingai-image-3-omni-user-guide
- Image-to-video API (3.0 & Omni): https://www.klingai.com/document-api/api/video/3-0-omni/image-to-video
- quickstart overview: https://kling.ai/quickstart
- Adjective list: https://kling.ai/blog/best-kling-ai-adjectives-video-prompts

Key points: the official position is "clear scene-director-style writing, not a secret formula". Images go through MVL (natural language + image), with per-scenario formulas (editing: "keep everything else unchanged, change X to Y"; face reference covers face/hairstyle/clothing/action/background/lighting/atmosphere); the subject should be concrete and visual. Video six elements `subject + action + scene + camera + light/shadow and atmosphere (+ dialogue)`; camera movement on two tracks (natural-language table + `camera_control` six-axis protocol); pacing/duration set per storyboard shot with `multi_prompt` (sum of shots = total duration, ≤6 shots). i2v: first frame `image` (last frame optional) + prompt adds action/camera movement; official example "镜头拉远，女生微笑" ("camera pulls back, the girl smiles"). Negation = **has `negative_prompt` (≤2500), but vendors recommend supplementing with negative sentences in the positive prompt**. Language: Chinese and English examples side by side, no claim about which is better; dialogue supports 5 languages, Chinese/English/Japanese/Korean/Spanish (this is output language).
Note: no model-by-model writing conventions; the literal i2v formula "subject + motion + background" circulating online was not found on official pages and was not accepted.

---

## 3. Divergences and exceptions (listed separately)

1. **Negation handling is split three ways (biggest divergence)**:
   - Positive only, no negative channel: Google image, Vidu, MiniMax, OpenAI Sora.
   - Separate negative channel but descriptive wording required: Google Veo (`negativePrompt`), Kling (`negative_prompt`), Alibaba (supported by qwen-image/wan2.7 video, not by wan2.7-image).
   - Encourage writing exclusion sentences in the body: Volcengine Seedance ("constraint words"), OpenAI image (`no watermark/no logos`).
   - No position at all: xAI.
   → **It can be split even within one vendor**: Google (none for images / present for video), Alibaba (present / absent across models). ArcReel's negation strategy can't be "one size fits all"; it must distinguish by target model.

2. **Directional difference in the level-of-detail stance**: most vendors say "more detail, more control" (Google/Alibaba/Volcengine/MiniMax); but **OpenAI Sora states explicitly "short prompt = more creative freedom, long prompt = stronger control but suppresses creativity"**, the only official position that frames level of detail as a two-way trade-off rather than a monotonic "more detail is better" (https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide ).

3. **Narrative sentences vs. keywords — different strength**: Volcengine gives the strongest do/don't example (keyword stacking is the counter-example); OpenAI is the most neutral (tag-style/JSON-style also accepted).

4. **Huge variation in the completeness of i2v guidance**: Alibaba, Volcengine, MiniMax, Google, Kling and Vidu have usable official i2v content; **xAI has only one sentence in a blog, no systematic guide**; OpenAI's i2v-specific section is also just one paragraph (image input as first-frame anchor), with the rest of action/camera movement/duration shared between t2v and i2v and not separated.

5. **Questionable source for Vidu's "Q3 official prompt guide" (exception, needs verification)**: the Chinese-language "structure formula + quick-reference template (50–150 characters, sound effects at sentence end, staged i2v structure)" attributed to "Vidu API Open Platform" is visible only on CSDN/gitcode mirrors; **the original official URL on platform.vidu.cn/.com or vidu.com/blog could not be located**. This report does not include it in the conclusions; if ArcReel wants to cite specific points such as "50–150 characters", verify the original page with Vidu first.

6. **Language preference is a universal blank (the exception is that there is no exception)**: none of the 8 vendors states "Chinese / English, which is better". The only one with an explicit language-mechanism statement, Google Imagen, is a deprecated old line. This doesn't match the popular impression that Chinese models are "optimised for Chinese" — **there is no such statement at the official documentation level**; it is an independent decision for ArcReel.

7. **Parameter traps (official exceptions that affect prompt strategy)**:
   - Vidu: `movement_amplitude` has no effect on Q2/Q3 → motion amplitude can only be controlled through prompt text (https://platform.vidu.com/docs/image-to-video ).
   - Alibaba wan2.7: `shot_type` removed → camera control moved to natural language (https://help.aliyun.com/zh/model-studio/text-to-video-prompt ).
   - MiniMax: `prompt_optimizer` defaults to false for images and true for video → the default behaviour for automatic prompt rewriting is opposite (https://platform.minimax.io/docs/api-reference/video-generation-i2v ).
   - Alibaba: `prompt_extend` defaults to true, but wan2.7-image doesn't support it and uses `thinking_mode` instead (https://help.aliyun.com/zh/model-studio/text-to-image ).

---

## 4. Method and constraints

- This report accepts only official documentation sites / official blogs / model cards / official Help Centers; third-party tutorials, community experience and mirrored reposts are all excluded (the one Vidu mirror mentioned is clearly marked "unverified, no conclusion drawn").
- Volcengine, the MiniMax Chinese site and some Alibaba/Vidu docs are JS-rendered front ends; `web_fetch` only got the navigation shell, so all content was read after browser rendering; for MiniMax, the more complete English official site was used as the reference (consistent with the Chinese site's descriptions).
- Any question the vendors don't address explicitly is marked "no official guide found / no explicit official statement"; vendor positions were not filled in from model memory (hard constraint).
- This report does not include a rewrite plan for ArcReel's guidance — it delivers only research facts and common conclusions; the rewrite will be drawn up separately by the main-repo session after alignment.
