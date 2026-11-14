# Chart Library Selection: Settings-page Trend Chart and Top-bar Sparkline

> Status: research complete; conclusions to be rolled up in the map issue [#2286](https://github.com/ArcReel/ArcReel/issues/2286).
> Current state: this document recommends visx, but what was eventually adopted was the runner-up, Recharts — as part of the shadcn/ui refactor the frontend adopted its Chart component (`frontend/src/components/ui/chart.tsx`, based on Recharts), which draws the usage records trend chart. The conclusions below are kept as they were at research time.
> Related: [#2393](https://github.com/ArcReel/ArcReel/issues/2393) (this ticket), [#2290](https://github.com/ArcReel/ArcReel/issues/2290) (settings-page usage records prototype, hand-drawn SVG trend chart).
> Data date: 2026-09-07. Versions, release dates, and licences come from the npm registry (`npm view`); repository activity from the GitHub REST / Search API (`gh api`); full bundle sizes from the bundlephobia API; tree-shaken sizes from local measurement with Vite 8.2.2 + rolldown (see §3.2); API facts from each library's official docs (Context7) and the source / type definitions installed in `node_modules`.

## Conclusion in One Sentence

**Recommend visx (imported per package, e.g. `@visx/shape`): SVG + React native, injects no fonts / colours / animation, stacked bars + tooltip measured at 28.5 KB gzip, all four dataviz mark rules achievable natively, and it maps one-to-one onto the structure of the hand-drawn prototype, with migration taking about half a day to a day; runner-up Recharts 3: declarative, keyboard accessibility built in, but 125.5 KB gzip, and both the top-corner rounding and segment-gap rules require a custom `shape`.**

## Conclusions at a Glance

| Question | One-line conclusion |
|---|---|
| 1 Hard-constraint screening | All seven candidates run under React 19 + Vite 8 + TS; echarts-for-react (supply-chain incident + an open React 19 typing issue) and uPlot (no official React wrapper, stacked bars rely on an example plugin) are weakest on the "React ecosystem" criterion. |
| 2 Size (stacked bars + tooltip, gzip) | uPlot 26.8 KB + 0.8 KB CSS < visx 28.5 KB < Chart.js 57.0 KB < Plot 94.6 KB < nivo 97.8 KB < Recharts 125.5 KB < ECharts 203.7 KB. |
| 3 How natively the mark rules are supported (≤ 24px / rounded top / segment gap / legend, four rules) | visx 4/4 (segment gap by reducing height in the render-prop); Chart.js, uPlot, Plot 3/4 each; Recharts, nivo, ECharts 2/4 each. Details in §3.3. |
| 4 Recommendation / runner-up | Recommend visx, runner-up Recharts; if size ever becomes the only deciding factor, consider Chart.js (canvas, incompatible with the oklch CSS variable system, colour values must be resolved manually). |
| 5 Migration effort | Prototype → visx: 0.5–1 person-day; prototype → Recharts: about 1 person-day; both additionally need a "table alternative" and tests, which are independent of the library. |

## 1. Environment Facts (verified in the repository)

- `frontend/pnpm-lock.yaml`: `react@19.2.8`, `react-dom@19.2.8`, `vite@8.2.2`, `tailwindcss@4.3.3`, `typescript@6.0.3`; `frontend/package.json` has no chart / d3 dependencies at all.
- `frontend/src/index.css`: Tailwind v4 `@theme` defines an oklch palette (`--color-hairline*`, `--color-text*`, `--color-accent*`, etc.) and fonts (`--font-sans` Inter, `--font-mono` JetBrains Mono); the `.num` class uses the mono font; `@custom-variant dark` exists, but the product is single-theme dark.
- `frontend/src/main.tsx`: `createRoot` mounts directly, **without** a `<StrictMode>` wrapper, and there is no `hydrateRoot` / SSR. SSR is therefore a non-requirement; StrictMode compatibility only affects whether it can be turned on in the future.
- Hand-drawn prototype `frontend/src/components/pages/settings/prototype/UsageTrendChart.tsx` (branch `prototype/2290-usage-records-settings`, 201 lines): computes `niceMax` / ticks / a `scaleBand`-equivalent slot width itself, measures width with `ResizeObserver`, uses a `<pattern>` diagonal-hatch fill for "cancelled", hand-draws a 4px rounded top in the path, 2px gaps between segments, an HTML tooltip on whole-bucket hover, `role="img"` + `aria-label`.
- dataviz skill `references/marks-and-anatomy.md` (bundled-skills 2.1.263): bars ≤ 24px, 4px rounding only at the data end, square corners at the baseline; 2px **surface-colour gaps** between segments rather than strokes; hairline 1px solid grid; legend always shown for ≥ 2 series; text uses text tokens, not series colours. `interaction.md`: each bar / segment in a bar chart has its own hover tooltip, and the tooltip lists all series for that X.

## 2. Candidates Verified One by One

The four "maintenance status" numbers for each library: latest release date, the repository's last commit, and the number of issues opened / closed since 2026-06-01 (GitHub Search API, `is:issue`).

### 2.1 Recharts 3.10.1 (MIT)

- Maintenance: 3.10.1 released 2026-07-25; last commit 2026-09-07; since 2026-06, 39 issues opened / 56 closed; 27.5k stars. peerDeps explicitly list React 19.
- React 19 / StrictMode: no open React 19 compatibility issues; the only StrictMode-related one is a ResponsiveContainer problem closed in 2022 (#2878).
- Accessibility: `accessibilityLayer` defaults to `true` in 3.x (`es6/state/rootPropsSlice.js`); the root `<svg>` gets `role="application"` + `tabIndex=0` (`es6/container/RootSurface.js`), and the left/right arrow keys drive the tooltip (`es6/state/keyboardEventsMiddleware.js`); charts accept `title` / `desc` props that render `<title>` / `<desc>`; `role` can also be overridden. The table alternative must be written ourselves. — [storybook Accessibility.mdx](https://github.com/recharts/recharts/blob/main/storybook/stories/API/Accessibility.mdx)
- Style customisation: SVG text has no injected font; `CartesianGrid`'s `stroke` / `strokeDasharray` are both props (defaulting to the theme's `theme.grid`; the dark theme only defines `stroke: '#3f3f46'`, marked `@experimental`); the tooltip can be replaced with our own HTML via the `content` prop; animation must be disabled by passing `isAnimationActive={false}` to each `<Bar>`.
- Stacking / tooltip: stacking via `stackId`; BarChart's tooltip defaults to `axis` triggering, i.e. the whole bucket (`es6/chart/BarChart.js: defaultTooltipEventType: "axis"`); `maxBarSize` caps width; `radius` supports four corners `[tl, tr, br, bl]` (`src/cartesian/Bar.tsx`); the `shape` prop accepts a custom component ([Customise.mdx](https://github.com/recharts/recharts/blob/main/storybook/stories/Customise.mdx)); `<defs><pattern>` can be written directly as a child element.
- Size: full bundle 147.5 KB gzip (bundlephobia); tree-shaken stacked bars + tooltip 125.5 KB gzip (includes redux toolkit, a d3 subset, react-smooth, etc.).

### 2.2 visx 4.0.0 (MIT, Airbnb)

- Maintenance: 4.0.0 released 2026-06-11 ("v4 official with React 19 support?" #2005 closed); last commit 2026-06-22; since 2026-06, 4 issues opened / 5 closed; 21k stars. peerDeps `react ^18 || ^19`.
- React 19 / StrictMode: the `@visx/tooltip` and `@visx/responsive` source no longer contain `findDOMNode` (the earlier StrictMode issues #883/#1009 are closed).
- Accessibility: visx only produces `<rect>` / `<path>`; the `<svg>` is written by us, so `role` / `aria-label` / `<title>` can be added freely; the table alternative must be written ourselves.
- Style customisation: `@visx/shape`, `@visx/scale`, `@visx/grid`, `@visx/pattern` carry no font / colour defaults; only `@visx/axis` tick labels default to `fill:#222; fontFamily:'Arial'; fontSize:10` (`esm/axis/AxisBottom.js`), which can be overridden with `tickLabelProps`, or we can simply keep the prototype's own tick drawing; `@visx/tooltip` has `defaultStyles`, and passing `unstyled` removes them (`esm/tooltips/Tooltip.js`). No animation.
- Stacking / tooltip: `BarStack` is based on d3-shape stack, and its children render-prop provides each segment's `x/y/width/height/color/bar.data` (`packages/visx-shape/src/shapes/BarStack.tsx`); `BarRounded` natively supports single-side rounding such as `top` / `topLeft` (`esm/shapes/BarRounded.js`); `PatternLines` outputs a `<pattern>` ([visx-pattern README](https://github.com/airbnb/visx/blob/master/packages/visx-pattern/Readme.md)); `useTooltip` + `TooltipWithBounds` / `useTooltipInPortal` handle tooltip positioning ([visx-tooltip README](https://github.com/airbnb/visx/blob/master/packages/visx-tooltip/Readme.md)); `ParentSize` handles responsiveness.
- Size: `@visx/shape` full bundle 10.7 KB gzip; tree-shaken stacked bars + grid + two axes + tooltip + pattern + ParentSize total 28.5 KB gzip; the sparkline (`LinePath` + `scaleLinear`) alone is 15.2 KB, with a 1.7 KB increment when shared with the stacked bars.

### 2.3 nivo 0.99.0 (MIT)

- Maintenance: 0.99.0 released 2025-05-23, no new release in the 15 months since; last commit 2026-07-21; since 2026-06, 2 issues opened / 3 closed; 14k stars. peerDeps include React 19.
- React 19 / StrictMode: the React 19 support issue #2678 is closed; one React 19 key-warning issue remains open (#2801, Choropleth legend, does not affect bar).
- Accessibility: the `@nivo/bar` type definitions natively have `role`, `ariaLabel`, `ariaLabelledBy`, `ariaDescribedBy`, `isFocusable`, `barAriaLabel`, `barAriaHidden` (`dist/types/types.d.ts` lines 178–197), the most complete SVG-level accessibility attributes of the seven candidates. The table alternative must be written ourselves.
- Style customisation: a `theme` object overrides `text.fontFamily/fontSize`, `grid.line`, `axis.ticks`, `tooltip` (`@nivo/theming/dist/types/types.d.ts`); `animate={false}` turns off animation, but react-spring is still in the bundle; the `tooltip` prop swaps in our own component.
- Stacking / tooltip: `groupMode="stacked"`; native patterns via `defs` + `fill` rules (`patternLinesDef`); `innerPadding` also applies between segments in stacked mode (the stacked generator in the source `nivo-bar.mjs` reads `innerPadding`); `borderRadius` is a single number, rounding all four corners at once (`types.d.ts` line 152); the tooltip triggers **per segment**, but the `ComputedDatum.data` received by the callback is the whole raw row, so the whole bucket can be rendered ourselves; no `maxBarWidth`, bar width is determined by container width × `padding`.
- Size: full bundle 79.9 KB gzip (`sideEffects: true`); tree-shaken stacked bars + pattern + tooltip 97.8 KB gzip (including `@nivo/core`, react-spring, a d3 subset).

### 2.4 react-chartjs-2 5.3.1 + Chart.js 4.5.1 (MIT, canvas)

- Maintenance: Chart.js 4.5.1 released 2025-10-13, last commit 2026-05-27, since 2026-06 10 issues opened / 2 closed; react-chartjs-2 5.3.1 released 2025-10-27, last commit 2026-05-26, since 2026-06 issues 0 / 0. React 19 peer added (#1235 / #1238 / #1308 closed).
- React 19 / StrictMode: no related issues; the wrapper layer just creates and destroys the instance in `useEffect`.
- Accessibility: with canvas, only `role="img"` + `aria-label` and fallback content can be placed on the `<canvas>` ([accessibility.html](https://www.chartjs.org/docs/latest/general/accessibility.html)); no per-segment semantics; the table alternative must be written ourselves.
- Style customisation: `Chart.defaults.font.family` for a global font; `animation: false`; canvas **cannot use CSS variables directly**, so Darkroom's oklch tokens must be resolved with `getComputedStyle` before being fed to `backgroundColor`; the hover tooltip can be switched to HTML via the `external` callback.
- Stacking / tooltip: `scales.x/y.stacked`; `interaction.mode: 'index', intersect: false` for a whole-bucket tooltip ([interactions.html](https://www.chartjs.org/docs/latest/configuration/interactions.html)); `maxBarThickness` caps width; `borderRadius` + `borderSkipped: 'middle'` rounds only the outermost segment when stacked (source `dist/chart.js` `setBorderSkipped`: when `edge === 'middle' && stack`, decided by `stack._top` / `_bottom`); pattern fills require building a `CanvasPattern` ourselves with `ctx.createPattern` ([colors.html](https://www.chartjs.org/docs/latest/general/colors.html)); no native option for segment gaps.
- Size: full bundle 68.4 KB gzip; tree-shaken registering only `BarController/BarElement/CategoryScale/LinearScale/Tooltip` 57.0 KB gzip; 62.1 KB with a line sparkline added.

### 2.5 uPlot 1.6.32 (MIT, canvas)

- Maintenance: 1.6.32 released 2025-03-14, no new release in the 18 months since; last commit 2026-04-22; since 2026-06, 5 issues opened / 2 closed; maintained by a single author. No official React wrapper; community `uplot-react@1.2.4` (2025-07-05); the official docs suggest calling `new uPlot()` / `setData()` / `destroy()` yourself in an effect ([llms.txt](https://context7.com/leeoniya/uplot/llms.txt)).
- Accessibility: canvas, same as Chart.js; the legend is a DOM element.
- Style customisation: `uPlot.min.css` must be imported (0.8 KB gzip); the type of `series.fill` is `CanvasRenderingContext2D['fillStyle'] | (self, idx) => fillStyle` (`dist/uPlot.d.ts` line 794), which can return a `CanvasPattern`; no animation.
- Stacking / tooltip: **no concept of stacking**; the official `stacked-series.html` / `bars-grouped-stacked.html` examples rely on pre-accumulated data + `bands` + a 200-line `seriesBarsPlugin`; `uPlot.paths.bars({ size: [factor, maxPx], radius, gap })`, where `radius` can return `[top, bottom]` per series (the `high-low-bands.html` example rounds only the top layer); `gap` is between adjacent bars, not between segments; no tooltip, requiring a plugin written with the `setCursor` hook (the `legendAsTooltipPlugin` example).
- Size: full bundle 21.9 KB gzip; measured stacked bars + uplot-react 26.8 KB gzip + 0.8 KB CSS. It is positioned for time-series lines with tens of thousands of points; categorical stacked bars are "doable, but you have to write a plugin".

### 2.6 @observablehq/plot 0.6.17 (ISC, SVG)

- Maintenance: 0.6.17 released 2025-02-14, no new release in the 19 months since; last commit 2026-09-01 (ongoing commits on main); since 2026-06, 9 issues opened / 3 closed, 251 open issues.
- React: imperative; in `useEffect`, `Plot.plot()` returns an SVG which is `append`ed to a ref and `remove()`d on unmount ([getting-started.md](https://github.com/observablehq/plot/blob/main/docs/getting-started.md)); SSR needs a virtual `document` passed in. It bypasses React reconciliation; hover is driven by Plot's own DOM events.
- Accessibility: top-level `ariaLabel` / `ariaDescription` are written to the root SVG ([accessibility.md](https://github.com/observablehq/plot/blob/main/docs/features/accessibility.md)).
- Style customisation: the root SVG hard-codes a `font-family="system-ui, sans-serif"` attribute (`src/plot.js` line 254), which must be overridden with the `style` option or `className` + CSS; no animation.
- Stacking / tooltip: `barY` + implicit `stackY`; single-side rounding such as `ry2` / `rx1y1` is native ([rect.md](https://github.com/observablehq/plot/blob/main/docs/marks/rect.md)); `inset` / `insetTop` give native segment gaps; `tip` + `pointerX` only show the channels of the nearest **single** datum, so a whole-bucket tooltip requires first pivoting the data into one row per bucket and then listing them with `channels` ([tip.md](https://github.com/observablehq/plot/blob/main/docs/marks/tip.md)); `fill` literals only accept CSS colours ([marks.md](https://github.com/observablehq/plot/blob/main/docs/features/marks.md)), `url(#pattern)` is not a colour, so pattern fills require modifying the DOM after rendering.
- Size: full bundle 128.0 KB gzip; tree-shaken measured 94.6 KB gzip (the whole d3 family comes along).

### 2.7 echarts-for-react 3.0.6 + ECharts 6.1.0 (MIT / Apache-2.0)

- Maintenance: ECharts 6.1.0 released 2026-05-19, last commit 2026-09-04, since 2026-06 40 issues opened / 195 closed, extremely active. echarts-for-react 3.0.6 released 2026-01-21, last commit 2026-01-21, since 2026-06 issues 1 / 0.
- **Supply-chain incident**: issue #625 of 2026-05-20 records that in the 2026-05-19 "Mini Shai-Hulud" npm attack, the maintainer's account was compromised and malicious versions `echarts-for-react 3.0.7 / 3.1.7 / 3.2.7` and `size-sensor 1.0.4 / 1.1.4 / 1.2.4` were published; the maintainer replied that all have been deprecated and npm was asked to remove them. There is also a React 19 JSX namespace typing error #628 (2026-07-03, open) and an issue where the ref / effect can't get the instance under StrictMode, #608 (open). If ECharts is chosen, write our own hook directly on `echarts/core` rather than pulling in this wrapper layer.
- Accessibility: with `aria.enabled`, an `aria-label` is generated automatically on the chart DOM (`lib/visual/aria.js` lines 134/206), customisable via `aria.label.description`; `aria.decal` provides built-in patterns ([aria.md](https://github.com/apache/echarts-handbook/blob/master/contents/en/best-practices/aria.md)).
- Style customisation: `textStyle.fontFamily` globally; `animation: false`; `SVGRenderer` optional; ECharts 6 ships a new default theme, so all colours must be overridden explicitly.
- Stacking / tooltip: native `stack`; `tooltip.trigger: 'axis'` for the whole bucket; `barMaxWidth` caps width; `itemStyle.borderRadius` is set per item (`lib/chart/bar/BarView.js` line 700 `el.setShape('r', ...)`), so rounding only the top layer requires writing `itemStyle` per datum per bucket; segment gaps can only be simulated with a stroke of `borderColor` = surface colour + `borderWidth: 2`; native patterns via `itemStyle.decal`.
- Size: full bundle 368 KB gzip; tree-shaken (`echarts/core` + BarChart + Tooltip + Grid + Aria + SVGRenderer + `echarts-for-react/lib/core`) 203.7 KB gzip, 214.8 KB with line added.

## 3. Side-by-side Comparison

### 3.1 Hard Constraints and Maintenance

| Library | Rendering | React 19 peer | Known StrictMode issues | Latest release | Last commit | Issues opened / closed (since 2026-06) | Licence |
|---|---|---|---|---|---|---|---|
| Recharts 3.10.1 | SVG | ✅ Explicit | None | 2026-07-25 | 2026-09-07 | 39 / 56 | MIT |
| visx 4.0.0 | SVG | ✅ Explicit | None | 2026-06-11 | 2026-06-22 | 4 / 5 | MIT |
| nivo 0.99.0 | SVG | ✅ Explicit | None (#2801 is a key warning) | 2025-05-23 | 2026-07-21 | 2 / 3 | MIT |
| react-chartjs-2 5.3.1 / Chart.js 4.5.1 | canvas | ✅ Explicit | None | 2025-10-27 / 2025-10-13 | 2026-05-26 / 2026-05-27 | 0 / 0 and 10 / 2 | MIT |
| uPlot 1.6.32 | canvas | No peer (own effect) | None | 2025-03-14 | 2026-04-22 | 5 / 2 | MIT |
| Plot 0.6.17 | SVG (imperative) | No peer (own effect) | None | 2025-02-14 | 2026-09-01 | 9 / 3 | ISC |
| echarts-for-react 3.0.6 / ECharts 6.1.0 | canvas or SVG | `>=16` (typing issue #628 open) | #608 open | 2026-01-21 / 2026-05-19 | 2026-01-21 / 2026-09-04 | 1 / 0 and 40 / 195 | MIT / Apache-2.0 |

### 3.2 Size (gzip, KB)

Measurement method: create a Vite 8.2.2 project in a scratch directory, `build.lib` single-entry ES output, with `react` / `react-dom` / `react/jsx-runtime` set as external, oxc minification, and gzip the output JS. The entry only imports the modules needed for stacked bars (three series) + tooltip + grid + axes; the "sparkline" entry only imports the line; "combined" imports both. The bundlephobia column is a reference value for the whole package without tree-shaking.

| Library | Stacked bars + tooltip | Sparkline alone | Combined | bundlephobia full package |
|---|---|---|---|---|
| visx | **28.5** | 15.2 | 30.2 | 10.7 (`@visx/shape` only) |
| uPlot (+uplot-react) | 26.8 + 0.8 CSS | — | 26.9 | 21.9 |
| Chart.js + react-chartjs-2 | 57.0 | 53.2 | 62.1 | 68.4 + 1.0 |
| Observable Plot | 94.6 | — | 102.0 | 128.0 |
| nivo | 97.8 | — | — | 79.9 (`@nivo/bar` only) |
| Recharts | 125.5 | 101.0 | 130.1 | 147.5 |
| ECharts (SVG renderer) + echarts-for-react/lib/core | 203.7 | 195.6 | 214.8 | 368.0 + 3.6 |

For comparison: the prototype's hand-drawn SVG has 0 KB of dependencies. If the top-bar popover sparkline uses the same library, the increment is only the "combined − stacked bars" figure (visx 1.7 KB, Recharts 4.6 KB).

### 3.3 Checking the dataviz Mark Rules One by One

"Native" = one prop / option; "semi-native" = the library provides the geometry and we draw it ourselves; "hack" = we have to work around the library's abstraction.

| Rule | Recharts | visx | nivo | Chart.js | uPlot | Plot | ECharts |
|---|---|---|---|---|---|---|---|
| Bars ≤ 24px | Native `maxBarSize` | Semi-native `Math.min(24, bandwidth)` | Hack (no maxWidth, back-calculate `padding` from container width) | Native `maxBarThickness` | Native `size: [f, 24]` | Hack (no maxWidth, back-calculate `padding`) | Native `barMaxWidth` |
| 4px rounded top, only on the top layer of the stack | Hack (`radius` is per series only; buckets whose top layer is 0 need a custom `shape`) | Native `BarRounded top` + render-prop decides the top layer | Hack (`borderRadius` rounds all four corners, needs a custom `barComponent`) | Native `borderRadius` + `borderSkipped:'middle'` | Native `radius` function returns `[top, 0]` per series | Native `ry2` / `rx1y1` (buckets whose top layer is 0 still need pivot handling) | Hack (per-datum `itemStyle.borderRadius`) |
| 2px surface-colour gap between segments | Hack (custom `shape` reduces height) | Semi-native (reduce height by 2px in the render-prop) | Native `innerPadding` | Hack (`borderWidth` surface-colour stroke) | Hack (subtract value when pre-stacking) | Native `insetTop` | Hack (`borderColor` surface-colour stroke) |
| "Cancelled" pattern fill | Native (child `<defs><pattern>` + `fill="url()"`) | Native `PatternLines` | Native `defs` + `fill` rules | Hack (hand-built `CanvasPattern`) | Semi-native (`fill` function returns `CanvasPattern`) | Hack (`fill` only accepts CSS colours, modify the DOM after rendering) | Native `decal` |
| Whole-bucket tooltip | Native (axis trigger) | Semi-native (write it ourselves, bucket data at hand) | Semi-native (triggers per segment, `data` is the whole row) | Native `mode:'index'` | Hack (plugin) | Hack (pivot + `channels`) | Native `trigger:'axis'` |
| Legend always shown | Native `<Legend>` | Own HTML (already in the prototype) | Native legends | Requires registering the Legend plugin (inside canvas) | DOM legend | `color.legend` | Native `legend` |
| Hairline solid grid / font / palette | Override prop by prop | No defaults to override (Axis is the exception) | Override in one place via `theme` | Global defaults + resolve CSS variables | CSS variables usable (DOM parts) | Override the inline `font-family` via `style` / `className` | Override globally via `textStyle` |
| Remove animation | `isAnimationActive={false}` on every element | No animation | `animate={false}` | `animation:false` | No animation | No animation | `animation:false` |

## 4. Recommendation and Trade-offs

**Recommendation: visx.** Reasons:

1. The mark rules are decisive: of dataviz's four hard rules, only visx (and Plot) achieve them without hacks; and Plot's React integration is imperative, with workarounds needed for both tooltips and patterns.
2. "Darkroom-level" customisability: visx injects no fonts, colours, animation, or CSS; every pixel is determined by our JSX, and Tailwind classes and oklch tokens can be used directly; Recharts / nivo require switching off defaults prop by prop, and the three canvas libraries require resolving CSS variables.
3. Size: 28.5 KB gzip is the smallest of the SVG options, with a 1.7 KB increment for the sparkline.
4. Same structure as the prototype: the prototype is already a "scale + stack + path + HTML tooltip" structure; visx merely replaces the hand-written `niceMax` / slot calculation / stack accumulation / ResizeObserver / tooltip positioning with d3-scale, BarStack, ParentSize, and useTooltip, and the JSX barely changes.
5. Maintenance: made by Airbnb, 4.0.0 explicitly supports React 19, few issues and they get responses.

Cost: visx is not "configure it and get a chart"; axes, legend, and tooltip visuals all have to be written ourselves (already written in the prototype); if we later need pie / radar or other forms, each one has to be assembled again.

**Runner-up: Recharts 3.** If the team values declarative code and out-of-the-box keyboard navigation / the `role="application"` accessibility layer more, Recharts is the most active and most stable choice for React 19; the cost is a size about 4.4 times visx, and both rounding and segment gaps (including top-layer detection) have to be crammed into a custom `shape` component, so the final "amount of hand-written code" is comparable to visx.

**Reasons for not recommending the others**: nivo hasn't released in 15 months, has no `maxBarWidth`, and rounds all four corners; Chart.js and uPlot (canvas) are incompatible with the oklch CSS variable system, per-segment semantics, and Tailwind tooltips; Plot hasn't released in 19 months, has imperative integration, and requires workarounds for both patterns and whole-bucket tooltips; ECharts is 200 KB+ and its wrapper layer has just been through a supply-chain incident.

## 5. Migration Effort Estimate (prototype → recommended library)

The prototype `UsageTrendChart.tsx` is 201 lines, including `mergeWeeks` / `niceMax` / hand-drawn paths / tooltip / `TrendLegend`.

Prototype → visx (0.5–1 person-day):

| Prototype code | Replace with | Notes |
|---|---|---|
| `niceMax` + `divisions` ticks | `scaleLinear({ domain, range, nice: true })` + `scale.ticks(4)` | Removes about 15 lines |
| `slot` / `barW` | `scaleBand({ domain, range, padding })` + `Math.min(24, bandwidth)` | |
| Hand-written stack accumulation | `BarStack keys={...} value={...}` render-prop | Keep the top-layer check (`segs.slice(idx+1).every(v===0)`) |
| Hand-drawn rounded path | `BarRounded top radius={4}` or keep the original path | The 2px segment gap is still subtracted in the render-prop |
| `<pattern>` | `PatternLines` | Or keep the original `<pattern>` |
| `ResizeObserver` | `ParentSize` | |
| `hover` state + `tipLeft` | `useTooltip` + `TooltipWithBounds unstyled` | Tooltip content JSX kept as is |
| Axis tick `<text>` | Keep hand-written (avoids `@visx/axis`'s Arial default) | |
| New | Visually hidden `<table>` table alternative, i18n keys, vitest render tests | Independent of the library; needed either way |

Sparkline (top-bar popover): `LinePath` + `curveMonotoneX` + two `scaleLinear`, about 30 lines, can share `@visx/scale` with the trend chart.

Prototype → Recharts (about 1 person-day): data must be reshaped into one row per bucket; write a `StackedSegment` shape component that handles rounding / top-layer detection / segment gaps / patterns; replace `Tooltip content` with the prototype tooltip; turn off animation on each `<Bar>`; `ResponsiveContainer` needs a fixed-height parent container; the accessibility layer comes for free.

## 6. List of Data Sources

- npm registry: `npm view <pkg> version license time`, 2026-09-07.
- GitHub: `gh api repos/<r>`, `repos/<r>/releases`, `repos/<r>/commits`, `search/issues?q=repo:<r>+is:issue+created:>2026-06-01`, 2026-09-07.
- bundlephobia: `https://bundlephobia.com/api/size?package=<pkg>@<ver>`, 2026-09-07.
- Tree-shaken sizes: local scratch project, Vite 8.2.2 / rolldown 1.2.7 / React 19.2.8, entries as described in §3.2.
- Docs: each library's official docs / repository Markdown fetched via Context7 (links in each subsection).
- Source: dist / type files of recharts 3.10.1, @visx/* 4.0.0, @nivo/bar 0.99.0, chart.js 4.5.1, uplot 1.6.32, @observablehq/plot 0.6.17, echarts 6.1.0 in `node_modules` (paths in each subsection).
- echarts-for-react supply-chain incident: [hustcc/echarts-for-react#625](https://github.com/hustcc/echarts-for-react/issues/625).
