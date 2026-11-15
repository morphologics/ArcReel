/**
 * Hard range (closed interval): the same yardstick as is_valid_speech_rate in backend lib.speech.speech_rate.
 * For the basis of the lower bound (headroom for downstream duration conversion), see the comment on backend MIN_SPEECH_RATE_UPS.
 *
 * 硬区间（闭区间）：与后端 lib.speech.speech_rate 的 is_valid_speech_rate 同一把尺。
 * 下界取值依据（下游时长换算的余量）见后端 MIN_SPEECH_RATE_UPS 的注释。
 */
export const SPEECH_RATE_MIN = 0.001;
export const SPEECH_RATE_MAX = 20;

/** Whether the value can be submitted (null = not filled in, valid). / 该值是否可提交（null = 未填，合法）。 */
export function isValidSpeechRate(value: number | null): boolean {
  if (value === null) return true;
  return value >= SPEECH_RATE_MIN && value <= SPEECH_RATE_MAX;
}
