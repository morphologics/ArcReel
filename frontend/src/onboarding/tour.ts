/**
 * Thin adapter over driver.js.
 *
 * It exists to isolate the business concept of "tour steps" from the driver.js API: steps describe only the anchor name and
 * copy, while the anchor -> selector mapping, button copy, skin and exit-path funneling are all given here once. Later segments
 * adding steps only write TourStep and never touch driver config.
 *
 * Anchor convention: `anchor` comes from the `anchors.ts` registry and maps to `[data-onboarding="<name>"]`;
 * when null the step highlights no element and driver renders a centered popover (used for the opening and closing). When the anchor is currently
 * not in the DOM (target not rendered yet after cross-page navigation, or the element was removed), wait `anchorWaitMs` and then degrade
 * to a centered popover and keep going, without skipping or aborting, while leaving a clue via `console.warn`.
 *
 * driver.js 薄适配层。
 *
 * 存在的理由是把「引导步骤」这个业务概念与 driver.js 的 API 隔开：步骤只描述锚点名和
 * 文案，锚点→选择器的映射、按钮文案、皮肤、退出路径收口都在这里一次性给定。后续段落
 * 新增步骤时只写 TourStep，不碰 driver 配置。
 *
 * 锚点约定：`anchor` 取自 `anchors.ts` 的注册表，映射到 `[data-onboarding="<名字>"]`；
 * 为 null 时该步不高亮任何元素，driver 渲染居中气泡（开场与收尾用这个形态）。锚点当下
 * 不在 DOM 里（跨页导航后目标尚未渲染、或元素被改没了）时，等 `anchorWaitMs` 后降级
 * 成居中气泡继续讲，不跳步、不中止，同时 `console.warn` 留线索。
 */

import { driver, type Driver, type DriveStep, type PopoverDOM } from "driver.js";
import type { OnboardingAnchor } from "./anchors";

export interface TourStep {
  /** Anchor name -> `[data-onboarding="…"]`; null = centered popover, no element highlighted / 锚点名 → `[data-onboarding="…"]`；null = 居中气泡，不高亮元素 */
  anchor: OnboardingAnchor | null;
  title: string;
  body: string;
  /**
   * Route the step needs; the caller defines it and its semantics are not interpreted here -- passed through as is, for the caller to compare in
   * `onStepChange` whether navigation is needed. Omitted = no particular route required, keep going on the current page
   * (how centered steps other than the opening welcome popover usually use it).
   *
   * 该步所需的路由，调用方自行定义、这里不解释语义——纯粹原样透传，供调用方在
   * `onStepChange` 里比对是否需要导航。省略 = 不要求特定路由，留在当前页继续讲
   * （开场欢迎气泡以外的居中步通常这样用）。
   */
  route?: string;
  /**
   * Query params the step needs. Like `route`, passed through as is with no semantics interpreted here -- for the caller to compare and navigate when switching
   * in-page sections by query param under the same pathname (e.g. the settings page's `section`).
   *
   * 该步所需的查询参数。与 `route` 一样原样透传、这里不解释语义——供调用方在同一
   * pathname 下按查询参数切换页面内分区（如设置页的 `section`）时比对与导航。
   */
  query?: Record<string, string>;
  /**
   * Whether the step allows clicking the highlighted element itself. Defaults to inheriting the global `disableActiveInteraction: true`
   * (prevents clicking whatever is being explained and accidentally triggering generation). Set true only when the step's landing action is navigation (e.g. clicking into the demo
   * workbench) rather than a write -- otherwise the highlighted element is unclickable for the whole tour.
   *
   * 该步是否允许点击高亮元素本身。默认继承全局 `disableActiveInteraction: true`
   * （防止讲到哪点到哪，意外触发生成动作）。仅当该步的落点动作就是导航（如点进演示
   * 工作台）而非写操作时才置 true——否则高亮元素在整个引导期间都点不到。
   */
  interactive?: boolean;
  /**
   * Route (prefix, including subroutes) an `interactive` step lands on after clicking the anchor. The caller defines it and here
   * its semantics are not interpreted -- passed through as is, so the caller can tell "the user followed this step's entry" from "ran off elsewhere";
   * the former advances the tour to the next step, the latter is still dragged back by forced navigation. Only meaningful when `interactive` is true.
   *
   * `interactive` 步点击锚点后会落到的路由（前缀，含其子路由）。调用方自行定义、这里
   * 不解释语义——原样透传，供调用方区分「用户顺着这一步的入口走了」与「跑到了别处」，
   * 前者把引导顺势推进到下一步，后者仍按强制导航拽回。只在 `interactive` 为真时有意义。
   */
  interactiveTarget?: string;
}

