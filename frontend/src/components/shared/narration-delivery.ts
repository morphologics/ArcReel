import type { NarrationDelivery } from "@/types";

/** Project narration delivery config (docs/adr/0089): delivery mode + TTS snapshot. / 项目的旁白交付配置（docs/adr/0089）：交付方式 + TTS 快照。 */
export interface NarrationDeliveryValue {
  delivery: NarrationDelivery;
  /** "provider/model"; empty string means not selected. / "provider/model"；空串表示未选。 */
  audioBackend: string;
  narrationVoice: string;
  /** null means speech rate isn't passed to the provider. / null 表示不向供应商传语速。 */
  narrationSpeed: number | null;
}

export type NarrationDeliveryProblem = "model" | "voice";

/** TTS voiceover projects must carry a model and voice; post-production voiceover projects have no requirement. / TTS 配音项目必须带模型与音色；后期配音项目没有要求。 */
export function narrationDeliveryProblem(value: NarrationDeliveryValue): NarrationDeliveryProblem | null {
  if (value.delivery !== "use_tts") return null;
  if (!value.audioBackend) return "model";
  if (!value.narrationVoice.trim()) return "voice";
  return null;
}
