import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "wouter";

import { useConfirmLeave } from "@/components/shared/edit-unit/LeaveGuard";
import { RetainedEditUnit } from "@/components/shared/edit-unit/RetainedEditUnit";
import { EPISODE_SHOT_PARAM, EPISODE_VIEW_PARAM } from "@/app-routes";
import { EditTimelineEmptyState } from "@/components/canvas/edit-render/EditTimelineEmptyState";
import { EpisodeRenderShortcut } from "@/components/canvas/edit-render/EpisodeRenderShortcut";
import { RenderButton } from "@/components/canvas/edit-render/RenderButton";
import { EpisodeSourceReview } from "@/components/canvas/EpisodeSourceReview";
import { AdScriptHost } from "@/components/canvas/shared/AdScriptDialog";
import { PromptAuthoringHost } from "@/components/canvas/shared/PromptAuthoringDialog";
import { ScriptPlanHost } from "@/components/canvas/shared/ScriptPlanDialog";
import { TextTaskFailureNote } from "@/components/canvas/shared/TextTaskFailureNote";
import { LazyBoundary } from "@/components/shared/LazyBoundary";
import { WorkflowPanel } from "@/components/workflow/WorkflowPanel";
import { DemoEpisodePlaceholder } from "@/onboarding/DemoEpisodePlaceholder";
import { useCostStore } from "@/stores/cost-store";
import { useEpisodeSurfaceRequest } from "@/stores/episode-surface-store";
import { usePromptAuthoringStore } from "@/stores/prompt-authoring-store";
import type { ProjectData } from "@/types";
import type { EpisodeScript } from "@/types/script";
import { gridStoryboardEnabled, normalizeRoute } from "@/utils/generation-mode";
import { lazyNamed } from "@/utils/lazy-component";
import { previewAspect } from "@/utils/preview-aspect";

import {
  episodeViewTabId,
  episodeViewTabs,
  hasEpisodeDraft,
  resolveEpisodeView,
  showsEpisodeSource,
  withEpisodeView,
  type CanvasView,
  type EpisodeView,
  type EpisodeViewFacts,
} from "./episode-view";
import { EpisodeHead } from "./EpisodeHead";
import { EpisodeHeaderActions, EpisodeHeaderSlotProvider } from "./EpisodeHeaderActions";
import { EpisodePageHeader } from "./EpisodePageHeader";
import { EpisodeSwitcher } from "./EpisodeSwitcher";
import { openEpisodeInOpenReel } from "@/components/canvas/edit/open-in-openreel";
import { useDefaultEditor } from "@/preferences/user-options";
import { useEditFocusStore } from "@/stores/edit-focus-store";
import { EpisodeViewFactsProvider } from "./EpisodeViewScope";
import { StorySettingView } from "./StorySettingView";

// The edit view (timeline player and tracks) is only used when switching to "Edit", so it is loaded on demand
// 剪辑视图（时间线播放器与轨道）只在切到「剪辑」时用到，按需拉取
const EditTimelineView = lazyNamed(() => import("@/components/canvas/edit/EditTimelineView"), "EditTimelineView");

export interface EpisodeViewChangeOptions {
  /** Replace the current history entry instead of adding one: used for programmatic switches (e.g. jumping to a unit), so Back does not need an extra step. / 替换当前历史记录而不是新增一条：程序触发的切换（如跳到某个单元）用它，后退不必多退一步。 */
  replace?: boolean;
}

export interface EpisodeCanvasContext {
  view: CanvasView;
  onViewChange: (view: EpisodeView, options?: EpisodeViewChangeOptions) => void;
}

/**
 * The page of an episode: a two-row header (episode head, workflow progress, view tabs and action slot) and the view area below.
 *
 * The view is stored in the URL's `?view=` (see `episode-view.ts`). The script plan, multi-grid storyboard and shot views are served by the same canvas,
 * which is not unmounted when switching; edit units in the canvas hang under only one view, and `EpisodeViewFactsProvider` decides which navigations would unmount them.
 * Story setting (ad projects only) and the edit view replace the whole canvas. Before the script is generated, the script plan view is the episode source review.
 *
 * 一集的页面：两行页头（集头、制作进度、视图 tab 与动作插槽）与下方的视图区。
 *
 * 视图记在地址的 `?view=` 上（见 `episode-view.ts`）。脚本规划、多宫格分镜图与分镜三个视图由同一个画布承担，
 * 切换时画布不卸载；画布里的编辑单元只挂在某一个视图下，经 `EpisodeViewFactsProvider` 判断哪些跳转会卸载自己。
 * 故事设定（只在广告项目出现）与剪辑视图替换整个画布。剧本还没生成时脚本规划视图是集原文确认。
 */
