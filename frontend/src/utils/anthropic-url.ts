/**
 * Normalization and validation of the Agent credential base_url, matching the backend lib/config/url_utils.py verbatim.
 *
 * "Stored value is the call value": the Claude CLI's built-in SDK always appends /v1/messages to base_url,
 * so the address previewed in the frontend must be the same string as the backend probe and runtime requests.
 *
 * Agent 凭证 base_url 的归一与校验，与后端 lib/config/url_utils.py 逐字对应。
 *
 * 「存储值即调用值」：Claude CLI 内置 SDK 固定在 base_url 后拼 /v1/messages，
 * 前端预览的地址必须与后端探测、运行时请求是同一个字符串。
 */

/** Path segment always appended by the Claude CLI. / Claude CLI 固定追加的路径段。 */
export const MESSAGES_PATH = "/v1/messages";

/**
 * Trim surrounding whitespace and trailing slashes; drop the trailing /v1/messages when it's exactly at the end.
 *
 * Doesn't strip /vN or a lone /messages, and doesn't recognize or append /anthropic-style subpaths.
 *
 * 去首尾空白、去尾部斜杠；末尾恰好是 /v1/messages 时去掉这一段。
 *
 * 不剥 /vN、不剥单独的 /messages，也不识别或追加 /anthropic 类子路径。
 */
export function normalizeAnthropicBaseUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  // Strip trailing slashes again after removing the endpoint: the stored value of https://x//v1/messages must be https://x.
  // 剥掉端点后再去一次尾斜杠：https://x//v1/messages 的存储值须是 https://x。
  return trimmed.endsWith(MESSAGES_PATH)
    ? trimmed.slice(0, -MESSAGES_PATH.length).replace(/\/+$/, "")
    : trimmed;
}

/** Preview of the normalized call address; same value as the backend messages_url. / 归一后的调用地址预览；与后端 messages_url 同值。 */
export function anthropicMessagesUrl(raw: string): string {
  return normalizeAnthropicBaseUrl(raw) + MESSAGES_PATH;
}

/**
 * Whether it contains components the backend always rejects with 422: query, fragment, userinfo.
 *
 * Other invalid forms (missing scheme / host, etc.) are left to the backend, so the frontend doesn't replicate a set of parsing rules.
 *
 * 是否含后端一律 422 的成分：query、fragment、userinfo。
 *
 * 其余非法形态（缺 scheme / host 等）交给后端判定，避免前端复刻一套解析规则。
 */
export function hasUnsupportedUrlComponents(raw: string): boolean {
  const value = raw.trim();
  if (!value) return false;
  if (value.includes("?") || value.includes("#")) return true;
  try {
    const parsed = new URL(value);
    return parsed.username !== "" || parsed.password !== "";
  } catch {
    return false;
  }
}
