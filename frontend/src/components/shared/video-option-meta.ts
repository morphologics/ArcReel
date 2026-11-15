import type { TFunction } from "i18next";
import { formatDurationsLabel } from "@/utils/duration_format";
import { catalogDurations, lookupCatalogVideoAudio, lookupResolutions, lookupVideoAudioControl } from "@/utils/provider-models";
import type { CustomProviderInfo } from "@/types/custom-provider";
import type { MediaType, ProviderInfo, VideoRoute } from "@/types";

/**
 * Capability line renderer for dropdown option rows (duration / resolution / audio track), passed to `ProviderModelSelect.renderOptionMeta`.
 * The project settings page and global settings page share one assembly: the only difference between them is whether `endpointToMediaType` exists, so they don't each write one.
 *
 * Deliberately not a hook: callers may construct the renderer after an early-return branch, and calling `useTranslation` inside would violate
 * the hooks call-order rule; `t` is passed by the caller, and keys always carry the `dashboard:` prefix so they aren't affected by the caller's default namespace.
 *
 * 下拉选项行的能力线渲染器（时长 / 分辨率 / 音轨），交给 `ProviderModelSelect.renderOptionMeta`。
 * 项目设置页与全局设置页同一份拼装：两处只有 `endpointToMediaType` 有无之别，不各写一份。
 *
 * 刻意不是 hook——调用方可能在早退分支之后才构造渲染器，内部再调 `useTranslation` 会违反
 * hooks 调用顺序规则；`t` 由调用方传入，key 一律带 `dashboard:` 前缀以免受调用方默认命名空间影响。
 */
export function videoOptionMetaRenderer({
  t,
  providers,
  customProviders,
  endpointToMediaType,
  defaultRoute,
}: {
  t: TFunction;
  providers: ProviderInfo[];
  customProviders: CustomProviderInfo[];
  endpointToMediaType?: Record<string, MediaType>;
  /**
   * Execution path of the default layer dropdown. The default layer spans all purposes, so the path can only come from the caller's context (whether the project uses reference-to-video);
   * the global settings page has no project context, and omitting it falls back to the catalog i2v slot. Breakdown dropdowns ignore this value: their own
   * `key` is the path, see `asVideoRoute` below.
   */
  /** 默认层下拉的执行路径。默认层跨全部用途，路径只能由调用方的上下文给出（项目是否走参考生
   *  视频）；全局设置页无项目上下文，省略即回退目录 i2v 位。细分项下拉不看此值——它们各自的
   *  `key` 就是路径，见下方 `asVideoRoute`。 */
  defaultRoute?: VideoRoute;
}) {
  return (fullValue: string, subFieldKey?: string) => {
    // Breakdown dropdowns carry their own path; only the default layer falls back to the path the caller gave: the three video dropdowns on the same screen belong to different paths,
    // and sharing one would label the "image-to-video" cell by the reference-to-video convention (or vice versa).
    // 细分项下拉自带路径，默认层才回落到调用方给的路径：同屏三个视频下拉分属不同路径，
    // 共用一条会让「图生视频」那一格按参考生的口径标注（或反之）。
    const route = asVideoRoute(subFieldKey) ?? defaultRoute;
    const durations = catalogDurations(providers, customProviders, fullValue);
    const resolutions = lookupResolutions(
      providers,
      fullValue,
      customProviders,
      endpointToMediaType,
    ).options;
    // null when the model can't be found, and the audio track cell isn't rendered at all; neither branch makes up an answer.
    // 查不到模型时为 null，音轨格整格不渲染——两条分支都不臆造一个答案。
    const audioControl = route ? lookupVideoAudioControl(providers, fullValue, route) : null;
    const hasAudioTrack = route
      ? (audioControl === null ? null : audioControl !== "always_off")
      : (lookupCatalogVideoAudio(providers, fullValue)?.hasAudioTrack ?? null);
    const parts: string[] = [];
    if (durations?.length) parts.push(formatDurationsLabel(durations));
    if (resolutions.length) parts.push(resolutions.join(" / "));
    if (hasAudioTrack !== null) {
      parts.push(t(hasAudioTrack ? "dashboard:video_spec_audio_has" : "dashboard:video_spec_audio_none"));
    }
    return parts.length > 0 ? parts.join(" · ") : t("dashboard:video_option_caps_unknown");
  };
}

/**
 * The video execution path in a breakdown item's `key`. `LayeredModelFields` breakdown items are named by task-type bucket, and the two video buckets
 * happen to be exactly the two paths; image buckets (t2i / i2i) and text tiers aren't video paths, so return null and let the default layer path cover them.
 *
 * 细分项 `key` 中的视频执行路径。`LayeredModelFields` 的细分项按任务类型桶命名，视频两个桶
 * 恰好就是两条路径；图片桶（t2i / i2i）与文本档位不是视频路径，返回 null 交由默认层路径兜底。
 */
function asVideoRoute(subFieldKey: string | undefined): VideoRoute | null {
  return subFieldKey === "i2v" || subFieldKey === "r2v" ? subFieldKey : null;
}
