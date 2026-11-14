# Frontend primitives and test stubbing

Read this when installing or first using a primitive from `components/ui/`, writing tests for primitive components, or stubbing outbound calls. Review rules for primitive usage are in `docs/standards/frontend.md`.

## Installing primitives

Whichever change first uses a primitive runs `pnpm exec shadcn add <component>` (using the `shadcn` version locked in `package.json`) and commits the dependencies the CLI brings in along with it. Primitives not needed yet are not pre-installed; knip reports unused files and dependencies. Sets of primitives that need uniform rework (such as the various overlays) may be installed before their consumers; unreferenced files are temporarily registered in `ignore` in `knip.jsonc`, with a comment saying the first consumer removes the entry. Do not use `--overwrite` on already installed components: component source comes from the online registry, the locked version does not pin component content, and overwriting would wipe out local changes.

## Differences between Base UI and Radix

Most shadcn docs and community examples are based on Radix, whereas this repository's primitives are built on Base UI (`style` in `components.json` is `base-nova`). Type checking will not catch the following differences:

- A Popover in `modal` mode must have a Close inside the Popup.
- Portal renders an extra `<div>` layer, so selectors and styles that depend on DOM hierarchy need adjusting accordingly.
- The overlays of Combobox and Select open asynchronously after a click; in tests, wait with `findByRole("listbox")` or `findByRole("option")`. The Combobox search box is inside the overlay, and the trigger button has `role="combobox"`.
- Checkbox and Radio render as `span`s with roles; the disabled state is `aria-disabled="true"`, so `toBeDisabled()` does not hold.
- Collapsible unmounts the panel content when collapsed: click the trigger button open first, then query elements inside the panel; check the expanded state via the trigger button's `aria-expanded`.
- Slider thumbs cannot be found by role in jsdom; single-value sliders that need role-based tests keep the native `<input type="range">`.

For components not listed above, test keyboard, focus and close behaviour in a browser the first time you use them.

## Primitive styling

`no-restyle` only allows layout class names: spacing, borders and colours go on an ordinary element wrapping the primitive, and the primitive itself only uses margin to separate from it. Write collapse triggers as `<CollapsibleTrigger render={<Button variant="ghost" size="sm" />}>`, and rotate the expand icon with `group-aria-expanded/button:rotate-90`.

When `render` is passed a primitive component such as `Button`, the class names on it are checked against that component's contract; when passed a native element or a non-primitive component such as `Link`, they are checked against the outer primitive. Triggers that carry no styling of their own, and `Collapsible`, are registered in `UNSTYLED_PRIMITIVES` in `eslint.config.js` and are not restricted by class category; other primitives still only allow layout class names.

## Test stubbing

New outbound calls go through the `API` class so that tests can stub at that layer: `vi.spyOn(API, method)`. Tests of `api.ts` itself use hand-written fetch / Response stubs. Neither unit tests nor page-level suites (replaying recorded doubles; see `docs/standards/testing.md`) bring in msw. For SSE, use `FakeSseStream` from `src/test/fakeSseStream.ts`, returned by a spy on `API.openProjectEventStream` / `API.openAssistantEntriesStream`; `openSseStream` and the streaming wrappers in `api.ts` use `src/test/fakeSseFetch.ts`.
