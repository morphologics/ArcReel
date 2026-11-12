import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  BadgeCheck,
  CircleCheck,
  CircleHelp,
  Download,
  History,
  Loader2,
  Timer,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Toggle } from "@/components/ui/toggle";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import type {
  PresentationReadModel,
  PresentationResourceType,
  PresentationVariant,
} from "@/types/presentation";
import { errMsg } from "@/utils/async";
import { downloadBlob } from "@/utils/download";
import { useItemPositionLabel } from "@/components/shared/episode-item/useEpisodeItem";

interface PresentationPlayerProps {
  projectName: string;
  resourceType: PresentationResourceType;
  resourceId: string;
  videoVersion?: number;
  audioVersion?: number;
  posterPath?: string | null;
  initialVariant?: PresentationVariant;
  /**
   * 载入后从该时间（秒，单元视频自身的时间轴）开始播放；超出可播放范围时夹到范围内。
   * 同一个 `requestId` 只生效一次，换一个 `requestId` 就再定位一次。浏览器拦截自动播放时停在该位置等用户点播放。
   */
  startAt?: { seconds: number; requestId: string };
  /** `startAt` 生效后回调，参数为该请求的 `requestId`。 */
  onStartApplied?: (requestId: string) => void;
  className?: string;
  /**
   * Where the status icons (version, provenance, up to date, mechanical timing) go: e.g. the card's title row above
   * the preview. Without it they sit over the picture's top-left corner.
   */
  statusSlot?: HTMLElement | null;
}

/** 起始位置离可播放范围末尾至少留出这么多，免得一播放就撞上边界停住。 */
const START_END_MARGIN_SECONDS = 0.05;

interface PresentationLoadState {
  resourceKey: string;
  requestKey: string;
  presentation: PresentationReadModel | null;
  error: string | null;
  supportsVariants: boolean;
}

