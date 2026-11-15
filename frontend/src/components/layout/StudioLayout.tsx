import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useLocation } from "wouter";
import { useTranslation } from "react-i18next";
import { usePanelRef, type LayoutChangedMeta, type PanelSize } from "react-resizable-panels";
import { cn } from "cn";
import { ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { AgentCopilot } from "@/components/copilot/AgentCopilot";
import { useTaskRefresh } from "@/hooks/useTaskRefresh";
import { useProjectEventsSSE } from "@/hooks/useProjectEventsSSE";
import { useProjectsStore } from "@/stores/projects-store";
import { DemoAssistantPanel } from "@/onboarding/DemoAssistantPanel";
import { useDemoWorkbench } from "@/onboarding/use-demo-workbench";
import { isDemoProject } from "@/onboarding/demo-project";
import {
  ASSISTANT_PANEL_DEFAULT_WIDTH,
  ASSISTANT_PANEL_MAX_WIDTH,
  ASSISTANT_PANEL_MIN_WIDTH,
  useAppStore,
} from "@/stores/app-store";
import { GlobalHeader } from "./GlobalHeader";
import { useEditFocusStore } from "@/stores/edit-focus-store";
import { AssetSidebar } from "./AssetSidebar";
import { TaskFailureListener } from "./TaskFailureListener";
import { ScriptGenerationNoticeListener } from "./ScriptGenerationNoticeListener";
import { WorkspaceResizeHandle } from "./WorkspaceResizeHandle";
import {
  AGENT_PANEL_ID,
  AGENT_PANEL_TOGGLE_ID,
  CANVAS_MIN_WIDTH,
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_RAIL_WIDTH,
  persistSidebarCollapsed,
  persistSidebarWidth,
  readSidebarCollapsed,
  readSidebarWidth,
  sidebarAutoCollapseReason,
  useCompactTier,
} from "./workspace-layout";

// The resize handle takes 1px in document flow (the divider itself). / 调宽手柄在文档流里占 1px（分隔线本身）。
const HANDLE_WIDTH = 1;

interface StudioLayoutProps {
  children: React.ReactNode;
}

/**
 * Project workspace shell: the top bar spans the full width, below it three columns "sidebar | canvas | Agent panel", separated by resize handles.
 *
 * - The shell is the only place that switches tiers by viewport (1280). In the standard tier the Agent panel squeezes the canvas, which keeps at least 480px;
 *   in the compact tier the Agent panel is a non-modal layer over the right side of the canvas, Esc collapses it while focus is inside, and the sidebar collapses to an icon rail.
 * - The sidebar collapses to an icon rail automatically on single-episode pages and the episodes view; it can be expanded temporarily and resets on page change; leaving these pages returns to the user's own choice.
 * - The Agent panel's open state and width live in `app-store`; when collapsed its content stays mounted, so typed text and the session connection are unaffected.
 * - Size containers: sidebar `@container/sidebar`, canvas `@container/canvas` (width queries), Agent panel
 *   `@container-size/agent` (both width and height queryable; the input box's `40cqh` is computed from the panel height).
 *
 * 项目工作区外壳：顶栏横跨全宽，下方是「侧栏 | 画布 | Agent 面板」三栏，三栏之间用调宽手柄分隔。
 *
 * - 外壳是唯一按视口切换档位的地方（1280）。标准档的 Agent 面板挤压画布，画布至少保留 480px；
 *   紧凑档的 Agent 面板是覆盖在画布右侧的非模态层，焦点在面板内时 Esc 收起，侧栏收为图标栏。
 * - 侧栏在单集页与分集视图自动收为图标栏，可临时展开，换页后恢复；离开这些页面后回到用户自己的选择。
 * - Agent 面板的开合与宽度在 `app-store`；收起时内容保持挂载，输入中的文字与会话连接不受影响。
 * - 尺寸容器：侧栏 `@container/sidebar`，画布 `@container/canvas`（宽度查询），Agent 面板
 *   `@container-size/agent`（宽高都可查询，输入框的 `40cqh` 按面板高度计算）。
 */
/**
 * Run a panel resize / collapse now, or on the next frames if the panel library hasn't registered the panel yet (it
 * throws "Panel constraints not found" for a panel that just remounted, e.g. after the Edit view's focus mode).
 * Returns a cleanup that cancels a pending retry.
 */
function applyWhenRegistered(apply: () => void, attempts = 5): () => void {
  let frame = 0;
  const run = (left: number) => {
    try {
      apply();
    } catch (err) {
      if (left <= 0) throw err;
      frame = requestAnimationFrame(() => run(left - 1));
    }
  };
  run(attempts);
  return () => cancelAnimationFrame(frame);
}

export function StudioLayout({ children }: StudioLayoutProps) {
  const { t } = useTranslation("dashboard");
  const [location] = useLocation();
  const currentProjectName = useProjectsStore((s) => s.currentProjectName);
  // Demo projects do not exist on the backend: the task / project event streams and the Agent are real write paths, so none of them are connected in demo state
  // 演示项目在后端不存在：任务 / 项目事件流和 Agent 都是真实写路径，演示态下整条都不接
  const demoMode = useDemoWorkbench();

  // On a demo -> real switch demoMode becomes false before the store does; currentProjectName is checked separately
  // to cover the frame that still reads the old demo project name, avoiding an SSE connection to a nonexistent demo project that is bound to fail.
  // useTaskRefresh's projectName=null means "do not filter by project", not "disabled", so enabled must
  // mirror this check, otherwise that frame degrades into polling global tasks instead of really being disabled.
  // demoMode 演示→真实切换时先于 store 变为 false，currentProjectName 单独判一次
  // 兜住这一帧仍读到旧演示项目名的窗口，避免对不存在的演示项目建一次必然失败的 SSE 连接。
  // useTaskRefresh 的 projectName=null 语义是「不按项目过滤」而非「停用」，enabled 必须
  // 同步这一判定，否则该帧会退化成对全局任务的轮询而非真正停用。
  const isEffectivelyDemo = demoMode || isDemoProject(currentProjectName);
  const sseProjectName = isEffectivelyDemo ? null : currentProjectName;
  useTaskRefresh(sseProjectName, !isEffectivelyDemo);
  useProjectEventsSSE(sseProjectName);

  const compact = useCompactTier();
  const storeOpen = useAppStore((s) => s.assistantPanelOpen);
  // Edit view focus mode: no top bar, sidebar or Agent panel (the canvas takes the whole window)
  const focusOn = useEditFocusStore((s) => s.on);
  const toggleAssistantPanel = useAppStore((s) => s.toggleAssistantPanel);
  const agentWidth = useAppStore((s) => s.assistantPanelWidth);
  // The demo panel cannot be collapsed and does not cover the canvas: later guide steps highlight things on the canvas
  // 演示面板不可收起，也不覆盖画布：引导后面几步要在画布上高亮
  const agentOpen = demoMode || storeOpen;
  const overlay = compact && !demoMode;

  // ---- Sidebar collapse: collapsed by default when there is an auto-collapse reason, can be expanded temporarily, resets when the reason or page changes ----
  // ---- 侧栏折叠：有自动折叠的原因时默认折叠、可临时展开，原因或页面变化后复位 ----
  const collapseReason = sidebarAutoCollapseReason(location, compact);
  const [userCollapsed, setUserCollapsed] = useState(readSidebarCollapsed);
  const [tempExpanded, setTempExpanded] = useState(false);
  const resetKey = `${collapseReason}|${location}`;
  const [lastResetKey, setLastResetKey] = useState(resetKey);
  if (resetKey !== lastResetKey) {
    setLastResetKey(resetKey);
    setTempExpanded(false);
  }
  const sidebarCollapsed = collapseReason ? !tempExpanded : userCollapsed;
  const setSidebarCollapsed = useCallback(
    (collapsed: boolean) => {
      if (collapseReason) {
        setTempExpanded(!collapsed);
        return;
      }
      setUserCollapsed(collapsed);
      persistSidebarCollapsed(collapsed);
    },
    [collapseReason],
  );

  // ---- Panel sizes: open/collapse state drives the panel library, and results of user actions like dragging are written back to state ----
  // ---- 面板尺寸：开合与折叠由状态驱动面板库，拖动等用户操作的结果再写回状态 ----
  const shellRef = useRef<HTMLDivElement>(null);
  const agentFrameRef = useRef<HTMLElement>(null);
  const sidebarPanelRef = usePanelRef();
  const agentPanelRef = usePanelRef();
  // The remembered sidebar width is only read during imperative resizing and does not take part in rendering, so updating it does not redraw the shell
  // 记住的侧栏宽度只在命令式调宽时读取，不参与渲染，更新时不重绘外壳
  const [initialSidebarWidth] = useState(readSidebarWidth);
  const sidebarWidthRef = useRef(initialSidebarWidth);
  const latest = useRef({ sidebarCollapsed, agentOpen, agentWidth });
  useLayoutEffect(() => {
    latest.current = { sidebarCollapsed, agentOpen, agentWidth };
  });

  // The panel's defaultSize is the width a double-click on the handle resets to; on mount and on later open/collapse, the remembered width is applied to the panel here.
  // 面板的 defaultSize 是双击手柄复位到的默认宽度；挂载时与之后的开合、折叠都在这里按记住的宽度落到面板上。
  useLayoutEffect(() => {
    // focusOn: the panel comes back after focus mode and gets its remembered state again; right after it remounts
    // the library may not have registered it yet ("Panel constraints not found"), so retry on the next frames
    return applyWhenRegistered(() => {
      const panel = sidebarPanelRef.current;
      if (!panel) return;
      if (sidebarCollapsed) panel.collapse();
      else panel.resize(sidebarWidthRef.current);
    });
  }, [sidebarCollapsed, sidebarPanelRef, focusOn]);

  useLayoutEffect(
    () =>
      applyWhenRegistered(() => {
        const panel = agentPanelRef.current;
        if (!panel) return;
        // Width switches instantly on open/close with no width animation: animating width would re-layout the canvas every frame
        // 开合时宽度瞬时切换，不做宽度动画：给宽度做动画会让画布逐帧重新排版
        if (agentOpen) panel.resize(latest.current.agentWidth);
        else panel.collapse();
      }),
    [agentOpen, agentPanelRef, focusOn],
  );

  const rememberSidebarWidth = useCallback((width: number) => {
    if (width < SIDEBAR_MIN_WIDTH) return;
    sidebarWidthRef.current = width;
    persistSidebarWidth(width);
  }, []);
  const rememberAgentWidth = useCallback((width: number) => {
    if (width < ASSISTANT_PANEL_MIN_WIDTH) return;
    const store = useAppStore.getState();
    store.setAssistantPanelWidth(width);
    store.persistAssistantPanelWidth();
  }, []);

  // Keyboard resizing and double-click reset both go through the library's imperative API; the library reports the layout change synchronously but does not mark it as a user action;
  // so mark "user is resizing" during the adjustment, and the result is remembered just like a drag instead of being pulled back to the old width.
  // 键盘调宽与双击复位都经库的命令式接口完成，库同步通知布局变化，但不标记为用户操作；
  // 调整期间记下「用户在调宽」，结果就和拖动一样被记住，不会被拉回原来的宽度。
  const userResizingRef = useRef(false);
  const resizeByKeyboard = (panelRef: typeof sidebarPanelRef, width: number) => {
    const panel = panelRef.current;
    if (!panel) return;
    userResizingRef.current = true;
    try {
      panel.resize(width);
    } finally {
      userResizingRef.current = false;
    }
  };
  // The library handles double-click in the document capture phase; the window capture phase runs before it
  // 双击由库在 document 的捕获阶段处理，window 的捕获阶段先于它
  useEffect(() => {
    let resetTimer: ReturnType<typeof setTimeout> | undefined;
    const handleDoubleClick = () => {
      userResizingRef.current = true;
      clearTimeout(resetTimer);
      resetTimer = setTimeout(() => {
        userResizingRef.current = false;
      });
    };
    window.addEventListener("dblclick", handleDoubleClick, true);
    return () => {
      window.removeEventListener("dblclick", handleDoubleClick, true);
      clearTimeout(resetTimer);
    };
  }, []);

  const handleLayoutChanged = (_layout: unknown, meta: LayoutChangedMeta) => {
    const sidebar = sidebarPanelRef.current;
    const agent = agentPanelRef.current;
    const current = latest.current;
    if (meta.isUserInteraction || userResizingRef.current) {
      // When drag, keyboard or double-click makes the library collapse or expand the panel, sync back to the open state and remember the resulting width
      // 拖动、键盘或双击让库折叠、展开了面板时，同步回开合状态，并记住调出来的宽度
      if (sidebar) {
        const collapsed = sidebar.isCollapsed();
        if (collapsed !== current.sidebarCollapsed) setSidebarCollapsed(collapsed);
        else if (!collapsed) rememberSidebarWidth(sidebar.getSize().inPixels);
      }
      if (agent && !demoMode) {
        const closed = agent.isCollapsed();
        if (closed === current.agentOpen) toggleAssistantPanel();
        else if (!closed) rememberAgentWidth(agent.getSize().inPixels);
      }
      return;
    }
    // Other changes come from the viewport and tier: the library scales panels by their previous percentages, or squeezes panels for the canvas's minimum width. An expanded panel returns to its remembered
    // pixel width; when it does not fit, the library clamps the result within constraints, the layout does not change, so it is not triggered again.
    // 其余变化来自视口与档位：库按变化前的百分比缩放面板，或为画布的最小宽度挤窄面板。展开着的面板回到记住的
    // 像素宽度；放不下时库把结果夹在约束内，布局不变也就不会再次触发。
    if (sidebar && !current.sidebarCollapsed && !sidebar.isCollapsed()) {
      const sidebarWidth = sidebarWidthRef.current;
      if (Math.abs(sidebar.getSize().inPixels - sidebarWidth) > 1) sidebar.resize(sidebarWidth);
    }
    if (agent && current.agentOpen && !agent.isCollapsed()) {
      if (Math.abs(agent.getSize().inPixels - current.agentWidth) > 1) agent.resize(current.agentWidth);
    }
  };

  // In overlay mode the canvas lays out at the width of "canvas column + Agent panel", covered by the panel instead of squeezed
  // 覆盖模式下画布按「画布栏 + Agent 面板」的宽度排版，被面板盖住而不是被挤窄
  const handleAgentResize = (size: PanelSize) => {
    const covered = size.inPixels > 0 ? size.inPixels + HANDLE_WIDTH : 0;
    shellRef.current?.style.setProperty("--workspace-agent-width", `${covered}px`);
  };

  // If focus is inside the panel when collapsing it, focus returns to the top bar's "Agent" toggle instead of landing at the start of the page
  // 焦点在面板内时收起面板，焦点回到顶栏的「Agent」开关，不落到页面开头
  const restoreFocusRef = useRef(false);
  useEffect(
    () =>
      useAppStore.subscribe((state, prev) => {
        if (prev.assistantPanelOpen && !state.assistantPanelOpen) {
          restoreFocusRef.current = agentFrameRef.current?.contains(document.activeElement) ?? false;
        }
      }),
    [],
  );
  useLayoutEffect(() => {
    if (agentOpen || !restoreFocusRef.current) return;
    restoreFocusRef.current = false;
    document.getElementById(AGENT_PANEL_TOGGLE_ID)?.focus();
  }, [agentOpen]);

  // The compact tier's overlay is non-modal: Esc collapses the panel while focus is inside it. The listener is on the document bubble phase so controls in the panel handle their own Esc first
  // 紧凑档的覆盖层是非模态的：焦点在面板内时 Esc 收起面板。监听挂在 document 的冒泡阶段，面板内的控件先处理自己的 Esc
  useEffect(() => {
    if (!overlay || !agentOpen) return;
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      // Only accept keys inside the panel in the DOM; dialogs opened from the panel render via Portal, and Esc there closes the dialog
      // 只认 DOM 上在面板内的按键；从面板里打开的对话框经 Portal 渲染，按 Esc 关的是对话框
      if (!(event.target instanceof Node) || !agentFrameRef.current?.contains(event.target)) return;
      event.preventDefault();
      useAppStore.getState().toggleAssistantPanel();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [overlay, agentOpen]);

  return (
    // The shell root is a positioned element and the document itself does not scroll; scrolling only happens in the sidebar, canvas and Agent panel areas.
    // 外壳根节点是定位元素，文档本身不滚动；滚动只发生在侧栏、画布与 Agent 面板各自的区域里。
    <div ref={shellRef} className="relative flex h-dvh flex-col overflow-hidden text-foreground">
      <TaskFailureListener projectName={sseProjectName} />
      <ScriptGenerationNoticeListener />
      {!focusOn && <GlobalHeader />}
      <ResizablePanelGroup className="min-h-0 flex-1" onLayoutChanged={handleLayoutChanged}>
        {/* focus mode (Edit view): no sidebar */}
        {!focusOn && (
          <>
          <ResizablePanel
            id="workspace-sidebar"
            panelRef={sidebarPanelRef}
            collapsible
            collapsedSize={SIDEBAR_RAIL_WIDTH}
            minSize={SIDEBAR_MIN_WIDTH}
            maxSize={SIDEBAR_MAX_WIDTH}
            defaultSize={SIDEBAR_DEFAULT_WIDTH}
            groupResizeBehavior="preserve-pixel-size"
          >
            <AssetSidebar collapsed={sidebarCollapsed} onCollapsedChange={setSidebarCollapsed} />
          </ResizablePanel>
          <WorkspaceResizeHandle
            panelRef={sidebarPanelRef}
            panelSide="before"
            onKeyboardResize={(width) => resizeByKeyboard(sidebarPanelRef, width)}
            label={t("resize_sidebar")}
            disabled={sidebarCollapsed}
          />
          </>
        )}
        <ResizablePanel
          id="workspace-canvas"
          minSize={overlay ? 0 : CANVAS_MIN_WIDTH}
          // The library hard-codes overflow: auto on the panel's inner layer, which can only be overridden via style: canvas views scroll themselves;
          // in overlay mode the canvas must extend under the Agent panel and must not be clipped by this layer.
          // 库在面板内层写死 overflow: auto，只能经 style 覆盖：画布各视图自己滚动；
          // 覆盖模式下画布要伸到 Agent 面板底下，不能被这一层裁掉。
          // eslint-disable-next-line shadcn/no-inline-styles -- override the inline overflow the library writes on the inner layer / 覆盖库写在内层的内联 overflow
          style={{ overflow: overlay ? "visible" : "hidden" }}
        >
          <main
            className={cn(
              "@container/canvas relative isolate flex h-full min-w-0 flex-col",
              overlay && agentOpen && "w-[calc(100%+var(--workspace-agent-width))]",
            )}
          >
            {children}
          </main>
        </ResizablePanel>
        {/* focus mode (Edit view): no Agent panel */}
        {!focusOn && (
          <>
          {/* When the panel is collapsed the handle sits on the column's right edge and its hit area would stick out of the shell, so hide it; the handle stays in the DOM, disabled, so the library does not create a drag area on this boundary / 面板收起时手柄贴在分栏右缘，命中区会伸出外壳，因此隐藏；手柄仍留在 DOM 里并禁用，库就不在这条边界上生成拖动区域 */}
          <WorkspaceResizeHandle
            panelRef={agentPanelRef}
            panelSide="after"
            onKeyboardResize={(width) => resizeByKeyboard(agentPanelRef, width)}
            label={t("resize_assistant_panel")}
            disabled={!agentOpen || demoMode}
            hidden={!agentOpen}
          />
          <ResizablePanel
            id="workspace-agent"
            panelRef={agentPanelRef}
            collapsible
            collapsedSize={0}
            minSize={ASSISTANT_PANEL_MIN_WIDTH}
            maxSize={ASSISTANT_PANEL_MAX_WIDTH}
            defaultSize={ASSISTANT_PANEL_DEFAULT_WIDTH}
            groupResizeBehavior="preserve-pixel-size"
            onResize={handleAgentResize}
            // After collapsing the width is 0; the content stays mounted in place at minimum width, invisible, clipped by this layer
            // 收起后宽度为 0，内容保持最小宽度挂载在原处、不可见，由这一层裁掉
            // eslint-disable-next-line shadcn/no-inline-styles -- override the inline overflow the library writes on the inner layer / 覆盖库写在内层的内联 overflow
            style={{ overflow: "hidden" }}
          >
            <aside
              ref={agentFrameRef}
              id={AGENT_PANEL_ID}
              aria-label={t("agent_panel_label")}
              inert={!agentOpen}
              className={cn(
                "@container-size/agent relative h-full w-full min-w-80 transition-opacity duration-fast",
                overlay ? "bg-popover shadow-overlay" : "bg-card",
                !agentOpen && "invisible opacity-0",
              )}
            >
              {demoMode ? <DemoAssistantPanel /> : <AgentCopilot />}
            </aside>
          </ResizablePanel>
          </>
        )}
      </ResizablePanelGroup>
    </div>
  );
}
