---
status: accepted
---

# Share submissions become market-source PRs via the official service: no account needed, and the PR is the only container for approval

Submitting to the official market source originally had only one path, manually opening a PR against the official repository (`docs/adr/0078`), which required the user to know GitHub and organise the entry directory themselves. We decided to offer **share submission** in the client: the user submits a custom invocation endpoint to the official service, which pre-checks it and opens a PR against the official market source repository as a GitHub App; approval still happens on GitHub.

- **No account needed**: submitters are identified by the instance ID (`docs/adr/0093`) plus a submission token issued by the official service; the client uses the token to query status and the PR link. A GitHub username can optionally be provided, used only for mentioning in the PR, and not verified.
- **The PR is the only container for approval**: the official service has no approval console and does not store entry content, only the PR number and terminal state; merge means accepted, close means rejected, and status is written back via webhook as `open` → `merged` | `closed`. CI and manual review follow the manual submission path.
- **Repeat submissions from the same instance update the same branch**: while the PR is open, a new submission from the same instance for the same slug pushes a new commit to the original branch and the token stays the same; after the PR is closed, a new submission opens a new PR.
- **Same slug from a different instance is not blocked**: a PR is opened as usual and labelled `conflict`, for the reviewer to decide.
- **The payload is modelled by type from day one**: `{type, slug, files, meta}`, where `files` is a mapping from relative path to content and the PR lands in that type's directory; the first batch accepts only invocation endpoints, and other types return "type not yet supported". Pre-check dispatches by type to the same validator as the market repository's CI; on failure it returns a diagnostic code and does not open a PR.
- Anti-abuse is implemented in one place, the official service: open submissions per instance, submissions per IP per day, and file package size are all capped.

## Considered Options

- **The official service stores submissions for review first, and opens the PR after approval in an approval console**: adds an approval state parallel to GitHub's, and requires administrator login.
- **Require submitters to log in to GitHub**: back to the account problem, shutting out users unfamiliar with GitHub, who are exactly the people this path is meant to serve.
- **Reject the same slug from a different instance outright**: the first to arrive could squat on a slug; leaving it to the reviewer is safer.
- **Design the payload only for invocation endpoints**: adding entry types would require changing the interface and the client; a generalised file package costs very little.

## Consequences

- Share submission produces no installation record and does not change the binding between local endpoints and any market entry (`docs/adr/0079` unchanged).
- The manual PR submission path is kept and coexists with share submission; the market source repository's CI treats both the same.
- In the first phase, results are communicated only via client token queries, with no email or in-app notifications.
