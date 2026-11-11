import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { AlertTriangle, PanelRightClose, PanelRightOpen } from "lucide-react";
import { useTranslation } from "react-i18next";
import { isPhoneWidth, PHONE_QUERY, useMediaQuery } from "@/hooks/useMediaQuery";
import { useParams, useSearchParams } from "wouter";

import { cn } from "cn";

import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import type {
  EditPreviewNarrationAudio,
  EditTimelineIssue,
  EditTimelinePreviewMedia,
  EditTimelineReadout,
  EditTimelineSummary,
} from "@/types/edit-timeline";
import { LINK_TIME_PARAM, LINK_TIMELINE_PARAM, parseSeconds } from "@/utils/app-link";
import { errMsg } from "@/utils/async";
import { formatRelativeTime, isJustNow } from "@/utils/date-format";
import type { PreviewAspect } from "@/utils/preview-aspect";

import { ClipInspector, ISSUE_LIST_HEADING_ID, IssueList } from "./EditTimelineDetails";
import { EditTimelineMenu } from "./EditTimelineMenu";
import { EditTimelinePlayer } from "./EditTimelinePlayer";
import { EditTimelineTracks } from "./EditTimelineTracks";
import { buildPlaybackPlan, locate, type PlaybackPlan, type PlaybackSegment } from "./playback-schedule";
import {
  bgmPlacements,
  narrationPlacements,
  narrationSpans,
  placeSubtitles,
  type AudioPlacement,
} from "./preview-tracks";
import {
  isReferenceVideoScript,
  issueClipIds,
  pickDefaultTimeline,
  unitPositions,
  unitThumbnails,
  unitVideoPath,
  unusedUnitIds,
} from "./timeline-view";
import { useTimelinePlayback } from "./useTimelinePlayback";

/** 标签行右侧操作区拿到的当前剪辑时间线。 */
export interface EditTimelineActionsContext {
  timelineId: string;
  timelineName: string;
  /** 当前读取结果的 issues；读取完成前为 null。 */
  issues: readonly EditTimelineIssue[] | null;
  /** 滚动到「问题」列表并把焦点移过去。 */
  showIssues: () => void;
}

export interface EditTimelineEmptyStateContext {
  /** 新建剪辑时间线后调用，重新读取列表并选中最近修改的那条。 */
  reload: () => void;
}

interface EditTimelineViewProps {
  projectName: string;
  episode: number;
  /** 本集脚本：用于视频单元的源视频路径与缩略图。 */
  script: unknown;
  aspect: PreviewAspect;
  /** 项目的旁白交付方式是否为 TTS 配音；否则为后期配音，旁白轨不按缺配音处理。 */
  ttsNarration: boolean;
  /** 标签行右侧的操作区；不传时不渲染。 */
  renderActions?: (context: EditTimelineActionsContext) => ReactNode;
  /** 本集没有剪辑时间线时的空状态。 */
  renderEmptyState: (context: EditTimelineEmptyStateContext) => ReactNode;
}

/** 应用内链接带来的一次性定位：打开哪条剪辑时间线（缺省为当前选中的）、定位到全局时间的哪一秒（缺省为不动播放头）。 */
interface LinkJump {
  timelineId: string | null;
  seconds: number | null;
}

type Loaded<T> = { key: string; value: T } | { key: string; error: string };

/**
 * 集页的剪辑视图：只读预览一条剪辑时间线，标签菜单可重命名与删除。上方播放器按剪辑时间线实时拼接播放，下方横向时间线；
 * 项目有变更（含 Agent 写入新修订）时重新读取。
 */
