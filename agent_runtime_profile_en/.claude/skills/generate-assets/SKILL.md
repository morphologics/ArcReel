---
name: generate-assets
description: >-
  Unified asset-generation skill: accepts `--type=character|scene|prop|product`, or, when omitted, automatically scans all pending (missing sheet) resources and dispatches them by type. Use when the user says "generate character images"/"generate scene images"/"generate prop images"/"generate product images", wants to create reference images for new assets, or when an asset is missing its *_sheet.
---

# Generate Asset Sheets

Create asset sheets for the project's characters, scenes, props and products, keeping visual elements consistent across the whole video.
The image provider is chosen by project settings (no specific backend is locked in).

> For prompt-writing principles, see the "Prompt language" section of `.claude/references/generation-modes.md`.

## Shared conventions

- Every asset `description` is written as a **narrative paragraph**, not a keyword list.
- The user only maintains `description` in project.json; the full prompt handed to the image backend
  (including layout / anti-artifact phrases / negative prompt) is assembled server-side by `lib/prompts/prompt_builders.py`,
  so the WebUI and the Skill share a single source of truth.
- Pending rule: the asset sheet's status in the Artifact Manifest is `missing` (including a registered file that cannot be read); `stale` artifacts are reused and do not count as pending.
- Derivatives (`base/derivative`) go in the same batch as their base look: when the base look is also pending, the derivative is submitted only after the base image succeeds; if the base look fails, the derivative reports `generation_dependency_failed` and is not submitted.

---

## Character (character)

### description writing guide

Describe appearance, clothing and bearing in a coherent paragraph, including age, build, facial features and costume details.

**Example**:

> "A woman in her early twenties, slender, with clear almond eyes in an oval face; her willow-leaf brows carry a hint of melancholy when slightly knit. She wears a pale celadon embroidered gauze skirt, tied at the waist with a silk ribbon of the same color, looking dignified yet lively."

### Output layout

Landscape 16:9 three-view sheet on a pure white background: front / full side (90° profile) / back, arranged horizontally.
The character's face, hairstyle, clothing and accessories must stay exactly identical across all three panels.

> When filling in description, the user only needs to care about appearance / clothing etc.; the layout is injected by the builder.

---

## Scene (scene)

### description writing guide

Describe form, lighting and atmosphere in a coherent paragraph, highlighting distinctive features that are recognizable across shots.

**Example**:

> "A century-old locust tree at the village entrance, its trunk so thick it takes three people to encircle, the bark cracked and weathered. A prominent lightning scorch mark winds down the main trunk from the top. The canopy is dense, casting dappled shade in summer."

### Output layout

Landscape 16:9 single panoramic establishing shot of the environment.

---

## Prop (prop)

### description writing guide

Describe form, texture and details in a coherent paragraph, highlighting distinctive features that are recognizable across shots.

**Example**:

> "An emerald-green ancestral jade pendant, about the size of a thumb, the jade warm, smooth and translucent. Its surface is carved with an exquisite lotus pattern, petals unfolding layer upon layer. A red silk cord is tied to the pendant in a traditional Chinese knot."

### Output layout

Landscape 16:9 single prop asset sheet on a clean light-gray background.

---

## Product (product)

### description writing guide

Describe the product's appearance, brand text, color scheme, materials, proportions and structure in a coherent paragraph.

### Output layout

Landscape 16:9 single product asset sheet on a clean light-gray background with even studio lighting.

---

## Tool calls

Enqueue via MCP tools:

| Operation | Tool |
|------|------|
| List all / one type of pending | `mcp__arcreel__list_pending_assets({"type": "character"})` (type may be omitted) |
| List pending referenced by this episode | `mcp__arcreel__list_pending_assets({"episode_id": 3})` |
| Generate all pending referenced by this episode (incl. products and derivatives) | `mcp__arcreel__generate_assets({"episode_id": 3})` |
| Generate all pending (one round per each of the four types) | `mcp__arcreel__generate_assets({})` |
| Generate all pending of one type | `mcp__arcreel__generate_assets({"type": "character"})` |
| Generate several specified | `mcp__arcreel__generate_assets({"type": "prop", "names": ["玉佩", "密信"]})` |
| Generate a single one | `mcp__arcreel__generate_assets({"type": "scene", "names": ["村口老槐树"]})` |
| Generate a single product | `mcp__arcreel__generate_assets({"type": "product", "names": ["保温杯"]})` |
| Generate a derivative | `mcp__arcreel__generate_assets({"type": "character", "names": ["张三/战损"]})` |

