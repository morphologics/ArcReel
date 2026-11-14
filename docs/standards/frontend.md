---
paths:
  - "frontend/src/**"
---

# Frontend

## Resource occupancy and queueing

### Controls disabled by resource occupancy check occupancy when opened and when submitted, and disable sibling controls in sync

For controls that are disabled while a resource is occupied, such as edit, regenerate, upload, add to library and version restore, complete three checks when adding or changing them:

1. Check the current occupancy state when the dialog or panel opens.
2. On submit, re-check the latest occupancy state with `isResourceBusy(kind, projectName, resourceId)` from `frontend/src/stores/tasks-store.ts`. The occupancy state may have changed after opening; checking only on open leaves a race window.
3. Sibling controls on the same resource card bind to the occupancy state in sync.

Occupancy does not come only from queued tasks: in-flight write requests issued by the card itself (saving, uploading, renaming) are carried by component-local state, which `isResourceBusy` cannot read; local state takes part in these three checks as well.

When occupancy changes after a confirmation dialog opens, one-click destructive confirmations (the confirm button for delete, merge, rename) are disabled in real time; input forms stay editable, and the re-check on submit explains the reason.

## First-use onboarding

### When changing elements with `data-onboarding`, check the onboarding chain along with them

Onboarding highlights locate elements via the `data-onboarding` attribute: anchor names are registered in `frontend/src/onboarding/anchors.ts`, the step outline is in `steps.ts`, and the copy is in `frontend/src/i18n/*/onboarding.ts`. Anchor names are checked by typecheck, and `anchors.test.tsx` only checks that registered anchors exist in the mount scenario. The three items below have no compile-time or test constraint at all; when they go wrong, onboarding only degrades at runtime to a centered bubble, or points the user at a name that doesn't exist in the UI:

- **The attribute is still there, and the element is still rendered unconditionally.** If the mount point falls into a conditional branch (rendered only in the empty state, only when data is ready, only when a certain tab is active), the step can't find its anchor on the common path: onboarding doesn't abort, but after waiting `ANCHOR_WAIT_MS` it degrades to a centered bubble, leaving only a warn in the console. When migration is needed, move it to an unconditionally mounted container on the same screen, and update that entry's description in `anchors.ts`.
- **The step copy still describes this element.** If the function, fields or button availability conditions carried by the element change, update the corresponding step text in every language.
- **Entry names in the copy match the UI labels.** Sidebar items, tabs and buttons mentioned in steps always use the labels the user sees in the UI; when a label is renamed, update the copy in every language.

When deleting an element with an anchor, clean up the whole chain together: the entry in `anchors.ts`, the step in `steps.ts`, the copy keys in each language, and the assertions in `steps.test.ts` and `anchors.test.tsx`.

## Primitives and shadcn-generated files

### Intentional deviations from upstream in `components/ui/` get a one-line comment at the change explaining why

When changing a primitive's behavior or style to meet this repository's standards (for example adding a height-limited Body to Dialog, or making the overlay opaque), write a one-line comment at the change explaining why. When upgrading primitives, merge manually against upstream with `pnpm exec shadcn add <component> --diff`; local changes without a comment can't be distinguished from upstream code and are easily overwritten during the merge. These comments explain the reason for deviating from upstream and are not handled under the general rule about removing history-describing comments.

Pure lint fixes get no comment; for confirmed false positives, exempt with an inline `eslint-disable-next-line <rule> -- reason`.

### Code-type input fields use `mono` on `Input` and `Textarea`; do not hand-write a monospace font

`mono` controls only the font and `variant` only the appearance; the two can be used together. `InputGroupInput` and `InputGroupTextarea` pass `mono` through. Do not write `font-mono` on an input field or its outer element: the outer font enters the input through inheritance, `@shadcn/lint` can't detect it, and so the standard can't be checked.

Input fields that use `mono`:

- JSON, code and request body editing areas: an endpoint's JSON view, request body templates, definitions pasted on import, responses pasted when testing, the raw Markdown of agent memory.
- Model IDs.
- Tokens: credential inputs such as API keys and secrets.
- Request paths and templates: an endpoint's request, query and fetch URL templates, request header names and values, variable names, value paths, status values.

