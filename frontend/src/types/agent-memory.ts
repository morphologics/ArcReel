// Read model of the Agent memory directory (the overview response of backend lib/agent/agent_memory_store.py).
// User memory and project memory have exactly the same shape and differ only in the request path, so they share one set of types.
// Agent 记忆目录的读模型（后端 lib/agent/agent_memory_store.py 的 overview 响应）。
// 用户记忆与项目记忆的形状完全一致，差别只在请求路径，故共用一套类型。

/** Valid memory types in frontmatter; any other value makes the backend treat the file as having no frontmatter. / frontmatter 里合法的记忆类型；取值之外的一律由后端判为无 frontmatter。 */
export type AgentMemoryType = "user" | "feedback" | "project" | "reference";

/** Successfully parsed frontmatter; null when `name` / `description` is missing or all whitespace. / 解析成功的 frontmatter；`name` / `description` 缺失或全空白时为 null。 */
export interface AgentMemoryFrontmatter {
  name: string | null;
  description: string | null;
  type: AgentMemoryType;
}

export interface AgentMemoryFile {
  name: string;
  size: number;
  /** UTC ISO 8601 with time zone offset. / 带时区偏移的 UTC ISO 8601。 */
  modified_at: string;
  /** Null as a whole when frontmatter is missing, has a syntax error, or has an invalid type. / frontmatter 缺失、语法错误或 type 非法时整体为 null。 */
  frontmatter: AgentMemoryFrontmatter | null;
}

/** Stats of the index file MEMORY.md; it does not appear in `files`. / 索引文件 MEMORY.md 的统计；它不出现在 `files` 里。 */
export interface AgentMemoryIndexStats {
  exists: boolean;
  line_count: number;
  byte_size: number;
  over_limit: boolean;
}

export interface AgentMemoryOverview {
  /** Absolute server path of the memory directory, shown to creators as is, not run through i18n. / 记忆目录的服务端绝对路径，直接展示给创作者，不进 i18n。 */
  path: string;
  index: AgentMemoryIndexStats;
  files: AgentMemoryFile[];
}

/** Ownership level of the memory: the two levels only change the request path and the copy. / 记忆的归属层级：两级只换请求路径与文案。 */
export type AgentMemoryScope = { level: "user" } | { level: "project"; projectName: string };
