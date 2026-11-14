---
status: accepted
---

# Image editing forks the image: only the current image as reference, the instruction is the prompt, and image_prompt is not written back

When making local tweaks to an already-generated asset design image / storyboard image, redrawing the whole image from the original prompt throws out the satisfactory parts along with the rest, drifting further away with each change; uploading a replacement requires the user to bring their own external image-editing capability. We decided to add a separate image editing task type `image_edit`: it calls i2i with the resource's **current image as the only reference image** and the user's **incremental modification instruction as the only prompt**, producing a new image that keeps the original largely unchanged, which overwrites current, while the old image automatically goes into version history; the new version is marked with `source: "image_edit"`, and its prompt field is the edit instruction. Editing forks the **image**, not the prompt — the original image_prompt is not written back; triggering regeneration after editing still redraws from the original prompt, and the edit's effect can only be recovered from version history.

- **Current image only**: the automatic reference-image collection of the generation path (character/scene/prop sheets, previous storyboard) is not mixed in — extra references dilute the anchoring on the base image, repeating "the redraw differs hugely from the original". The task payload is naturally extensible; if adding extra reference images is opened up in future, it won't change the semantics of "the current image is the only base-image anchor".
- **The base image is read as current at execution time**: consistent with all generation tasks "reading the latest state at execution time"; no snapshot at enqueue time.
- **Always i2i, known at enqueue**: the one exception to capability being decided only at execution time (`docs/adr/0001`); i2i slot resolution is moved forward to fail fast at enqueue (if unavailable, reject without creating a task), and capability gating in the execution layer is kept as a backstop.
- **Zero loss on failure**: when the backend call fails, the current image and version files are not touched, and no write-back to the resource fields occurs.

## Explicitly not adopted

- **Mask / inpaint selection interaction**: in the current provider matrix only one provider natively supports masks, so the benefit is narrow and the cost high; instruction-based editing has no selection semantics.
- **Writing the edit instruction back / merging it into image_prompt**: the edit instruction is an increment relative to the current image and is meaningless detached from it; merging would pollute the complete generation prompt that can be redrawn independently.
- **Preview-and-confirm for edit results**: overwrite directly + version history as backstop, consistent with the mental model of regeneration.
- **Server-side mutual exclusion of tasks on the same resource**: symmetric with existing generation semantics; preventing duplicates is the responsibility of the frontend's busy state.

## Consequences

- "Regenerating after an edit returns to the original prompt and loses the edit's effect" is an inherent property of edit semantics; no prompt sync mechanism is introduced; recovering the edit's effect goes through version rollback.
- Editing scope is limited to character / scene / prop / product design images and storyboard images; grid images (intermediate artifacts; editing would require re-cutting and back-filling), global asset library images (not attached to a project, while tasks/versions/accounting/SSE are all in-project mechanisms), and character reference images (user-uploaded material; if unsatisfactory, re-upload) are not covered.
- `image_edit` is a separate task_type (media_type=image, using the image concurrency lane), clearly distinguished from generation tasks in usage statistics, failure troubleshooting, and the task list; its completion event has the same shape as the generation completion event for the same resource, so the frontend refresh mechanism need not distinguish the two.