Input fields that keep a proportional font:

- API URLs, such as the default API URLs of providers and endpoints, and proxy addresses. For values that don't need spell checking write `spellCheck={false}`; don't express it by changing the font.
- Body text such as names, versions and prompts. A login password is not a token and likewise keeps a proportional font.

### Text input with suggestions: use Combobox when only suggestions can be chosen; use Base UI Autocomplete when values outside the suggestions are allowed

Base UI's Combobox only accepts suggestions and cannot submit text outside them; for fields like model ID, where gateway lists are often incomplete and free entry must be allowed, use `@base-ui/react/autocomplete`. shadcn's base-nova registry has no Autocomplete; follow the approach in `components/agent/ModelIdField`: the input uses `components/ui/input-group`, and the popup surface reuses `bg-popover`, `shadow-overlay` and `z-overlay`.

### When relaxing style and layout guards, list each relaxation in the PR description

All non-test source under `frontend/src/` is constrained by `@shadcn/lint` style rules, the scroll and responsive guards and the class name guards (defined in `frontend/eslint.config.js`). Each relaxation makes a class of code legal again within the registered scope, yet in the diff it's just one line of configuration, which review easily lets through. So list them one by one in the PR description, and review judges whether the reasons hold:

- New component variants, `allow` or `contracts` entries for `@shadcn/lint`.
- Files registered in `VIEWPORT_BREAKPOINT_ALLOWLIST`. Only allow scenarios that genuinely need to judge by viewport, such as the shell switching between the standard and compact tiers, or popup widths; the available width of components inside the shell changes with the sidebar and split panes, so always use container queries.
- Configuration blocks that add or widen `restrictSyntax(...)` exemptions.

How configuration blocks combine (flat config replaces the options of `no-restricted-syntax` as a whole) is described in the configuration file's header comment and verified by `src/lint-class-guards.test.ts`.

### This repository's usage of Sheet, DropdownMenu, Command and Chart

- **Sheet**: the default width for both the left and right sides is `w-md`. To adjust the right-side width write `data-[side=right]:w-*`; writing `w-*` directly is overridden by the default width with the `data-[side=right]:` prefix in the primitive.
- **DropdownMenu**: irreversible menu items such as delete use `variant="destructive"` on `DropdownMenuItem`.
- **Command** (often placed in a Popover as a searchable selection list): `CommandList` is a listbox; pass a localized `label` to override the default English name, and put only `CommandEmpty`, `CommandGroup` and `CommandItem` inside it. Separators and loading and load-failure messages go outside `CommandList`, otherwise axe reports `aria-required-children`; fixed entries not filtered by the search term (such as "全部项目" (All projects)) are written as a `Button` or `Link` outside Command. When the Popover opens, use `initialFocus` to hand focus to `CommandInput`.
- **Chart**: wrap the chart in an outer `role="img"` with the chart name as `aria-label`, immediately followed by a `sr-only` table listing the same data, so that screen reader users can get the values in the chart (precedent: `components/usage/UsageTrendChart.tsx`). Series colors reference tokens from `index.css`, and the tooltip surface matches popups (`bg-popover`, `shadow-overlay`).

## Color and text hierarchy

### Colors use only the semantic tokens from `index.css`; lightness is expressed with opacity modifiers

Raw color values and inline colors are blocked by `@shadcn/lint`'s `no-raw-colors` and `no-inline-styles`. The following points can't be judged by lint and are checked by review:

