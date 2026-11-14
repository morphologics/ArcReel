---
status: accepted
---

# Asset identification folds into per-episode script planning: whole-book asset extraction and the asset list gate are retired

Whole-book asset extraction put an entire long source text into one context; changing a single byte of the source made the whole list stale and blocked all downstream actions; and it fitted neither episodes that bring their own source text nor free creation without source text. We decided to put asset identification into **per-episode AI script planning**: with the names, aliases and descriptions of registered assets as context, assets not yet registered in this episode and appearance changes (derivatives) are produced together with planning; the handling of each new item (create new, assign to existing, derivative, do not register) is decided, with justification, by the AI that has read this episode's source text; the creator can change it at content confirmation, and confirmation registers them together with the formal script. Whole-book extraction and the asset list gate are retired accordingly.

The AI recognises the same asset by how it is referred to, whereas asset descriptions describe appearance, so assets carry **aliases**, which are only for the AI's reference and do not take part in programmatic matching; the program does only one merge: at registration, a new item whose name exactly matches an already registered asset of the same type is assigned to the existing asset.

Ads/shorts have no script plan, so asset identification likewise folds into their whole-script generation, and handling decisions take effect on write without content confirmation: content confirmation exists to gate before prompt authoring, while whole-script generation writes the visual layer in one step, so that gate has nothing to stop; and without identification, projects that start only from product information and creative inspiration would have no characters or scenes for storyboards to reference.

## Considered Options

- **Fold into episode planning**: covers only whole-book source text; episodes with their own source text would still need a second identification path.
- **Keep whole-book extraction as an optional action alongside per-episode identification**: duplication and conflicts between the two paths would have to be handled separately in the long run.
- **Have the creator choose the handling item by item at content confirmation**: hands the judgement to someone who most likely has not read the source text.
- **Aliases take part in programmatic matching**: generic terms of address (such as 「大哥」 ("big brother")) may refer to different people in different episodes, and the program would mismatch.
- **For ads/shorts, keep requiring assets to be registered in advance, or also put new assets through content confirmation**: the former leaves a typical start with no assets to reference; the latter just adds one more step.

## Consequences

- When the AI misidentifies and the creator does not notice either, the same asset gets registered twice; the way out is to merge the two in the project's assets.
- Planning and generation never modify the descriptions of registered assets: if the description written at first appearance is incomplete, only the creator can complete it, and appearance changes go through derivatives.
- Hand-written scripts are not AI-generated, so their assets are still registered manually.