export interface TourLabels {
  next: string;
  prev: string;
  done: string;
  skip: string;
  close: string;
  /** Accessible progress text, e.g. "Step 1 of 2" / 进度的无障碍文本，如「第 1 步，共 2 步」 */
  progress: (current: number, total: number) => string;
}

export interface TourHandle {
  /** Which step we are on now (0-based). Used to continue on rebuild instead of going back to the start. / 当前停在第几步（0 基）。用于重建时接着讲，而不是退回开头。 */
  currentIndex: () => number;
  /** Tear down proactively (component unmount etc.). Does not trigger onExit. / 主动收起（组件卸载等）。不触发 onExit。 */
  dispose: () => void;
}

/**
 * The overlay shares the `--scrim` token with Dialog and Sheet (both color and opacity are in the token). driver writes it into the
 * SVG path's inline `fill`; inline styles can reference CSS variables, so opacity is fixed at 1 and not computed separately.
 *
 * 遮罩与 Dialog、Sheet 共用 `--scrim` token（颜色与不透明度都在 token 里）。driver 把它写进
 * SVG 路径的内联 `fill`，内联样式可以引用 CSS 变量，因此透明度固定为 1，不再另算一份。
 */
const OVERLAY_FILL = "var(--scrim)";

/** Fade-in duration of overlay and popover, matching `duration-base`; driver's default 400ms exceeds the motion duration cap. / 遮罩与气泡的淡入时长，与 `duration-base` 一致；driver 默认 400ms，超出动效时长上限。 */
const FADE_MS = 200;

/**
 * Wait cap (ms) when an anchor is absent. During it driver attaches a MutationObserver waiting for the element;
 * if it appears it highlights normally, otherwise degrades to a centered popover. Long enough for target components to mount after cross-page navigation, short enough not to leave users
 * staring at an empty overlay.
 *
 * 锚点缺席时的等待上限（毫秒）。driver 在这段时间里挂 MutationObserver 等元素出现，
 * 等到就正常高亮，等不到就降级居中气泡。够跨页导航后目标组件挂载完，又不至于让用户
 * 对着空遮罩发呆。
 */
const ANCHOR_WAIT_MS = 1500;

export function anchorSelector(anchor: OnboardingAnchor): string {
  return `[data-onboarding="${anchor}"]`;
}

/**
 * driver's own highlight-box movement and popover fade-in are element-level animations in its own styles, which the `.arc-tour` skin
 * cannot override. Users who declared reduced motion would see 11 large cross-page movements during the tour, so turn off
 * driver's animation switch here and have each step snap into place instantly -- the explanation content is unchanged.
 *
 * `matchMedia` may be absent in test environments (jsdom without a stub); when unavailable treat it as "not reduced".
 *
 * driver 自带的高亮框位移与气泡淡入是元素级动画，写在它自己的样式里，`.arc-tour` 皮肤
 * 覆盖不到。声明了 reduced motion 的用户整场引导会看到 11 次跨页大幅位移，这里直接关掉
 * driver 的动画开关，改为逐步瞬间就位——讲解内容一字不少。
 *
 * `matchMedia` 在测试环境（jsdom 未 stub 时）可能缺席，取不到就按「不减弱」处理。
 */
