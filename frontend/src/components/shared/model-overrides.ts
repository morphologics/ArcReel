import type { ModelConfigValue } from "./ModelConfigSection";

/** The three model channels in which a project overrides global defaults. / 项目覆盖全局默认的三个模型通道。 */
export type ModelChannel = "video" | "image" | "text";

type ChannelFields = Pick<
  ModelConfigValue,
  | "videoBackend"
  | "videoProviderI2V"
  | "videoProviderR2V"
  | "imageBackendDefault"
  | "imageBackendT2I"
  | "imageBackendI2I"
  | "textBackendDefault"
  | "textBackendSimple"
  | "textBackendComplex"
>;

/** Model fields in each channel that follow global when empty: the default model and two breakdown items (three tiers for text). / 每个通道里留空即跟随全局的模型字段：默认模型与两个细分项（文本是三个档位）。 */
export const CHANNEL_MODEL_FIELDS: Record<ModelChannel, readonly (keyof ChannelFields)[]> = {
  video: ["videoBackend", "videoProviderI2V", "videoProviderR2V"],
  image: ["imageBackendDefault", "imageBackendT2I", "imageBackendI2I"],
  text: ["textBackendDefault", "textBackendSimple", "textBackendComplex"],
};

/**
 * Number of global defaults the project overrides: the count of non-empty fields among the 9 model fields, plus 1 when "Generate video with audio" doesn't follow global (non-null).
 * Resolution and default duration aren't counted: they have no global layer, so there's no "follow global".
 *
 * 项目覆盖全局默认的项数：9 个模型字段中非空的个数，加上「生成有声视频」不跟随全局（非 null）时的 1 项。
 * 分辨率与默认时长不计入：它们没有全局层，不存在「跟随全局」。
 */
export function countModelOverrides(value: ChannelFields, generateAudio: boolean | null): number {
  const fields = Object.values(CHANNEL_MODEL_FIELDS).flat();
  return fields.filter((field) => value[field] !== "").length + (generateAudio === null ? 0 : 1);
}

/** Whether a channel overrides global defaults. "Generate video with audio" belongs to the video channel. / 通道是否覆盖了全局默认。「生成有声视频」归在视频通道。 */
export function channelOverridden(channel: ModelChannel, value: ChannelFields, generateAudio: boolean | null): boolean {
  if (channel === "video" && generateAudio !== null) return true;
  return CHANNEL_MODEL_FIELDS[channel].some((field) => value[field] !== "");
}
