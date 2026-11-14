---
status: accepted
---

# Admission belongs to AI actions, not pipeline position: production status states the content situation, whether actions can run, and the suggested next step

Production status used to be a judgement chain checking preconditions in a fixed order: source text exists, asset list current, episodes planned, script plan confirmed, formal script, pending authoring, asset images, storyboard images, video; it returned the first unmet condition as the single current step and the single next step. Agent workflows only executed actions consistent with that step, and the web guidance only presented that step. Source text, the asset list and content confirmation existed to protect particular AI actions, yet were implemented as stage preconditions of the project and episode; so creation that does not start from source text could only squeeze through via special cases: ads/shorts had more than ten `mode != "ad"` exemptions in the judgement chain, while free creation without source text and hand-written scripts "starting from blank" had no path at all. Meanwhile the entry points of the AI actions themselves mostly did not check this order, so the status rules and real admission had long since diverged.

Decision: **admission belongs to AI actions, not to content, and not to pipeline position.**

- Content (episodes, scripts, video units, assets) keeps only minimal document constraints (unique ids, correct skeleton); empty is a legitimate intermediate state, and editing content is not blocked by any pipeline precondition.
- Each AI action carries its own admission predicate, checking only whether that action's inputs hold (e.g. AI episode planning requires the whole source text; AI script planning requires the episode's source text; prompt authoring requires entries pending authoring in the formal script; generation entry points require that referenced assets are registered and have asset images, see `docs/adr/0073`). Production status and action entry points call the same predicate: the reason a button is greyed out in the UI, the reason the agent is refused, and the basis of the suggested next step are the same conclusion (same-source principle as in `docs/adr/0056`).
- Production status now states three things: the current situation of each kind of content and artifact; whether each AI action can run right now and, if not, why; and one suggested next step, with parallel options at forks (such as "AI script planning" and "start from blank"). The only remaining blocking items are of the "whole project unusable" kind: migration failure, project data or artifact manifest unreadable; pipeline position no longer produces blocking items.
- The step order of the production plan is used only for presentation; each step states the situation of its own content and is not shown as pending because the previous step is incomplete.
- For an action the user names, the agent executes it as long as admission holds, and otherwise relays the reason; when the user only says "continue", it executes the suggested next step, and at forks the user chooses.

## Considered Options

- **Keep a single current step and only relax the various preconditions**: smallest change, but the "can only point to one step" structure remains, forked entry points and manual paths still cannot be expressed, and exemptions would keep accumulating by creation type.
- **Give only a list of actions, no suggestion**: the agent loses its routing basis for "continue", and the web guidance cannot highlight the main entry.
- **Write the criteria twice and keep them aligned through tests**: status saying "do X first" while the entry point does not block X is exactly the divergence this decision aims to eliminate.

## Consequences

- Admission has two layers: production status computes only **structural admission** that requires no external IO (whether a button can be clicked, and why not); **submission admission**, which needs quotes or video request facts, is still computed at preview and submission, and all-or-nothing whole-batch admission is unchanged (`docs/adr/0061`). Per-entry actions are stated in status aggregated per episode, and individual buttons are judged in place with the same predicate.
- The scattered exemptions for ads/shorts disappear: actions that do not apply are marked not applicable for that creation type, rather than being skipped in the judgement chain.
- An empty formal script is a legitimate state: "non-empty" remains only in the output acceptance of each AI generation action, and production status suggests "add storyboard" for an empty script rather than blocking.
- The project summary no longer has a "current stage": multi-episode projects have no unified stage anyway, so the project level now states episode progress and reminders that need attention.
- Stale artifacts remain usable and do not enter the suggested next step; they are only stated in the situation of the corresponding content, with an entry to update them (`docs/adr/0062`).
