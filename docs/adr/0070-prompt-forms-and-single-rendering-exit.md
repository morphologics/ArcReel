---
status: accepted
---

# Prompts keep two forms, structured and plain text, and the text actually sent has only one rendering exit

The visual prompts for storyboard images and storyboard image-to-video may be saved as structured JSON or plain text. The structured form lets the system project and supplement content by field; the plain-text form preserves an overall expression the creator has already finalized, without requiring it to be split back into structure to enter the generation pipeline. Both are formal prompt content: plain text is not demoted to a preview cache or a temporary rendering of the structured form, and no promise is made that the two forms can be converted into each other losslessly.

Whatever form it is saved in, the final text sent to the provider goes through only the one rendering exit for that media type (for storyboard images `render_storyboard_image_prompt` in `lib/prompts/prompt_builders.py`, for storyboard image-to-video `render_storyboard_video_prompt` in `lib/prompts/prompt_utils.py`). Although plain text is already readable, it still needs to receive the same system-derived content as the structured form, such as the project art style, negative constraints, reference image type declarations (including product fidelity requirements), or voicing declarations; if preview, admission, and execution each assembled it themselves, the same prompt would yield different requests depending on the entry point, and repeated rendering could also stack declarations. The single exit is therefore the boundary for "what you see in preview is what gets executed" and for idempotent rendering, not a requirement that all prompts first be converted to the same storage form.

Both forms enter the artifact basis under the formal-content approach of `docs/adr/0062` (`lib/artifacts/visual_artifact_provenance.py`): a change in body makes the corresponding artifact stale, and switching between structured and plain text is also treated as a change in formal content. The artifact basis keeps the prompt's content and form, and captures neither the renderer-derived wrapping in the final provider text nor the renderer version; otherwise merely adjusting injected wording would retroactively judge existing visual artifacts stale. Exact request reuse can separately record the text actually sent, but that doesn't change this approach to artifact currency.

## Explicitly not adopted

- **Plain text bypassing rendering and being sent directly**: it would miss system-derived constraints and give plain text and the structured form different generation boundaries.
- **Parsing plain text back into structured JSON**: parsing can't be guaranteed lossless and would turn the creator's finalized expression into fields guessed by the system.
- **Using the final text actually sent as the artifact currency basis**: changes in renderer wording would make existing artifacts whose formal content hasn't changed go stale wholesale, conflicting with the dividing line `docs/adr/0062` draws between generation intent and formal content.

## Consequences

- New preview, admission, or execution entry points must reuse the rendering exit of the corresponding media type and must not write a separate plain-text assembly branch.
- New system-derived declarations are implemented only in the shared rendering exit, ensuring that calling it repeatedly on already rendered plain text doesn't inject them twice.
- Prompt templates (`docs/adr/0077`) are the source of renderer-derived wrapping and likewise don't enter the artifact basis: neither wording rewrites of built-in templates with version upgrades nor later system-level edits to wording make existing artifacts stale.
- Artifact currency comparison continues to target formal prompt content; scenarios that need to judge whether the exact provider request is the same use a separate execution-sensitive digest (`lib/artifacts/video_visual_provenance.py`), which captures the text actually sent and the provider request configuration, and is not shared with the artifact manifest's visual content basis.