function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * driver.js isolates the underlying UI only with `pointer-events: none` and a focus trap that intercepts only Tab,
 * without touching the accessibility tree -- a screen reader's virtual cursor can bypass both and read and activate
 * the underlying workbench controls during the tour. Here we explicitly mark body's existing children `inert`, removing them from the accessibility tree,
 * and restore on tour exit.
 *
 * Not just `#app-root` (mount point in main.tsx): overlays like Dialog and Sheet are portaled
 * directly into `document.body` as siblings of `#app-root`, not descendants, so
 * making only `#app-root` inert cannot cover the case "a dialog was already open when the tour started". Instead, snapshot
 * body's direct children at call time and mark each inert.
 *
 * Isolation is applied/removed as a whole only once at tour start/end; `interactive` steps do not remove it wholesale -- that would
 * open up "New project", other real project cards, the settings entry and so on in `#app-root`, not just the target
 * element. Instead `interactive` steps have `openInteractiveHole` punch a hole leading only to the target element
 * (see below); driver's own overlay/popover are always excluded from the snapshot and are unaffected.
 *
 * driver.js 只用 `pointer-events: none` 和一个仅拦截 Tab 键的焦点陷阱隔离底层界面，
 * 不触及无障碍树——屏幕阅读器的虚拟光标导航能绕开这两者，在引导期间直接读到并激活
 * 底层工作台的控件。这里显式给 body 的既有子节点打 `inert`，把它们从无障碍树摘除，
 * 引导退出时复原。
 *
 * 不止 `#app-root`（挂载点见 main.tsx）：Dialog、Sheet 等弹层经 Portal
 * 直接挂到 `document.body`，是 `#app-root` 的兄弟节点而非子孙，只
 * 打 `#app-root` 的 inert 罩不住"引导启动时已有弹窗开着"这种情形。这里改为在调用
 * 时刻快照 body 的直接子节点、逐个打 inert。
 *
 * 隔离只在引导启动/结束时整体施加/解除一次，`interactive` 步不整体解除——那样会
 * 把 `#app-root` 里「新建项目」、其他真实项目卡片、设置入口等一并开放，不只是目标
 * 元素。`interactive` 步改由 `openInteractiveHole` 单独凿一个只通向目标元素的孔
 * （见下方），driver 自己的 overlay/popover 因为一直被排除在快照之外，不受影响。
 */
const DRIVER_PORTAL_SELECTOR = ".driver-overlay, .driver-popover, #driver-dummy-element";

let peripheralElements: Array<[element: HTMLElement, wasInert: boolean]> = [];
/**
 * Whether isolation is currently applied -- avoids re-snapshotting when switching between `interactive` steps, which would record
 * the "inert from the previous isolation" state as the original value and make restoration fail.
 */
/** 当前是否已施加隔离——避免 `interactive` 步之间来回切换时重复快照，把「已因上次
 *  隔离而变 inert」的状态误当作原始值记下，导致复原时回不去。 */
let isolationApplied = false;

/**
 * `inert` cannot remove global keyboard listeners that underlying dialogs attach to `document`/`window` -- listeners like Esc-to-close
 * and Enter-to-submit do not care who is in the accessibility tree,
 * and would still fire during the tour, quietly closing dialogs or even submitting forms behind the overlay. Having each listener
 * check tour state itself is bound to miss some, so intercept uniformly instead: while the tour is active, in document's capture phase
 * stop all `keydown` (except `Tab`, let through to driver's own focus trap). driver's own
 * Esc/arrow handling is on `keyup`, not `keydown`, so it is unaffected.
 *
 * `inert` 摘不掉底层弹窗自己挂在 `document`/`window` 上的全局键盘监听——Esc 关闭、
 * Enter 提交这类监听不看谁在无障碍树里，
 * 引导期间照样会被触发，在遮罩后台悄悄关弹窗、甚至提交表单。逐个让每个监听器自行
 * 判断引导状态属于挂一漏万，这里改为统一拦截：引导激活期间在 document 的捕获阶段
 * 拦下所有 `keydown`（`Tab` 除外，放行给 driver 自己的焦点陷阱）。driver 自己的
 * Esc/方向键处理挂在 `keyup` 而非 `keydown`，不受影响。
 */
let suspendKeyboard: (() => void) | null = null;

function setPeripheralIsolation(hidden: boolean): void {
  if (hidden === isolationApplied) return;
  isolationApplied = hidden;
  if (hidden) {
    peripheralElements = Array.from(document.body.children)
      .filter((el): el is HTMLElement => el instanceof HTMLElement)
      .filter((el) => !el.matches(DRIVER_PORTAL_SELECTOR))
      .map((el) => [el, Boolean(el.inert)]);
    peripheralElements.forEach(([el]) => {
      el.inert = true;
    });

    const onKeyDownCapture = (e: KeyboardEvent) => {
      if (e.key !== "Tab") e.stopPropagation();
    };
    document.addEventListener("keydown", onKeyDownCapture, true);
    suspendKeyboard = () => document.removeEventListener("keydown", onKeyDownCapture, true);
  } else {
    peripheralElements.forEach(([el, wasInert]) => {
      el.inert = wasInert;
    });
    peripheralElements = [];

    suspendKeyboard?.();
    suspendKeyboard = null;
  }
}

