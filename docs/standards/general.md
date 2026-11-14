---
paths:
  - "**"
---

# General

## Code and documentation

### Comments state only the code's current behavior and constraints

The person reading a comment has only this code in hand. Reasons for a change, issue and PR numbers, and time-bound wording such as "recently", "this time" and "measured in practice" belong in the commit message and PR description; written into a comment, they become false statements as soon as the code changes, and nobody will update them. When modifying a file, rewrite any existing references of this kind into statements about current behavior while you're at it.

Allowed when: this rule constrains comments in code and tests; topic documents under `docs/` (ADRs, runbooks, etc.) may reference spec numbers to one another.

### Defend only against normally reachable scenarios

Normally reachable means the program can get there under normal operation: the normal flows of the product UI and the agent; supported deployment shapes, including concurrent requests and restart after a graceful shutdown; foreseeable external failures, such as network errors, provider errors and rate limits, and a full disk. For paths reached only through a hard kill at an arbitrary moment, power loss, the user hand-editing project files or permissions, or hardware failure, do not add checks, fallbacks or compensation; when rejecting a review comment on this basis, the reply states which kind of abnormal condition it depends on. Every defense is a branch that has to be maintained and tested, and review loops keep circling around the same kind of comment.

Allowed when: the guarantee comes from the root at an existing single boundary without adding a branch, for example adding fsync to an atomic write.

### When a tool reports a problem, change the code; exemptions only for confirmed tool false positives

This rule applies to the tools in the gate: lint, type checking, dependency checks and audit scripts. When they report an error, change the code; do not add a baseline or a count threshold, and do not turn off the whole rule; those approaches let future real problems of the same kind through as well. Exemptions are only for confirmed false positives. Whether an inline exemption states its reason on the same line is checked by `SUPPRESSION-REASON` in `scripts/audit_conventions.py`; review only judges whether the reason holds. Style or grammar hints attached by review bots (such as LanguageTool) are not gate tools and are not handled under this rule.

When inline syntax is not available, the exemption goes next to the configuration entry, with a one-line comment stating the reason next to each entry:

- actionlint: in `paths.<file>.ignore` of `.github/actionlint.yaml`, matched exactly by error-message regex.
- deptry's `DEP002` / `DEP004`: in `[tool.deptry.per_rule_ignores]`, listed one package name at a time.
- ESLint file-level disabling: via a `files` override in `eslint.config.js`.

Policy-threshold findings (such as the number of Dependabot cooldown days) are handled by adjusting the configuration as the tool requires, and likewise do not go through exemptions.

The reason should explain "why the tool is wrong here" or "why it can only be written this way here". "Too much hassle", "temporary" and "later fix" don't hold; "React setter reference is stable", "mount-only initialization" and "third-party library provides no types" do.

Allowed when: import-linter's `ignore_imports` (`[tool.importlinter]`) is the baseline for existing violations. Each ignore disables the layering contract at that point, and dependencies in the same direction keep growing along it, so before adding an entry, first try to remove the corresponding dependency edge; only when it really cannot be removed is a new entry allowed, with a comment above the entry stating the reason or the corresponding ADR.

### Feature changes and their documentation go in the same PR

The following changes update the corresponding pages in `website/docs/` at the same time (for each page's responsibilities, see "各页职责" (Page responsibilities) in `CONTRIBUTING.md`). While the docs lag behind the code, users following the docs will fail.

- A new creation type or generation mode.
- A new provider or media capability.
- Changes to the deployment directory, ports or environment variables.
- Changes to the data directory, backup method or migration behavior.
- Changes to the external API, license or commercial-use terms.
- Changes to how external agents are installed: update `public/agent-installation-guide.md` too.

### Pre-filled and default values take a valid value from the context where they actually apply

When a default value only holds for a particular provider, model or type (for example a voice belongs to only one speech model, or a media type is available only from some providers), take and validate the value in the context where it actually applies: the model that is finally written, the currently selected provider, not a global default or a fixed set. The value it depends on may be inferred automatically or changed by the user to another vendor, and a fixed default is bound to be invalid there: a new project gets written with a combination that is certain to fail synthesis, or a form submission fails at the backend's second step and leaves an orphaned entry.

### When changing the source or handling policy of a capability dimension, list all read points and change them together

The same capability dimension is read in many places: each backend's request payload construction, the runtime gate, connection testing, pre-submission cost estimation (one path each for built-in and custom providers) and settlement metering. When switching a capability source to the effective capability with user overrides layered on, or changing a class of handling policy across the board (e.g. silent dropping changed to raising an error), first search for every read point by the dimension name, list them in the PR, then change them together. If only the conspicuous few are changed, the other read points keep working under the old policy: a missing first frame or reference image is skipped and the render is produced and billed as usual; a capability the user narrowed is let through by connection testing and charged for real; estimating as silent and settling as with-audio lets the cost admission check pass an over-budget request.

Allowed when: the PR intentionally changes only some read points, and states the reason the remaining read points keep the old policy and the responsible issue.

### A value's format or value range is defined in one place only, and producers, validators and consumers all reference it

The same value is often interpreted by several parties on their own: an ID-issuing or output function produces it, schemas and violation checks validate it, and request construction, capability resolution, comparison logic and the UI consume it. When each party writes its own policy (regex digit count, integer type, algorithm prefix, entry kinds), after one party changes the others keep working under the old policy: an ID issued by the server is rejected by its own validation; validation lets `1.0` through while capability resolution still accepts only integers; the read exit adds a prefix while the write-back entry still compares against the bare value, so it is judged a concurrency conflict; entries of a new type are allowed to be shared while the install dialog still accepts only the old type. When adding or changing such a value, all parties reference the same definition (a constant, type or parsing function); when that's not possible, list all read and write points in the PR and add a round-trip test that drives every validator and consumer with the producer's real output at the boundaries.

### Static guards that match by name or path have test cases covering every way of reaching the same target

Guards like lint rules and `scripts/audit_conventions.py` identify the blocked module or function from source text, and the same target often has several spellings: import paths have the `@/` alias, relative paths, `..` segments and re-exports; deleting functions have `from os import remove as rm`, `import os as fs` and `aiofiles.os`. When a guard recognizes only one spelling, the others still reach the target, and it also creates the impression that it's been blocked. When adding or changing a guard, list every spelling that can reach the target, with one should-fail test case for each.

## Copy

### User-visible text is written as translation keys, covering all supported languages

Frontend UI text goes into `frontend/src/i18n/`, and errors, events and other messages the backend returns to users go into `lib/i18n/`; each one's language directories are the language list. Text hard-coded in the code shows up as-is in UIs in other languages, and users see mixed languages. Whether the key sets match across languages is verified by typecheck and `tests/unit/lib/i18n/`; review only judges whether a piece of text should go through a translation key.

Allowed when: logs and exception messages meant only for developers; names that come from catalog data, such as model display names, follow the data entry and get no translation key.

### Failure messages state the cause and next step first; the raw text is collapsed

The summary uses localized copy to explain the cause and the way out, without problem codes, internal IDs or raw server text; the raw text goes into an expandable details section. When there is no known cause to state, the raw text is the summary.