- Light backgrounds, borders and selected states are written as the base color plus an opacity modifier (`bg-primary/15`, `border-border/50`); don't add a variant token for a particular shade: variant tokens produce several near-identical colors for the same meaning, and the old palette's variant colors have been deleted on this principle. When opacity is needed in CSS, write `color-mix(in oklab, var(--primary) 15%, transparent)`.
- Form control borders use `border-input` without an opacity modifier: in both light and dark, `--input` is no lower than 3:1 against the page background, cards and popups (WCAG 1.4.11), and translucency would make the contrast drift with the background. Fills may use opacity like `bg-input/12`, but don't tune a control's fill so that the placeholder drops below 4.5:1. Decorative borders such as cards and dividers still use `border`.
- Don't mix semantics: dangerous actions use `destructive`; amber `warn` means only warnings and expiry.
- Text has only three tiers: `foreground`, `subtle-foreground` and `muted-foreground`. Don't layer opacity or `opacity` on top of `muted-foreground`: its contrast against the page background is only slightly above 5:1 in both light and dark, and lowering it further drops below the 4.5:1 required by WCAG AA.
- When coloring by episode, video unit or asset type, use identity color tokens (`episode`, `unit-clip`, `unit-narration`, `asset-*`); hue and color scheme are obtained via `episodeHue`, `unitHue` and `assetColor`, without computing colors separately at the call site.

## Popups and notifications

### Dialog, Sheet and AlertDialog are composed of Header, Body and Footer; content of indeterminate length goes only in Body

Popups are height-limited by the primitive. `DialogHeader` and `DialogFooter` are fixed at the two ends, and `DialogBody` (`SheetBody` and `AlertDialogBody` in Sheet and AlertDialog) is the only scroll area. If content of indeterminate length, such as error messages, impact explanations and long lists, is placed in the Header or Footer, a short window or a longer translation clips it along with the main action, and it can't be scrolled to, so it always goes in Body.

- Width is chosen with `size`; the call site does not write `max-w-*`. Multi-step wizards use `size="wizard"`; all steps share one `DialogBody`, and on entering a new step its `scrollTop` is set to 0.
- Every popup has a Title; when there is no visible title, add `sr-only` to the Title, otherwise screen readers can't announce the popup's name.
- When Body has no focusable elements and its content may overflow, give Body `tabIndex={0}`, `role="region"` and `aria-label`, otherwise it can't be scrolled with the keyboard.
- When the Footer already has a "关闭" (Close) button, pass `showCloseButton={false}` to Content, to avoid two close entry points.

### Confirm irreversible actions first: popups use AlertDialog, list rows confirm inline; reversible actions execute directly and offer "撤销" (Undo) in the notification

When an AlertDialog opens, focus lands on "取消" (Cancel), so an accidental Enter doesn't execute the action. The confirm button uses `variant="destructive"`; while submitting, disable both buttons and ignore close requests in `onOpenChange`, so Esc doesn't close the dialog before the request completes.

Lightweight objects in list rows (such as queued messages in the tray) are confirmed inline: the row is replaced with prompt text, a `variant="destructive"` confirm button and an `outline` "取消" (Cancel), with focus on the "取消" button; Esc or Cancel exits confirmation and returns focus to the button that triggered it, and focus leaving the confirmation area also exits confirmation. The precedent is deletion in `QueuedMessageTray`.

Recoverable actions such as remove, move and hide don't pop up a confirmation; after executing, call `pushToast(text, tone, { action: { label: t("common:undo"), onClick } })`. Clicking "撤销" (Undo) runs the callback and closes the notification.

Notification buttons can also be used to open a just-completed result: when a long-running operation (such as importing a project) completes after the user has left the originating page, don't force a navigation; instead push a notification with a button like "打开项目" (Open project). "Has left" is judged after the await by re-checking `signal.aborted` of the signal aborted on unmount; the write request that was issued is not itself attached to this signal. When the user has left, every result branch (success, failure, needs confirmation) must have a notification, and must not be silently lost because the dialog unmounted with the page. Reference implementation: `frontend/src/components/pages/lobby/useProjectImport.tsx`.

## Buttons

### Buttons use `Button` from `components/ui/button`, with the variant chosen by the action's semantics

- `default`: the primary action within an area; at most one per area.
- `outline`: secondary actions, such as "取消" (Cancel) and "重新渲染" (Re-render).
- `ghost`: toolbar buttons and icon buttons.
- `destructive`: irreversible actions such as delete and overwrite.
- `secondary`, `link`: low-emphasis actions and inline link-style actions.

