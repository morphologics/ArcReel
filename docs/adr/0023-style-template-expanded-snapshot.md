---
status: accepted
---

# Project style stores an expanded snapshot of the template prompt, mutually exclusive with the style reference image

Storing only `style_template_id` and expanding it via table lookup at generation time would let template upgrades propagate automatically, but would make projects that have already rendered output change style abruptly after registry changes, breaking consistency with existing final cuts. We decided that when a style template is selected, the entire art-style prompt is expanded and written into the `style` field of project.json (while keeping `style_template_id` as a source marker — when PATCH passes a new template id it is re-expanded and written; read-time migration only does a one-time resolution for legacy short labels missing an id, and does not re-expand by the stored id); subsequent registry changes are not proactively written back to old projects. Project style is defined as a mutually exclusive terminal state of three choices — "template / custom style reference image / none" — guaranteed by the data write paths so the two never take effect at the same time, letting the prompt composition side consume only a single, already-expanded source.

## Consequences

- Template improvements do not proactively propagate to existing projects; the semantics of `style` change from a short label to long text (transparent for feeding the LLM, but breaking).
- The mutual exclusion constraint runs through multiple write paths — create / PATCH / migration: writing a template clears `style_image`, writing a style reference image clears `style_template_id`, and explicitly clearing the template (id set to null) clears the expanded `style` along with it, leaving no orphan text; in historical data races style_image takes precedence and template_id is actively cleared.
