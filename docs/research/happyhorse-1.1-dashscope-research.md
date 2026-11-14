# HappyHorse 1.1 (Alibaba Cloud Bailian DashScope) Video Generation Series Research

Research date: 2026-08-08. Sources are limited to Alibaba Cloud first-hand channels: the official `help.aliyun.com` documentation (highest authority) and official articles on the `developer.aliyun.com` Alibaba Cloud developer community (used for pricing press releases and version comparisons, second in authority). Items that could not be found are marked "could not be confirmed".

## 0. Is "happyhorse" an alias?

**Conclusion: it is not an alias; it is a real, public series developed in-house by Alibaba Cloud. High confidence.**

- The official API reference on `help.aliyun.com` has documentation pages built directly around `happyhorse-1.1-t2v` / `happyhorse-1.1-i2v` / `happyhorse-1.1-r2v` / `happyhorse-1.0-*` as model ids (see the sources in each section below).
- Alibaba Cloud developer community introduction: HappyHorse (快乐小马) is a native multimodal video generation model developed in-house by Alibaba (ATH Innovation Business Unit), with a grey-release test starting 2026-04-27, 15B parameters, single-stream Transformer, joint audio-video generation; the watermark text is fixed as "Happy Horse". Sources: <https://developer.aliyun.com/article/1731571>, <https://developer.aliyun.com/article/1731726>
- The `happyhorse-1.0-{i2v,t2v,r2v}` registered in this repository's `lib/config/registry.py` (720P ¥0.9/s, 1080P ¥1.6/s, 3–15s, r2v 9 reference images) match the official documentation item by item, i.e. the ids in the registry are the official ids, and no "real series" mapping is needed.
- The series was released after the knowledge cut-off of common models, so public searches must rely on material from after April 2026.

## 1. Summary Table of Conclusions

| Modality | model id | Resolution tiers | Duration | Capabilities | Pricing (RMB/second, list price) |
|------|----------|-----------|------|--------|------------------|
| Text-to-video | `happyhorse-1.1-t2v` | 480P / 720P / 1080P (default) | 3–15s integer, default 5 | No first frame; synchronised audio-video with sound; 9 ratio options | 480P 0.45 / 720P 0.9 / 1080P 1.2 |
| Image-to-video (first frame) | `happyhorse-1.1-i2v` | 480P / 720P / 1080P (default) | 3–15s integer, default 5 | First frame ✓, last frame ✗; aspect ratio follows the first frame, no ratio parameter; synchronised audio-video with sound | Same as above |
| Reference-to-video | `happyhorse-1.1-r2v` | 480P / 720P / 1080P (default) | 3–15s integer, default 5 | 1–9 reference images; no first frame; synchronised audio-video with sound | Same as above |
| First and last frame | **No 1.1 variant** | — | — | The official recommendation for first/last-frame scenarios is `wan2.7-i2v-2026-04-25` | — |
| Video editing | `happyhorse-1.0-video-edit` (1.0 only, no 1.1) | 720P / 1080P (default) | Output 3–15s (input video 3–60s) | Video + 0–5 reference images + text instruction, style transfer/local replacement | Could not be confirmed |

Promotion: the whole 1.1 series once had a limited-time 40% discount (2026-06-22 to 2026-07-06, 720P 0.54 / 1080P 0.72 RMB/second), while 1.0 got 20% off in the same period; that promotion has ended, and whether it will be renewed or a new promotion offered could not be confirmed. Free quota for new users: 10 seconds of video generation.

## 2. Details and Citations

### 2.1 Text-to-video `happyhorse-1.1-t2v`

Source: <https://help.aliyun.com/zh/model-studio/happyhorse-text-to-video-api-reference>

