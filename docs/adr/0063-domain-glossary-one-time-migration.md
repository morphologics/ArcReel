---
status: accepted
---

# Domain terminology is migrated once, with no compatibility aliases kept

`GLOSSARY.md` is fully rewritten into the single-level short-entry format conforming to `CONTEXT-FORMAT.md`: the heading is the canonical Chinese product name with the machine identifier in parentheses, the definition is 1–2 sentences saying only what it is, and `_Avoid_` lists only synonyms and ambiguous terms. Statement/mechanism entries and generic technical concept entries are deleted; cross-module invariants in the deleted content that cannot be derived from code are written into the docstring of the single source-of-truth function or module.

## Decision

- **Rewrite everything at once, with no compatibility aliases kept.** Old entry wording (e.g. `隔离草稿` (quarantined draft) → `待修复草稿` (draft awaiting repair), `内容待确认` (content pending confirmation) → `内容确认` (content confirmation), `设计图` (design image) → `资产图` (asset image)) is replaced directly by the canonical product name, and no redirect entries from old names to new names are kept in the new glossary. Code behavior across the repository is unchanged — the glossary governs only human wording (docs, commits, PRs, agent prompts), not a renaming of code symbols.
- **Machine identifiers go in the heading, not in _Avoid_.** Glossary readers need to know which code symbol a product concept corresponds to, and the heading's parentheses are the shortest cross-reference; `_Avoid_` is reserved for synonyms and ambiguous terms to be avoided, and machine identifiers are not in that category.
- **Statement/mechanism/generic technical concept entries are deleted.** The glossary answers only "what is it"; "how does it work", "the relationship between two things", and "the three meanings of some field" belong to implementation documentation and are carried by ADRs and code docstrings. Cross-module invariants in deleted entries that cannot be derived from code are written into the docstring of the single source-of-truth code.
- **Canonical product names are held by `GLOSSARY.md`.** Spec #1840 is the input to this migration; after the migration, the current canonical product names and the synonyms to avoid are maintained only in `GLOSSARY.md`, and ADRs do not copy snapshots of word lists that evolve with product language.

## Explicitly not adopted

- **Keeping alias entries from old names to new names**: as long as aliases exist there's no convergence — someone will always keep writing the old name and cite the alias entry as justification. A one-time switch is cheap (the glossary is not an API contract), while maintaining aliases indefinitely is costly.
- **Migrating in batches or replacing section by section**: glossary entries reference each other, and partial replacement would create an intermediate state with old and new wording cross-referencing each other, more error-prone than a full replacement.
- **Changing code symbols along with product names**: the glossary governs human wording, not code identifiers. Code symbols with the same name but different meanings (e.g. `scene` is both an asset type and a scene entry within the script skeleton) are disambiguated by context, not resolved at the glossary level.

## Consequences

- New docs, commit titles, PR descriptions, and agent prompt templates across the repository uniformly use the product names settled here; an old name appearing in new text is treated as a wording error.
- Old names in existing code comments and docs are not mechanically batch-replaced — they are updated only incidentally when the file they're in is modified, avoiding large diffs with no content change.