`episode_id` is not combined with `type` / `names`; "which assets this episode references" is computed server-side, the same list as the Web episode view.

Results are returned per ID under `requested / succeeded / failed / blocked / skipped`, with IDs like `character/张三` (derivatives: `character/张三/战损`);
stale but reusable old images go into `skipped` and are not regenerated automatically;
decide the next step from each item's own `problem.code` and `problem.action`; do not parse the text.
See `.claude/references/generation-results.md` for the structure.

## Workflow

1. **Load project metadata** — find assets whose asset sheet status is `missing` in the Artifact Manifest
2. **Enqueue generation tasks** — submit only asset names; at execution the server renders from the description stored in the project (`lib.prompts.prompt_builders` injects layout / anti-artifact / negative). If an original image is declared but cannot be read, generation is rejected (`asset_original_missing`); ask the user to re-upload the original image or clear the original-image field
3. **Review checkpoint** — following "Review checkpoint: review images", dispatch the review-images subagent to look at the images, and hand the report and regeneration suggestions to the user for confirmation; the user may approve, request regeneration, or request edits
4. **Update project.json** — update the `character_sheet` / `scene_sheet` / `prop_sheet` / `product_sheet` paths

## Review checkpoint: review images

After asset sheets are generated, first have the read-only review-images subagent `review-images` look at them, then hand its conclusions to the user for confirmation. Image review never triggers generation or editing and incurs no generation cost; the subagent's model calls for viewing images count toward Agent session usage as usual.

1. **Dispatch**: dispatched by the main Agent (subagents cannot dispatch subagents: when this skill runs inside a generation subagent, the generation results go back to the main Agent, which then dispatches). Dispatch only the assets generated successfully in this round, each group a single type with at most 20 images; with multiple groups, issue one `review-images` call per group in the same message so the groups run in parallel. The dispatch message states the image type (`character` / `scene` / `prop` / `product` / `character_derivative`), the asset names in this group (derivatives written as "base name/derivative name") and the user's concerns.
2. **Hand to the user**: summarize the groups' reports for the user: which are usable, which are suggested for regeneration or editing and why, which are doubtful. When any asset is suggested for regeneration, editing, or is doubtful, use AskUserQuestion multi-select (`multiSelect: true`) to have the user tick the assets to handle; each option states the asset name, the reason and the suggested handling. Do not regenerate or edit before the user confirms.
3. **Execute**: handle the ticked assets per the next section "Edit vs regenerate". When done, you may dispatch `review-images` again on the new versions for acceptance.

When the user asks follow-up questions about specific asset sheets, use `mcp__arcreel__inspect_images` to look at the images directly and answer, without dispatching a subagent.

## Review checkpoint: edit vs regenerate

When the user comments on an asset sheet, first determine the kind of request; picking the wrong path will overturn parts they were happy with or lose unexpected changes:

- **Only wants a local change** (change hair color, remove clutter, adjust lighting mood, etc.), and is happy with the composition and overall design → use
  `mcp__arcreel__edit_images({"resource_type": "character", "edits": [{"id": "张三", "instruction": "把头发改成红色"}]})`
  to fine-tune on top of the existing image; multiple assets of the same type can be sent in one batch
- **Wants to overturn the composition / overall design and start over**, or actually needs to change the description (which in turn changes the results later
  regenerated from description) → use `generate_assets` to regenerate the whole image from the updated description
- Editing does not update `description` / prompt — triggering `generate_assets` after an edit still redraws
  from the original description, and the edited result can only be recovered from version history

## Quality checks

- **Character**: face, hairstyle, clothing and accessories exactly identical across the three panels (front / full side / back)
- **Scene**: overall composition and signature features stand out, lighting and atmosphere appropriate
- **Prop**: subject clear, details match the description, special textures clearly visible
- **Product**: brand, text, color scheme, materials, proportions and structure from the reference image stay consistent; no hands, models or other on-camera people retained
