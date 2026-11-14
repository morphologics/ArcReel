# Unattended triage

The daily triage sweep runs `.agents/skills/triage/SKILL.md` according to this document. The interactive step in the skill, "give a recommendation and wait for the maintainer's instructions", is replaced during the sweep by the routing rules in this document. When a maintainer invokes `/triage` manually, the skill is interactive as written.

Every comment the sweep posts has the AI disclosure required by the skill on its first line and `<!-- triage-sweep -->` on its second line. When the conclusion for an object has not changed, the existing comment is kept. When the sweep changes an object's state, it replaces the existing state label with the new one, so each object keeps only one state label.

## 1. Selecting objects

The objects of this sweep are:

- Issues and external PRs without a state label. External PRs are PRs whose author's `author_association` is not `OWNER`, `MEMBER` or `COLLABORATOR`, and that are not from a bot.
- Issues and external PRs labelled `needs-info`.
- Issues and external PRs where the maintainer has replaced `needs-triage` with another state label but there is no corresponding sweep comment yet.
- `ready-for-agent` external PRs where change requests have been sent, and PRs the sweep is taking over: only check deadlines or advance the takeover per "Takeover" in step 5.
- External PRs labelled `merge-ready`: only check per "Directly mergeable" whether new commits have appeared or required checks have failed.

`needs-triage` is the maintainer's pending-decision queue: the sweep hands objects over into it, and the maintainer decides by changing the label.

Objects carrying multiple state labels at once are only listed in the sweep summary, for the maintainer to resolve the conflict.

Done when: the object list has been produced, with each object marked with the category it belongs to.

## 2. Preliminary checks for external PRs

When an external PR's request clearly hits an entry in `.out-of-scope/`, or its linked issue carries `ready-for-agent` (for the reason see `CONTRIBUTING.md`), comment citing the corresponding entry or reason, apply the category label and `wontfix`, and close it, without the checks below.

Other external PRs go through the following two checks in order before review. PRs that fail stop at this step and are labelled `needs-info`; if `needs-info` has lasted more than 14 days without passing, regardless of whether there were replies in the meantime, comment that it can be reopened once completed, then close it:

1. **CLA**: the `license/cla` status is success. When the PR's latest commit has no such status, first visit `https://cla-assistant.io/check/ArcReel/ArcReel?pullRequest=<PR number>` to trigger a recheck, then read the status; if there is still no status, do not treat it as unsigned, but list it in the sweep summary. On failure, post one signing reminder; remind only once per PR.
2. **Discuss direction first**: new features, new providers, new endpoints, or changes exceeding 200 lines added plus removed need a linked issue. Without one, ask the author to open an issue to discuss the direction first; when it is only over the line limit, the PR can also be split into smaller ones.

For PRs labelled `needs-info` because of the preliminary checks, once they pass both checks, remove `needs-info` and continue to step 3.

## 3. Examination

Gather context per steps 1 and 3 of the skill's "Triage a specific issue or PR": read the full body and comments, check whether it is already implemented, compare against `.out-of-scope/`, reproduce the bug or verify the PR's diff. If it cannot be reproduced, write down why in the record. For external PRs, also review the diff with the `code-review` skill: the fixed point is the PR's base branch, the spec is the linked issue, and when there is no linked issue the PR body serves as the spec.

## 4. Routing

Objects whose state label the maintainer has already changed, as found in step 1, go straight to step 5. Other objects take the result of the first matching row in the table below.

| Situation | Handling |
| --- | --- |
| The requested behaviour is already implemented | Comment pointing to where it is implemented, label `wontfix` and close |
| `needs-info` for more than 14 days, with no reply, commit or body edit from the asker or author | Comment that it can be reopened once the information is supplied, then close |
| Insufficient information, or cannot be reproduced with the steps provided | Post Triage Notes using the skill's needs-info template, label `needs-info` |
| During `needs-info`, the asker or author posted a new reply, commit or body edit | Remove `needs-info` and go back to step 3 to re-examine |
| An external PR meets all the conditions of "Directly mergeable" | Label `ready-for-human` and `merge-ready`, and comment 「建议合并」 (Recommend merging) with the grounds |
| Other | Hand over to the maintainer |

