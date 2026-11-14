# Research: duration ratios for the eight-section framework of product-selling short videos

> Purpose: the basis for the per-duration-tier ratio numbers in the script-generation prompt of ad/short mode (content_mode=ad). The four-tier ratio table has been approved by the maintainers (2026-06-11); this document keeps the research source chain and inference annotations so later calibration and new tiers can build on it. Method: read through the coreyhaines31/marketingskills repo (50 skills) + cross-checked against public industry material.

## 1. Inventory of the marketingskills repo

The contents of the repo (https://github.com/coreyhaines31/marketingskills) relevant to this task are below. **The repo has no ad duration ratio table broken down into 15/30/60/90-second tiers** — it is a SaaS-marketing-oriented skill set, and the closest thing is two short-video structure templates with second ranges in the social skill. There are quite a few transferable structural principles:

| Skill (path) | Applicable lessons |
|---|---|
| **social** (Short-Form Video section of `skills/social/SKILL.md`) | Most directly relevant. ① "3-second rule": visual hook + spoken hook + text hook all at once, landing in the first second; ② optimal platform durations: TikTok 15-60s / Reels 15-30s / Shorts 30-60s; ③ **Problem-Solution structure (15-30s): hook 0-3s → pain amplification 3-10s → solution 10-25s → CTA 25-30s** (direct skeleton for the 30-second tier); ④ common mistakes: hook set-up too slow, not cutting shorter when possible, no CTA |
| **social/references/short-form-video.md** | ① Hook library in four types (curiosity/value/story/controversy), can be fed directly into the hook-section prompt; ② the script template is fixed as "Hook 0-3s + Body + **CTA in the last 3-5s**"; ③ Story Arc (45-60s): hook 0-3 / set-up 3-15 / process 15-45 / result 45-55 / CTA 55-60; ④ **shot rhythm: B-roll fast cuts 1-3s/shot, slideshow 2-4s/slide** (consistent with the 2-6s per-shot constraint) |
| **ads** (`skills/ads/SKILL.md` + `references/ad-copy-templates.md`) | ① PAS (problem → agitate → solve → CTA), BAB, Social Proof Lead, **Direct Response formula (bold promise → proof → CTA with urgency)** — the latter is the basis for the trust→price_promo→cta order; ② CTAs come in three tiers, soft/hard/urgent, chosen by goal; ③ **testing priority: the hook has the biggest impact on performance**, followed by headline, selling points, CTA |
| **ad-creative** (`skills/ad-creative/SKILL.md`) | ① Before writing creative, settle on 3-5 "angles" (pain/outcome/social proof/urgency/identity……), which map one-to-one onto the eight-section taxonomy; ② awareness of platform specs such as TikTok ad copy being limited to 80 characters |
| **marketing-psychology** (`skills/marketing-psychology/SKILL.md`) | ① AIDA (attention → interest → desire → action) supports the eight-section order; ② "build trust → authority + social proof" (material direction for the trust section), "create urgency → scarcity + loss aversion" (how to write the price_promo section); ③ price presentation techniques (Rule of 100, mental accounting "1 yuan a day") can go into the price_promo prompt |
| **video** (`skills/video/SKILL.md`) | Leans toward the production toolchain and gives no input on ratios; but three points — "**85% of social video is watched muted**, 9:16, AI models can't render legible text" — are useful constraints for shot prompt generation |
| **copywriting** (`references/copy-frameworks.md`) | Headline formulas ("{outcome} without {pain}", etc.) can be reused as hook line templates; the landing-page section order (problem → solution → social proof → FAQ → CTA) again confirms the section order |

**Distilled transferable principles** (repo evidence → how the ratio table uses it):

1. **Hook and CTA are "absolute-duration" sections**: in every template, whether 15s or 60s total, the hook is always 0-3s and the CTA always the last 3-5s — a longer video does not scale the head and tail proportionally.
2. **All the added time goes to the middle** (solution/demo/proof), which is the only stretchable region between tiers.
3. **The order pain → solution → proof → action does not change with duration**; shorter tiers "cut sections" rather than "reorder".
4. **Fast cuts of 1-4s per shot, with a new piece of information every few seconds** is the underlying rhythm of short video.

## 2. Key industry findings (with sources)

**Platform official / official data**

| # | Finding | Source |
|---|---|---|
| 1 | TikTok official: among the videos with the highest CTR, **63% show the core message or product within the first 3 seconds** | Official TikTok PDF "9 Creative Tips to drive performance" https://ads.tiktok.com/business/library/Auction_Ads_Creative_Tips.pdf (cited via: https://lebesgue.io/tiktok-ads/how-to-increase-tiktok-ctr-9-creative-tips ) |
| 2 | TikTok official: **the first 6 seconds capture 90% of ad recall**; recommends a three-part hook–body–close structure | TikTok For Business blog https://ads.tiktok.com/business/en/blog/creative-best-practices-top-performing-ads (fetching this domain directly was blocked by certificate validation; the numbers were cross-confirmed via https://www.stackmatix.com/blog/tiktok-hook-first-3-seconds and https://www.2pointagency.com/glossary/tiktok-creative-best-practices-the-3-second-rule/ ) |
| 3 | TikTok data: **videos with multiple scenes/camera angles convert 38% better** and get 40%+ more impressions; **99% of e-commerce hit videos used multiple scenes and angles**; 93% of hits have audio | https://tinuiti.com/blog/paid-social/tiktok-best-practices/ (citing official TikTok data; original https://ads.tiktok.com/business/en-US/blog/creative-that-drives-conversions ) |
| 4 | TikTok data: **1/4 of the best-performing videos are 21-34 seconds long** (impressions +1.6%) | https://creatify.ai/blog/tiktok-ads-complete-guide-to-creating-high-performing-creatives-in-2026 (citing TikTok data) |
| 5 | Meta: Reels ads max out at 90s; the sweet spot is 15-60s, **beyond 30s it is noticeably hard to hold attention on FB Reels**; the ideal length for most campaigns is 6-15s; **the product/pain/outcome should be in frame within the first 0.3-2 seconds** | https://www.jonloomer.com/meta-video-ad-length-requirements/ , https://thedesignsfirm.com/en/blog/facebook-video-ad-length , https://www.brandwatch.com/blog/facebook-video-ads-best-practices/ , official specs https://www.facebook.com/business/help/817989058548892 |

**Douyin / Chinese product-selling**

| # | Finding | Source |
|---|---|---|
| 6 | Share of the four blocks in a hit product-selling script: **opening 0-3 seconds pain-point question → next 20% of the duration reinforces the need → middle 50% walks through selling points + product showcase → final 10% gives "the reason you must buy + perks/price advantage" to close the sale** | Qinggua Media, "Breaking down the script structure of hit product-selling short videos" https://www.opp2.com/317828.html (fetched directly and verified) |
| 7 | Golden 3 seconds: **over 50% of users decide whether to swipe away in the first 3 seconds**; three hook paradigms (suspense / pain point / benefit up front) | Chanmama, "Methodology for optimizing the golden 3 seconds in Douyin e-commerce" https://www.chanmama.com/yunyingquan/article/1428.html (fetched directly and verified) |
| 8 | 3-second retention ≥58% is the healthy line, overall completion ≥32% (hits 45%+); **place a point of interest every 5 seconds**; **beginners should start practising with 30 seconds; 90 seconds demands a lot of pacing control** | https://www.cnblogs.com/huizhudev/p/19148789 (industry experience post, not an official position) |
| 9 | E-commerce product-selling videos are mostly **around 15 seconds, capped at 30 seconds**; product highlights/price/promotion should be visible within 8-15 seconds; **price is the most important factor in placing an order**, so the price advantage goes at the end to close the sale | https://www.changbiyuan.com/douyin/duanshipin/2022/duanshipin_1003/54743.html |

**UGC / Direct Response structure**

| # | Finding | Source |
|---|---|---|
| 10 | UGC direct-response formula **Hook → Problem → Solution → Value Prop → Social Proof → CTA**; hook capped at 2-3 seconds; "15s compresses the middle, 30s balanced, 60s expands the demo" | https://motionapp.com/blog/how-to-write-ugc-ad-scripts (fetched and verified) |
| 11 | **60-second UGC five-section timing: hook 0-3 / pain 3-10 / solution 10-25 / social proof 25-45 / CTA 45-60**; change the visual every 5-8 seconds; write ≥5 hooks per script and test them separately | https://www.retiplex.com/blog/ugc-ad-script-guide (fetched and verified) |
| 12 | **90-second e-commerce demo structure: benefit hook 0-5 / guided demo 6-75 (selling points proven while demonstrated, social proof woven into the middle) / urgent CTA 76-90**; **90-second narrative: character and conflict 0-15 / process 16-75 / reveal + CTA 76-90**; 30-second social ad: hook 0-3 / value 4-25 / CTA 26-30; **15-second TikTok-native: flashy opener 0-1 / high-density value 2-12 / curiosity gap + CTA 13-15** | https://shortgenius.com/blog/ad-script-example (fetched and verified) |
| 13 | Explainer video script ratios **Problem 30% / Solution 40% / Proof 20% / Action 10%**; speech-rate conversion **150 English words/minute**: 30s=60-75 words, 60s=140-160 words, 90s=210-230 words | https://vidico.com/news/explainer-video-script-examples/ (fetched and verified) |
| 14 | UGC voice-over **about 75-120 words for 30 seconds**; the first 3-5 seconds decide everything | https://billo.app/blog/ugc-scripts/ (fetched and verified) |
| 15 | VSL experience: **the hook carries about 80% of the weight for success or failure**; "the biggest drop-off happens in the first 10-30 seconds"; the proof section (real customer results with concrete numbers) directly carries conversion; the 2026 trend is the 30-90 second "micro-VSL" | https://www.blog.theperformers.io/p/video-sales-letters-ads , https://adlibrary.com/guides/vsl-ads-ecommerce-guide |
| 16 | Direct-response UGC recommends 15-30 seconds; "the most common mistake is too long a set-up" | https://www.rathlymarketing.com/faq/best-ugc-ad-structure/ (fetched and verified) |

## 3. Four-tier ratio table (approved)

**General rules** (apply to all tiers; written into the script-generation prompt together with the ratio table):

- hook and cta are **absolute-duration sections** (hook 2-4s, cta 3-6s) and do not scale proportionally with the tier; added seconds go first to selling_point/demo, then trust (basis: #10, #11, #12, repo principles 1-2).
- price_promo always sits right before cta, forming a "closing block" (basis: #6, #9, the Direct Response formula from the ads skill).
- Even if the hook is not a product shot, **the product should be in frame within the first 3 seconds** (text/close-up/hand-held all count) (basis: #1, #5).
- Any single section over 6 seconds must be split into multiple shots; the whole film averages 3-5 seconds/shot, and the opening may use 2-3-second fast cuts (basis: #11 change visual every 5-8s, #8 a point of interest every 5s, repo B-roll fast cuts 1-3s).
- Better more shots than fewer: multiple scenes and angles have official backing for higher conversion (basis: #3).

### Tier 1: 15 seconds (impulse buy / paid-traffic ad, 5-6 shots)

| section | Seconds | Cumulative | Shots | Notes |
|---|---|---|---|---|
| hook | 3 | 0-3 | 1 | Pain-point question or outcome-first opener; **the hook doubles as pain_point** |
| product_reveal | 2 | 3-5 | 1 | Product enters frame + name |
| selling_point | 3 | 5-8 | 1 | Only 1 core selling point |
| demo | 4 | 8-12 | 1-2 | 1 usage scenario / before-and-after comparison |
| cta | 3 | 12-15 | 1 | Call to action; **may carry one promotional line, doubling as price_promo** |

**Cut**: pain_point (folded into the hook — in PAS the problem already sits in 0-3s, see #6, #7), trust (15 seconds cannot fit a standalone proof section, see the 15s form in #12), price_promo (folded into one line of the cta, see #12 "curiosity gap + CTA 13-15").
**Basis**: the overall skeleton aligns with #12's 15-second TikTok-native form (0-1 opener / 2-12 high-density value / 13-15 CTA): the reveal+selling+demo in seconds 3-12 of this table total 9 seconds, i.e. the "high-density value block"; hook 3s and cta 3s come from the repo's social skill template and #1. Shot count = 15s ÷ 2.5-3s fast cuts.

### Tier 2: 30 seconds (standard product-selling slot, default recommended tier, 8-10 shots)

| section | Seconds | Cumulative | Shots | Notes |
|---|---|---|---|---|
| hook | 3 | 0-3 | 1 | All three hooks at once (visual + spoken + stylized on-screen text) |
| pain_point | 4 | 3-7 | 1-2 | Amplify the pain point, relatable scene |
| product_reveal | 3 | 7-10 | 1 | Product enters as "the answer" |
| selling_point | 6 | 10-16 | 2 | 1-2 selling points |
| demo | 6 | 16-22 | 2 | Hands-on / on-face / real test |
| trust | 3 | 22-25 | 1 | One-line social proof (sales/ratings/before-and-after), can be an on-screen text overlay |
| price_promo | 2 | 25-27 | 1 | Price card / flash of the discount |
| cta | 3 | 27-30 | 1 | Call to action + urgency |

**Cut/shrink**: all eight sections kept, but trust and price_promo compressed into "one-line shots"; if the product has a very low price point and no credible endorsement, **cut trust first** and give its seconds back to demo.
**Basis**: the skeleton strictly follows the repo social skill's Problem-Solution 15-30s template (hook 0-3 / pain 3-10 / solution 10-25 / CTA 25-30) — in this table 3-10 is pain+reveal, 10-25 is selling+demo+trust, 25-30 is price+cta, so the three block boundaries coincide exactly; it also matches #12's 30s form. The reasons for keeping trust as a standalone beat are in #16 and #15 (proof carries conversion). 21-34s is the optimal duration band in TikTok data (#4); this is the default recommended tier.

### Tier 3: 60 seconds (complete persuasion chain, 13-16 shots)

| section | Seconds | Cumulative | Shots | Notes |
|---|---|---|---|---|
| hook | 3 | 0-3 | 1 | Same as above |
| pain_point | 7 | 3-10 | 2 | Pain point dramatized as scenes (1-2 concrete situations) |
| product_reveal | 5 | 10-15 | 1-2 | Entrance + what it is + who it's for |
| selling_point | 12 | 15-27 | 3 | 2-3 selling points, one shot of 4-6s each |
| demo | 15 | 27-42 | 3-4 | Multi-angle demo / before-and-after / data visualization |
| trust | 8 | 42-50 | 2 | Review screenshots + sales/certifications, two shots |
| price_promo | 5 | 50-55 | 1-2 | Original-price anchor → final price → limited-time perks |
| cta | 5 | 55-60 | 1 | Call to action + restate the core benefit |

**Trade-offs**: all eight sections kept, each as its own block; nearly all the extra time in 60 seconds goes to selling_point+demo (27s together, 45%).
**Basis**: pain 3-10, hook 0-3 and the closing block are taken directly from #11 (the retiplex 60s template); the difference from #11 is thinning its 20-second social proof block (trust 8s) and giving the seconds to demo — because #10 says "60s expands the demo" and in #6 the middle 50% is "walking through selling points + product showcase" (selling+demo in this table = 27s = 45% ≈ Qinggua's 50%). Ratio check: pain 12% / solution block 53% / proof 13% / closing 17%; compared with #13's 30/40/20/10 — lighter on pain, heavier on closing, because the target scenario is direct-response product selling rather than an explainer video, so the closing share moves toward #6's "final 10% + price advantage" and the DR formula.

### Tier 4: 90 seconds (narrative / high price point, 18-22 shots)

| section | Seconds | Cumulative | Shots | Notes |
|---|---|---|---|---|
| hook | 4 | 0-4 | 1 | Can use a suspense/story hook ("3 months ago I was still……") |
| pain_point | 10 | 4-14 | 2-3 | Short narrative with character + conflict |
| product_reveal | 6 | 14-20 | 1-2 | Turning point: meeting the product |
| selling_point | 20 | 20-40 | 3-4 | 3 selling points, developed one by one |
| demo | 24 | 40-64 | 4-6 | Usage process across multiple scenes (core block) |
| trust | 12 | 64-76 | 2-3 | User testimonials / test reports / sales |
| price_promo | 8 | 76-84 | 2 | Price anchor + breakdown of the limited-time offer |
| cta | 6 | 84-90 | 1 | Close with urgency |

**Trade-offs**: all eight sections kept; compared with 60s, the extra goes to demo (+9), selling_point (+8), trust (+4), pain_point (+3).
**Basis**: the skeleton aligns with #12's two 90s templates — the narrative form's "character and conflict 0-15" (hook+pain = 0-14 in this table), the demo form's "guided demo 6-75" (reveal+selling+demo = 14-64 in this table as the demo body, with trust 64-76 woven in after it), and both templates' shared "urgent CTA 76-90" (price_promo+cta = 76-90 in this table, coinciding exactly). **Risk note**: #5 (>30s struggles to hold attention on FB Reels, 90s is the Reels maximum) and #8 (90 seconds demands a lot of pacing) both indicate this tier suits only high-price/education-needed products; the prompt should require the 90s tier to be organized as a "short story" rather than a flat list of selling points, and keep a new piece of information every ~5 seconds.

**Appendix: voice-over word count reference** (constrains line length): English 15s≈40 words, 30s≈60-120 words (#13, #14), 60s≈140-160 words, 90s≈210-230 words (#13). For Chinese, at ~4 characters/second, roughly 15s≈60 characters / 30s≈120 characters / 60s≈240 characters / 90s≈360 characters — **this conversion is an inferred value with no direct source; calibrate it against measured TTS speech rate**.

## 4. Sources and inference annotations

**Numbers with a direct source**:

- hook always 0-3s (consistent across sources: #1/#6/#7/#11/#12/repo social skill); cta always the last 3-6s (#11/#12/repo short-form-video.md).
- The three main block boundaries of the 30s tier (0-3 / 3-25 / 25-30): given directly by two sources, the repo's Problem-Solution template and #12.
- The 60s tier's pain 3-10 and the start of the closing CTA block: seconds from the original text of #11.
- The 90s tier's 0-15 conflict block and 76-90 closing block: seconds from the original text of #12.
- 2-6s per shot / 2-3s fast cuts: repo B-roll 1-3s, slideshow 2-4s, #11 change visual every 5-8s, #8 a point of interest every 5s.
- "Show the product in the first 3 seconds" (#1's 63%), "first 6 seconds 90% recall" (#2), "multiple scenes 38% conversion lift" (#3), "21-34s optimal band" (#4), the share skeleton 3s/20%/50%/10% (#6) and 30/40/20/10 (#13), word count conversions (#13/#14).

**Inferences based on structural principles (no single direct source)**:

1. **All per-section values precise to the second** (e.g. 30 tier trust=3s, price_promo=2s; 60 tier reveal=5s; 90 tier selling=20s, etc.): industry sources only give ranges for 3-5 coarse blocks; this table interpolates the coarse block boundaries according to the eight-section taxonomy — the interpolation principle is "keep coarse block boundaries fixed, split evenly within a block by number of selling points/shot length". Each tier summing exactly to its duration is a normalization made so the prompt is executable.
2. **The decision to cut three sections in the 15-second tier**: industry 15s templates have only 3-4 sections, never 8; cutting pain_point/trust/price_promo rather than other sections is reasoned from: in PAS the problem already occupies 0-3s (the hook can double as it), the promo can be compressed into one line within the CTA (the final part of #12's 15s form is a merged "curiosity gap + CTA"), while product/selling point/demo are the minimal persuasion units a product-selling video cannot cut (#9: product highlights must be visible within 8-15 seconds).
3. **Shot count ranges**: calculated as "tier duration ÷ 2-6s per shot", leaning toward the middle-to-high values (multiple scenes have official backing in #3); not given directly by any source.
4. **The 60/90 tiers compress the retiplex-style 20-second social proof block to 8-12s**: a compromise between #11 (proof-heavy) and #6/#10 (demo-heavy), leaning toward the latter because the target scenario is product-selling clips made via image-to-video, where demo visuals are more generatable; this is a product-oriented judgment, approved by the maintainers together with the ratio table.
5. **Chinese characters/second**: inferred by analogy from the English 150 words/minute; no authoritative source for Chinese speech rate was found.

**Not found / not verified**: the marketingskills repo has no tiered duration ratio content at all (inventoried as is); the two URLs of the official TikTok blog could not be fetched directly due to certificate problems, and the related numbers were all cross-confirmed via ≥2 third-party citations; "3-second retention ≥58%" comes from an industry experience post (#8), not an official Douyin position, so when citing it, downgrade it to a reference value.
