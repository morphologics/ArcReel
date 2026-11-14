# Asset Image Prompt Research: Multi-panel Composition, Side Effects of i2i Reference Images, Anti-artifact Sentences and Negative Tail Sentences

> Purpose: provide first-hand evidence for [#2058](https://github.com/ArcReel/ArcReel/issues/2058) "Asset image prompt template optimisation", covering three kinds of internalised text in `lib/prompts/prompt_builders.py`: `_CHARACTER_LAYOUT` / `_SCENE_LAYOUT` / `_PROP_LAYOUT` / `_PRODUCT_LAYOUT`, `_*_GUARD`, and `_NEGATIVE_TAIL_*`.
> Scope: five question blocks — A: official positions on multi-panel / multi-view / sheet-style composition; B: side effects of using a multi-panel collage as an i2i reference image; C: actual effectiveness of positive anti-artifact sentences and textual negative tail sentences; D: what the picture of a reference image used as conditioning input should look like (shot size / angle / pose / background / count); E: aspect ratio. Blocks D and E are targeted supplementary research from 2026-08-24, answering "what should a single image show, and in which aspect ratio".
> Source discipline: only official documentation sites / model cards / vendor cookbooks / API references, plus arXiv and peer-reviewed papers, are accepted. Second-hand blogs, forums and aggregated tutorials do not enter the conclusions; they are mentioned only where something has to be marked "community lore, no official backing", and marked as such. Every conclusion carries a source URL and publication/update date; where no date is available, "date not given" is written. Where no official statement can be found, "no official statement found" is written, never filled in with guesses.
> Note on fetching: several vendor documentation sites are front-end rendered or return 403 to direct requests (`docs.midjourney.com`, `www.volcengine.com/docs`, `docs.byteplus.com`, `platform.minimax.io`). The body text of these pages was obtained through a rendering proxy; quotations and URLs are all content of the original official pages, and no second-hand paraphrase is cited.
> Research date: 2026-08-24
> Relation to the existing report: this report is an increment to `arcreel-prompt-best-practices-research.md` (2026-07-13) and does not restate its Q1/Q2/Q4; the first half of block C cites and refines its Q3, and blocks A/B/D/E cover new ground not covered by that report. See section 7 for details.

---

## 1. Block A: Official positions on multi-panel / multi-view / sheet-style composition

### 1.1 Conclusion matrix

| Vendor | Does the vendor recommend "laying out multiple panels/views in one image"? | Official consistency path | Official statements on panel count/consistency | Character sheet use case |
| --- | --- | --- | --- | --- |
| Google Gemini (Nano Banana series) | **Partially recommended (narrative panels only)**: section 6 of the official prompting guide, Sequential art (comic panel / storyboard), gives the template "Make a 3 panel comic in a [style]"; **multiple views of the same subject explicitly go the opposite way**: section 7, Character consistency: 360 view, calls for iterative generation angle by angle, not a single-image layout | **Iterative generation + multi-reference-image channel**: 360 views rely on "iteratively prompting for different angles" and feeding already generated images back as references; multi-reference quotas: Gemini 3 Pro 6 object images + 5 character images + 3 style images, 3.1 Flash 10 object images + 4 character images, 3.1 Flash Lite 14 object images and no character channel, total cap 14 images | Explicit limitation: "The model might not create the exact number of images you ask for"; for 2.5 Flash Image, no more than 3 input images is recommended, for 3 Pro Image no more than 14 | The words "character sheet" not found; the official equivalent use case is the 360 view, done as separate images per angle |
| OpenAI gpt-image | **Recommended** (narrative panels only): the cookbook has a Story-to-Comic Strip subsection, whose example is a single image with "4 equal-sized panels" | Describe each panel within a single image + reference multiple input images by index | "one per panel"; no cap on count and no consistency guarantee | Not found (the words character sheet / multi-view / turnaround do not appear anywhere) |
| Black Forest Labs FLUX.2 | **Explicitly the opposite**: the official multi-panel section calls for generating each panel separately | "Generate each panel separately while keeping character descriptions consistent"; consistency comes from repeating the character description in each panel's prompt | "Repeat these details in every panel prompt" | Not found (multi-reference use is listed as character consistency across variations) |
| ByteDance Seedream 4.0/4.5 (Volcengine Ark / BytePlus ModelArk) | **Explicitly the opposite**: the vendor files multi-view/set requirements under "multi-image output" | "Multi-image output" — trigger group generation with "a series", "a set" or a specified number of images; the vendor names storyboarding, comic creation, IP product design, emoji pack | Technical report: "image sequences that remain both character-consistent and stylistically aligned" | No "single-image character sheet" statement found; set-design scenarios go through multi-image output |
| Alibaba Tongyi Wanxiang / Qwen-Image (DashScope) | Not found | The official prompt guide only gives the formula "subject + scene + style + camera language + atmosphere words + detail modifiers" and a five-dimension vocabulary | Not found | No official statement found |
| MiniMax image-01 | Not found | `subject_reference` (`type: "character"`) reference-image channel, see the i2i endpoint | Not found | No official statement found |
| Midjourney | No statement on multi-panel/sheet composition found | Omni Reference (`--oref`/`--ow`, one image only), Character Reference (`--cref`/`--cw`, replaced by Omni Reference from V7) | Not found | No official statement found; the official Character Reference best practices conversely require the reference image to show a **single character** |
| Jimeng Dreamina | Not found | Not found | Not found | No official statement found |

**One fact that generalises across vendors**: among all vendors whose body text could be obtained in this research, **not a single one writes "multiple views of the same subject laid out in one image" into its official guide**. The official consistency paths fall into two kinds — a multi-reference-image channel (Google, Midjourney, MiniMax, BFL) or grouped multi-image generation (Seedream). The single-image multi-panel official use cases that do appear (three in total: OpenAI comic strip, BFL comic strip, Google 3 panel comic) are all **narrative panels**, none is multiple views of the same subject; and BFL's approach is to generate each panel separately. A stronger piece of contrary evidence comes from Google: the vendor has a dedicated 360 view subsection for "multiple angles of the same character", and its approach is to generate each angle as a separate image and feed already generated images back as references, not to lay them out in one image.

### 1.2 Evidence item by item

**Google Gemini: consistency is framed as a multi-reference-image problem, and the vendor states explicitly that the count cannot be guaranteed.**
Reference image quotas (official table, per model): "Up to 14 reference images: You can now mix up to 14 reference images to produce the final image." Gemini 3.1 Flash Lite Image allows up to 14 object images, with the character and style channels both N/A; Gemini 3.1 Flash Image up to 10 object images + 4 character images; Gemini 3 Pro Image up to 6 object images + 5 character images + 3 style images (the original text of the character-image column is "Up to N images of characters to maintain character consistency").

Section 7 of the prompting guide on the same page is titled "Character consistency: 360 view", body: "You can generate 360-degree views of a character by iteratively prompting for different angles. For best results, include previously generated images in subsequent prompts to maintain consistency. For complex poses, include a reference image of the selected pose." The template is "A studio portrait of [person] against [background], [looking forward/in profile looking right/etc.]" — i.e. **one angle at a time, single-image output**; the official example prompt is "A studio portrait of this man against white, in profile looking right".

Section 6 on the same page, "Sequential art (comic panel / storyboard)", is the only official single-image multi-panel template: "Make a 3 panel comic in a [style]. Put the character in a [type of scene]." The vendor notes "For accuracy with text and storytelling ability, these prompts work best with Gemini 3 Pro and Gemini 3.1 Flash Image." This use case is narrative panels, not multiple views of the same subject. Searching the whole page for `character sheet / turnaround / multiple views` yields no hits.
Source: https://ai.google.dev/gemini-api/docs/image-generation (Last updated 2026-08-10 UTC)

Official limitations page: "The model might not create the exact number of images you ask for." "For best results using Gemini 2.5 Flash Image, include a maximum of three images in an input. For best results using Gemini 3 Pro Image, include a maximum of 14 images in an input."
Source: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-image-generation-limitations (Last updated 2026-08-21 UTC)

None of the seven items on the official best-practices page concerns multi-panel composition; the remedy for character drift is "restart a new conversation with a detailed description".
Source: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-image-generation-best-practices (Last updated 2026-08-21 UTC); https://developers.googleblog.com/en/how-to-prompt-gemini-2-5-flash-image-generation-for-the-best-results/ (2025-08-28)

**OpenAI: the only vendor that writes single-image multi-panel into its official guide with a complete example, but the purpose is narrative panels.**
Capability item: "Complex structured visuals, including infographics, diagrams, and multi-panel compositions".
Section 4.7: "For story-to-comic generation, define the narrative as a sequence of clear visual beats, one per panel. Keep descriptions concrete and action-focused so the model can translate the story into readable, well-paced panels." The example prompt begins "Create a short vertical comic-style reel with 4 equal-sized panels." and writes out Panel 1–4 one by one.
Searching the whole page for `multi-view / character sheet / turnaround / three-view / contact sheet / collage` yields no hits.
Source: https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide (2026-04-21)

**Black Forest Labs: the official multi-panel section's approach is to generate each panel separately.**
"Comic Strips and Sequential Art — Create consistent comic panels with character continuity. The key is to define your character in detail and maintain that description across panels." "Generate each panel separately while keeping character descriptions consistent", with a note at the end of the example: "Character Consistency: Notice how Diffusion Man's description stays detailed and consistent across panels... Repeat these details in every panel prompt."
Multiple reference images are a separate channel: "[pro] API has a 9MP total limit for input+output. At 1MP output you can use up to 8 reference images, at 2MP output up to 7, and so on." The officially listed use cases are fashion shoots / interior design / product composites / character consistency (Maintain identity across variations).
Source: https://docs.bfl.ai/guides/prompting_guide_flux2 (date not given)

**Seedream: the vendor explicitly files "set/multi-view" requirements under multi-image output, not single-image multi-panel.**
The "Multi-image output" subsection of the official prompt guide: "Seedream 4.5 and 4.0 support generating image sequences with consistent character continuity and unified style, making it suitable for storyboarding, comic creation, and set-based design scenarios that require a cohesive visual identity, such as IP product design or emoji pack creation." "When generating multiple images, you can trigger series generation with phrases like "a series", "a set", or by specifying the number of images."
"Reference-based generation" on the same page requires spelling out two things at once: "Reference Target: Clearly describe the elements to be extracted and retained from the reference image" and "Generated Scene Description: Provide detailed information about the desired generated content, including scene, layout, and other specifics."
Source: https://docs.byteplus.com/en/docs/ModelArk/1829186 (Last updated 2026-07-06; Chinese same-source page https://www.volcengine.com/docs/82379/1829186?lang=zh )

Supporting evidence from the technical report: "Leveraging strong capabilities in global planning and in-context consistency, Seedream 4.0 supports the generation of image sequences that remain both character-consistent and stylistically aligned."
Source: https://arxiv.org/html/2509.20427v2 (arXiv:2509.20427, 2025-09)

**Alibaba DashScope: the official prompt guide has no relevant statement.**
The `text-to-image-prompt` page gives basic/advanced formulas and a five-dimension vocabulary of "shot size, angle, shot type, style, lighting", and does not touch on multiple panels, three-view drawings, character design sheets or collages.
Source: https://help.aliyun.com/zh/model-studio/text-to-image-prompt (the page has an "updated" field, but the fetched date value was empty; recorded as date not given)

**MiniMax image-01: no statement on multi-panel composition found; a reference-image channel exists.**
The t2i endpoint parameter table is `model / prompt / aspect_ratio / width / height / response_format / seed / n / prompt_optimizer`, with no negative channel and no explanation of multi-panel composition. The official example for the i2i endpoint includes `subject_reference: [{ "type": "character", "image_file": ... }]`.
Source: https://platform.minimax.io/docs/api-reference/image-generation-t2i , https://platform.minimax.io/docs/api-reference/image-generation-i2i (date not given)

**Midjourney: no statement on multi-panel/sheet composition found; the reference-image mechanisms are fully documented (details in block B).**
Source: https://docs.midjourney.com/hc/en-us/articles/32040250122381-Image-Prompts , https://docs.midjourney.com/hc/en-us/articles/36285124473997-Omni-Reference , https://docs.midjourney.com/hc/en-us/articles/32162917505293-Character-Reference (all date not given)

**Jimeng Dreamina: no official prompt-guide-level content found.**
`jimeng.jianying.com/features/resource/*` are general explainer/marketing how-to pages on the official site, with only generic advice such as "avoid ambiguous words in prompts", and no statements on multi-panel composition, reference image selection, negative phrasing or human anatomy. On the ModelArk side, Jimeng's official docs currently only show the Dreamina Seedance video tutorial series (https://docs.byteplus.com/en/docs/modelark/2291680 ); no separate image-side prompt guide was seen.
Source: https://jimeng.jianying.com/features/resource/text-to-image-prompts (date not given)

---

## 2. Block B: Side effects of using a multi-panel collage as an i2i reference image

### 2.1 Official side: composition inheritance is a reference-image behaviour vendors name explicitly

**Midjourney officially lists "composition" as one of the three things an image prompt influences.**
First sentence of the Image Prompts page: "Want to influence the content, composition, and colors of your Midjourney creations? Include an image as part of your prompt!" The strength of the influence is controlled by `--iw` (V7 default 1, range 0–3). The same page has another note directly relevant to collages: multiple images without a text prompt are blended together.
Source: https://docs.midjourney.com/hc/en-us/articles/32040250122381-Image-Prompts (date not given)

**Midjourney's official best practice for character reference images is "single character".**
First Best Practices item on the Character Reference page: "Use Midjourney Images: For best results, start with an image of a single character created by Midjourney." Second item: "Limit Character References: While you can use more than one image of the same character, it's often not necessary."
The Omni Reference page phrases multi-subject reference images as a qualified possibility, not a recommendation: "Multiple Characters: While you can only use one image as an Omni Reference, you can try using an image that contains multiple characters/people and describe them in your prompt." — i.e. a multi-subject reference image must have each subject identified in text, and the vendor does not promise results.
Source: https://docs.midjourney.com/hc/en-us/articles/32162917505293-Character-Reference , https://docs.midjourney.com/hc/en-us/articles/36285124473997-Omni-Reference (all date not given)

**Midjourney officially acknowledges that reference images and style instructions compete for influence (style drift).**
Omni Reference page: "Reinforce the Style: If you want your image in a different style than your reference, mention your desired style at both the start and end of your prompt... With a lower weight you will need to reinforce the physical characteristics you want to preserve using your prompt text." and "If you're using high stylize or `--exp` values you may want to also use a higher Omni Reference Weight, as these will all compete for influence." There is also an official warning about the consequence of too high a weight: "it's best to keep your weight below 400, otherwise your results may be unpredictable."
Source: as above (date not given)

**Google's only official count/content constraint on reference images comes down to "a single subject".**
Veo Subject image: "You provide up to three images of a single person, character, or product. Veo preserves the subject's appearance in the output video." The Style image is limited to a single image. This is the closest explicit official statement in this research to "a reference image should show a single subject", but it belongs to the video reference-image context; no equivalent statement was seen on the image side.
Source: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/video/use-reference-images-to-guide-video-generation (Last updated 2026-08-21 UTC)

**Seedream officially requires explicitly identifying "what to extract" from the reference image.**
"Reference Target: Clearly describe the elements to be extracted and retained from the reference image" — the vendor's default assumption is that the reference image's content will not automatically be selected correctly, and the prompt needs to identify it. The same page also gives a remedy specifically for the case where "the reference image's content is complex and hard to describe accurately in words": "visual indicators such as arrows, bounding boxes, or doodles can be used to specify the editing target and its location."
Source: https://docs.byteplus.com/en/docs/ModelArk/1829186 (Last updated 2026-07-06)

**What has no official statement**: no vendor's official docs directly discuss the consequences of the specific situation of "using a multi-panel/collage image as a reference image". The items above are related evidence derived from three kinds of explicit statement — "reference images carry composition over", "reference images should show a single subject", "the extraction target in a reference image must be identified" — and are not themselves official judgements about collage reference images.

### 2.2 Paper side: there is direct mechanism-level evidence for composition inheritance and dilution of subject anchoring

**Reference image injection copies the reference content as a whole rather than just the subject (copy-paste effect).**
Conceptrol (arXiv:2503.06568, 2025-03-09): "zero-shot adapters inject the reference image as a condition in the attention block with Direct Adding or MM-Attention. To that end, **the reference image is not attached to any explicit text concept**. This often results in a **copy-paste effect** or poor compositional generation." The concrete failure mode given in the caption of Figure 1 is "duplicating the book" — an object that appears twice in the reference image is also duplicated in the output.
The same paper's description of conditioning strength explains why this cannot be worked around by tuning: "a low image conditioning strength (IP Scale) in IP-Adapter fails to preserve the concept effectively while increasing the scale causes deviations from the text prompt and leads to a copy-paste effect."
Source: https://arxiv.org/abs/2503.06568 , https://arxiv.org/html/2503.06568v1 (2025-03-09 v1)

**There is direct attention-layer evidence for dilution of subject anchoring.**
Of the same paper's three empirical observations, the first is exactly the phenomenon this issue refers to: "In the absence of textual concept constraints, **the attention map for reference images derived from adapters does not focus on the target subject requiring customization**"; the second gives the consequence on the output side: "Adapters do well at transferring appearance of reference images within regions of high attention scores" — i.e. wherever the attention lands, that is where the appearance gets transferred. When a reference image contains several highly salient regions, attention being spread out is a direct consequence of this mechanism.
Source: as above

**A vendor's own technical report admits that "what should be preserved" is itself underdetermined.**
Seedream 4.0 technical report: "reference-based generation presents a more challenging trade-off between preservation and creativity. This difficulty arises from **the inherently ambiguous definition of what should be preserved**. In some cases, the target is a person's ID or IP, a particular artistic style, or even an abstract concept."
Source: https://arxiv.org/html/2509.20427v2 (2025-09)

**Evidence boundary (must be stated)**: Conceptrol's experiments target open-source adapter architectures (IP-Adapter / OminiControl, on Stable Diffusion, SDXL, FLUX base models). Most of what ArcReel actually calls are closed-source commercial APIs whose reference-image injection implementations are not public. "The same mechanism holds equally for closed-source APIs" is an **inference**, not a conclusion of the paper. The only official signal it can be aligned with is Midjourney's explicit statement that "an image prompt influences composition".

---

## 3. Block C: Anti-artifact sentences and textual negative prompts

### 3.1 Positive anti-artifact sentences (`五官对称、手指完整为五指、肢体比例协调` — "symmetrical facial features, hands complete with five fingers, well-proportioned limbs")

**Conclusion: across all official docs whose body text could be obtained in this research, no vendor was found to recommend declaring facial symmetry, finger count or limb proportions in the positive prompt.**

Vendor-by-vendor check results:
- Google: none of the seven recommendations on the image best-practices page is about anatomy; the failure modes listed on the limitations page are language support, image count, text rendering and safety blocking, not anatomical breakdown. (https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-image-generation-best-practices , Last updated 2026-08-21 UTC)
- OpenAI: **does** have guidance on people, but its form is the opposite of anti-artifact sentences — **visible compositional descriptions** rather than quality declarations: "For people in scenes, describe scale, body framing, gaze, and object interactions. Examples: "full body visible, feet included," "child-sized relative to the table," "looking down at the open book, not at the camera," or "hands naturally gripping the handlebars." These details help with body proportion, action geometry, and gaze alignment." (https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide , 2026-04-21)
- BFL FLUX.2, the Seedream prompt guide, the DashScope text-to-image prompt guide, MiniMax, Midjourney, Jimeng: no anatomical anti-artifact advice anywhere.
- Alibaba DashScope is the only vendor that names "多余的手指" ("extra fingers") in its official docs, but it puts the term in the **negative channel**: `negative_prompt` "描述不希望在画面中出现的内容，如"模糊"、"多余的手指"等。仅用于辅助优化质量。" ("describes content you don't want to appear in the image, such as "blurry", "extra fingers", etc. Only used to assist in optimising quality.") (https://help.aliyun.com/zh/model-studio/text-to-image , date not given)

**Characterising the strength of evidence**: within the scope of this research, positive anti-artifact sentences are "community lore, no official backing". They share an origin with the positive/negative quality-word habits of the early Stable Diffusion ecosystem, but **this report found no first-hand source proving that they are still effective or ineffective on contemporary models** — "no official backing" is not the same as "disproven", and the two must be stated separately. The closest citable official contrary signals are the two already recorded in the existing report: Vidu calls `cinematic dramatic beautiful high quality` "model noise"; Seedream's official counter-example lists keyword stacking as a not-recommended style.

### 3.2 Textual negative exclusions (`画面避免：水印、多余文字、Logo` — "image should avoid: watermarks, extra text, logos" — written at the end of the prompt)

This question has to be split into two non-equivalent sub-questions; the existing report's Q3 answered only the first.

**Sub-question 1: each vendor's official attitude to "writing exclusions in the body" — split three ways, and split even within a single vendor.**
The three-way conclusion of the existing report's Q3 still holds; this research adds and updates the following:

| Vendor | Official position | Currency as verified this time |
| --- | --- | --- |
| Google image | Explicitly opposed, requires positive rewriting: "Describe what you want, not what you don't: Instead of saying "no cars", describe the scene positively by saying, "an empty, deserted street with no signs of traffic."" | Verified valid, Last updated 2026-08-21 UTC |
| Black Forest Labs FLUX.2 | **New** (the existing report did not cover BFL): "**No negative prompts: FLUX.2 does not support negative prompts. Focus on describing what you want, not what you don't want.**" i.e. no negative channel and an explicit requirement to rewrite positively | https://docs.bfl.ai/guides/prompting_guide_flux2 , date not given |
| OpenAI image | Explicitly encourages writing exclusions in the body: "State exclusions and invariants explicitly (e.g., "no watermark," "no extra text," "no logos/trademarks," "preserve identity/geometry/layout/brand elements")"; the example prompts repeatedly contain "No extra text, no watermarks, no unrelated logos" and "Constraints: - Original artwork only - No trademarks - No watermarks - No logos" | Verified valid, 2026-04-21 |
| Alibaba DashScope | Split across models: the qwen-image series supports `negative_prompt`; "wan2.7-image-pro、wan2.7-image 不支持 `negative_prompt` 参数，对于不希望出现的元素，请在正向提示词中描述（不要出现xxx）。" ("wan2.7-image-pro and wan2.7-image do not support the `negative_prompt` parameter; for elements you don't want to appear, describe them in the positive prompt (xxx must not appear).") — **for models without a negative channel, the vendor explicitly instructs writing the negative sentence into the positive prompt** | https://help.aliyun.com/zh/model-studio/text-to-image , date not given |
| Seedream image | **This research revises one classification in the existing report**: the existing report put Volcengine in the "encourages writing exclusion sentences in the body" category, based on the "constraint words" subsection for Seedance **video** 2.0. The Seedream 4.0–4.5 **image** prompt guide has no exclusion/constraint-word subsection anywhere, only preservation instructions such as "if other elements should stay unchanged, say so explicitly". Volcengine's "encourages negation" conclusion applies to the video line and cannot be extrapolated directly to the image line | https://docs.byteplus.com/en/docs/ModelArk/1829186 , Last updated 2026-07-06 |
| MiniMax image-01 | No negative channel; the official docs take no position | Date not given |
| Midjourney | No official position on negative phrasing inside the prompt found (not seen on any of the three pages Image Prompts / Omni Reference / Character Reference in this research) | Date not given |
| Jimeng Dreamina | No official statement found | — |

**Sub-question 2: does textual negation backfire on contemporary models (mentioning reinforces)? — there is peer-review-level evidence, and its direction is unfavourable to textual negation.**

- **T2I models comply with in-body negation at a very low rate, and the failure mode is "drawing it anyway".** "Relations, Negations, and Numbers: Looking for Logic in Generative Text-to-Image Models" (arXiv:2411.17066, 2024-11-26) measured on DALL·E 3: "**Unmodified prompts specifically prohibiting an entity X invariably led to images showing X.**" Accuracy for unrewritten negation prompts is **12.3%**; even with LLM rewriting, overall accuracy only reaches **40.7%**. The paper's breakdown of rewriting strategies is also worth noting: 71% of rewrites are "Addition" (the original object is still mentioned, with other objects added), and such rewrites "seems to fail more often than it succeeds. While additional objects are indeed added in the image, the original object often remains".
  Source: https://arxiv.org/abs/2411.17066 , https://arxiv.org/html/2411.17066v1 (2024-11-26, v1)
- **The underlying text encoders are nearly insensitive to negation.** "Vision-Language Models Do Not Understand Negation" (arXiv:2501.09425, v1 2025-01-16, v2 2025-05-13, **CVPR 2025**): "Our evaluation reveals that modern VLMs struggle significantly with negation, **often performing at chance level**." The paper targets the CLIP family — i.e. the text-conditioning encoder of most diffusion models.
  Source: https://arxiv.org/abs/2501.09425
- **One point that must be kept separate: the negative channel (negative_prompt) has its own, effective mechanism, and cannot be used to vouch for textual negation.** "Understanding the Impact of Negative Prompts: When and How Do They Take Effect?" (arXiv:2406.02965, 2024-06-05) characterises two behaviours — Delayed Effect (the negative acts after the positive content has already been rendered) and Deletion Through Neutralization (it cancels out against the positive condition in latent space) — that depend on CFG's separate negative-condition branch; **this is a different mechanism from writing negative sentences into the positive prompt**. ArcReel currently declares in the module docstring of `lib/prompts/prompt_builders.py` that it has given up the negative_prompt parameter channel and uniformly uses text tail sentences, so this paper's conclusion **does not support** the current implementation; if anything, it is favourable evidence for the path the current implementation abandoned.
  Source: https://arxiv.org/abs/2406.02965 (2024-06-05)

**Correcting the wording "mentioning reinforces"**: the paper evidence supports "negative sentences are often not carried out, and the negated entity still appears" (low compliance), not the stronger "writing it makes it more likely to appear than not writing it" (active reinforcement). This research found no first-hand source that ran a controlled "write the negation vs. don't write it" experiment and reported the former to be worse. Stating the conclusion as "mentioning reinforces" would go beyond the evidence.

### 3.3 Three-tier classification at a glance (block C)

- **Supported by first-hand evidence**: Google / BFL "positive rewriting beats negation" (explicit official); OpenAI "write exclusions in the body" (explicit official, and in direct conflict with the former); Alibaba instructs writing `不要出现xxx` ("xxx must not appear") for models without a negative channel (explicit official); T2I compliance with textual negation is very low (supported by papers, CVPR/arXiv).
- **Community lore never officially backed**: positive anatomical anti-artifact sentences (symmetrical facial features, five complete fingers, well-proportioned limbs); stacking positive quality words.
- **Explicitly ruled out by official docs**: using a negative prompt on FLUX.2 (officially unsupported); using "no X"-style negation on the Google image line (vendor requires rewriting as positive); using the `negative_prompt` parameter on wan2.7-image / wan2.7-image-pro (officially unsupported; write it into the positive prompt instead).

---

## 4. Block D: What the picture of a reference image used as conditioning input should look like

The question in this block is not "does the asset image look good to a human", but "as conditioning input for downstream i2i / reference-to-video generation, what shot size, angle, pose, background and count should the asset image itself have". Sources come from two sides: vendors' official specs and content advice for reference images, and ablations concerning reference images in the subject-driven / personalized generation literature.

### 4.1 Conclusion matrix

| Sub-question | Official vendor position | Paper-side evidence | Conclusion that can be drawn |
| --- | --- | --- | --- |
| D1 Shot size (full body / half body / bust) | **Not found**: none of the 7 vendors searched page by page in this round (Google, OpenAI, BFL, Seedream, DashScope, MiniMax, Midjourney) says anything about reference-image shot size; Jimeng keeps the first-round conclusion — nothing relevant seen in the official docs | Two indirect items: under latent diffusion, "relatively small faces" are reconstructed inaccurately (DiffBody); when building its training set, PhotoMaker enforces "face occupies >10% of the image after cropping" | No direct evidence supports any shot size; two adjacent items both point the same way, that **the pixel share of the face is a variable**, but neither is a direct experiment on "reference image shot size" |
| D2 Angle (front / 45° / three-quarter) | **Not found**; "45° conveys a sense of three-dimensionality" has zero hits across the 7 vendors' official docs searched in this round, Jimeng has nothing relevant | No ablation on reference-image angle found; IDAdapter's mechanism explanation shows, conversely, that the face angle of a single image is absorbed by the model | Not found. The 45° in the current templates is **community lore, no official backing** |
| D3 Pose (A-Pose / T-Pose / natural pose) | **No** advice on reference-image pose found from any image vendor; Google has one adjacent statement: for complex poses, **provide a separate pose reference image** | No A-Pose ablation found on the T2I side (A-pose appears as a canonical representation in 3D character reconstruction pipelines, which is not the same chain as i2i identity anchoring) | Not found. The pose-control path the vendor gives is a **separate pose reference image**, not inheriting the pose from the identity reference image |
| D4 Background (plain vs. with environment) | **Not found**: none of the 7 vendors in this round has an explicit requirement for reference-image background | **Empirical, and consistent in direction**: Subject-Diffusion segments the subject before encoding, and removing location control degrades all metrics; PhotoMaker fills regions other than the person with random noise to "eliminate the influence of the background"; PuLID lists background/lighting/composition/style as "ID-irrelevant" and requires them to keep the original model's behaviour | The paper side consistently points to "the reference-image background is a source of contamination; the mainstream practice is to remove it before encoding". Same origin as the copy-paste effect in B7 |
| D5 Number of reference images (single vs. several from multiple angles) | Vendors give only **quotas**, no advice: Google 3 Pro up to 5 character images, BFL pro up to 8 at 1MP output, Seedream 4.0–4.5 up to 14, MiniMax **supports only a single** subject_reference per request | **Quantitative ablations exist — the hardest part of this block**: DreamBooth 3–5 images, supplementary-material ablation over 1→5 images, about 4 best; PhotoMaker's inference-time gain is most pronounced from 1→2 images, but trades off against text controllability; SynCD 3 images vs. 1; IDAdapter's N ablation **is a training-time hyperparameter, not the inference-time number of reference images** (see the boundary note in 4.2) | Several images of the same subject from different angles beat a single image; **all the empirical evidence is built on "several separate images", none on "several panels laid out in one image"**. Note that the "count" in the different papers is not the same quantity: for DreamBooth it is the number of fine-tuning samples, for PhotoMaker/SynCD the number of inference-time reference images, for IDAdapter the number of images mixed at training time |

### 4.2 Evidence item by item

**D1 Shot size: no direct evidence; two adjacent items both point to the pixel share of the face.**

DiffBody gives a mechanism explanation on the **generation side**: "the VAE used in the LDM cannot reconstruct relatively small faces accurately from low-dimensional latent maps"; its ablation shows that adding a face-specific refinement lowers the ID metric (lower is better) from 0.403 to 0.175 (Table 3). This concerns identity loss because the face takes up too small a share of the latent in a full-body composition; **it acts on the output side, not the reference-image side**, and cannot be taken directly as evidence that "reference images shouldn't be full body".
Source: https://arxiv.org/abs/2401.02804 (2024-01-05)

PhotoMaker's cropping rule when building its ID training set is a **constraint the authors imposed deliberately**: "we crop the image with a larger square box based on the detected face area while ensuring that the facial region can occupy more than 10% of the image after cropping". This shows the authors believe that too small a face share harms learning of ID features. Extrapolating it to inference-time reference images is an **inference**; the paper did not run that experiment.
Source: https://arxiv.org/abs/2312.04461 , https://arxiv.org/html/2312.04461v1 (2023-12-07)

Vendor side: in this round, pages from 7 vendors were obtained and searched one by one — the Gemini image generation page, the OpenAI image guide and API reference, the BFL FLUX.2 docs, the Seedream prompt guide and API reference, the MiniMax image guide, the Midjourney docs, and the DashScope text-to-image page (Jimeng: no new pages could be obtained this round, so the first-round conclusion stands) — for "full body / close-up / portrait / bust / framing / 景别 / 全身 / 半身" (shot size / full body / half body); **no vendor was found to say what shot size a reference image should use**. The only adjacent official requirements are those already recorded in block B — Midjourney's "character reference images should show a single character" and Google Veo's "a single person, character, or product" — and both constrain **the number of subjects**, not shot size.

**D2 Angle: no source found for "45° conveys a sense of three-dimensionality".**

Searching the 7 vendors' official docs obtained page by page in this round for "45 / three-quarter / 四分之三 / 侧视 / angle of view" (three-quarter / side view), **no vendor was found to write "a 45° angle better conveys three-dimensionality" or anything similar into a reference-image/prompt guide** (Jimeng keeps the first-round conclusion: nothing relevant seen in the official docs). Within the scope of this research, the claim is classified as **community lore, no official backing**.

On the paper side, no controlled experiment on "front vs. 45° reference image, which gives better downstream identity fidelity" was found either. The closest first-hand statement available is IDAdapter's mechanism explanation for multi-image mixing: "This enriched feature is derived from multiple images under the same identity, so their common characteristics (i.e., the identity information) will be greatly enhanced, while others (such as **the face angle and expression of any specific image**) will be somewhat weakened." — what it shows is that **the angle of a single reference image is absorbed by the model along with everything else**, not which angle is better.
Source: https://arxiv.org/abs/2403.13535 (2024-03-20)

There is one adjacent field pointing in a related direction for which **no citable first-hand data was obtained this time**: face recognition accuracy drops at large yaw angles (pose-invariant face recognition literature). This is consistent in direction with "reference images should lean towards frontal", but it concerns recognition models rather than the diffusion conditioning channel, and no directly citable original data was checked this time, so **it is not a conclusion of this report**.

**D3 Pose: vendors hand pose control to a separate pose reference image.**

Google's original text in the Character consistency: 360 view subsection contains one directly relevant statement: "For complex poses, include a reference image of the selected pose." — i.e. the pose is carried by **another, dedicated pose reference image**, not expected to be inherited from the identity reference image.
Source: https://ai.google.dev/gemini-api/docs/image-generation (Last updated 2026-08-10 UTC)

Searching for A-Pose / T-Pose: zero hits in the 7 vendors' official docs this round, nothing relevant from Jimeng; among the mainline subject-driven generation papers (DreamBooth / Textual Inversion / IP-Adapter / InstantID / PuLID / PhotoMaker / Subject-Diffusion / IDAdapter), **no work was found that ablates "neutral pose in the reference image" as a variable**. A-pose does appear as a canonical intermediate representation in 3D character reconstruction (the StdGEN line of work), but that chain is "single image → canonicalisation → 3D reconstruction", not the same chain as this issue's "reference image → i2i condition", and no citable verbatim original text was obtained this time either, so it does not enter the conclusions.

One related fact pointing the other way: InstantID explicitly uses "five facial keypoints (two for the eyes, one for the nose, and two for the mouth)" as a **weak spatial condition**, designed precisely so that the reference image's pose is **not** transferred to the output, letting users "generate customized images with various poses or styles". This shows that in that architecture, the reference image's pose is not what determines the downstream pose.
Source: https://arxiv.org/abs/2401.07519 (v1 2024-01-15, v2 2024-02-02)

**D4 Background: the strongest item in this block, with a clear direction — the reference-image background is a source of contamination.**

Subject-Diffusion segments first and then encodes: "we feed the **segmented** subject image into the CLIP image encoder to obtain 256-length patch feature tokens", with segmentation done by SAM ("these detection boxes are used as input prompt to SAM to obtain their respective object masks"); the ablation shows "if we remove the location control (object masks), our model will apparently degenerate over all evaluation metrics".
Source: https://arxiv.org/abs/2307.11410 (2023-07-21)

PhotoMaker fills the regions other than the person with random noise, and states the reason plainly: "we filled the image areas other than the body part of a specific ID with random noises **to eliminate the influence of other IDs and the background**", using Mask2Former's person-class panoptic segmentation.
Source: https://arxiv.org/abs/2312.04461 (2023-12-07)

PuLID divides "what should be carried over from the reference image and what shouldn't" into two explicit categories: "an ideal ID insertion should alter only ID-related aspects, such as face, hairstyle, and skin color, while image elements not directly associated with the specific identity, such as **background, lighting, composition, and style**, should be consistent with the behavior of the original model"; the paper also admits that the current state is "Methods with higher ID fidelity tend to induce more severe style degradation", quantified with CLIP-I (PuLID 0.812 vs. InstantID 0.680).
Source: https://arxiv.org/abs/2404.16022 (v1 2024-04-24, v2 2024-10-31)

SynCD provides corroborating evidence from the evaluation side: existing metrics "can struggle with capturing overall quality and **favor methods that copy-paste the target object on a new background**" — i.e. copy-paste is a known failure mode of this family of methods, and it gets rewarded by the metrics.
Source: https://arxiv.org/abs/2502.01720 , https://arxiv.org/html/2502.01720v2 (v2 2025-10-13)

These four items point to the same thing as B7 in block B (copy-paste effect, attention map not focused on the target subject): **every salient region in the reference image other than the subject takes part in conditioning**. B8's boundary applies here too — all this evidence comes from open-source architectures, and whether it holds for closed-source commercial APIs is an inference. On "what a reference image's background should look like", **no explicit statement was found** from any of the 7 vendors checked page by page in this round.

**D5 Number of reference images: quantitative ablations exist, and the shape of their conclusions is worth noting.**

- **DreamBooth**: "Given only a few (typically **3-5**) casually captured images of a specific subject"; on the input images, "We do not impose any restrictions on input image capture settings and the subject image can have varying contexts"; the supplementary material contains a count ablation — training "5 models per subject with input images ranging from 1 to 5", with the optimum around 4 images; the paper notes "This number can vary depending on the subject". The paper's limitations also name a failure mode relevant to this issue: "context and subject appearance to become entangled", i.e. **the context of the input images becomes entangled with the subject's appearance**.
  Source: https://arxiv.org/abs/2208.12242 (v1 2022-08-25, v2 2023-03-15)
- **Textual Inversion**: "Using only **3-5** images of a user-provided concept"; no count ablation was found in the body this time.
  Source: https://arxiv.org/abs/2208.01618 (2022-08-02)
- **PhotoMaker**: "using more images to form a stacked ID embedding can improve the metrics related to ID fidelity"; the gain is "particularly noticeable" **going from 1 to 2 images** (Figure 7, DINO about 47.0 → 49.5), with diminishing returns after that; and it states the cost explicitly: "there may exist a trade-off between text controllability and ID fidelity" (CLIP-T drops as the count increases).
  Source: https://arxiv.org/abs/2312.04461 (2023-12-07)
- **IDAdapter**: gives a count ablation table (Table 2), but **the meaning of its N must be made clear first**: the paper's original text is "Changing the number of images N used in **training** process", and "At the testing and inference stage, we use only **one** image and simply duplicate it N times to serve as the input for the network". In other words, **N is the number of same-identity images mixed at training time, not the number of reference images the user provides at inference time** — this evidence cannot directly answer "how many reference images should asset images provide downstream"; what it answers is "the effect of the multi-image mixing mechanism on locking in pose/expression" —

  | N (training time) | ID-Sim | Expr-Div | Pose-Div (pitch) | Pose-Div (yaw) |
  | --- | --- | --- | --- | --- |
  | 1 | 0.602 | 37% | 5.02 | 12.90 |
  | 2 | 0.601 | 58% | 6.97 | 15.39 |
  | 3 | 0.604 | 61% | 7.03 | 15.44 |
  | 4 | 0.603 | 65% | 7.90 | 16.47 |
  | 5 | 0.601 | 64% | 7.88 | 16.42 |

  **ID similarity barely changes with N (0.601–0.604); what changes is the diversity of expression and pose** (Expr-Div 37% → 65%, yaw 12.90 → 16.47); the authors take N=4 as the best balance. The mechanism explanation is the sentence quoted in D2: what multiple images have in common (identity) is strengthened, and the angle and expression of any single one are weakened. **What can be extrapolated** is this mechanism direction; **what cannot** is "therefore 4 images should be given at inference time" — that paper's inference time uses a single image.
  Source: https://arxiv.org/abs/2403.13535 (2024-03-20)
- **SynCD**: with the same model, 1 vs. 3 reference images raises MDINOv2-I from 0.777 to 0.822; the paper directly calls "the lack of a multi-image dataset of the same object under **different poses, backgrounds and lighting**" the main bottleneck in this direction ("the lack of a dataset comprising multiple images of the same object in diverse poses, backgrounds, and lighting conditions has been a major bottleneck").
  Source: https://arxiv.org/abs/2502.01720 (v2 2025-10-13)
- **InstantID**: the representative of the single-image approach — "Given only one reference ID image, InstantID aims to generate customized images with various poses or styles from a single reference ID image while ensuring high fidelity"; with multiple images its approach is "For multiple reference images, we take the average mean of ID embeddings as image prompt".
  Source: https://arxiv.org/abs/2401.07519 (2024-01-15)

**The most important point of this set of ablations for this issue**: in all the empirical evidence, "several images" means **several independent image inputs** (several training images / several reference images / stacked embedding); **not a single paper stitches multiple views into one image and then uses it as a single reference input**. So D5's "multiple angles help" cannot be read as "laying multiple angles out in one asset image helps" — on the contrary, it points in the same direction as block A (the official consistency paths are multi-reference-image channels or multi-image output) and block B (a multi-panel image used as a reference passes on its layout and dilutes the subject).

---

## 5. Block E: Aspect ratio

### 5.1 Conclusion matrix

| Vendor | Requirements on the **reference image** aspect ratio | Official description of behaviour when reference-image ratio ≠ output ratio | Output ratio options |
| --- | --- | --- | --- |
| Google Gemini | Not found (no hard requirement on input image ratio/resolution seen) | **Explicitly official**: "By default, the model matches the output image size to that of your input image, or otherwise generates 1:1 squares."; **no statement found on how a mismatch is handled when `aspect_ratio` is set explicitly and differs from the input image** (no explicit statement on cropping/padding/stretching) | `1:1`, `3:2`, `2:3`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9` |
| OpenAI gpt-image | No requirement on input image ratio found; only a size limit was obtained: "Combined file size no more than 50MB" | Not found | gpt-image-1: `1024x1024` / `1024x1536` / `1536x1024`; gpt-image-2 accepts any resolution but "Long edge to short edge ratio must not exceed 3:1" |
| BFL FLUX.2 | The constraint is a **pixel budget**, not a ratio: "[pro] API has a 9MP total limit for input+output. At 1MP output you can use up to 8 reference images, at 2MP output up to 7" | Not found (the value `match_input_image` appears only in third-party API resale docs and **could not be confirmed on the official docs.bfl.ai pages**, so it is not accepted) | "1:1 (Square), 16:9 (Widescreen), 9:16 (Portrait), 4:3 (Classic), 21:9 (Ultrawide)"; "Minimum 64×64, maximum 4MP" "Output dimensions must be multiples of 16" |
| ByteDance Seedream 4.0–5.0 | **Explicitly official, and the only vendor in this research to give a numeric range for reference-image ratio**: reference-image aspect ratio `[1/16, 16]`, width and height both > 14px, total pixels `[196, 6000×6000]`, single image ≤ 30MB; reference-image count cap is 14 for 4.0/4.5/5.0 Lite and 10 for 5.0 Pro | No adaptive behaviour described for the regular generation mode; only the "layer decomposition" mode has `auto`, which "generates output based on the input image dimensions and aspect ratio" | Two mutually exclusive ways: give a resolution tier (1K/1.5K/2K/4K, varies by version) + describe the ratio in natural language in the prompt; or give width × height in pixels directly, ratio range `[1/16, 16]` |
| Alibaba DashScope | No requirement on reference-image ratio found | Not found | wan2.7-image-pro: 1K/2K/4K, custom 768×768–4096×4096, ratio 1:8–8:1; wan2.7-image: 1K/2K, 768×768–2048×2048; qwen-image-3.0 series: 512×512–2048×2048, ratio 1:8–8:1. The vendor also notes "4K resolution and custom 4096×4096 are only supported for text-to-image or multi-image generation scenarios without input images" |
| MiniMax image-01 | No ratio requirement found; **the vendor states explicitly "Only a single reference image is supported per request."** | Not found | `aspect_ratio` supports 1:1, 16:9, 4:3, 3:2, 2:3, 3:4, 9:16, 21:9 |
| Midjourney | Not found | Not found (the Aspect Ratio page does not mention interaction with image prompts / reference images) | `--ar #:#`, default 1:1, decimals not accepted; the vendor also notes "Aspect ratio isn't the same as image dimensions" |

### 5.2 Evidence item by item and assessment

**E1 — Official requirements on reference-image aspect ratio: only Seedream gives a numeric range; the others either only constrain file size/pixel budget, or say nothing at all.**
Seedream's reference-image specs (formats JPEG/PNG/WebP/BMP/TIFF/GIF/HEIC/HEIF, aspect ratio `[1/16, 16]`, width and height > 14px, ≤30MB, total pixels `[196, 36,000,000]`) are in the ModelArk image generation API reference.
Source: https://docs.byteplus.com/en/docs/ModelArk/1541523 (date not given); same-source prompt guide page https://docs.byteplus.com/en/docs/ModelArk/1829186 (Last updated 2026-07-06)
BFL's constraint takes the form of a pixel budget rather than a ratio.
Source: https://docs.bfl.ai/guides/prompting_guide_flux2 (date not given)
For OpenAI's input images, only the size limit (no more than 50MB combined) was obtained this time; on the output side, gpt-image-2 has the hard constraint "Long edge to short edge ratio must not exceed 3:1".
Source: https://developers.openai.com/api/docs/guides/image-generation (date not given)
MiniMax accepts only a single subject_reference per request.
Source: https://platform.minimax.io/docs/guides/image-generation (date not given)

**E2 — the only item in this block with an explicit official statement, and its direction is crucial.**
Google official (original text of the Aspect ratios and image size subsection): "By default, the model matches the output image **size** to that of your input image, or otherwise generates 1:1 squares. You can control the aspect ratio and the size of the output image using the `aspect_ratio` and `image_size` fields under `response_format` when type is set to "image"." That is, **as long as a reference image is sent and no ratio is specified explicitly, the output size/ratio follows the reference image**. This means a 16:9 character asset image will push downstream generations that don't specify a ratio to 16:9 by default.
Source: https://ai.google.dev/gemini-api/docs/image-generation (Last updated 2026-08-10 UTC)

**The other vendors: none was found to say whether a ratio mismatch results in cropping, padding or stretching.** Checking page by page across 7 vendors — Gemini (the explicit-ratio branch), the OpenAI image guide and API reference, the BFL FLUX.2 text-to-image/image editing/prompting guide, the Seedream prompt guide and API reference, the MiniMax image guide, the Midjourney Aspect Ratio page and the DashScope text-to-image page — none has such a statement. **The hypothesis "if the vendor crops, the reference image's ratio directly determines which part of the content enters the condition" has, within the scope of this research, been neither confirmed nor refuted by any vendor.**

One piece of **implementation-side corroboration, applicable only to open-source architectures**: IP-Adapter's preprocessing is "we resize the shortest side of the image to 512 and then center crop the image with 512×512 resolution" — i.e. a non-1:1 reference image is **center-cropped**, and the edge content does not enter the condition. This shows that the mechanism "the reference image's ratio may affect which part of the content is fed in" is technically real, but it **cannot be extrapolated to closed-source commercial APIs**; that would be an inference.
Source: https://arxiv.org/abs/2308.06721 (2023-08-13)

**E3 — Whether the reference image's ratio itself affects generation quality or anchoring: no first-hand evidence found.** No statement on the vendor side, and no ablation with "reference-image aspect ratio" as the independent variable was found on the paper side this time.

**E4 — Side effects of carrying a single-subject standing figure in a landscape 16:9 frame (lots of empty space left and right): no official or paper statement found.** All that can be offered are three adjacent facts, each of which needs its boundary stated:
1. Google's default inheritance from E2 — a 16:9 asset image pulls downstream outputs that don't specify a ratio to 16:9 by default (explicitly official, Google line only).
2. IP-Adapter's center square crop — the left and right sides of a landscape reference image don't enter the condition (paper/open-source implementation, cannot be extrapolated). For a centred single subject this is actually harmless; for **a horizontally arranged multi-panel layout, it means the panels on both sides are cropped off outright**.
3. The set of evidence in D4 — salient regions in the reference image other than the subject take part in conditioning. Whether empty space itself counts as a "salient region" — **no evidence whatsoever was found** this time, and no inference is made.

The claim that "large empty space induces the model to fill in background elements" — **no first-hand source whatsoever was found** within the scope of this research, neither an official statement nor empirical paper evidence.

---

## 6. Evidence strength grading

| # | Conclusion | Strength |
| --- | --- | --- |
| A1 | No vendor officially recommends "laying out multiple views of the same subject in one image" | Not found (checked vendor by vendor across 8 vendors, of which Midjourney/Jimeng/DashScope/MiniMax are "topic absent from the docs"; the three official single-image multi-panel use cases — OpenAI, BFL, Google — are all narrative panels) |
| A2 | Google's consistency approach is iterative angle-by-angle generation + multiple reference images (3 Pro: 6 object + 5 character + 3 style; 3.1 Flash: 10 object + 4 character; 3.1 Flash Lite: 14 object, no character channel) | Explicitly official |
| A3 | Google states explicitly "the model might not create the exact number of images you ask for" | Explicitly official |
| A4 | OpenAI officially supports and demonstrates single-image 4-panel narrative panels | Explicitly official |
| A5 | BFL officially requires comic panels to be **generated separately, panel by panel**, with consistency from repeating the character description in each panel | Explicitly official |
| A6 | Seedream files "set/series" requirements under multi-image output ("a series"/"a set"/specified count), naming storyboarding, comic, IP design | Explicitly official |
| A7 | Seedream group generation can keep characters and style consistent | Supported by papers (vendor technical report arXiv:2509.20427) |
| A8 | Google has a dedicated 360 view subsection for "multiple angles of the same character"; the official approach is separate images per angle, feeding already generated images back as references, not a single-image layout | Explicitly official |
| A9 | Google does have an official single-image multi-panel template ("Make a 3 panel comic"), limited to narrative panels | Explicitly official |
| B1 | An image prompt influences the content, composition and colors of the output | Explicitly official (Midjourney) |
| B2 | The best practice for character reference images is "a single-character image" | Explicitly official (Midjourney) |
| B3 | Multi-subject reference images need each subject identified in the prompt, and the vendor promises no result | Explicitly official (Midjourney, worded as "you can try") |
| B4 | Reference images and style instructions compete for influence; too high a weight makes results unpredictable | Explicitly official (Midjourney) |
| B5 | Video reference images are officially limited to "a single person, character, or product" | Explicitly official (Google Veo; no equivalent statement on the image side) |
| B6 | The prompt must explicitly identify "what to extract and retain" from the reference image | Explicitly official (Seedream) |
| B7 | When reference-image injection is not bound to a text concept, a copy-paste effect results, and the attention map does not focus on the target subject | Supported by papers (arXiv:2503.06568, open-source adapter architectures) |
| B8 | The above mechanism applies equally to closed-source commercial image APIs | **Inference** (no direct official or paper evidence; only B1 provides an official signal pointing the same way) |
| B9 | Official docs directly discuss the consequences of "using a multi-panel collage as a reference image" | Not found (not a single vendor) |
| C1 | Positive anatomical anti-artifact sentences (facial symmetry / five fingers / proportions) have official backing | Not found (none of the 8 vendors; for Midjourney limited to the three pages checked); the practice is classified as community lore without backing |
| C2 | OpenAI officially encourages writing "no watermark / no extra text / no logos" in the body | Explicitly official |
| C3 | Google image officially requires rewriting negation as positive description | Explicitly official |
| C4 | FLUX.2 does not support negative prompts; the vendor requires positive description | Explicitly official |
| C5 | For the wan2.7-image series, which doesn't support `negative_prompt`, Alibaba officially instructs writing `不要出现xxx` ("xxx must not appear") in the positive prompt | Explicitly official |
| C6 | The Seedream **image** prompt guide has no exclusion/constraint-word subsection (the existing report's "Volcengine encourages negation" conclusion comes from the Seedance video line) | Explicitly official (based on "the page has no such content") |
| C7 | Unrewritten in-body negation prompts on DALL·E 3 "invariably led to images showing X", accuracy 12.3% | Supported by papers (arXiv:2411.17066) |
| C8 | CLIP-family text encoders understand negation at close to chance level | Supported by papers (arXiv:2501.09425, CVPR 2025) |
| C9 | The negative_prompt parameter channel has a separate, explainable mechanism (Delayed Effect / Deletion Through Neutralization) | Supported by papers (arXiv:2406.02965) — note that this mechanism does not apply to the text-tail-sentence approach |
| C10 | "Mentioning reinforces" (writing it makes it more likely to appear) | Not found (papers support only "low compliance", not "reverse reinforcement") |
| C11 | Official positions of Midjourney / MiniMax / Jimeng on negative phrasing inside the prompt | Not found |
| D1 | Official vendor statements on what shot size (full body/half body/bust) a reference image should use | Not found (7 vendors checked page by page this round; Jimeng keeps the first-round "nothing relevant seen in the docs") |
| D2 | Under latent diffusion, a face that takes up too small a share of the image harms identity reconstruction (generation-side mechanism, not reference-image side) | Supported by papers (arXiv:2401.02804, DiffBody) |
| D3 | PhotoMaker enforces "face occupies >10% of the image after cropping" when building its ID training set | Supported by papers (arXiv:2312.04461) — extrapolation to inference-time reference images is an **inference** |
| D4 | "A 45° angle conveys three-dimensionality" has official or paper backing | Not found (zero official hits across 7 vendors this round, no controlled experiment on the paper side); classified as community lore without backing |
| D5 | Neutral poses such as A-Pose / T-Pose in the reference image make downstream re-posing easier | Not found (no ablation of this variable from vendors or mainline T2I papers) |
| D6 | The officially given pose-control path is **a separate pose reference image**, not inheritance from the identity reference image | Explicitly official (Google: "For complex poses, include a reference image of the selected pose.") |
| D7 | Under a weak-spatial-condition architecture, the reference image's pose is not transferred to the output (five-keypoint design) | Supported by papers (arXiv:2401.07519, InstantID) |
| D8 | Mainstream subject-driven methods **remove the reference-image background** before encoding (segmentation/masking/noise fill); removing this control degrades all metrics | Supported by papers (arXiv:2307.11410 Subject-Diffusion, arXiv:2312.04461 PhotoMaker, arXiv:2404.16022 PuLID) |
| D9 | Official vendor requirements on reference-image background (plain vs. with environment) | Not found (7 vendors checked page by page this round; Jimeng keeps the first-round conclusion) |
| D10 | 3–5 reference images is the mainstream setting; ablation over 1→5 images, optimum around 4 | Supported by papers (arXiv:2208.12242 DreamBooth and its supplementary material; arXiv:2208.01618 also 3–5 images) |
| D11 | More reference images improve ID fidelity, with the gain most pronounced from 1→2, and a trade-off with text controllability | Supported by papers (arXiv:2312.04461, DINO about 47.0→49.5, CLIP-T drops) |
| D12 | The benefit of mixing multiple same-identity images is **releasing the lock-in to a single image's pose/expression**, not raising ID similarity (N=1→4: ID-Sim 0.602→0.603, Expr-Div 37%→65%, yaw 12.90→16.47); **this N is a training-time hyperparameter, and at inference time the paper only duplicates a single image N times**, so extrapolating to the inference-time number of reference images is an **inference** | Supported by papers (arXiv:2403.13535, IDAdapter); the extrapolated part is an inference |
| D13 | Going from 1 to 3 reference images improves object identity fidelity (MDINOv2-I 0.777→0.822) | Supported by papers (arXiv:2502.01720, SynCD) |
| D14 | All the empirical evidence for "several images help" above is built on **several independent image inputs**; none is "multiple views stitched into one image" | Explicitly official + supported by papers (the input form in each paper; consistent with block A) |
| D15 | Vendors give only quotas, not advice, on reference-image count (Google 5 character images, BFL 8, Seedream 14, MiniMax 1 per request) | Explicitly official |
| E1 | Seedream officially gives a reference-image aspect ratio range `[1/16, 16]`, width and height >14px, ≤30MB, total pixels `[196, 36,000,000]` | Explicitly official (the only vendor in this research to give a numeric range for reference-image ratio) |
| E2 | Google: when a reference image is sent and no ratio is specified explicitly, **the output size/ratio follows the reference image by default** (original text "matches the output image size to that of your input image") | Explicitly official |
| E3 | Specific behaviour when the reference-image ratio differs from an explicitly specified output ratio (crop/pad/stretch/ignore) | Not found (no explicit statement from any of the 7 vendors this round, including Google's explicit-ratio branch) |
| E4 | In open-source adapter implementations, non-1:1 reference images are **resized by the short side + center square cropped**, and edge content does not enter the condition | Supported by papers (arXiv:2308.06721, IP-Adapter); whether it holds for closed-source APIs is an **inference** |
| E5 | The reference image's aspect ratio itself affects generation quality or identity anchoring | Not found (neither official nor paper evidence uses it as an independent variable) |
| E6 | Carrying a single-subject standing figure in 16:9 (large empty space) has side effects such as inducing the model to fill in background elements | Not found (no first-hand source at all) |
| E7 | Each vendor's output-side ratio options and hard constraints (Google 10 options; OpenAI gpt-image-2 long-to-short edge ratio ≤3:1; BFL 5 options, dimensions must be multiples of 16, ≤4MP; Seedream `[1/16,16]`; DashScope 1:8–8:1; MiniMax 8 options; Midjourney `--ar` does not accept decimals) | Explicitly official |

---

## 7. Relation to `arcreel-prompt-best-practices-research.md`

**What this adds (not covered at all by that report)**
1. **Block A as a whole**: none of the existing report's four research questions (how to write image prompts, how to write i2v prompts, the official position on negation, prompt language) concerns the **layout/composition form** of the output image. The positions on multi-panel/multi-view/sheet composition in this report are new.
2. **Block B as a whole**: the existing report discusses the first-frame image only in the i2v context ("the first frame already provides subject/scene/style, the prompt only describes motion"), and does not discuss composition inheritance and subject dilution in i2i reference images. This report adds the official positions on reference-image selection and paper evidence on adapter mechanisms.
3. **Vendor coverage**: the existing report's 8 vendors are those ArcReel has already integrated, **not including Midjourney and Black Forest Labs**. This report adds these two, of which BFL provides the most clear-cut official position on negation in this research (FLUX.2 does not support negative prompts).
4. **Positive anti-artifact sentences**: not covered by the existing report. After a vendor-by-vendor check, this report concludes "no official backing found".

**What this revises/refines**
1. **Volcengine's negation classification needs to be split by image/video line.** The existing report's Q3 put Volcengine in "Type C: vendors encourage writing prohibit/exclude constraint sentences directly in the body", based on the "constraint words" subsection of the Seedance 2.0 video prompt guide. This research checked the whole Seedream 4.0–4.5 **image** prompt guide (Last updated 2026-07-06) and **found no exclusion/constraint-word subsection**. The existing report's conclusion holds for Volcengine's video line and is **unsupported** for its image line. ArcReel's asset images go through the image line, so this boundary must be kept in mind when citing it.
2. **The existing report Q3's "common implicit baseline" can be tightened one notch.** The existing report says "negation is not a cure-all, and several vendors state explicitly that constraints are not 100% controllable"; the paper evidence added in this report quantifies "not a cure-all" to a concrete order of magnitude (accuracy of unrewritten negation prompts 12.3%).
3. **One key thing the existing report's Q3 did not distinguish**: the negative_prompt **parameter channel** and **textual negation** in the body of the prompt are two different mechanisms. The existing report's three-way classification is organised by "whether the vendor provides a negative channel / whether it recommends writing negation in the body", and does not discuss whether the effectiveness of one can be inferred from the other. This report's answer is that it can't: the mechanism explanation in arXiv:2406.02965 covers only the parameter channel; the existing implementation in `lib/prompts/prompt_builders.py` uses text tail sentences, so that paper does not vouch for it.
4. **Currency check**: the Google image best-practices page URL cited by the existing report (`cloud.google.com/gemini-enterprise-agent-platform/...`) now 301-redirects to `docs.cloud.google.com/...`; the content is still there, Last updated 2026-08-21 UTC, and the original text "Describe what you want, not what you don't" is unchanged. The OpenAI cookbook page has been updated to 2026-04-21, and the original text "State exclusions and invariants explicitly" is unchanged. These two items in the existing report's Q3 need no revision.

---

## 8. Direct implications for the four layout types in #2058

This section only states what the evidence above points to; it makes no ruling on keeping/deleting/reshaping.

**On the "multiple panels in one image" form itself**
- Evidence A1/A5/A6 points to: putting "multiple views of the same subject" into one image has, within the scope of this research, **no backing from any official guide**; the three vendors with official multi-panel sections (OpenAI, BFL, Google) all demonstrate **narrative panels**, and BFL's official approach is to generate each panel separately; Seedream officially directs "set design" requirements to multi-image output rather than single-image multi-panel.
- Evidence A2/A3/A8 points to: on the Google line, the official path for the same character across viewing angles is iterative angle-by-angle generation plus feeding multiple references back in; at the same time the vendor states explicitly that the number of images generated cannot be guaranteed — hard requirements on panel count such as "four panels" or "three views" are not something that can be promised under the official position.
- Evidence A9 points to: Google does have an official single-image multi-panel template, but its scope is limited to narrative panels (comic / storyboard), which is a different kind of requirement from an asset sheet's "same subject, multiple views"; on the same page, the vendor directs the latter to the iterative approach in A8.
- Relation to the issue's criterion "does each panel's composition have a downstream consumer": this research **does not cover** ArcReel's internal consumption chain and cannot provide external evidence for that criterion. What it can provide is a fact from the other side — even if a given panel has a consumer, laying it out in the same image is not the officially recommended implementation; what vendors recommend is multiple images / multiple reference images. These two points are independent, and whether the criterion holds is not decided by this report.

**On multi-panel images entering the i2i reference chain**
- Evidence B1 (Midjourney official: an image prompt influences composition) and B7 (paper: copy-paste effect + attention not focused on the target subject) point the same way: the reference image's overall layout and all salient regions in it take part in conditioning; the "panel layout inherited downstream" observed in #1803 is consistent with both.
- Evidence B2/B3/B5/B6 points to: the officially expected form of a reference image is **a single subject, explicitly identified in the prompt**. A multi-panel image satisfies neither — it has multiple salient regions, and the current templates do not identify which panel in the reference image is meant.
- The boundary of evidence B8 must be passed on as well: the paper evidence comes from open-source adapter architectures, and whether it holds for closed-source APIs is an inference.

**On the positive anti-artifact sentences in `_*_GUARD`**
- Evidence C1 points to: declarations such as symmetrical facial features / five complete fingers / well-proportioned limbs have no backing in any of the 8 vendors' official docs. The comparable official practices are OpenAI-style **visible compositional descriptions** ("full body visible, feet included", "hands naturally gripping the handlebars"), and Alibaba putting `多余的手指` ("extra fingers") in the negative channel.
- To be considered separately are `_SCENE_GUARD`'s `画面中没有人物出镜` ("no people appear in the image") and `_PRODUCT_GUARD`'s fidelity declaration: these two are not anatomical anti-artifact sentences but **exclusions in positively rewritten form**, which happen to share the shape of the Google/BFL official position "Describe what you want, not what you don't" — evidence C3/C4 supports this style; what it opposes is the "no X" form.

**On the `_NEGATIVE_TAIL_*` text tail sentences**
- The evidence is polarised, and the divergence is between vendors, not over time: OpenAI (C2) explicitly encourages it, Google (C3) and BFL (C4) explicitly require rewriting as positive, and Alibaba (C5) instructs writing it into the positive prompt when there's no parameter channel. **The effectiveness of the `画面避免：水印、多余文字、Logo` ("image should avoid: watermarks, extra text, logos") style depends on the target model**; ArcReel's "no backend lock-in, pure text concatenation" design means the same tail sentence is sent to models with opposite positions.
- Evidence C7/C8 points to: even on models that encourage writing exclusions, compliance with textual negation is far from reliable. C10 also caps this conclusion — there is no evidence that writing it is worse than not writing it.
- Evidence C9 points to: the module docstring's judgement that "image backends mostly silently drop negative_prompt" partly matches the official facts verified in this research (FLUX.2, MiniMax image-01 and wan2.7-image indeed have no such parameter) and partly doesn't (the qwen-image series has an officially supported `negative_prompt` field; Kling's field of the same name belongs to the video line and is not a counter-example on the image line). The cost of the decision to give up the parameter channel is giving up the separate-mechanism reduction capability characterised in arXiv:2406.02965.

**On the discipline "each of the four negative tail sentences is defined separately, and they are not merged even when their content is identical"**
- This research found no external evidence for or against that discipline — it is an internal convention of the repository for organising constants, unrelated to vendor positions. The only thing the evidence can offer is an adjacent fact: official positions split by **target model** (C2 vs. C3/C4/C5), not by **image type**; the current constants are split along the image-type dimension.

---

## 9. Assessment of support for shape and ratio decisions

This section answers only one question: **is the evidence above sufficient to directly choose a specific shot size / angle / pose / background / aspect ratio?** It makes no ruling on the shape itself.

**The assessment is split; item by item below.**

| Dimension | Can the evidence support a direct choice? | Basis |
| --- | --- | --- |
| Background (plain vs. with environment) | **Yes, with a clear direction** | D8 is the only set of evidence in this research where several independent papers agree in direction and include ablations: mainstream subject-driven methods all remove the background before encoding, and removing that control degrades all metrics; PuLID explicitly classes background as "ID-irrelevant, should keep the original model's behaviour". Same origin as B7's copy-paste effect. The only reservation is B8's boundary — the evidence comes from open-source architectures, and closed-source APIs are an inference. This item **can be set in direction without A/B testing**; testing is only needed to confirm the magnitude |
| Number of reference images / whether a single image | **Yes, but only at the level of "several independent images > one combined image"** | D10–D14 have quantitative ablations from four papers (of which IDAdapter's N is a training-time hyperparameter and cannot be read directly as an inference-time count). But the proposition they support is "several independent image inputs beat one", and **they do not support "how many views should be laid out inside an asset image"** — there is simply no corresponding experiment for the latter in the evidence. For the already settled premise "multi-panel → single image", this set of evidence is a reinforcement; for "after moving to single images, should several single images be produced", the evidence is supportive but it is a new shape question |
| Shot size (full body / half body / bust) | **No, must be tested** | D1 has zero direct evidence. The two adjacent items (D2/D3) only support a much weaker statement: the pixel share of the face is a real variable. It cannot rank "full body", "half body" and "bust", because the share is affected by both the output resolution and the subject's position in the frame |
| Angle (front / 45°) | **No, must be tested** | No source at all for D4; the 45° in the current templates is community lore. There is not even directional evidence on the paper side |
| Pose (A-Pose / natural pose) | **No, must be tested** | Zero evidence for D5. D6/D7 show only one thing: neither vendors nor mainstream architectures expect the pose to be inherited from the identity reference image (pose comes from a separate reference image / is stripped away with weak spatial conditioning). This **weakens the premise "the reference image's pose matters" itself**, but cannot be used to rank A-Pose against natural pose |
| Aspect ratio | **Cannot be chosen directly, but one official constraint must be taken into account first** | E3/E5/E6 all not found. The only hard official fact is E2: on the Google line, when a reference image is sent and no ratio is specified explicitly, the output ratio follows the reference image — this is a **downstream behavioural constraint**, meaning the choice of asset image ratio spills over into downstream calls that don't specify a ratio. Which ratio to choose still needs testing, but the fact that "the asset image ratio is the downstream default" holds without testing |

**Overall conclusion**: the evidence is sufficient to support **two things** — reference images should have irrelevant background removed (D8), and the benefit of multiple angles belongs to "several independent images" rather than "one multi-panel image" (D14); and it provides **one official constraint that must be built into the design** — the asset image ratio becomes the default output ratio downstream when no ratio is specified (E2). **For shot size, angle, pose, and which specific aspect ratio to choose — these four items — this research could find no first-hand evidence that supports deciding directly; they can only be decided by A/B testing.**

This split is itself explainable: shot size, angle and A-Pose are **industry conventions of art-asset pipelines** (character three-view drawings, turnarounds), not variables the diffusion-model literature would ablate; the variables the literature ablates are count, background handling and encoding granularity. A convention is not necessarily wrong, but under this research's source discipline it gets no first-hand backing, and **continuing to use it amounts to acknowledging that it is an unverified default**.

**If A/B testing is done, the only external input this report can provide is "what should be held fixed"**: in testing, background (removed) and reference-image form (single subject, single image) should be held constant, with only shot size / angle / pose / ratio as variables — because the first two already have evidence and the last four do not. Nor should the test's criterion be "does the asset image itself look good"; it should be the criterion shared by all the evidence in block D: **downstream generation's fidelity to identity, and the freedom of downstream pose/composition relative to the reference image** (corresponding to the paired-metric form of IDAdapter's ID-Sim and Pose-Div).