- The same page lists the two model ids `happyhorse-1.1-t2v` and `happyhorse-1.0-t2v` side by side.
- `resolution`: 480P / 720P / 1080P (default 1080P). Note: the parameter table is presented jointly for 1.1/1.0, so whether 480P applies to both versions could not be confirmed (the 1.1 launch press release only advertises 720P/1080P).
- `duration`: "[3, 15] 之间的整数,默认值为 5" (an integer between [3, 15], default 5).
- `ratio`: 16:9 (default), 9:16, 1:1, 4:3, 3:4, 4:5, 5:4, 9:21, 21:9.
- prompt: "长度不超过 5000 个非中文字符或 2500 个中文字符,超过部分将自动截断" (length must not exceed 5000 non-Chinese characters or 2500 Chinese characters; the excess is truncated automatically) — the same silent truncation behaviour as wan2.7.
- `watermark`: default `true`, watermark in the bottom-right corner, text fixed as "Happy Horse"; pass `false` to disable.
- Audio: the API parameter table has **no audio switch**; the official video model overview describes text-to-video as "通过文本生成有声视频" (generating video with sound from text), and the 1.1 feature press release describes "音画同步" (synchronised audio-video) native joint generation → inferred that audio is always on with no switch (consistent with the 1.0 description). Sources: <https://help.aliyun.com/zh/model-studio/video-generate-edit-model/>, <https://developer.aliyun.com/article/1749474>
- Available regions: North China 2 (Beijing), Singapore, Japan (Tokyo), Germany (Frankfurt), US (Virginia).

### 2.2 Image-to-video `happyhorse-1.1-i2v`

Source: <https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference>

- First frame ✓, last frame ✗ (neither 1.1 nor 1.0 supports a last frame; the documentation page title itself is "图生视频-基于首帧" (Image-to-video — based on first frame)).
- "图生视频的宽高比自动跟随输入首帧图像" (the aspect ratio of image-to-video automatically follows the input first-frame image), "不支持 ratio 参数" (the ratio parameter is not supported).
- Input image constraints: JPEG/JPG/PNG/WEBP; width and height both ≥300px; aspect ratio 1:2.5–2.5:1; ≤20MB.
- Resolution/duration/prompt/watermark same as t2v.

### 2.3 Reference-to-video `happyhorse-1.1-r2v`

Source: <https://help.aliyun.com/zh/model-studio/happyhorse-reference-to-video-api-reference>

