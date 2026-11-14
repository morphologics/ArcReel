# README Demo Asset Guidelines

This document is not aimed at ordinary users; it gives maintainers the guidelines for the screenshots, animated images and finished showcase examples needed on the README home page.

The current README already uses:

- `docs/assets/screenshots/hero.webp` (hero screenshot)
- `docs/assets/screenshots/{video-units,props,scenes,ad-mode}.webp` (UI overview)
- `docs/assets/feishu-qr.png`
- `docs/assets/sponsors/`

Conversion on the home page of a product-style open-source project depends mainly on showing real results. ArcReel in particular needs to show both the "workbench" and the "final cut"; it cannot show only the settings page or a feature list.

## 1. Recommended asset directory

```text
docs/assets/
├── screenshots/
│   ├── hero.webp
│   ├── video-units.webp
│   ├── props.webp
│   ├── scenes.webp
│   └── ad-mode.webp
├── sponsors/
├── demo-workflow.webp
├── showcase-novel-cover.jpg
├── showcase-narration-cover.jpg
├── showcase-ad-cover.jpg
├── showcase/
│   ├── novel-input.png
│   ├── novel-character-sheet.png
│   ├── novel-storyboard.png
│   ├── novel-final.mp4
│   ├── narration-final.mp4
│   └── ad-final.mp4
└── feishu-qr.png
```

A GitHub README is not suited to hosting very large videos directly. Final videos can be:

- placed in a GitHub Release;
- uploaded via a GitHub Issue/PR to get a generated media link;
- placed on the project's official website or in stable object storage;
- linked from the README via a cover image.

## 2. Hero screenshot

### Goal

Without reading any text, users should understand:

- this is a complete workbench;
- there is an agent on the right;
- the middle shows the video production workflow;
- characters, storyboards or task status are visible.

### Recommended specs

- Aspect ratio: 16:9
- Recommended size: 1600 × 900
- Minimum width: 1200
- Format: PNG or high-quality WebP
- File size: preferably under 1.5 MB

### Screenshot content

Prefer showing a project that already has real data:

- project or asset navigation on the left;
- characters, storyboards or the timeline in the middle;
- the agent on the right;
- one visible piece of task or cost information.

Do not show:

- an empty project;
- large settings forms;
- real API keys;
- customer names;
- full novel text that has not been anonymized;
- irrelevant browser areas.

## 3. Workflow animation

### Goal

Show, in 15–30 seconds:

1. opening an existing project;
2. asking the agent to continue to the next stage;
3. viewing generation tasks;
4. opening the storyboard or video results;
5. showing version history or the Jianying (CapCut) export.

### Recommended specs

- Prefer animated WebP;
- Width: 1200–1600;
- Frame rate: 12–20 FPS;
- Keep it under 8 MB;
- Do not record long waits;
- Use editing to skip provider queueing.

The animation should highlight the "reviewable workflow", not just show button clicks.

## 4. Three finished showcase examples

The README should have at least three examples representing different scenarios.

### 4.1 Novel to AI comic drama

Record:

- input word count;
- creation type;
- number of storyboard shots;
- number of character assets used;
- total generation time;
- video duration;
- providers used;
- actual cost range;
- final video link.

### 4.2 Narration/commentary short video

Record:

- narration word count;
- number of storyboard shots;
- TTS voice;
- whether a Jianying (CapCut) draft was exported;
- final aspect ratio;
- actual cost range;
- final video link.

### 4.3 Product ad short

Record:

- number of product reference images;
- number of storyboard shots;
- video duration;
- whether reference-to-video was used;
- notes on product authenticity review;
- actual cost range;
- final video link.

## 5. Showcase copy template

```markdown
### Example name

[![Example cover](docs/assets/showcase/example-cover.jpg)](video link)

- **Input**: a novel excerpt of about 2,000 characters
- **Creation type**: dramatized story
- **Generation method**: storyboard image-to-video
- **Output**: 8 storyboard shots / 48 seconds / 9:16
- **Providers**: text A, image B, video C
- **Generation time**: about XX minutes
- **API cost**: about XX; prices and queue times may change
- **Editable output**: final video + Jianying (CapCut) draft
```

Costs must:

- state the currency;
- state that they apply only to that example;
- state that provider prices may change;
- not add up different currencies without explanation;
- not promise that all users will get the same cost.

## 6. Privacy and security check

Before publishing screenshots and videos, check:

- [ ] no API keys;
- [ ] no tokens;
- [ ] no real admin passwords;
- [ ] no internal base URLs;
- [ ] no customer names;
- [ ] no unauthorized novel content;
- [ ] no personal file paths;
- [ ] no browser account avatars or notifications;
- [ ] product and brand materials are authorized for display;
- [ ] sample music, fonts and images can legally be shown.

## 7. Adding to the README

Once the demo assets are done, it is recommended to add below the hero image:

```markdown
<p align="center">
  <a href="full demo video link">Watch the 90-second full demo</a>
  ·
  <a href="#showcase">See finished examples</a>
</p>
```

And after "Suitable creative scenarios", add:

```markdown
## Showcase

| Novel to AI comic drama | Narration/commentary short video | Product ad |
|---|---|---|
| Cover and video link | Cover and video link | Cover and video link |
```

When there are no real, publishable examples, do not use placeholder videos, fictional costs or model promotional material to pass them off as ArcReel output.
