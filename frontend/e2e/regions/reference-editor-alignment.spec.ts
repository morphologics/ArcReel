import type { Page } from "@playwright/test";
import { clearAgentOverlay } from "../support/region-helpers.ts";
import { defineRegionScenarios } from "../support/scenarios.ts";
import { recorded } from "../support/recorded.ts";
import { expect, test, type ApiOverrides } from "../support/test.ts";

// The reference-to-video script editor is two stacked layers: a bottom <pre> does coloring, a top <textarea> with transparent text handles
// caret and selection. If the two layers differ in font, glyph width, wrapping or usable width anywhere, the caret and selection land beside
// the visible text.
// 参考生视频的文稿编辑器是两层叠放：底层 <pre> 负责着色，上层文字透明的 <textarea> 负责光标与选区。
// 两层的字体、字宽、折行与可用宽度只要有一处不同，光标与选区就落在可见文字旁边。

// Headless Chromium defaults to --hide-scrollbars, so scrollbars take no width and "the input gets narrower once a scrollbar appears, and
// wrapping diverges from the mirror layer" cannot be tested. This file restores scrollbars, using the page's real 10px custom scrollbar;
// launchOptions can only be set at the file top level, so this group of scenarios is its own file and other files' screenshots are unaffected.
// This setting only takes effect when the browser is launched locally (CI and local `pnpm e2e`). The run-server used by `pnpm e2e:remote`
// filters the client's ignoreDefaultArgs, scrollbars stay hidden, and the scenarios skip with the reason noted.
// 无头 Chromium 默认带 --hide-scrollbars，滚动条不占宽度，「输入框出现滚动条后可用宽度变窄、
// 折行与镜像层分叉」就测不出来。本文件恢复滚动条，用页面上真实的 10px 自绘滚动条；
// launchOptions 只能在文件顶层设置，所以这组场景单独成文件，其他文件的截图不受影响。
// 这个设置只在本地启动浏览器时生效（CI 与本机 `pnpm e2e`）。`pnpm e2e:remote` 连接的 run-server
// 会过滤客户端传来的 ignoreDefaultArgs，滚动条仍被隐藏，场景跳过并注明原因。
test.use({ launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] } });

// Playwright connects to a remote run-server via this env var (`pnpm e2e:remote`); otherwise it launches the browser locally.
// Playwright 经这个环境变量连接远端 run-server（`pnpm e2e:remote`），否则在本地启动浏览器。
const viaRunServer = Boolean(process.env.PW_TEST_CONNECT_WS_ENDPOINT);

const UNITS_PATH = "/app/projects/demo/episodes/1?view=board";
const UNITS_API = "/api/v1/projects/demo/reference-videos/episodes/1/units";

interface RecordedProject {
  project: Record<string, unknown>;
}

const project = recorded<RecordedProject>("project-demo.json");

// Stress text: every line mixes base-look references, derivative references and numbers, with enough lines that the input shows a scrollbar even at the tallest viewport.
// 压力文本：每行都混着本体引用、衍生引用与数字，行数足以让输入框在最高的视口下也出现滚动条。
const MENTION_HEAVY_TEXT = Array.from(
  { length: 80 },
  (_, i) =>
    `镜头 ${i + 1}（第 ${i * 4} 秒起 3.5s）：@[林夕] 与 @[陈默] 在 ${i + 2} 号街口相遇，` +
    `@[林夕/雨夜] 撑开第 ${i * 7 + 1} 把旧伞，@[陈默/便装] 数着 ${1024 + i * 37} 块青石板。`,
).join("\n");

const UNIT_ID = "E1U1";