export function EditTimelineView({
  projectName,
  episode,
  script,
  aspect,
  ttsNarration,
  renderActions,
  renderEmptyState,
}: EditTimelineViewProps) {
  const { t, i18n } = useTranslation("dashboard");
  const snapshotRevision = useProjectsStore((s) => s.projectSnapshotRevisions[projectName] ?? 0);
  const [retry, setRetry] = useState(0);
  const listKey = `${projectName}::${episode}::${snapshotRevision}::${retry}`;
  const [list, setList] = useState<Loaded<EditTimelineSummary[]> | null>(null);
  const [chosenId, setChosenId] = useState<string | null>(null);
  const reload = useCallback(() => setRetry((n) => n + 1), []);
  const tabIdPrefix = useId();

  // 链接参数 tl / t 读入后立刻从地址栏去掉：它们是一次性的定位指令，刷新页面不应再次跳走。
  const [searchParams, setSearchParams] = useSearchParams();
  // 跳到别的项目时，地址先变、当前项目后切：这一刻地址栏里的参数属于即将挂载的那个视图，不在这里消费。
  const routeProject = useParams<{ projectName?: string }>().projectName;
  const linkForOtherProject = routeProject !== undefined && routeProject !== projectName;
  const linkTimeline = searchParams.get(LINK_TIMELINE_PARAM);
  const linkTime = searchParams.get(LINK_TIME_PARAM);
  const [jump, setJump] = useState<LinkJump | null>(null);
  useEffect(() => {
    if (linkForOtherProject || (linkTimeline === null && linkTime === null)) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 订阅地址栏的一次性参数：读入后清掉，再点同一条链接才能重新触发
    setJump({ timelineId: linkTimeline || null, seconds: parseSeconds(linkTime) });
    setSearchParams(
      (params) => {
        params.delete(LINK_TIMELINE_PARAM);
        params.delete(LINK_TIME_PARAM);
        return params;
      },
      { replace: true },
    );
  }, [linkForOtherProject, linkTimeline, linkTime, setSearchParams]);
  // 删除后选中的标签回落到默认那条（最近修改的）。
  const handleDeleted = useCallback(() => {
    setChosenId(null);
    setRetry((n) => n + 1);
  }, []);
  // Bumped when the issue list must be shown while the details column is folded away.
  const [issuesRequest, setIssuesRequest] = useState(0);
  const showIssues = useCallback(() => {
    if (focusIssueList()) return;
    setIssuesRequest((n) => n + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    API.listEditTimelines(projectName, episode, { signal: controller.signal })
      .then(({ timelines }) => {
        if (controller.signal.aborted) return;
        setList({ key: listKey, value: timelines });
        // 首次看到剪辑时间线时固定默认选中的那条，之后新建或修改其他时间线不会把正在看的标签切走。
        setChosenId((previous) => previous ?? pickDefaultTimeline(timelines));
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setList({ key: listKey, error: errMsg(cause) });
      });
    return () => controller.abort();
  }, [episode, listKey, projectName]);

  // 刷新期间沿用上一次的列表，避免每次项目变更都闪回加载态。
  const timelines = list && "value" in list ? list.value : null;

  // 列表到了才能判断链接指向的剪辑时间线还在不在：在就直接选中它，不在就提示并放弃这次定位。
  const jumpTimelineId = jump?.timelineId ?? null;
  const jumpTarget =
    timelines !== null && jumpTimelineId !== null && timelines.some((item) => item.id === jumpTimelineId)
      ? jumpTimelineId
      : null;
  // 沿用中的旧列表可能还没有刚新建的那条，只按本轮读到的列表判定「不存在」。
  const listFresh = list?.key === listKey;
  // react-doctor-disable-next-line react-doctor/no-effect-chain -- 地址栏的定位指令与异步读到的列表齐备后才能判定目标不存在，需要提示一次，提示是副作用只能放在 effect
  useEffect(() => {
    if (!jump || !timelines) return;
    if (timelines.length === 0) {
      // react-doctor-disable-next-line react-doctor/no-adjust-state-on-prop-change -- 定位指令来自地址栏、列表异步到达，两者齐备时才能判定作废
      setJump(null); // eslint-disable-line react-hooks/set-state-in-effect -- 没有剪辑时间线可定位，作废这次定位
      return;
    }
    if (jumpTimelineId !== null && jumpTarget === null && listFresh) {
      useAppStore.getState().pushToast(t("edit_view_link_timeline_missing"), "warning");
      // react-doctor-disable-next-line react-doctor/no-adjust-state-on-prop-change -- 与上面的提示同属一次性通知：目标不存在时提示一次并作废定位
      setJump(null);
    }
  }, [jump, jumpTimelineId, jumpTarget, listFresh, timelines, t]);
  // 定位完成后把链接那条记为选中，此后标签不随定位指令清空而回落。
  const handleJumpApplied = useCallback(() => {
    if (jumpTarget !== null) setChosenId(jumpTarget);
    setJump(null);
  }, [jumpTarget]);
  const selectedId =
    jumpTarget ??
    (timelines && chosenId && timelines.some((item) => item.id === chosenId)
      ? chosenId
      : timelines
        ? pickDefaultTimeline(timelines)
        : null);
  const selected = timelines?.find((item) => item.id === selectedId);

  // 每次项目变更都重新读取：读取结果还取决于视频单元的 current 版本，不只取决于修订。
  const readoutKey = `${listKey}::${selectedId ?? ""}`;
  const [readout, setReadout] = useState<(Loaded<EditTimelineReadout> & { timelineId: string }) | null>(null);
  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    API.getEditTimeline(projectName, selectedId, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setReadout({ key: readoutKey, timelineId: selectedId, value });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        // 刷新失败时保留这条剪辑时间线上一次的读取结果，播放不被打断。
        setReadout((previous) =>
          previous?.timelineId === selectedId && "value" in previous
            ? previous
            : { key: readoutKey, timelineId: selectedId, error: errMsg(cause) },
        );
      });
    return () => controller.abort();
  }, [projectName, readoutKey, selectedId]);

  // 旁白配音与字幕随读取结果一起刷新；读取失败时沿用上一次的结果，还没有结果时只是不出旁白、不显示字幕。
  const [previewMedia, setPreviewMedia] = useState<{ timelineId: string; value: EditTimelinePreviewMedia } | null>(
    null,
  );
  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    API.getEditTimelinePreviewMedia(projectName, selectedId, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setPreviewMedia({ timelineId: selectedId, value });
      })
      .catch(() => {});
    return () => controller.abort();
  }, [projectName, readoutKey, selectedId]);

  if (list && "error" in list && !timelines) {
    return <LoadFailed message={list.error} onRetry={reload} />;
  }
  if (!timelines) return <Centered>{t("edit_view_loading")}</Centered>;
  if (timelines.length === 0 || !selected) {
    return renderEmptyState({ reload });
  }

  const current = readout?.timelineId === selected.id ? readout : null;
  const tabId = (timelineId: string) => `${tabIdPrefix}-tab-${timelineId}`;
  const panelId = `${tabIdPrefix}-panel`;
  const choose = (timelineId: string) => {
    setChosenId(timelineId);
    // 手动切换标签后放弃尚未完成的链接定位。
    setJump(null);
  };
  const authorName = t(`edit_view_author_${selected.updated_by.kind}`);
  const updatedJustNow = isJustNow(selected.updated_at);
  const updatedAt = formatRelativeTime(selected.updated_at, i18n.language) ?? selected.updated_at;

  // 整个视图不滚动。The timeline tabs, their menu and the "edited by" line stack in a narrow column left of the
  // preview (instead of a row above it), so the player keeps that height; the column scrolls if there are many.
  // Narrower than 40rem (phones) the column becomes one row above the preview (tabs, menu, Render), scrolling
  // sideways, without the "edited by" line.
  return (
    <div className="@container-size/edit-view dark flex h-full min-h-0 flex-col bg-background text-foreground @min-[40rem]/edit-view:flex-row">
      <div className="relative flex shrink-0 items-center gap-2 overflow-x-auto border-b border-border/50 p-2 @min-[40rem]/edit-view:w-40 @min-[40rem]/edit-view:flex-col @min-[40rem]/edit-view:items-stretch @min-[40rem]/edit-view:overflow-x-visible @min-[40rem]/edit-view:overflow-y-auto @min-[40rem]/edit-view:border-r @min-[40rem]/edit-view:border-b-0">
        {/* 方向键移动焦点即切换：切换标签只换预览内容，不卸载别的东西 */}
        <Tabs
          value={selected.id}
          onValueChange={(value: string) => choose(value)}
          orientation="vertical"
          className="min-w-0 shrink-0"
        >
          <TabsList
            aria-label={t("edit_view_timelines_aria")}
            activateOnFocus
            className="@max-[40rem]/edit-view:flex-row! @min-[40rem]/edit-view:w-full"
          >
            {timelines.map((item) => (
              <TabsTrigger
                key={item.id}
                value={item.id}
                id={tabId(item.id)}
                aria-controls={panelId}
                className="min-w-0 justify-start"
              >
                <span className="truncate">{item.name}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <div className="flex shrink-0 items-start gap-1 @min-[40rem]/edit-view:min-w-0 @min-[40rem]/edit-view:shrink">
          <EditTimelineMenu projectName={projectName} timeline={selected} onRenamed={reload} onDeleted={handleDeleted} />
          <p className="m-0 hidden min-w-0 pt-1 text-xs text-muted-foreground @min-[40rem]/edit-view:block">
            {updatedJustNow
              ? t("edit_view_updated_just_now", { author: authorName })
              : t("edit_view_updated", { author: authorName, time: updatedAt })}
          </p>
        </div>
        {renderActions?.({
          timelineId: selected.id,
          timelineName: selected.name,
          issues: current && "value" in current ? current.value.issues : null,
          showIssues,
        })}
      </div>

      <div
        role="tabpanel"
        id={panelId}
        aria-labelledby={tabId(selected.id)}
        // 播放器、详情栏与轨道固定深色：浅底会干扰对画面明暗的判断
        className="flex min-h-0 min-w-0 flex-1 flex-col px-2 py-2"
      >
        {current && "value" in current ? (
          <TimelinePreview
            key={selected.id}
            projectName={projectName}
            readout={current.value}
            media={previewMedia?.timelineId === selected.id ? previewMedia.value : null}
            script={script}
            aspect={aspect}
            ttsNarration={ttsNarration}
            seekRequest={jump && (jump.timelineId === null || jump.timelineId === selected.id) ? jump : null}
            onSeekHandled={handleJumpApplied}
            issuesRequest={issuesRequest}
          />
        ) : current && "error" in current ? (
          <LoadFailed message={current.error} onRetry={reload} />
        ) : (
          <Centered>{t("edit_view_loading")}</Centered>
        )}
      </div>
    </div>
  );
}

interface TimelinePreviewProps {
  projectName: string;
  ttsNarration: boolean;
  readout: EditTimelineReadout;
  media: EditTimelinePreviewMedia | null;
  script: unknown;
  aspect: PreviewAspect;
  /** 链接要求定位的位置；处理完调用 `onSeekHandled`。 */
  seekRequest: LinkJump | null;
  onSeekHandled: () => void;
  /** Counter from the actions area's "show issues": open the details column and focus the issue list. */
  issuesRequest: number;
}

/** Scroll to the issue list heading and focus it; false when the list is not rendered (details folded away). */
function focusIssueList(): boolean {
  const heading = document.getElementById(ISSUE_LIST_HEADING_ID);
  if (!heading) return false;
  heading.scrollIntoView({ block: "nearest", behavior: "smooth" });
  heading.focus({ preventScroll: true });
  return true;
}

// The timeline's height is the viewer's choice, kept per browser: null = its natural height.
const TIMELINE_HEIGHT_KEY = "arcreel.editTimeline.height";
const TIMELINE_MIN_HEIGHT = 72;
/** The player keeps at least this much height when the timeline is dragged up. */
const PLAYER_MIN_HEIGHT = 160;
const TIMELINE_KEY_STEP = 16;

function readTimelineHeight(): number | null {
  try {
    const raw = localStorage.getItem(TIMELINE_HEIGHT_KEY);
    const value = raw === null ? Number.NaN : Number(raw);
    return Number.isFinite(value) && value >= TIMELINE_MIN_HEIGHT ? value : null;
  } catch {
    return null;
  }
}

function saveTimelineHeight(value: number | null) {
  try {
    if (value === null) localStorage.removeItem(TIMELINE_HEIGHT_KEY);
    else localStorage.setItem(TIMELINE_HEIGHT_KEY, String(Math.round(value)));
  } catch {
    // storage blocked: the height simply isn't remembered
  }
}

/**
 * The bar between the player and the timeline: drag it (or use the arrow keys) to give the player or the
 * timeline more height; double-click or Home goes back to the timeline's natural height.
 */
function TimelineSplitter({
  areaRef,
  timelineRef,
  height,
  onChange,
}: {
  areaRef: RefObject<HTMLDivElement | null>;
  timelineRef: RefObject<HTMLDivElement | null>;
  height: number | null;
  onChange: (value: number | null) => void;
}) {
  const { t } = useTranslation("dashboard");
  const drag = useRef<{ startY: number; startHeight: number } | null>(null);
  const clamp = (value: number) => {
    const area = areaRef.current?.getBoundingClientRect().height ?? Number.POSITIVE_INFINITY;
    return Math.max(TIMELINE_MIN_HEIGHT, Math.min(value, area - PLAYER_MIN_HEIGHT));
  };
  const current = () => height ?? timelineRef.current?.getBoundingClientRect().height ?? TIMELINE_MIN_HEIGHT;
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { startY: event.clientY, startHeight: current() };
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    onChange(clamp(drag.current.startHeight - (event.clientY - drag.current.startY)));
  };
  const onPointerUp = () => {
    drag.current = null;
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      event.preventDefault();
      onChange(clamp(current() + (event.key === "ArrowUp" ? TIMELINE_KEY_STEP : -TIMELINE_KEY_STEP)));
    } else if (event.key === "Home") {
      event.preventDefault();
      onChange(null);
    }
  };
  // A focusable separator is a widget in ARIA (window splitter pattern), which jsx-a11y does not know.
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- window splitter: dragged with the pointer, moved with the arrow keys
    <div
      role="separator"
      aria-orientation="horizontal"
      aria-label={t("edit_view_timeline_resize")}
      aria-valuenow={height === null ? undefined : Math.round(height)}
      title={t("edit_view_timeline_resize")}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- window splitter: keyboard users move it with the arrow keys
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onDoubleClick={() => onChange(null)}
      onKeyDown={onKeyDown}
      className="group focus-ring flex h-2 shrink-0 cursor-row-resize touch-none items-center justify-center rounded-sm"
    >
      <span className="h-0.5 w-10 rounded-full bg-border transition-colors group-hover:bg-primary/60 group-focus-visible:bg-primary" />
    </div>
  );
}