let interactiveHoleElements: Array<{ el: HTMLElement; prevInert: boolean }> = [];

/**
 * Punch a hole leading only to the target element for an `interactive` step. `inert` cannot be overridden by descendants (as above),
 * so `inert` must be removed from every node itself on the ancestor chain from the target element to `document.body`; at the same time
 * every other sibling at each level of the chain is explicitly made `inert` (most already are from the whole isolation; this only
 * handles intermediate levels of the chain not covered by the top-level snapshot), ensuring only the target element's path is reachable,
 * rather than opening the whole `#app-root` along with it. The chain nodes' own original `inert` values must be recorded too -- otherwise
 * `closeInteractiveHole` would restore only siblings, and chain nodes (including `#app-root`) would stay at
 * `inert = false` until `setPeripheralIsolation(false)` fixes them incidentally at the end of the whole tour,
 * and subsequent steps in between would wrongly keep this path reachable.
 *
 * When making siblings inert, `DRIVER_PORTAL_SELECTOR` must also be excluded -- when the chain goes all the way up to
 * `document.body`, `#app-root`'s siblings include driver's own overlay/popover/
 * dummy-element; without excluding them its own Next/Previous/Close buttons would be locked too.
 *
 * 为 `interactive` 步凿一个只通向目标元素的孔。`inert` 不能被后代自行覆盖（同上），
 * 因此要把目标元素到 `document.body` 祖先链上每一层节点自身的 `inert` 解除；同时
 * 把链上每层的其余兄弟节点显式打成 `inert`（多数已因整体隔离而是 `inert`，这里只
 * 处理链路本身此前未被顶层快照覆盖到的中间层），确保只有目标元素这一条路径可达，
 * 而不是连带打开整个 `#app-root`。链路节点自身原始的 `inert` 值也要记下——不记的话
 * `closeInteractiveHole` 只会复原兄弟节点，链路节点（含 `#app-root`）会一直停留在
 * `inert = false`，直到整场引导结束才被 `setPeripheralIsolation(false)` 顺带修正，
 * 期间的后续步骤都会误留这条路径可达。
 *
 * 打兄弟节点 inert 时同样要排除 `DRIVER_PORTAL_SELECTOR`——链路一路往上走到
 * `document.body` 时，`#app-root` 的兄弟节点里就包含 driver 自己的 overlay/popover/
 * dummy-element，不排除的话会连自己的 Next/Previous/Close 按钮一起锁死。
 */
function openInteractiveHole(target: HTMLElement): void {
  let node: HTMLElement | null = target;
  while (node && node !== document.body) {
    const parent: HTMLElement | null = node.parentElement;
    if (parent) {
      Array.from(parent.children).forEach((sibling) => {
        if (
          sibling === node ||
          !(sibling instanceof HTMLElement) ||
          sibling.inert ||
          sibling.matches(DRIVER_PORTAL_SELECTOR)
        )
          return;
        sibling.inert = true;
        interactiveHoleElements.push({ el: sibling, prevInert: false });
      });
    }
    interactiveHoleElements.push({ el: node, prevInert: node.inert });
    node.inert = false;
    node = parent;
  }
}

function closeInteractiveHole(): void {
  interactiveHoleElements.forEach(({ el, prevInert }) => {
    el.inert = prevInert;
  });
  interactiveHoleElements = [];
}

/**
 * driver writes `aria-haspopup="dialog"`, `aria-expanded`, `aria-controls` on the highlighted element and deletes all three
 * on leaving. These attributes are invalid on non-interactive elements like div and section (axe's aria-allowed-attr),
 * and deleting them also erases the trigger's own same-name attributes (e.g. the "New project" dropdown button). During the tour the UI is inert as a whole,
 * so these attributes have nothing for screen readers to announce; snapshot the originals before each highlight and restore after driver writes or deletes them.
 *
 * driver 给高亮元素写 `aria-haspopup="dialog"`、`aria-expanded`、`aria-controls`，离开时把这三个
 * 属性整组删掉。这组属性写在 div、section 这类非交互元素上不合法（axe 的 aria-allowed-attr），
 * 删除时还会抹掉触发器自己的同名属性（如「新建项目」的下拉按钮）。引导期间界面整体是 inert，
 * 这组属性没有读屏可以宣读的对象，因此每次高亮前快照原值，driver 写完或删完后还原。
 */
