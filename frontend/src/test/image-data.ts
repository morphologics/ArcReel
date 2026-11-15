/** Build a base64 payload that decodes to about `bytes` bytes, to drive image budget boundaries. */
/** 生成解码后约为 `bytes` 字节的 base64 负载，用于驱动图片预算边界。 */
export function base64OfSize(bytes: number): string {
  return "A".repeat(Math.ceil(bytes / 3) * 4);
}