Size uses `size`: `xs`, `sm`, `default`, `lg`; icon-only buttons use `icon`, `icon-xs`, `icon-sm`, `icon-lg`. Icons inside a button get `data-icon="inline-start"` or `data-icon="inline-end"`, with no size classes. `Button` has no loading prop; while loading, disable the button and replace the leading icon with `Loader2` with `animate-spin`.

Whole-row items (list rows, episode directory rows), thumbnail triggers and drop zones are not action buttons; use a native `<button>` with `focus-ring`: switching to `Button` would require restyling it at the call site, which `no-restyle` doesn't allow. Navigation entries that look like buttons use `Link` with `buttonVariants`, not a `Button` that calls `navigate`: screen readers announce it as a link, and it can be opened in a new tab.

## Scrolling and horizontal overflow

### A scroll area's height is passed down the flex chain; main columns that can show a scrollbar reserve the gutter

The document itself does not scroll; scrolling happens only in containers designated by the shell: the sidebar, the shell body in the constrained-width and full-width tiers, and the columns of full-bleed sections. Scroll containers must be positioned elements and must not use viewport height for fixed heights; these two points are guarded by lint; document scrolling and content clipping are backstopped by the e2e overflow probe in existing scenarios, while new components and uncovered widths still rely on review. When reviewing a newly written or changed scroll area, confirm two points:

- **Height is passed down the flex chain.** Every flex child layer between the shell and the scroll container gets `min-h-0` (`min-w-0` horizontally), and the root node of a full-bleed section gets `flex min-h-0 flex-1`. If one layer is missing, the flex child's minimum height equals its content height, the scroll container is stretched by its content instead of scrolling, and the overflow is clipped by the shell and can't be scrolled to. Don't use `sticky` to simulate an independently scrolling column.
- **Main columns where a scrollbar can appear with content get `[scrollbar-gutter:stable]`.** The shell body already has it; full-bleed sections and new main scroll columns at the canvas's narrow breakpoint need it too. Without it, when content grows and a scrollbar appears, the whole column jumps horizontally (#3067).

Allowed: short lists in popups and small scroll areas not side by side with the content area may skip reserving the gutter.

### Content that doesn't fit horizontally is handled by type: long strings wrap, items scroll with a fade, tables fix column widths, single-line text truncates