const DRIVER_ARIA = ["aria-haspopup", "aria-expanded", "aria-controls"] as const;

/** Placeholder element driver highlights for centered popovers; callbacks do not receive it, so get it by id. / 居中气泡时 driver 高亮的占位元素，回调里拿不到它，按 id 取。 */
const DRIVER_DUMMY_ID = "driver-dummy-element";

function snapshotAria(snapshots: Map<Element, (string | null)[]>, el: Element): void {
  if (!snapshots.has(el)) snapshots.set(el, DRIVER_ARIA.map((name) => el.getAttribute(name)));
}

function restoreAria(snapshots: Map<Element, (string | null)[]>): void {
  snapshots.forEach((values, el) => {
    DRIVER_ARIA.forEach((name, i) => {
      const value = values[i];
      if (value == null) el.removeAttribute(name);
      else el.setAttribute(name, value);
    });
  });
  const dummy = document.getElementById(DRIVER_DUMMY_ID);
  DRIVER_ARIA.forEach((name) => dummy?.removeAttribute(name));
}

/**
 * Call back once on the next frame when the page structure changes (nodes added/removed, text changed). driver re-aligns the highlight box only on window resize and scroll;
 * content above the anchor rendering after it was highlighted (e.g. returning from settings to the lobby, the greeting area appears after the "Sample project"
 * section) pushes the anchor away while the highlight box stays put. Attribute changes are not observed: driver only writes attributes when aligning itself.
 *
 * 页面结构变化时（增删节点、改文字）在下一帧回调一次。driver 只在窗口缩放与滚动时重新对齐
 * 高亮框；锚点高亮之后，排在它上方的内容才渲染出来（如从设置页回到大厅，问候区晚于「示例项目」
 * 区块出现），会把锚点推开，高亮框却留在原位。不观察属性变化：driver 自己对齐时只写属性。
 */
function observeLayoutChanges(onChange: () => void): () => void {
  let frame = 0;
  const observer = new MutationObserver(() => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      onChange();
    });
  });
  observer.observe(document.body, { childList: true, characterData: true, subtree: true });
  return () => {
    observer.disconnect();
    cancelAnimationFrame(frame);
  };
}

/** Progress perforation track -- decoration; semantics carried by the sibling sr-only text / 进度齿孔轨道 —— 装饰，语义由同级的 sr-only 文本承载 */
function renderProgress(progress: HTMLElement, current: number, total: number, label: string): void {
  progress.replaceChildren();

  const strip = document.createElement("span");
  strip.className = "arc-tour-filmstrip";
  strip.setAttribute("aria-hidden", "true");
  for (let i = 0; i < total; i += 1) {
    const cell = document.createElement("span");
    cell.dataset.on = i <= current ? "1" : "0";
    strip.appendChild(cell);
  }

  const sr = document.createElement("span");
  sr.className = "arc-tour-sr-only";
  sr.textContent = label;

  progress.appendChild(strip);
  progress.appendChild(sr);
}

/**
 * Start the tour.
 *
 * @param onExit Called once on any exit path (skip / close / finish); not called by `dispose()`.
 * @param startIndex Which step to start from (0-based). Defaults to 0; on rebuild pass the previous `currentIndex()`.
 * @param anchorWaitMs Wait cap when an anchor is absent, defaults to `ANCHOR_WAIT_MS`.
 * @param onStepChange When "Next/Previous" is triggered (button clicks and arrow keys share one internal callback,
 *   see `onNextClick`/`onPrevClick`), synchronously reports the upcoming step number before driver actually switches the highlight.
 *   The caller can navigate ahead of driver trying to highlight the new step's anchor -- the inherent
 *   timing gap is absorbed by driver's own `waitForElement` polling, no extra wait logic needed.
 *
 * 启动引导。
 *
 * @param onExit 任一退出路径（跳过 / 关闭 / 走完）都会调用一次；`dispose()` 不调用。
 * @param startIndex 从第几步开始（0 基）。默认 0；重建时传入上一次的 `currentIndex()`。
 * @param anchorWaitMs 锚点缺席时的等待上限，默认 `ANCHOR_WAIT_MS`。
 * @param onStepChange 「下一步/上一步」被触发时（按钮点击与方向键都走同一条内部回调，
 *   见 `onNextClick`/`onPrevClick`）、在 driver 实际切换高亮之前，同步上报即将停靠的
 *   步号。调用方可以据此在 driver 尝试高亮新步骤的锚点之前先行导航——两者天然存在的
 *   时间差由 driver 自己的 `waitForElement` 轮询吸收，不需要额外的等待逻辑。
 */