export function PresentationPlayer({
  projectName,
  resourceType,
  resourceId,
  videoVersion,
  audioVersion,
  posterPath,
  initialVariant = "post_production",
  startAt,
  onStartApplied,
  className = "",
  statusSlot = null,
}: PresentationPlayerProps) {
  const { t } = useTranslation("dashboard");
  const itemLabel = useItemPositionLabel(resourceId);
  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const retainedVideoRef = useRef<HTMLVideoElement>(null);
  const retainedAudioRef = useRef<HTMLAudioElement>(null);
  const requestEpochRef = useRef(0);
  const [variant, setVariant] = useState<PresentationVariant>(initialVariant);
  const [loadState, setLoadState] = useState<PresentationLoadState>({
    resourceKey: "",
    requestKey: "",
    presentation: null,
    error: null,
    supportsVariants: false,
  });
  const [downloading, setDownloading] = useState(false);
  const [positionMicroseconds, setPositionMicroseconds] = useState(0);
  const [nativeCaptionsShowing, setNativeCaptionsShowing] = useState(false);
  const canonicalPath =
    resourceType === "videos"
      ? `videos/scene_${resourceId}.mp4`
      : `reference_videos/${resourceId}.mp4`;
  const fingerprint = useProjectsStore((state) => state.getAssetFingerprint(canonicalPath));
  const narrationFingerprint = useProjectsStore((state) =>
    state.getAssetFingerprint(`audio/segment_${resourceId}.wav`),
  );
  const projectSnapshotRevision = useProjectsStore(
    (state) => state.projectSnapshotRevisions[projectName] ?? 0,
  );
  const posterFingerprint = useProjectsStore((state) =>
    posterPath ? state.getAssetFingerprint(posterPath) : null,
  );
  const resourceKey = JSON.stringify([projectName, resourceType, resourceId, videoVersion, audioVersion]);
  const requestKey = JSON.stringify([
    projectName,
    resourceType,
    resourceId,
    variant,
    videoVersion,
    audioVersion,
    fingerprint,
    narrationFingerprint,
    projectSnapshotRevision,
  ]);
  const requestMatches = loadState.requestKey === requestKey;
  const presentation = requestMatches ? loadState.presentation : null;
  const error = requestMatches ? loadState.error : null;
  const loading = !requestMatches;

  const bindVideo = useCallback((video: HTMLVideoElement | null) => {
    videoRef.current = video;
    if (video) retainedVideoRef.current = video;
  }, []);

  const bindNarration = useCallback((audio: HTMLAudioElement | null) => {
    audioRef.current = audio;
    if (audio) retainedAudioRef.current = audio;
  }, []);

  const stopRetainedMedia = useCallback(() => {
    retainedVideoRef.current?.pause();
    retainedAudioRef.current?.pause();
    retainedVideoRef.current = null;
    retainedAudioRef.current = null;
  }, []);

  useEffect(() => {
    const epoch = ++requestEpochRef.current;
    const controller = new AbortController();
    void API.getPresentation(projectName, resourceType, resourceId, {
      variant,
      videoVersion,
      audioVersion,
      signal: controller.signal,
    })
      .then((value) => {
        if (requestEpochRef.current !== epoch) return;
        setLoadState({
          resourceKey,
          requestKey,
          presentation: value,
          error: null,
          supportsVariants: value.speech_mode === "narrator_voiceover",
        });
        setPositionMicroseconds(0);
      })
      .catch((cause: unknown) => {
        if (
          requestEpochRef.current !== epoch ||
          (cause instanceof DOMException && cause.name === "AbortError")
        ) {
          return;
        }
        setLoadState((previous) => ({
          resourceKey,
          requestKey,
          presentation: null,
          error: errMsg(cause),
          supportsVariants: previous.resourceKey === resourceKey && previous.supportsVariants,
        }));
      });
    return () => {
      controller.abort();
      stopRetainedMedia();
      if (requestEpochRef.current === epoch) requestEpochRef.current += 1;
    };
  }, [
    projectName,
    resourceType,
    resourceId,
    variant,
    videoVersion,
    audioVersion,
    fingerprint,
    narrationFingerprint,
    projectSnapshotRevision,
    resourceKey,
    requestKey,
    stopRetainedMedia,
  ]);

  const enforceVideoAudioPolicy = useCallback(
    (video: HTMLVideoElement) => {
      if (!presentation) return;
      if (!presentation.video.audio_enabled || presentation.video.gain === 0) {
        if (!video.muted) video.muted = true;
      }
    },
    [presentation],
  );

  const synchronizeNarrationControls = useCallback(
    (video: HTMLVideoElement) => {
      const audio = audioRef.current;
      const narration = presentation?.narration_audio;
      if (!audio || !narration || !presentation) return;
      audio.volume = Math.min(1, video.volume * narration.gain);
      audio.muted = presentation.video.audio_enabled && presentation.video.gain > 0 ? video.muted : false;
      audio.playbackRate = video.playbackRate;
    },
    [presentation],
  );

  useEffect(() => {
    if (videoRef.current && presentation) {
      videoRef.current.volume = presentation.narration_audio?.gain ?? presentation.video.gain;
      videoRef.current.muted = !presentation.video.audio_enabled || presentation.video.gain === 0;
      enforceVideoAudioPolicy(videoRef.current);
      synchronizeNarrationControls(videoRef.current);
    }
  }, [enforceVideoAudioPolicy, presentation, synchronizeNarrationControls]);

  const synchronizeNarration = useCallback(
    (play: boolean) => {
      const video = videoRef.current;
      const audio = audioRef.current;
      const narration = presentation?.narration_audio;
      if (!video || !audio || !narration) return;
      synchronizeNarrationControls(video);
      const start = narration.start_microseconds / 1_000_000;
      const duration = narration.duration_microseconds / 1_000_000;
      const audioTime = video.currentTime - start;
      if (audioTime < 0 || audioTime >= duration) {
        audio.pause();
        return;
      }
      if (Math.abs(audio.currentTime - audioTime) > 0.1) audio.currentTime = audioTime;
      if (play) void audio.play().catch(() => undefined);
    },
    [presentation, synchronizeNarrationControls],
  );

  const appliedStartRef = useRef<string | null>(null);
  useEffect(() => {
    const video = videoRef.current;
    if (!startAt || !presentation || !video || appliedStartRef.current === startAt.requestId) return;
    const first = presentation.video.start_microseconds / 1_000_000;
    const end = first + presentation.video.duration_microseconds / 1_000_000;
    const target = Math.min(Math.max(startAt.seconds, first), Math.max(first, end - START_END_MARGIN_SECONDS));
    const apply = () => {
      appliedStartRef.current = startAt.requestId;
      video.currentTime = target;
      // 播放器可能在可滚动栏里的折叠位置之下，先滚到可见处。
      video.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
      void video.play().catch(() => undefined);
      onStartApplied?.(startAt.requestId);
    };
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) {
      apply();
      return;
    }
    video.addEventListener("loadedmetadata", apply, { once: true });
    return () => video.removeEventListener("loadedmetadata", apply);
    // 视频元素在 presentation 就绪后才挂载，所以载入结果变化时要重跑。
  }, [startAt, presentation, onStartApplied]);

  const stopAtPresentationBoundary = useCallback(
    (video: HTMLVideoElement) => {
      if (!presentation) return false;
      const boundaryMicroseconds =
        presentation.video.start_microseconds + presentation.video.duration_microseconds;
      const boundarySeconds = boundaryMicroseconds / 1_000_000;
      if (video.currentTime < boundarySeconds) return false;
      if (video.currentTime !== boundarySeconds) video.currentTime = boundarySeconds;
      video.pause();
      audioRef.current?.pause();
      setPositionMicroseconds(presentation.video.duration_microseconds);
      return true;
    },
    [presentation],
  );

  const refreshNativeCaptionMode = useCallback(() => {
    const tracks = videoRef.current?.textTracks;
    let showing = false;
    if (tracks) {
      for (let index = 0; index < tracks.length; index += 1) {
        if (tracks[index]?.mode === "showing") {
          showing = true;
          break;
        }
      }
    }
    setNativeCaptionsShowing(showing);
  }, []);

  useEffect(() => {
    const tracks = videoRef.current?.textTracks;
    refreshNativeCaptionMode();
    tracks?.addEventListener?.("change", refreshNativeCaptionMode);
    return () => tracks?.removeEventListener?.("change", refreshNativeCaptionMode);
  }, [presentation, refreshNativeCaptionMode]);

  const videoUrl = presentation
    ? API.getFileUrl(projectName, presentation.video.artifact_path, presentation.video.content_digest)
    : null;
  const narrationUrl = presentation?.narration_audio
    ? API.getFileUrl(
        projectName,
        presentation.narration_audio.artifact_path,
        presentation.narration_audio.content_digest,
      )
    : null;
  const posterUrl = posterPath
    ? API.getFileUrl(projectName, posterPath, posterFingerprint)
    : undefined;
  const captions = useMemo(
    () =>
      presentation?.subtitles_webvtt
        ? `data:text/vtt;charset=utf-8,${encodeURIComponent(presentation.subtitles_webvtt)}`
        : undefined,
    [presentation],
  );
  const activeCue = presentation?.subtitles.find(
    (cue) =>
      cue.start_microseconds <= positionMicroseconds &&
      positionMicroseconds < cue.start_microseconds + cue.duration_microseconds,
  );

  const chooseVariant = (next: PresentationVariant) => {
    if (next === variant) return;
    setVariant(next);
  };

  const downloadBundle = async () => {
    if (!presentation || downloading) return;
    setDownloading(true);
    try {
      const { blob, filename } = await API.downloadPresentationBundle(
        projectName,
        resourceType,
        resourceId,
        {
          variant: presentation.variant,
          videoVersion: presentation.video.version,
          audioVersion: presentation.narration_audio?.version,
        },
      );
      downloadBlob(blob, filename);
    } catch (cause) {
      useAppStore
        .getState()
        .pushToast(t("presentation_download_failed", { message: errMsg(cause) }), "error");
    } finally {
      // react-doctor-disable-next-line react-doctor/no-unowned-async-error-clear -- 下载在途时按钮禁用且入口先判 downloading，不会有更新的下载同时持有这个标志
      setDownloading(false);
    }
  };

  // 播放器各状态都固定深色：叠在画面上的状态色和控件按深色取值
  if (loading && !presentation) {
    return (
      <div className={`dark grid h-full w-full place-items-center bg-black/35 ${className}`}>
        <Loader2 className="h-5 w-5 animate-spin text-white/60" aria-label={t("presentation_loading")} />
      </div>
    );
  }

  if (!presentation || !videoUrl) {
    return (
      <div
        className={`dark flex h-full w-full flex-col items-center justify-center gap-2 bg-black/40 p-4 text-center ${className}`}
      >
        <p role="alert" className="text-xs text-warn">
          {error || t("presentation_unavailable")}
        </p>
        {loadState.supportsVariants && (
          <VariantControls
            variant={variant}
            postProductionLabel={t("presentation_post_production")}
            useTtsLabel={t("presentation_use_tts")}
            onChoose={chooseVariant}
          />
        )}
      </div>
    );
  }

  // the presentation's status as icons with tooltips (the words for screen readers)
  const historical = presentation.selection !== "current";
  const statusIcons = (
    <>
      <StatusIcon
        icon={historical ? History : BadgeCheck}
        label={t(`presentation_selection_${presentation.selection}`)}
        className={historical ? "text-warn" : undefined}
      />
      {presentation.provenance === "unavailable" && (
        <StatusIcon icon={CircleHelp} label={t("presentation_provenance_unavailable")} className="text-warn" />
      )}
      {presentation.currency && (
        <StatusIcon
          icon={presentation.currency === "current" ? CircleCheck : TriangleAlert}
          label={t(`presentation_currency_${presentation.currency}`)}
          className={presentation.currency === "current" ? "text-good" : "text-warn"}
        />
      )}
      {presentation.timing === "mechanical" && (
        <StatusIcon icon={Timer} label={t("presentation_mechanical_timing")} />
      )}
      {presentation.speech_mode === "narrator_voiceover" && (
        <VariantControls
          variant={variant}
          postProductionLabel={t("presentation_post_production")}
          useTtsLabel={t("presentation_use_tts")}
          onChoose={chooseVariant}
        />
      )}
    </>
  );

  return (
    <div className={`dark relative h-full w-full bg-black ${className}`}>
      <video
        ref={bindVideo}
        src={videoUrl}
        poster={posterUrl}
        aria-label={t("presentation_video_aria", { id: itemLabel })}
        controls
        playsInline
        preload="metadata"
        muted={!presentation.video.audio_enabled || presentation.video.gain === 0}
        onVolumeChange={(event) => {
          enforceVideoAudioPolicy(event.currentTarget);
          synchronizeNarrationControls(event.currentTarget);
        }}
        onRateChange={(event) => synchronizeNarrationControls(event.currentTarget)}
        onPlay={(event) => {
          if (!stopAtPresentationBoundary(event.currentTarget)) synchronizeNarration(true);
        }}
        onPlaying={(event) => {
          if (!stopAtPresentationBoundary(event.currentTarget)) synchronizeNarration(true);
        }}
        onPause={() => audioRef.current?.pause()}
        onWaiting={() => audioRef.current?.pause()}
        onStalled={() => audioRef.current?.pause()}
        onSeeked={(event) => {
          if (!stopAtPresentationBoundary(event.currentTarget)) {
            synchronizeNarration(!event.currentTarget.paused);
          }
        }}
        onTimeUpdate={(event) => {
          if (!stopAtPresentationBoundary(event.currentTarget)) {
            setPositionMicroseconds(Math.round(event.currentTarget.currentTime * 1_000_000));
            synchronizeNarration(!event.currentTarget.paused);
          }
        }}
        onEnded={() => audioRef.current?.pause()}
        className="h-full w-full object-contain"
      >
        <track kind="captions" src={captions} srcLang="und" label={t("presentation_captions")} />
      </video>
      {narrationUrl && (
        <audio
          ref={bindNarration}
          src={narrationUrl}
          aria-label={t("presentation_tts_track_aria", { id: itemLabel })}
          preload="metadata"
          className="hidden"
        >
          <track kind="captions" src={captions} srcLang="und" label={t("presentation_captions")} />
        </audio>
      )}

      {activeCue && !nativeCaptionsShowing && (
        <div className="pointer-events-none absolute inset-x-3 bottom-12 flex justify-center">
          <span className="max-w-[92%] rounded-md bg-black/75 px-2.5 py-1 text-center text-xs leading-relaxed text-white shadow-lg">
            {activeCue.text}
          </span>
        </div>
      )}

      <div className="absolute inset-x-2 top-2 flex flex-wrap items-center gap-1.5">
        {statusSlot ? createPortal(statusIcons, statusSlot) : <div className="flex rounded-md bg-black/70 px-1">{statusIcons}</div>}
        <span className="flex-1" />
        <Button
          variant="secondary"
          size="icon-xs"
          onClick={() => void downloadBundle()}
          disabled={downloading}
          aria-label={t("presentation_download")}
        >
          {downloading ? <Loader2 className="animate-spin" aria-hidden /> : <Download aria-hidden />}
        </Button>
      </div>
    </div>
  );
}

/**
 * Original audio vs. TTS overlay as one "TTS" toggle: pressed = the TTS overlay, released = the original audio; the
 * tooltip names the one playing.
 */
function VariantControls({
  variant,
  postProductionLabel,
  useTtsLabel,
  onChoose,
}: {
  variant: PresentationVariant;
  postProductionLabel: string;
  useTtsLabel: string;
  onChoose: (variant: PresentationVariant) => void;
}) {
  const tts = variant === "use_tts";
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Toggle
            size="sm"
            pressed={tts}
            onPressedChange={(pressed) => onChoose(pressed ? "use_tts" : "post_production")}
            aria-label={useTtsLabel}
          />
        }
      >
        TTS
      </TooltipTrigger>
      <TooltipContent>{tts ? useTtsLabel : postProductionLabel}</TooltipContent>
    </Tooltip>
  );
}

/** One status as an icon; hovering, focusing or tapping it shows the words (a button only so it can take focus). */
function StatusIcon({ icon: Icon, label, className }: { icon: LucideIcon; label: string; className?: string }): ReactNode {
  return (
    <Tooltip>
      <TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label={label} />}>
        <Icon aria-hidden className={className ?? "text-muted-foreground"} size={16} />
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