const API: ApiOverrides = {
  // The project event stream is SSE, not recorded; reject with a non-retryable status so the page stays on the replaced data.
  // 项目事件流是 SSE，没有录制；按不可重试的状态拒绝，页面停在替换后的数据上。
  "GET /api/v1/projects/demo/events/stream": { status: 404, body: { detail: "页面级套件不回放事件流" } },
  "GET /api/v1/projects/demo": {
    status: 200,
    body: {
      ...project,
      project: {
        ...project.project,
        generation_mode: "reference_video",
        characters: {
          林夕: { description: "茶馆二楼的说书人。", derivatives: { 雨夜: { description: "披上蓑衣。" } } },
          陈默: { description: "巡夜人。", derivatives: { 便装: { description: "换下号服。" } } },
        },
      },
    },
  },
  [`GET ${UNITS_API}`]: {
    status: 200,
    body: {
      units: [{ unit_id: UNIT_ID, text: MENTION_HEAVY_TEXT, duration_seconds: 8, note: null }],
      unit_capabilities: {
        [UNIT_ID]: {
          unit_id: UNIT_ID,
          declared_capability: "r2v",
          hydrated_capability: "r2v",
          declared_references: [],
          unavailable_references: [],
          unregistered_references: [],
          allowed_durations: [4, 8],
          excluded_durations: {},
          duration_endpoint_fixed: false,
          duration_endpoint_fixed_reason: null,
          problem: null,
          problems: [],
        },
      },
    },
  },
  "GET /api/v1/projects/demo/episodes/1/drafts": { status: 200, body: { episode: 1, drafts: [] } },
};

const editor = (page: Page) => page.getByRole("combobox", { name: "视频单元提示词" });

/** Measure scrollbar width with a probe without scrollbar-gutter, independent of the two layers under test; 0 means the browser hides scrollbars. */
/** 用不带 scrollbar-gutter 的探针量滚动条宽度，与被测的两层无关；为 0 说明浏览器隐藏了滚动条。 */
async function measureScrollbarWidth(page: Page): Promise<number> {
  return page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.cssText = "position: fixed; width: 100px; height: 100px; overflow: scroll";
    document.body.append(probe);
    const width = probe.offsetWidth - probe.clientWidth;
    probe.remove();
    return width;
  });
}

/** Confirm the browser shows scrollbars that take width. Skip only when connected via run-server; a locally launched browser hiding scrollbars always fails. */
/** 确认浏览器显示占宽度的滚动条。只有经 run-server 连接时跳过，本地启动的浏览器隐藏滚动条一律失败。 */
async function requireVisibleScrollbars(page: Page) {
  const scrollbarWidth = await measureScrollbarWidth(page);
  test.skip(
    viaRunServer && scrollbarWidth === 0,
    "run-server 会过滤 ignoreDefaultArgs，滚动条仍被隐藏；该场景只在 CI 与本机 pnpm e2e 下生效",
  );
  expect(scrollbarWidth, "浏览器应显示占宽度的滚动条：无头模式须去掉 --hide-scrollbars").toBeGreaterThan(0);
}

interface LayerAlignment {
  /** Content-box width of the input and the coloring mirror layer. / 输入框与着色镜像层的内容区宽度。 */
  textareaClientWidth: number;
  preClientWidth: number;
  /** Input content exceeds the visible height. / 输入框内容超出可视高度。 */
  overflowing: boolean;
  /** Number of characters actually compared that lie inside the input's visible area. / 实际比对过的、落在输入框可视区内的字符数。 */
  checked: number;
  /** The first few misaligned characters: their position in the coloring layer hits a different caret offset in the input (null = missed the input). */
  /** 前几个错位的字符：着色层里它所在的位置，在输入框里命中的是另一个光标偏移（null 为没命中输入框）。 */
  mismatches: { offset: number; char: string; caretOffset: number | null }[];
}

/**
 * Compare the two layers character by character in the real layout: take the point a quarter in from each mirror character's left edge and
 * ask the browser for the input's caret offset at that point. When the layers match in font, glyph width, wrapping and usable width, the hit
 * offset is that character itself; if either layer gains or loses width, offsets accumulate along the line and shift by whole segments at
 * wraps. Only characters inside the input's visible area are compared: the mirror scrolls with the input and those outside are clipped.
 *
 * 在真实布局里逐字比对两层：取着色镜像层每个字符左侧四分之一处，问浏览器输入框在这一点的光标偏移。
 * 两层字体、字宽、折行与可用宽度一致时，命中的偏移就是这个字符自己；任一层多出或少了宽度，
 * 偏移会沿行累积、在折行处整段错开。只比对输入框可视区内的字符：镜像层随输入框滚动，区外的被裁掉。
 */