export function startTour(
  steps: TourStep[],
  labels: TourLabels,
  {
    onExit,
    startIndex = 0,
    anchorWaitMs = ANCHOR_WAIT_MS,
    onStepChange,
  }: {
    onExit: () => void;
    startIndex?: number;
    anchorWaitMs?: number;
    onStepChange?: (index: number) => void;
  },
): TourHandle {
  const total = steps.length;
  let exited = false;
  let disposing = false;
  const ariaSnapshots = new Map<Element, (string | null)[]>();
  // Whether the highlight box has settled on the current anchor. During transitions driver moves the box frame by frame itself, so do not re-align then;
  // page changes during the transition are aligned together on settling (see onHighlighted).
  // 高亮框是否已停在当前锚点上。转场途中 driver 自己逐帧移动高亮框，这时不重新对齐，
  // 途中的页面变化留到落定时一并对齐（见 onHighlighted）。
  let highlightSettled = false;

  const driveSteps: DriveStep[] = steps.map((step) => ({
    ...(step.anchor === null ? {} : { element: anchorSelector(step.anchor) }),
    data: { anchor: step.anchor, interactive: Boolean(step.interactive) },
    ...(step.interactive ? { disableActiveInteraction: false } : {}),
    popover: { title: step.title, description: step.body },
  }));

  const instance: Driver = driver({
    steps: driveSteps,
    popoverClass: "arc-tour",
    animate: !prefersReducedMotion(),
    duration: FADE_MS,
    overlayColor: OVERLAY_FILL,
    overlayOpacity: 1,
    stagePadding: 8,
    stageRadius: 10,
    // Read-only throughout: driver's `.driver-active *{pointer-events:none}` already seals the underlying UI,
    // and here the highlighted element's own interaction is turned off too, ruling out "clicking whatever is being explained" accidentally triggering generation.
    // 全程只读：driver 的 `.driver-active *{pointer-events:none}` 已经封死了底层界面，
    // 这里再关掉高亮元素本身的交互，杜绝"讲到哪就能点到哪"意外触发生成动作。
    disableActiveInteraction: true,
    showProgress: true,
    showButtons: ["next", "previous", "close"],
    // When the anchor is absent wait a while first (driver attaches a MutationObserver internally); on timeout do not skip the step --
    // the explanation still holds, only the highlight is lost; driver falls back to its placeholder element and centers the popover.
    // 锚点缺席时先等一会儿（driver 内部挂 MutationObserver），超时后不跳过这一步 ——
    // 讲解本身仍然成立，丢的只是高亮，driver 会退回自己的占位元素、把气泡摆到屏幕中央。
    waitForElement: anchorWaitMs,
    skipMissingElement: false,
    // driver's built-in arrow-key stepping calls moveNext()/movePrevious() directly, bypassing
    // onNextClick/onPrevClick below -- the cross-page onStepChange report would be skipped. Turn it off here,
    // and let the keyup listener at the end of this file take over Esc/arrows, so the keyboard path and button path share the same
    // reporting logic.
    // driver 自带的方向键切步直接调 moveNext()/movePrevious()，不经过下面的
    // onNextClick/onPrevClick——跨页导航的 onStepChange 上报会被绕过。这里关闭它，
    // 改由本文件末尾的 keyup 监听统一接管 Esc/方向键，确保键盘路径与按钮路径走同一条
    // 上报逻辑。
    allowKeyboardControl: false,
    nextBtnText: labels.next,
    prevBtnText: labels.prev,
    doneBtnText: labels.done,
    onPopoverRender: (popover: PopoverDOM) => {
      const current = instance.getActiveIndex() ?? 0;
      popover.closeButton.setAttribute("aria-label", labels.close);
      renderProgress(popover.progress, current, total, labels.progress(current + 1, total));
      decorateSkip(popover, instance.isLastStep(), labels.skip);
    },
    // When the highlighted element is driver's placeholder, the callback receives element as undefined. If the step
    // declared an anchor yet landed here, the anchor cannot be found on the page -- degradation already happened; this only leaves a clue.
    // On every switch first close the hole the previous step may have punched, then an `interactive` step punches a new one for the current target element --
    // making only the target reachable; the rest of `#app-root` (new project, other project cards, settings entry, etc.)
    // stays isolated and is not opened wholesale just because this step is interactive.
    // 高亮到的元素是 driver 的占位元素时，回调收到的 element 是 undefined。步骤本来就
    // 声明了锚点却落到这里，说明锚点在页面上找不到 —— 降级已经发生，这里只负责留线索。
    //
    // 每次切换先收起上一步可能凿开的孔，`interactive` 步再针对当前目标元素重新凿一个——
    // 只让目标元素可达，`#app-root` 里其余内容（新建项目、其他项目卡片、设置入口等）
    // 仍保持隔离，不因为这一步是 interactive 就整体开放。
    onHighlightStarted: (element, step) => {
      const anchor = step.data?.anchor as OnboardingAnchor | undefined;
      if (anchor && !element) {
        console.warn(`[onboarding] anchor "${anchor}" not found; falling back to a centered popover`);
      }
      closeInteractiveHole();
      if (step.data?.interactive && element instanceof HTMLElement) {
        openInteractiveHole(element);
      }
      if (element) snapshotAria(ariaSnapshots, element);
      highlightSettled = false;
    },
    // By now driver has written that set of aria attributes on the current element and deleted them from the previous one; restore both.
    // The popover is positioned at the start of the transition (halfway with animation) per the anchor's position then, and on settling driver only recomputes the highlight box:
    // if the anchor is pushed away in between (the greeting area renders after the "Sample project" section) the popover would cover the anchor, so align once more after settling.
    // driver 此时已给当前元素写上、给上一个元素删掉那组 aria 属性，两者都还原。
    // 气泡在转场开始时（有动画时在半程）按锚点当时的位置摆好，落定时 driver 只重算高亮框：
    // 锚点在这之间被推开（问候区晚于「示例项目」区块渲染），气泡会压在锚点上，所以落定后再对齐一次。
    onHighlighted: () => {
      restoreAria(ariaSnapshots);
      highlightSettled = true;
      instance.refresh();
    },
    // All exits funnel here rather than driver's onDestroyed. The latter fires only after driver internally writes the highlighted element
    // into state, and that write is queued in requestAnimationFrame -- with synchronous destroy
    // and in environments without DOM frames the callback is silently missed. Instead use the two entries we control, "buttons + proactive teardown",
    // so an exit is always recorded once.
    // 退出全部收口到这里，而不是 driver 的 onDestroyed。后者只在 driver 内部把高亮元素
    // 写进 state 之后才会触发，而那次写入排在 requestAnimationFrame 里 —— 同步 destroy
    // 与无 DOM 帧的环境下会静默漏掉回调。改走「按钮 + 主动收起」这两个我们自己掌握的
    // 入口，退出必然被记一次。
    onNextClick: () => handleNext(),
    onPrevClick: () => handlePrev(),
    onCloseClick: () => finish(),
    // Esc and clicking the overlay go through driver's internal teardown, calling back here before actually tearing down.
    // Esc 与点击遮罩走 driver 内部的收起流程，在真正拆掉之前回调这里。
    onDestroyStarted: () => finish(),
  });

  /** Next: shared by button clicks and the `ArrowRight` key, keeping cross-page reporting consistent. / 下一步：按钮点击与 `ArrowRight` 键共用，保证跨页上报一致。 */
  function handleNext(): void {
    if (instance.isLastStep()) {
      finish();
      return;
    }
    onStepChange?.((instance.getActiveIndex() ?? 0) + 1);
    instance.moveNext();
  }

  /**
   * Previous: shared by button clicks and the `ArrowLeft` key, keeping cross-page reporting consistent. The first step has nothing to go back to --
   * the button is disabled by driver then and will not fire, but the keyboard path has no such disabled state; if not blocked,
   * `instance.movePrevious()` would destroy the whole tour when driver.js internally finds no previous step.
   *
   * 上一步：按钮点击与 `ArrowLeft` 键共用，保证跨页上报一致。首步无上一步可退——
   * 按钮此时被 driver 禁用不会触发，但键盘路径没有这层禁用态，不挡住的话
   * `instance.movePrevious()` 会在 driver.js 内部找不到上一步时把整个引导销毁掉。
   */
  function handlePrev(): void {
    if (instance.isFirstStep()) return;
    onStepChange?.((instance.getActiveIndex() ?? 0) - 1);
    instance.movePrevious();
  }

  /** Record one exit and tear down. Repeated calls record only once. / 记一次退出并收起。重复调用只记一次。 */
  function finish(): void {
    if (!exited && !disposing) {
      exited = true;
      onExit();
    }
    window.removeEventListener("keyup", onKeyUp);
    stopRealign();
    closeInteractiveHole();
    setPeripheralIsolation(false);
    instance.destroy();
    restoreAria(ariaSnapshots);
    // driver returns focus to the element focused when the last step began, which is the removed popover button, so focus lands on body.
    // If a modal dialog was open before the tour started, Tab from body would skip the dialog's focus trap into the background page,
    // so return focus to the element focused before the tour started; skip if it was unmounted by a page change.
    // driver 把焦点还给最后一步开始时的焦点元素，那是已移除的气泡按钮，焦点于是落到 body。
    // 引导启动前开着模态对话框时，从 body 按 Tab 会越过对话框的焦点陷阱进到背景页面，
    // 这里还给引导启动前的焦点元素；它已随换页卸载时不处理。
    if (focusBeforeTour instanceof HTMLElement && focusBeforeTour.isConnected) focusBeforeTour.focus();
  }

  // With allowKeyboardControl off driver no longer handles Esc/arrows itself, so take over here: Esc goes to finish()
  // (consistent with driver's default allowClose behavior; this file does not override that config, whose default is true);
  // arrows reuse handleNext/handlePrev, sharing the reporting path with button clicks.
  // allowKeyboardControl 关闭后 driver 不再自行处理 Esc/方向键，这里接管：Esc 走 finish()
  // （与 driver 默认的 allowClose 行为一致，本文件未覆盖该配置，其默认值即为 true）；
  // 方向键复用 handleNext/handlePrev，与按钮点击走同一条上报路径。
  function onKeyUp(e: KeyboardEvent): void {
    if (e.key === "Escape") {
      finish();
    } else if (e.key === "ArrowRight") {
      handleNext();
    } else if (e.key === "ArrowLeft") {
      handlePrev();
    }
  }

  // Isolation would make the focused element inert and lose focus, so record it first / 隔离会让焦点所在的元素变 inert 而失焦，先记下
  const focusBeforeTour = document.activeElement;
  setPeripheralIsolation(true);
  window.addEventListener("keyup", onKeyUp);
  const stopRealign = observeLayoutChanges(() => {
    if (highlightSettled) instance.refresh();
  });
  instance.drive(Math.min(Math.max(startIndex, 0), total - 1));

  return {
    currentIndex: () => instance.getActiveIndex() ?? 0,
    dispose: () => {
      disposing = true;
      window.removeEventListener("keyup", onKeyUp);
      stopRealign();
      closeInteractiveHole();
      setPeripheralIsolation(false);
      instance.destroy();
      restoreAria(ariaSnapshots);
    },
  };
}

/** "Skip" button -- the last step has nothing to skip, leaving only "Done" / 「跳过」按钮 —— 最后一步没有可跳过的内容，只留「完成」 */
function decorateSkip(popover: PopoverDOM, isLastStep: boolean, label: string): void {
  popover.footer.querySelector(".arc-tour-skip-btn")?.remove();
  if (isLastStep) return;

  const skip = document.createElement("button");
  skip.type = "button";
  skip.className = "driver-popover-footer-btn arc-tour-skip-btn";
  skip.textContent = label;
  skip.addEventListener("click", () => popover.closeButton.click());
  popover.footer.insertBefore(skip, popover.footerButtons);
}