- Reference images: "1~9 张" (1 to 9 images) (1.1 and 1.0 have the same limit).
- `input.media` elements `{type: "reference_image", url}`; the prompt refers to reference images with `[Image N]`.
- Resolution 480P/720P/1080P (default 1080P), duration 3–15s.
- No first-frame input (different from wan2.7-r2v's "first frame + reference images" form).

### 2.4 Video Editing `happyhorse-1.0-video-edit`

Source: <https://help.aliyun.com/zh/model-studio/happyhorse-video-edit-api-reference>

- **1.0 only**, no 1.1 variant. Input video MP4/MOV (H.264 recommended), 3–60s, long side ≤4096px, short side ≥360px, ≤100MB, >8fps; 0–5 reference images; output 3–15s, 720P / 1080P (default). Pricing could not be confirmed.

### 2.5 Pricing and Free Quota

- **1.1 list price: 480P ¥0.45/second, 720P ¥0.9/second, 1080P ¥1.2/second**, the same price for t2v/i2v/r2v (billed by output seconds). 720P/1080P source: <https://developer.aliyun.com/article/1750051> ("720P at 0.9 yuan/second and 1080P at 1.2 yuan/second"), consistent with back-calculation from the 40%-off promotional prices 0.54/0.72; 480P was verified by the maintainer from the Bailian console price list (the help.aliyun Model Catalog page is dynamically rendered and could not be scraped from the public page).
- Limited-time 40% off (2026-06-22 to 2026-07-06): 720P ¥0.54/second, 1080P ¥0.72/second. Sources: <https://developer.aliyun.com/article/1743014>, <https://developer.aliyun.com/article/1742909>
- **1.0 list price: 720P ¥0.9/second, 1080P ¥1.6/second** (consistent with the current values in this repository's registry). Source: <https://developer.aliyun.com/article/1731470>
- Free quota: "免费领取 HappyHorse 免费 10 秒视频生成" (claim 10 seconds of free HappyHorse video generation). Source: <https://developer.aliyun.com/article/1743014>

### 2.6 Differences from 1.0

Source: <https://developer.aliyun.com/article/1742909> (Alibaba Cloud developer community feature deep dive)

- 1080P unit price reduced: ¥1.6/s → ¥1.2/s (720P unchanged at ¥0.9/s).
- Stricter instruction following, higher fidelity to reference images (text/borders are rendered faithfully), improved lip-sync precision, relaxed content safety thresholds, faster output (1080P 15s takes about 12 minutes vs about 14 minutes for 1.0).
- Tiers: both versions are 3–15s; the API reference now lists a 480P tier (early 1.0 material only had 720P/1080P; which version the 480P tier belongs to could not be confirmed).
- **Deprecation plan: could not be confirmed**. As of the research date, the whole 1.0 series is still on sale in the official API reference and Token Plan documentation (and once had a 20%-off promotion at the same time as 1.1); no official end-of-sale/shutdown announcement was found.

## 3. Token Plan Coverage and Call Paths

Sources: <https://help.aliyun.com/zh/model-studio/token-plan-overview>, <https://help.aliyun.com/zh/model-studio/token-plan-multimodal-gen>, <https://help.aliyun.com/zh/model-studio/token-plan-personal-overview>, <https://help.aliyun.com/zh/model-studio/token-plan-faq>

### Covered Models

- The official personal edition documentation explicitly lists the video models included in the plan: `happyhorse-1.1-i2v` / `happyhorse-1.1-t2v` / `happyhorse-1.1-r2v` (for video models the personal and team editions are "双版本通用" (common to both editions)).
- The multimodal integration documentation uses `happyhorse-1.1-t2v` as the default model and gives call examples for `happyhorse-1.0-t2v` and `happyhorse-1.1-r2v` → the 1.0 series can also be called within the plan; the complete list is per the console's model list page.
- Wanxiang (wan) video series: not seen in the video examples of the plan's official docs; whether it is included in the plan **could not be confirmed** (the FAQ mentions that the `wan2.7-image` image model is within the plan's scope).

### Call Path: DashScope Native Async Path, Not compatible-mode

The video calls given in the official multimodal integration documentation are:

```http
POST https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis
GET  https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/tasks/{task_id}
```

That is, **the native two-step async flow, completely isomorphic to standard DashScope** (including the `X-DashScope-Async: enable` header), just with a different domain + the plan key. Only text chat goes through `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` (OpenAI protocol, also compatible with the Anthropic protocol).

### Plan Domain vs Standard dashscope.aliyuncs.com

| Dimension | Standard DashScope | Token Plan |
|------|----------------|-----------|
| Domain | `dashscope.aliyuncs.com` | `token-plan.cn-beijing.maas.aliyuncs.com` (North China 2 - Beijing only) |
| API Key | Regular Bailian key | Dedicated key with the `sk-sp-` prefix; personal edition/team edition keys are independent and cannot be mixed |
| Paths | Native `/api/v1/...` + `/compatible-mode/v1` | Both kinds of path are likewise supported; video uses the native async path |
| Billing | Pay-as-you-go | Unified deduction from Credits; **when the quota is used up, calls are blocked outright (429 Allocated quota exceeded), with no fallback to pay-as-you-go** |
| Consequence of mixing | — | Sending a plan key to `dashscope.aliyuncs.com` or mixing the other way → `401 InvalidApiKey` (the FAQ explicitly says "误用了百炼通用 Base URL" (mistakenly using the general Bailian Base URL) is a common cause of 401); does not silently switch to pay-as-you-go |
| Quota period | — | Team edition per-seat monthly quota (25k/100k/250k Credits) resets on expiry and does not carry over; personal edition rolling window (7-day limit, unused amounts within the window do not carry over) |

Credits conversion rate for video models (how many Credits are deducted per second/per tier): **could not be confirmed**; not disclosed in the official docs, per the console.

## 4. Suggested Additions to the ArcReel Registry/Backend

Checked against ADR 0018 (fail-loud: if `supported_durations` is not registered, reject; no implicit fallback):

### `lib/config/registry.py` (video section of the DashScope provider)

Add three entries, with fields isomorphic to the 1.0 entries:

- `happyhorse-1.1-t2v` / `happyhorse-1.1-i2v` / `happyhorse-1.1-r2v`
  - `supported_durations = list(range(3, 16))` (officially 3–15s integers, must be registered explicitly)
  - `resolutions = ["480p", "720p", "1080p"]`
  - `pricing = _dashscope_video_pricing(..., {"480p": 0.45, "720p": 0.9, "1080p": 1.2})` (list prices, 480P is the console-verified price; promotional prices don't go into the registry)
  - Suggest moving `default=True` from `happyhorse-1.0-i2v` to `happyhorse-1.1-i2v` (1080P is cheaper and the official overview presents 1.1 as the recommended series), and changing `lib/backends/video_backends/dashscope.py::DEFAULT_MODEL` accordingly
- Keep the three 1.0 entries (still on sale, no deprecation announcement); `happyhorse-1.0-video-edit` has no corresponding capability form in ArcReel's existing pipeline, so it is not registered for now

### `lib/backends/video_backends/dashscope.py::_MODEL_PROFILES`

- `"happyhorse-1.1-t2v": VideoCapabilities(first_frame=False)`
- `"happyhorse-1.1-i2v": VideoCapabilities(first_frame=True)`
- `"happyhorse-1.1-r2v": VideoCapabilities(first_frame=False, max_reference_images=9)`
- The invariant that no key is a substring of another still holds (`happyhorse-1.0-*` and `happyhorse-1.1-*` do not contain one another), so the substring-tolerant logic of `_profile_for_model` doesn't need to change
- Consider adding `max_prompt_chars` for the whole happyhorse series (officially: 5000 non-Chinese / 2500 Chinese; exceeding the limit is silently truncated and still billed as usual — the same kind of trap as wan2.7, which currently is the only one with a pre-payment gate). Note that happyhorse's limit is a dual threshold distinguishing Chinese and non-Chinese text; the existing single `max_prompt_chars` field cannot express the Chinese 2500 tier, so either extend the field first or register the conservative value 2500
- watermark: 1.1 still defaults to `true` (text "Happy Horse"); the backend must keep explicitly passing `watermark: false` when building the payload (same as for 1.0)

### Surroundings

- The `happyhorse` regex (3–15) in `lib/custom_provider/duration_presets.py` and the `"happyhorse"` substring routing (dashscope-async-video) in `lib/custom_provider/endpoints.py` naturally cover 1.1; no changes needed
- Don't put first/last-frame requirements on happyhorse (no last frame across the series); the official first/last-frame recommendation is `wan2.7-i2v-2026-04-25`
- Token Plan integration: video uses the native async path with an isomorphic schema, so in theory the existing `DashScopeVideoBackend` can be reused by switching `base_url` (`https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1`) + an `sk-sp-` key; however, quota exhaustion is a hard 429 block rather than a switch to pay-as-you-go, so if integrated, that error needs to be mapped to a user-readable quota message, and only the Beijing region is available
- The official source entry points for the HappyHorse models registered in the repository are recorded in `docs/api-docs/providers/dashscope.md`; this research keeps the page-by-page source list below and no longer maintains a copy of the official text

## Source List

Official documentation (help.aliyun.com):

- Text-to-video API: <https://help.aliyun.com/zh/model-studio/happyhorse-text-to-video-api-reference>
- Image-to-video (first frame) API: <https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference>
- Reference-to-video API: <https://help.aliyun.com/zh/model-studio/happyhorse-reference-to-video-api-reference>
- Video editing API: <https://help.aliyun.com/zh/model-studio/happyhorse-video-edit-api-reference>
- Video generation and editing overview: <https://help.aliyun.com/zh/model-studio/video-generate-edit-model/>
- Model Catalog: <https://help.aliyun.com/zh/model-studio/models> (dynamic page, prices could not be scraped)
- Token Plan overview / personal edition / multimodal integration / FAQ: <https://help.aliyun.com/zh/model-studio/token-plan-overview>, <https://help.aliyun.com/zh/model-studio/token-plan-personal-overview>, <https://help.aliyun.com/zh/model-studio/token-plan-multimodal-gen>, <https://help.aliyun.com/zh/model-studio/token-plan-faq>

Alibaba Cloud developer community (pricing press releases and version comparisons):

- 1.0 pricing: <https://developer.aliyun.com/article/1731470>
- Series introduction: <https://developer.aliyun.com/article/1731571>, <https://developer.aliyun.com/article/1731726>
- 1.1 price breakdown (40% off 0.54/0.72): <https://developer.aliyun.com/article/1743014>
- 1.1 feature deep dive/cost guide (promotion window, 1.0 comparison): <https://developer.aliyun.com/article/1742909>
- 1.1 feature introduction (synchronised audio-video, tiers): <https://developer.aliyun.com/article/1749474>
- Token Plan analysis (1.1 list prices 0.9/1.2): <https://developer.aliyun.com/article/1750051>