async function measureLayerAlignment(page: Page): Promise<LayerAlignment> {
  return editor(page).evaluate((node) => {
    const textarea = node as HTMLTextAreaElement;
    const pre = textarea.parentElement!.querySelector("pre")!;
    const top = textarea.getBoundingClientRect().top + textarea.clientTop;
    const bottom = top + textarea.clientHeight;
    const mismatches: LayerAlignment["mismatches"] = [];
    let checked = 0;
    let offset = 0;
    const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode() as Text | null; text; text = walker.nextNode() as Text | null) {
      const content = text.data;
      for (let i = 0; i < content.length; i += 1) {
        const char = content[i];
        // Newlines have no clickable glyph; the trailing zero-width placeholder only gives body text ending in a newline an extra line and maps to no input character.
        // 换行符没有可点的字形；末尾的零宽占位只给换行结尾的正文撑出一行，不对应输入框里的字符。
        if (char === "\n" || char === "\u200b") continue;
        const range = document.createRange();
        range.setStart(text, i);
        range.setEnd(text, i + 1);
        const rect = range.getClientRects()[0];
        if (!rect || rect.width === 0) continue;
        const y = rect.top + rect.height / 2;
        if (y < top || y > bottom) continue;
        const caret = document.caretPositionFromPoint(rect.left + rect.width / 4, y);
        const caretOffset = caret?.offsetNode === textarea ? caret.offset : null;
        checked += 1;
        if (caretOffset !== offset + i && mismatches.length < 5) {
          mismatches.push({ offset: offset + i, char, caretOffset });
        }
      }
      offset += content.length;
    }
    return {
      textareaClientWidth: textarea.clientWidth,
      preClientWidth: pre.clientWidth,
      overflowing: textarea.scrollHeight > textarea.clientHeight,
      checked,
      mismatches,
    };
  });
}

async function expectLayersAligned(page: Page) {
  const alignment = await measureLayerAlignment(page);
  expect(alignment.overflowing, "压力文本应让输入框出现滚动条").toBe(true);
  expect(alignment.checked, "可视区内应有可比对的字符").toBeGreaterThan(100);
  expect(alignment.mismatches, "着色层字符位置与输入框光标偏移应逐字重合").toEqual([]);
  expect(alignment.preClientWidth, "两层可用宽度应一致").toBe(alignment.textareaClientWidth);
}

defineRegionScenarios("参考视频文稿编辑器", [
  {
    name: "含多个本体与衍生引用、数字的长文稿，滚动前后着色层与输入框逐字对齐",
    path: UNITS_PATH,
    api: API,
    ready: async (page) => {
      await page.getByRole("tab", { name: "视频单元" }).waitFor();
      await editor(page).waitFor();
    },
    act: async (page) => {
      await requireVisibleScrollbars(page);
      // The compact-tier Agent panel covers the right of the editor; collapse it before point-by-point hit testing
      // 紧凑档的 Agent 面板盖住编辑框右侧，逐点命中前先收起
      await clearAgentOverlay(page);
      await expect(editor(page)).toHaveValue(MENTION_HEAVY_TEXT);
      await expectLayersAligned(page);
      // Scroll to the bottom: after the mirror follows the input's scroll, the last lines still overlap character by character
      // 滚到底：镜像层跟随输入框滚动后，末尾几行仍逐字重合
      const scrollTop = await editor(page).evaluate((el) => {
        el.scrollTop = el.scrollHeight;
        return el.scrollTop;
      });
      expect(scrollTop).toBeGreaterThan(0);
      const pre = editor(page).locator("xpath=preceding-sibling::pre");
      await expect.poll(() => pre.evaluate((el) => el.scrollTop)).toBe(scrollTop);
      await expectLayersAligned(page);
    },
  },
]);
