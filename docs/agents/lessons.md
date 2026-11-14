# Recording and promoting lessons

[`docs/LESSONS.md`](../LESSONS.md) is the lessons log: it records mistakes worth improving the environment over, and promotes a kind of problem once it has recurred up to the threshold. Both coding and process problems can be included, and when recording, do not prejudge where an entry will end up.

The lessons log is not read during implementation or review. Its only writer is the [`lessons-sweep`](../../.agents/skills/lessons-sweep/SKILL.md) sweep; the user can also edit it directly when running `/retro` manually. All other agents only submit candidates through the end-of-task self-check below.

## End-of-task self-check

Any work that will ultimately become a PR gets one self-check at the end, including interactive sessions and small fixes; in AFK batches, the implementer, stage-reviewer and review-looper each do one self-check.

Ask yourself: did this work involve a mistake worth improving the environment over? It counts if any of the following holds:

- **Leaked into main**: a post-merge fix, a revert, a main CI failure.
- **Data · security · real spend**: judge by actual consequences, i.e. whether the mistake would ultimately lose or corrupt user data, break a security boundary, or make the user pay real money for unexpected model calls, for example a test connection that actually called a model. Being caught within the PR still counts. ArcReel's own cost records and estimates are reference values; recording them wrongly does not count by itself, only when it ultimately leads to real spend.
- **Mechanically detectable**: lint, type checks, scripts or tests could detect this kind of mistake, but the existing checks did not report it. Being caught within the PR still counts, and the destination is a new check.
- **Expensive rework**: overturning code already written, or going back and forth on the same problem for 2 or more rounds.

Anything matching none of the four is not a lesson; common examples: a judgement mistake pointed out by review within the PR and fixed in one go, which shows review is working normally; mistakes reported by existing checks, or pointed out by review citing existing standards; rejected review comments; pure wording changes.

When there is such a mistake, after the PR is created post a comment on that PR: the first line is `<!-- lesson-candidate -->`, followed by one entry in the [entry format](#entry-format), with `Root cause: <…>` added to the occurrence record before "not caught". Do not post the same candidate again on a PR where it has already been posted.

When the problem originated in an earlier PR, the comment still goes on the current PR, and the number is written according to the counting unit in "Counting and promotion".

When an AFK role does not have its own PR yet (the implementer, and the stage-reviewer before the stage PR is created), it writes the candidate in the same format into its handoff, and the team-lead posts the comments together on the stage PR.

## Entry format

```markdown
### <One-sentence rule, stated positively>
Pattern: <in what situation it happens, and how it shows up>
Cost: <consequence> (severity: routine / data · security · real spend)
Detection: mechanical (planned via <tool>) / judgement required
- #3101 · 2026-10-13 · Found in review: <one-sentence symptom>; not caught: <reason>
- #3139 · 2026-10-14 · Post-merge fix: <one-sentence symptom>; not caught: <…>
```

- Occurrence count and last occurrence date are derived from the occurrence records; there are no separate fields for them.
- The rule, pattern and cost describe practices and consequences that hold long-term; details that go stale, such as SHAs, line numbers and file paths, go only in the occurrence records.
- The source states how it was found, e.g. self-check, found in review, post-merge fix, revert, main CI failure.
- "Missed because" takes one of three values: no corresponding rule; a rule exists but was not followed; the check was not wired in. When the mistake was already stopped within the PR by an existing check or standard, write "none, stopped by <check or rule>", and at promotion review first confirm whether that interception is already sufficient.

## Grouping

- Group by failure pattern. Grouping by code location splits the same pattern into several places, and the count never reaches the threshold.
- Groups use level-2 headings, entries use level-3 headings. There is no predefined taxonomy: an entry goes into the closest group; those that fit nowhere go into "Ungrouped" first, and a new group is created once two or more similar entries accumulate; groups can be merged and split.
- The "Promoted" section at the end records entries that were moved out, stating where they went and keeping their occurrence records.

## Information boundary

This repository is public. Entries contain only technical facts (PR and commit numbers, symptoms); customer, channel and business information does not go into the lessons log; lessons involving private implementations are not written into this repository.

## Counting and promotion

- **Counting unit**: distinct PRs or issues. Repeated occurrences within the same PR count as 1. One incident gets only one occurrence record, numbered with the PR that introduced the problem, or the PR that found it if that cannot be determined; when the same incident is collected from multiple sources, merge them into one, and the fix PR number can be attached in the symptom as evidence.
- **Promotion thresholds**: mechanically detectable ones become a check at 2 occurrences; judgement ones become a rule at 3; anything with severity data · security · real spend enters promotion review after 1 occurrence.
- **Destination**: can be a check, a standard, a skill, a navigation pointer or architecture docs, decided by the criteria in "Maintenance" in [`CODING_STANDARDS.md`](../../CODING_STANDARDS.md). If the occurrence records include "a rule exists but was not followed", a written rule is not enough, and a check or an architectural elimination is preferred. Entries that fail the admission criteria stay in the lessons log and keep counting, with a line `Reviewed, not promoted · <date> · <one-line reason>` appended (not counted as an occurrence); only new records after that line trigger another review. Cross-domain patterns go into `docs/standards/general.md`.
- **Expiry**: entries whose last occurrence is more than 30 days ago are deleted; git history serves as the archive. Entries noting an open issue number, and the "Promoted" section, do not expire.

## Retiring standards

- The rule has been turned into a check, or that kind of mistake has become architecturally impossible: delete the rule.
- If none of the review comments on the last 30 merged PRs whose changes hit the rule's applicable paths cited it, it is a deletion candidate, decided by the counterfactual test in the admission criteria. The counting starts from the latest of the following three: the lessons log's first reviewed-through (`486a901`, 2026-10-19), the merge of the PR that added the rule to the standards, and the merge of the last sweep PR that ruled to keep it; if fewer than 30 PRs have hit it since the starting point, no ruling is made. Each sweep rules on at most 5 rules.
- Rules that originated from incidents are demoted back to the lessons log: attach the known occurrence records from the "Promoted" section, then append a line `Demoted · <date> · from <standard file + rule title>`; expiry counts from this line; re-promotion counts only occurrences after this line, with earlier records serving only as background.