In a scroll area with only `overflow-y-auto` (popup Body, canvas columns, the tray in the agent panel), content wider than the container is turned into horizontal scrolling by the browser rather than being clipped or raising an error, and button rows and long strings get pushed out of view. The e2e overflow probe covers only existing scenarios; new components, long text and narrow widths rely on review. When adding or changing text, button rows and lists, confirm by type that they fit at the narrowest supported width (such as the agent panel's minimum width):

- **Long strings** (URLs, tokens without spaces, long lines of dialogue in old data): use a wrapping class like `wrap-break-word`. `whitespace-pre-wrap` does not force long strings to break.
- **Button rows, confirmation rows**: allow wrapping or shrinking; don't write non-shrinkable fixed groups (#3139).
- **Horizontally scrollable items** (tags, thumbnail strips): the container gets `overflow-x-auto scroll-fade-x`, which shows a fade on the clipped side.
- **Tables**: use `table-fixed` and `<colgroup>` to give short columns such as status, time and actions fixed widths, plus `whitespace-nowrap`; cells in the main text column use `TruncatedText`.
- **Single-line text** (names, paths, model IDs): use `components/shared/TruncatedText`, with `min-w-0` on the parent. Don't substitute the `title` attribute; keyboard users can't see it. When placed inside a focusable element such as a button, pass `focusable={false}`, and the full text is provided by the outer accessible name. Numbered names render the number separately as a non-truncated prefix.

Areas that genuinely need horizontal scrolling declare `overflow-x-auto`, which the probe uses to recognize them; undeclared horizontal scrolling is reported as a defect.

## Edit units and saving

### When an edit unit has unsaved changes, generation goes through `unit.saveAndGenerate` and generates only after saving succeeds

For the structure of edit units (how fields and actions are divided, where the save bar is), see "编辑单元与离开拦截" (Edit units and leave guard) in `website/docs/dev/architecture.md`. Generation reads the saved content and is billed by input. When an edit unit has unsaved changes, the generate button reads "保存并生成" ("Save and generate") (`common:save_and_generate`), and clicking it calls `unit.saveAndGenerate(generate, { confirm })`: calling `discard()` first would throw away the creator's changes, and submitting generation directly would generate and bill against the old input (#3045, #3049). `confirm` is a confirmation such as "regenerating will invalidate downstream": on cancel nothing is saved, and on save failure nothing is generated. The leave guard can't cover this, because generation doesn't trigger leaving.

Allowed when: there are no unsaved changes; generate directly.

### Leaving and switching are handled by the leave guard; don't build your own unsaved-changes confirmation dialog or `beforeunload`

`LeaveGuardProvider` at the root of `AppRoutes` is the only leave guard; for the mechanism, see "编辑单元与离开拦截" (Edit units and leave guard) in the architecture doc. A page's own confirmation dialog or `beforeunload` pops up in duplicate with it, or blocks again after it lets the user through; failing to hook into the guard lets unsaved changes be silently lost on navigation or unmount. Such omissions recur when adding switching actions and self-managed forms (#3040, #3044, #3049, #3052, #3264). When reviewing a diff that changes editable content or switching actions, confirm item by item:

- **Self-managed forms register truthfully.** Forms that don't go through `useEditUnit` call `useLeaveGuard({ dirty, saving, save, discard })`. `save` truthfully returns whether it succeeded: always returning `true` lets changes whose save failed be lost on navigation. `saving` truthfully reflects whether a save or create request is in flight: if under-reported, "放弃修改" ("Discard changes") lets through a save that has already been sent, and "保存并离开" ("Save and leave") submits twice.
- **Switches that don't go through the router are wrapped in `useConfirmLeave()`.** Previous and next for storyboards, memory files and assets, and closing a Sheet all count; the wrapped action must complete the switch synchronously. Multi-step switches (switch view first, then select) go into the same action; the leave guard keeps only the most recent request.
- **For actions with a second confirmation, wrap the confirmation, not the opening.** For delete and remove, wrap the confirm action inside the confirmation dialog; wrapping the opening of the dialog would make "discard changes first, then cancel the deletion" throw away changes for nothing. Actions that may fail return `Promise<boolean>` and report the result truthfully; changes are kept until the action succeeds before being discarded.
- **Fields that submit immediately** also use `useRetainWhile` to participate in retention against external removal, in addition to `useLeaveGuard`.
- **Switches not initiated by the user don't show the guard.** Automatic focusing and navigation brought by agent changes first check `useHasUnsavedChanges()`, and are simply skipped when there are unsaved changes.
- **Navigation goes through wouter.** Don't call `window.history` directly or rewrite `window.location`, otherwise navigation bypasses the guard.

Allowed: navigation that doesn't unmount the edit unit (page switches within the same edit unit) is let through with `allowNavigation(to)` at registration; `beforeunload` before a full page reload is handled centrally by `utils/lazy-component.ts`.

### Follow-up actions that depend on new data are offered only after the refresh settles as `success`

Refresh after a write goes through `refreshAfterWrite` (for its structure, see "项目数据刷新" (Project data refresh) in the architecture doc). Follow-up actions that depend on new data, such as undo, switching to the new entry, navigating to a newly created episode and a "成功" (Success) notification, are offered only after the settled value is `success`; when the refresh fails, show only the refresh-failure notification and don't report success at the same time, otherwise the creator sees a success notification while stuck on old data. See `frontend/src/components/canvas/episodes/CreateEpisodeDialog.tsx`.

Allowed when: the wrap-up of the write itself, such as closing the edit dialog or leaving the page of a deleted entry.

## Async: cancellation and catch-up fetching

When handling "data may be stale after an await", use the mechanisms below according to the scenario, rather than manually propagating closure flags.

### Async chains that cross function boundaries cancel with AbortSignal

Once a call chain leaves a single function (an effect calls an async function, an async function makes further requests), cancellation propagates via AbortSignal. A `cancelled` closure flag only guards its own function's awaits and can't reach into called functions: the called function writes to the store and opens SSE connections as usual after its await, and a late response overwrites the new state.

- API-layer methods accept `options?: { signal?: AbortSignal }` and pass it unchanged to `fetch`. Network await points reject automatically after abort.
- After non-network await points and before executing side effects (writing the store, opening SSE connections, etc.), re-check `signal.aborted`, covering the window where "the abort happens after the response has already resolved".
- The taker-over rotates controllers: a new round of loading first calls `abort()` on the previous controller, then creates a new one.
- Wrap-up (such as resetting loading) is the taker-over's responsibility. The aborted side first checks `signal.aborted` in `finally`; if it has been invalidated, it doesn't touch shared state, otherwise it would interrupt the taker-over's in-progress load.

Allowed when: all awaits are in the same function and cancellation doesn't need to reach called functions (such as the configuration status polling in `frontend/src/router.tsx`); the `cancelled` flag may stay. One-off user-triggered actions that are not data loading (such as export) may continue to completion after leaving the page and are outside the scope of this rule.

Reference implementation: `frontend/src/hooks/useAssistantSession.ts` (the init auto-selection and the `loadSession` loading chain).

### Invalidating a request triggered by a one-off event requires a catch-up fetch

After an in-flight request triggered by a one-off event (an SSE notification, a user confirmation) is aborted, the event has already been consumed and the data won't come again. The invalidating side (or the taker-over) itself starts a round of fetching after the abort to fill the gap, otherwise the UI stays on old data forever. "Cancellation" and "compensation" come in pairs: if a diff has only the abort and no catch-up fetch, ask who will bring this data back afterwards.

Reference implementation: `frontend/src/hooks/useScriptReviewDraft.ts` (invalidates the in-flight fetch and re-fetches when an adoption is written).

### When switching scope, the old scope's interaction state is invalidated as a whole, and async writes land only while their generation is still valid

When interaction state (confirmation requests, in-flight markers, error messages, pagination cursors and totals) belongs to a scope (project, session, view), gather it into a session with a generation number: on scope switch, take a new generation and reset everything, and before each async write lands, check that the generation at which it was issued is still the current one. Aborting just one request or clearing just a few of the fields lets other writes that return after the switch, and fields that were missed, carry the old scope's results into the new scope: confirming in one project cancels a task in another project; after switching views, the old view's cursor is sent to a different API; after switching projects, the previous session's error is still shown. When several parallel request sources write the same set of shared fields, they share one invalidation boundary (the same refresh coordinator or the same generation number), instead of invalidating separately.

Reference implementations: `frontend/src/components/usage/use-task-cancellation.ts` (the confirmation session's `gen`), `frontend/src/stores/usage-records-store.ts` (two views share one pagination coordinator).

## Async: compensation for deliberate skips

### Skipped compensation is recorded and made up when the protection window closes

When a protection condition holds and a compensating action (catch-up fetch, reconciliation, wrap-up switch) is deliberately skipped once, the skipped compensation will not happen again on its own. The skipping side records it under the key that triggered it, and the side that closes the protection window is responsible for making it up; the make-up goes in a place like `finally` that covers both success and failure, otherwise on the failure path the protection window never closes and the recorded compensation never runs.

### Protection state is bucketed by the full scope; stackable state uses a count

Protection state that survives across requests (in-flight counts, skip flags) is bucketed by its full semantic scope (request, project, session). With one dimension missing, different operations share a bucket: another operation running in parallel lifts this operation's protection, or a long-running operation in another scope keeps blocking compensation that should have happened. State that can stack concurrently uses a count, not a boolean.

Reference implementation: `deletingCurrentRef` (count bucketed by project × session) and `deferredRefreshRef` (records skipped list catch-up fetches, made up after the deletion wraps up) in `frontend/src/hooks/useAssistantSession.ts`.