function TimelinePreview({
  projectName,
  ttsNarration,
  readout,
  media,
  script,
  aspect,
  seekRequest,
  onSeekHandled,
  issuesRequest,
}: TimelinePreviewProps) {
  const { t } = useTranslation("dashboard");
  const [selectedClipId, setSelectedClipIdState] = useState<string | null>(null);
  // The details column folds away to give the player the width. It starts open only when the timeline has
  // issues to read, opens when a clip is selected, and otherwise stays as the viewer left it (it does not
  // fold by itself when a refresh clears the issues).
  // On a phone it starts folded and stays folded on a clip selection (it would cover the player); an "Issues" link
  // above the preview opens it over the player.
  const phone = useMediaQuery(PHONE_QUERY);
  const [detailsOpen, setDetailsOpen] = useState(() => readout.issues.length > 0 && !isPhoneWidth());
  const setSelectedClipId = useCallback((clipId: string | null) => {
    setSelectedClipIdState(clipId);
    if (clipId !== null && !isPhoneWidth()) setDetailsOpen(true);
  }, []);
  useEffect(() => {
    if (issuesRequest === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-off request from the actions area: unfold the details column so the issue list can take focus
    setDetailsOpen(true);
  }, [issuesRequest]);
  // Focus once per request, as soon as the column is open (not again when the viewer later reopens it).
  const handledIssuesRequest = useRef(0);
  useEffect(() => {
    if (issuesRequest > handledIssuesRequest.current && detailsOpen) {
      handledIssuesRequest.current = issuesRequest;
      focusIssueList();
    }
  }, [issuesRequest, detailsOpen]);
  const [timelineHeight, setTimelineHeightState] = useState<number | null>(readTimelineHeight);
  const setTimelineHeight = useCallback((value: number | null) => {
    setTimelineHeightState(value);
    saveTimelineHeight(value);
  }, []);
  const areaRef = useRef<HTMLDivElement>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const [showSubtitles, setShowSubtitles] = useState(true);

  // 内容相同的重新读取不换计划引用，播放不因项目的无关变更而重新定位。
  const planJson = useMemo(() => JSON.stringify(buildPlaybackPlan(readout, media)), [readout, media]);
  const plan = useMemo(() => JSON.parse(planJson) as PlaybackPlan, [planJson]);
  // 源视频地址只随视频单元的 current 版本变化；脚本对象每次刷新都换引用，这里只取它的形状。
  const referenceVideo = isReferenceVideoScript(script);
  const sourceUrl = useCallback(
    (segment: PlaybackSegment) =>
      segment.hasVideo && segment.videoVersion !== null
        ? API.getFileUrl(projectName, unitVideoPath(referenceVideo, segment.unitId), segment.videoVersion)
        : null,
    [projectName, referenceVideo],
  );
  // 音频摆放与旁白配音地址同样按内容去重，内容不变的刷新不打断正在播放的音频。
  const audioJson = useMemo(
    () => JSON.stringify([...narrationPlacements(readout, media), ...bgmPlacements(readout, media)]),
    [readout, media],
  );
  const audio = useMemo(() => JSON.parse(audioJson) as AudioPlacement[], [audioJson]);
  const audioSourcesJson = useMemo(
    () =>
      JSON.stringify({
        narration: Object.fromEntries(
          (media?.units ?? []).flatMap((unit) => (unit.narration_audio ? [[unit.unit_id, unit.narration_audio]] : [])),
        ),
        bgm: Object.fromEntries((media?.bgm ?? []).map((item) => [item.bgm_id, item.path])),
      }),
    [media],
  );
  const audioUrl = useMemo(() => {
    const sources = JSON.parse(audioSourcesJson) as {
      narration: Record<string, EditPreviewNarrationAudio>;
      bgm: Record<string, string>;
    };
    return (placement: AudioPlacement) => {
      // BGM 按字节登记（bgm/{随机 id}）、文件不再改写，地址本身就唯一，显式不带版本号。
      if (placement.kind === "bgm") {
        const path = sources.bgm[placement.sourceId];
        return path ? API.getFileUrl(projectName, path, null) : null;
      }
      const source = sources.narration[placement.sourceId];
      return source ? API.getFileUrl(projectName, source.path, source.version) : null;
    };
  }, [audioSourcesJson, projectName]);
  const playback = useTimelinePlayback(plan, sourceUrl, audio, audioUrl);
  const narration = useMemo(() => narrationSpans(readout, ttsNarration), [readout, ttsNarration]);
  const subtitles = useMemo(() => placeSubtitles(readout, plan, media), [readout, plan, media]);
  const bgm = useMemo(() => audio.filter((placement) => placement.kind === "bgm"), [audio]);

  // 链接带了时间点：播放头移过去（暂停在那一刻），并选中落在该时间的片段。
  const { seek } = playback;
  useEffect(() => {
    if (!seekRequest) return;
    if (seekRequest.seconds !== null) {
      seek(seekRequest.seconds);
      const location = locate(plan, seekRequest.seconds);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- 一次性的链接定位：移动播放头的同时选中对应片段
      if (location) setSelectedClipId(plan.segments[location.index].clipId);
    }
    // 处理完立即作废，plan 随后的刷新不会再把播放头拉回去。
    onSeekHandled();
  }, [seekRequest, seek, plan, onSeekHandled, setSelectedClipId]);

  const trimIgnored = useMemo(() => issueClipIds(readout, "trim_ignored"), [readout]);
  const unused = useMemo(() => unusedUnitIds(readout), [readout]);
  const thumbnails = useMemo(() => unitThumbnails(script), [script]);
  const positions = useMemo(() => unitPositions(script), [script]);
  const activeClipId = plan.segments[playback.index]?.clipId ?? null;
  const currentClip = readout.clips.find((clip) => clip.id === activeClipId);
  const selectedClip = readout.clips.find((clip) => clip.id === selectedClipId);
  const clipIds = useMemo(() => new Set(readout.clips.map((clip) => clip.id)), [readout]);

  // Player on the left, the details column (280px or 340px with the canvas width, or folded to a thin rail)
  // on the right; the tracks run full width below, behind a splitter that sets their height.
  return (
    <div ref={areaRef} className="flex min-h-0 flex-1 flex-col">
      {phone && !detailsOpen && readout.issues.length > 0 && (
        <Button variant="link-destructive" size="inline" className="mb-1 self-start" onClick={() => setDetailsOpen(true)}>
          <AlertTriangle aria-hidden data-icon="inline-start" />
          {t("edit_view_issues_title", { count: readout.issues.length })}
        </Button>
      )}
      <div className="relative flex min-h-0 flex-1 gap-2">
        <div className="min-h-0 min-w-0 flex-1">
          <EditTimelinePlayer
            plan={plan}
            playback={playback}
            aspect={aspect}
            current={currentClip}
            positions={positions}
            trimIgnored={currentClip ? trimIgnored.has(currentClip.id) : false}
            subtitles={subtitles}
            showSubtitles={showSubtitles}
            onToggleSubtitles={() => setShowSubtitles((shown) => !shown)}
          />
        </div>
        {detailsOpen ? (
          /* 详情栏是视图里唯一的滚动区；可以只有说明文字，所以自身可聚焦，键盘也能滚动 */
          <div
            role="region"
            aria-label={t("edit_view_details_aria")}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- 详情栏可能只有说明文字，须能用键盘聚焦后滚动
            tabIndex={0}
            className={cn(
              "focus-ring relative flex min-h-0 shrink-0 flex-col gap-4 overflow-y-auto rounded-lg border border-border bg-card p-3",
              // on a phone it lies over the player instead of beside it
              // (later in the page than the player, so it lies above it without a z-index)
              phone ? "absolute inset-0" : "w-[280px] @6xl/canvas:w-[340px]",
            )}
          >
            <Button
              variant="ghost"
              size="icon-xs"
              className="absolute top-2 right-2"
              onClick={() => setDetailsOpen(false)}
              aria-label={t("edit_view_details_hide")}
              title={t("edit_view_details_hide")}
            >
              <PanelRightClose aria-hidden />
            </Button>
            <section className="shrink-0 pr-6" aria-label={t("edit_view_inspector_aria")}>
              <ClipInspector
                projectName={projectName}
                clip={selectedClip}
                trimIgnored={selectedClip ? trimIgnored.has(selectedClip.id) : false}
                thumbnail={selectedClip ? thumbnails.get(selectedClip.unit_id) : undefined}
                positions={positions}
              />
            </section>
            <div className="shrink-0 border-t border-border pt-4">
              <IssueList issues={readout.issues} clipIds={clipIds} positions={positions} onSelectClip={setSelectedClipId} />
            </div>
          </div>
        ) : phone ? null : (
          <Button
            variant="outline"
            size="icon-sm"
            className="shrink-0 self-start"
            onClick={() => setDetailsOpen(true)}
            aria-label={t("edit_view_details_show")}
            title={t("edit_view_details_show")}
          >
            <PanelRightOpen aria-hidden />
          </Button>
        )}
      </div>
      <TimelineSplitter
        areaRef={areaRef}
        timelineRef={timelineRef}
        height={timelineHeight}
        onChange={setTimelineHeight}
      />
      <div
        ref={timelineRef}
        className={cn("min-w-0 shrink-0", timelineHeight !== null && "relative h-(--timeline-height) overflow-y-auto")}
        style={timelineHeight !== null ? ({ "--timeline-height": `${timelineHeight}px` } as CSSProperties) : undefined}
      >
        <EditTimelineTracks
          projectName={projectName}
          readout={readout}
          t={playback.t}
          selectedClipId={selectedClipId}
          activeClipId={activeClipId}
          trimIgnored={trimIgnored}
          unusedUnits={unused}
          positions={positions}
          thumbnails={thumbnails}
          narration={narration}
          subtitles={subtitles}
          bgm={bgm}
          onSelectClip={setSelectedClipId}
          onSeek={playback.seek}
        />
      </div>
    </div>
  );
}

function Centered({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-6 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

function LoadFailed({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useTranslation("dashboard");
  return (
    <Centered>
      <p role="alert" className="text-destructive">
        {t("edit_view_load_failed", { message })}
      </p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        {t("edit_view_retry")}
      </Button>
    </Centered>
  );
}
