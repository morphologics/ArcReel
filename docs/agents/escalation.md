# When to go to the user

Read this when deciding whether to settle something yourself, open an issue, or go to the user. It applies to all agents: interactive sessions, AFK roles, review loops, and lesson sweeps.

## Order of judgement

Judge in order and stop at the first match:

1. **Factual questions** (whether something exists, whether it holds, conflicting evidence): verify it yourself, reach a conclusion, and continue.
2. **The answer can be derived from existing sources of truth**: do what the derived answer says. Sources of truth include specs, ADRs, standards, `GLOSSARY.md`, the design system, the project positioning (`PRODUCT.md`) and precedent in existing code.
3. **Cannot be derived, and getting it wrong is cheap**: decide yourself according to the project positioning, and leave a one-line rationale in the PR for the user to spot-check.
4. **Cannot be derived, and getting it wrong is expensive**: go to the user.

Getting it wrong is expensive if any of the following holds:

- Hard to roll back.
- External-facing: public APIs, published content, operations that incur real costs, licenses, security boundaries.
- Changes behaviour or data existing users rely on: removing features, changing defaults, migrating data.
- Sets a direction that will be followed from then on: scope of a new capability, a new interaction paradigm.
- Expansion beyond the authorised scope.

The judgement looks only at the costs above, regardless of whether it is a technical or a product decision.

## How to ask

Combine the items that need the user into one message at the end. Number each item, give a recommended option, and word it so that the user answering "yes" accepts the recommendation. For flows that have built-in pause points (such as the pause boundaries of `afk-team-workflow`), ask at the pause point in the same format.

## How to handle items that do not go to the user

- **Genuine defects outside the scope**: after going through the three steps below in order, open a `ready-for-agent` issue.
  1. Threshold: reproduced or backed by code evidence, and affects correctness or user experience. "Could be better" suggestions do not get an issue.
  2. Dedup: if it matches an existing open issue, add a comment with the evidence on the original issue.
  3. Merge: multiple small defects found in the same module in the same batch go into one issue.

  If the fix meets the criteria for expensive-if-wrong, open a `needs-triage` issue instead, stating the options and the recommendation.
- **Suggestions of unclear value**: if it was prompted by a mistake, and that mistake meets the criteria of "End-of-task self-check" in [lessons.md](lessons.md), write it up as a lesson candidate; otherwise discard it.
