/** API Key metadata (for list display, without the full key). / API Key 元数据（列表展示用，不含完整 key）。 */
export interface ApiKeyInfo {
  id: number;
  name: string;
  key_prefix: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
}

/** Response of creating an API Key (includes the full key, only at creation). / 创建 API Key 的响应（含完整 key，仅在创建时出现）。 */
export interface CreateApiKeyResponse {
  id: number;
  name: string;
  key: string;
  key_prefix: string;
  created_at: string;
  expires_at: string | null;
}