Every examined object gets the category label `bug` or `enhancement`. `code-review` findings on external PRs are posted to the author as PR comments.

### Directly mergeable

An external PR that meets all of the following conditions:

- Changes only Markdown documentation, or is a bug fix of no more than 50 lines added plus removed.
- `license/cla`, `ci-required` and `codeql-required` are all success.
- Neither `code-review` nor verification found issues needing changes.
- The change does not touch sensitive paths: pricing and billing (`lib/config/`, `lib/billing/`), model catalog and presets (`lib/catalog/`, `packages/arcreel-catalog-core/`), UI copy (`lib/i18n/`, `frontend/src/i18n/`), prompts (`lib/prompts/`, `agent_runtime_profile/`), database and project migrations (`alembic/`, `lib/project/project_migrations/`), CI (`.github/`), dependency manifests and lockfiles.
- The PR is not a draft.
- Its main purpose is not promoting a commercial service (see `CONTRIBUTING.md`).

`merge-ready` means the sweep has reviewed and verified it; merging is done by the maintainer. When the PR gets new commits or a required check fails, remove `merge-ready` and go back to step 3.

### Handing over to the maintainer

Label `needs-triage`, and comment with:

- The suggested category and state (`ready-for-agent`, `ready-for-human`, `needs-info` or `wontfix`) and the reasons.
- Reproduction or verification results.
- Similar `.out-of-scope/` entries (cite them if any).
- For external PRs, additionally: whether the direction is acceptable, a list of quality issues, and whether it is worth taking over and why. Worth taking over means the problem it solves has real value, and the remaining work is clearly smaller than a rewrite.

## 5. Carrying out the maintainer's decision

The state label the maintainer applies determines the sweep's action:

| Label | Issue | External PR |
| --- | --- | --- |
| `ready-for-agent` | Post an agent brief per the skill's AGENT-BRIEF.md | Direction acceptable and worth taking over: send the change requests to the author, stating that if there is no response within 7 days the maintainers will take it over |
| `ready-for-human` | State, per the skill, why it cannot be delegated | No action needed |
| `needs-info` | Handle per the needs-info rules in step 4 | Direction acceptable, not taking over: send the change requests to the author, then handle per the needs-info rules in step 4 |
| `wontfix` | Close with the reason attached | Close with the reason attached |

The closing reason is taken from the maintainer's comment on that object; when there is no comment, reuse the reason from the sweep's earlier recommendation, citing the `.out-of-scope/` entry if one was hit. Newly appearing rejection reasons are listed in the sweep summary, for the maintainer to decide whether to add them to `.out-of-scope/`.

**Takeover**: for a `ready-for-agent` external PR, if there are no new commits within 7 days from when the change requests were sent or the author last replied (whichever is later), the sweep dispatches a subagent to take it over. If the author allows maintainer edits, append commits on the original PR branch; otherwise open a new PR from the latest `main`, keeping a `Co-authored-by` credit in the commits, comment on the original PR with a link to the new PR, then close the original. After the follow-up changes are done, run `pr-ai-review-loop`; once it converges, change the PR to `ready-for-human`, then judge by the "Directly mergeable" conditions whether to label it `merge-ready`; the sensitive-path condition does not apply to taken-over PRs. If the author pushes new commits within the deadline, the author continues the changes, and the sweep goes back to step 3.

## 6. Sweep summary

Done when: every object in the step 1 list is in a state prescribed by one of the rules in this document.

At the end, output a summary in Chinese: objects handled autonomously and the actions taken, objects with conflicting state labels, objects newly handed over to `needs-triage`, maintainer decisions carried out, progress on external PRs being taken over, and rejection reasons suggested for adding to `.out-of-scope/`.