function EpisodePageContent({
  projectName,
  episode,
  projectData,
  script,
  demo,
  onSaveTitle,
  onViewUnit,
  onRegenerate,
  renderCanvas,
}: {
  projectName: string;
  episode: number;
  projectData: ProjectData | null;
  /** This episode's final script; null when there is none yet. / 本集的正式剧本；还没有时为 null。 */
  script: EpisodeScript | null;
  /** Demo project: read-only, no workflow progress or edit view. / 演示项目：只读，没有制作进度与剪辑视图。 */
  demo: boolean;
  /** Save the episode title; on reject the title stays in edit mode. / 保存集标题；reject 时标题保持编辑态。 */
  onSaveTitle: (title: string) => Promise<void>;
  /** "View" a unit from workflow progress. / 制作进度里「查看」某个单元。 */
  onViewUnit: (unitId: string) => void;
  /** Regenerate the given unit of a step from workflow progress; when not passed, only "View" is offered. / 制作进度里重生某一步的指定单元；不传时只给「查看」。 */
  onRegenerate?: (stepId: string, unitIds: string[]) => void;
  renderCanvas: (context: EpisodeCanvasContext) => ReactNode;
}) {
  const { t } = useTranslation("dashboard");
  const [searchParams, setSearchParams] = useSearchParams();
  const [actionsSlot, setActionsSlot] = useState<HTMLDivElement | null>(null);

  const meta = projectData?.episodes?.find((entry) => entry.episode === episode);
  // the Edit view prefetches these episodes' streams (list order = play order): the next one first, then the previous
  const episodeOrder = projectData?.episodes;
  const neighbourEpisodes = useMemo(() => {
    const order = episodeOrder ?? [];
    const at = order.findIndex((entry) => entry.episode === episode);
    if (at < 0) return [];
    return [order[at + 1], order[at - 1]].flatMap((entry) => (entry ? [entry.episode] : []));
  }, [episodeOrder, episode]);
  const isAd = projectData?.content_mode === "ad";
  const route = normalizeRoute(projectData?.generation_mode);
  const hasScript = Boolean(script);
  const hasDraft = hasEpisodeDraft(meta);
  const grid = gridStoryboardEnabled(projectData);
  const sourceReview = showsEpisodeSource(projectData, episode, script, demo);
  const facts = useMemo<EpisodeViewFacts>(
    () => ({ isAd, route, grid, hasScript, hasDraft, sourceReview, demo }),
    [isAd, route, grid, hasScript, hasDraft, sourceReview, demo],
  );
  const tabs = useMemo(() => episodeViewTabs(facts), [facts]);
  const view = resolveEpisodeView(searchParams.get(EPISODE_VIEW_PARAM), facts);
  // Edit view focus mode (useEditFocusStore): ends when leaving the Edit view or this page
  const focusOn = useEditFocusStore((s) => s.on);
  const enterFocus = useEditFocusStore((s) => s.enter);
  const exitFocus = useEditFocusStore((s) => s.exit);
  useEffect(() => {
    if (view !== "edit") exitFocus();
  }, [view, exitFocus]);
  useEffect(() => () => exitFocus(), [exitFocus]);

  // A link to a shot (`?shot=E9S19`, e.g. from the editor): show it in the shot view, then drop the parameter
  const linkedShot = searchParams.get(EPISODE_SHOT_PARAM);
  useEffect(() => {
    if (!linkedShot || !hasScript) return;
    onViewUnit(linkedShot);
    setSearchParams((params) => {
      const next = new URLSearchParams(params);
      next.delete(EPISODE_SHOT_PARAM);
      return next;
    }, { replace: true });
  }, [linkedShot, hasScript, onViewUnit, setSearchParams]);

  // Switching to an unselectable view has no effect; switching to the current view writes no history. / 切到不可选的视图不生效；切到当前视图不写历史。
  // With OpenReel as the default editor (Settings → Options), the Edit tab opens the episode in OpenReel's own tab
  const { editor: defaultEditor, ready: defaultEditorReady } = useDefaultEditor();
  const changeView = useCallback(
    (next: EpisodeView, options?: EpisodeViewChangeOptions) => {
      if (next === view || !tabs.some((tab) => tab.view === next && !tab.disabled)) return;
      setSearchParams((params) => withEpisodeView(params, next, facts), { replace: options?.replace });
    },
    [view, tabs, facts, setSearchParams],
  );
  // The Edit tab itself (not links that open the Edit view for a purpose, e.g. "Show issues")
  const selectTab = useCallback(
    (next: EpisodeView, options?: EpisodeViewChangeOptions) => {
      if (next === "edit" && defaultEditorReady && defaultEditor === "openreel") {
        openEpisodeInOpenReel(projectName, episode);
        return;
      }
      changeView(next, options);
    },
    [changeView, defaultEditor, defaultEditorReady, projectName, episode],
  );

  // "View" in workflow progress: shots and video units can only be selected in the shot view. When not in that view, switching view and locating are combined into one leave guard,
  // and the locate request is sent only after it passes; choosing to keep editing changes nothing, and leaves no pending locate to be consumed after the shot view mounts.
  // 制作进度里的「查看」：分镜与视频单元只在分镜视图里选得中。不在该视图时，切视图与定位合成一次离开拦截，
  // 放行后才发出定位请求；选择继续编辑时什么都不变，也不留下等分镜视图挂载后才消费的定位。
  const confirmLeave = useConfirmLeave();
  const viewUnit = useCallback(
    (unitId: string) => {
      if (view === "board") {
        onViewUnit(unitId);
        return;
      }
      confirmLeave(() => {
        changeView("board", { replace: true });
        onViewUnit(unitId);
      });
    },
    [view, changeView, onViewUnit, confirmLeave],
  );

  useEpisodeSurfaceRequest(projectName, episode, "script_plan", () => changeView("plan"));
  useEpisodeSurfaceRequest(projectName, episode, "prompt_authoring_draft", () => changeView("board"));

  // The header's estimated cost reads the cost estimate; for demo projects the cost table clears itself. / 页头的预估费用读费用估算；演示项目由费用表自己清空。
  const debouncedFetch = useCostStore((s) => s.debouncedFetch);
  useEffect(() => {
    debouncedFetch(projectName);
  }, [projectName, episode, debouncedFetch]);

  const openAuthorPrompts = useCallback(
    () => usePromptAuthoringStore.getState().open({ projectName, episode, scope: "pending" }),
    [projectName, episode],
  );

  const ttsNarration = projectData?.narration_delivery === "use_tts";
  // prefix of the final cut's download name: project title and episode (RenderDialog adds timeline, version, time)
  const renderFileStem = `${projectData?.title || projectName} - E${episode}`;
  let body: ReactNode;
  if (view === "edit") {
    body = (
      <LazyBoundary variant="pane">
        <EditTimelineView
          key={`${projectName}::${episode}`}
          projectName={projectName}
          episode={episode}
          script={script}
          aspect={previewAspect(projectData)}
          ttsNarration={ttsNarration}
          onViewUnit={viewUnit}
          neighbourEpisodes={neighbourEpisodes}
          renderActions={({ timelineId, timelineName, issues, showIssues }) =>
            // Until loading finishes it is unknown whether there are blocking issues, so the render entry is withheld for now.
            // 读取完成前不知道有没有阻断级 issue，先不给出片入口。
            issues === null ? null : (
              <EpisodeHeaderActions>
                <RenderButton
                  projectName={projectName}
                  timelineId={timelineId}
                  timelineName={timelineName}
                  issues={issues}
                  narrationAvailable={ttsNarration}
                  onShowIssues={showIssues}
                  fileStem={renderFileStem}
                />
              </EpisodeHeaderActions>
            )
          }
          renderEmptyState={({ reload }) => (
            <EditTimelineEmptyState projectName={projectName} episode={episode} onCreated={reload} />
          )}
        />
      </LazyBoundary>
    );
  } else if (view === "setting") {
    body = (
      <EpisodeViewFactsProvider value={facts}>
        <StorySettingView projectName={projectName} overview={projectData?.overview} readOnly={demo} />
      </EpisodeViewFactsProvider>
    );
  } else if (demo && !script) {
    body = <DemoEpisodePlaceholder />;
  } else if (sourceReview) {
    body = <EpisodeViewFactsProvider value={facts}><EpisodeSourceReview projectName={projectName} episode={episode} episodes={projectData?.episodes ?? []} /></EpisodeViewFactsProvider>;
  } else {
    body = <EpisodeViewFactsProvider value={facts}>{renderCanvas({ view, onViewChange: changeView })}</EpisodeViewFactsProvider>;
  }

  return (
    <EpisodeHeaderSlotProvider value={actionsSlot}>
      <div className="flex min-h-0 flex-1 flex-col">
        <EpisodePageHeader
          head={
            // Reset on episode change: a half-edited title draft and delete confirmation must not carry over to another episode
            // 换集时重置：编辑到一半的标题草稿与删除确认不能带到另一集
            <EpisodeHead
              key={`${projectName}::${episode}`}
              projectName={projectName}
              episode={episode}
              meta={meta}
              route={route}
              canEditTitle={Boolean(meta) && !demo}
              onSaveTitle={onSaveTitle}
              canDelete={!isAd && !demo}
            />
          }
          progress={
            // Demo mode has no real project facts to project, so workflow progress is not mounted. / 演示态没有真实项目事实可投影，制作进度不挂载。
            demo ? null : (
              <WorkflowPanel
                projectName={projectName}
                episode={episode}
                onViewUnit={viewUnit}
                onRegenerate={onRegenerate}
                onAuthorPrompts={script ? openAuthorPrompts : undefined}
              />
            )
          }
          tabs={tabs}
          view={view}
          onViewChange={selectTab}
          boardLabel={t(route === "reference_video" ? "episode_view_units" : "episode_view_board")}
          onActionsSlot={setActionsSlot}
          // the Edit view needs the height for its player
          compact={view === "edit"}
          editLaunches={defaultEditorReady && defaultEditor === "openreel"}
          focus={
            view === "edit"
              ? {
                  on: focusOn,
                  onEnter: enterFocus,
                  onExit: exitFocus,
                  switcher: (
                    <EpisodeSwitcher projectName={projectName} episodes={projectData?.episodes ?? []} current={episode} />
                  ),
                }
              : null
          }
        />
        {/* Render shortcut on the views other than Edit (the Edit view places its own via EditTimelineView). YouReel:
            not with the built-in editor as the default, whose Edit view has Render already (owner 2026-11-17) */}
        {!demo && view !== "edit" && defaultEditorReady && defaultEditor !== "arcreel" && (
          <EpisodeHeaderActions>
            <EpisodeRenderShortcut
              projectName={projectName}
              episode={episode}
              narrationAvailable={ttsNarration}
              fileStem={renderFileStem}
              onShowIssues={() => changeView("edit")}
            />
          </EpisodeHeaderActions>
        )}
        {!demo && (
          <>
            <TextTaskFailureNote projectName={projectName} episode={episode} isAd={isAd} hasScript={hasScript} />
            <ScriptPlanHost
              projectName={projectName}
              episode={episode}
              savedInstructions={meta?.script_plan_instructions}
            />
            {isAd && <AdScriptHost projectName={projectName} episode={episode} />}
            <PromptAuthoringHost
              projectName={projectName}
              episode={episode}
              script={script}
              savedInstructions={meta?.prompt_authoring_instructions}
            />
          </>
        )}
        <div role="tabpanel" aria-labelledby={episodeViewTabId(view)} className="flex min-h-0 flex-1 flex-col">
          {body}
        </div>
      </div>
    </EpisodeHeaderSlotProvider>
  );
}

/** What is kept is the visible episode page; when the script arrives during source editing, async events must not unmount the editor. / 保留的是可见集页；原文编辑期间脚本到达，不让异步事件卸载编辑器。 */
export function EpisodePage(props: Parameters<typeof EpisodePageContent>[0]) {
  const { t } = useTranslation("dashboard");
  const meta = props.projectData?.episodes?.find((entry) => entry.episode === props.episode);
  const source = showsEpisodeSource(props.projectData, props.episode, props.script, props.demo);
  const identity = `${props.projectName}:${props.episode}:${!meta ? "missing" : source ? "source" : props.script ? "script" : "draft"}`;
  const message = !meta ? "episode_externally_removed" : props.script || hasEpisodeDraft(meta) ? "episode_source_replaced" : "episode_script_removed";
  return (
    <RetainedEditUnit identity={identity} value={props} message={t(message)}>
      {(shown) => <EpisodePageContent {...shown} />}
    </RetainedEditUnit>
  );
}
