import type { AgentMemoryOverview, AgentMemoryScope, AgentMemoryType } from "@/types/agent-memory";
import { parseIsoTimestamp } from "@/utils/date-format";

/** The index file is not in the list response's `files`; it is counted separately under `index`, and the frontend pins it to the top as a virtual entry. / 索引文件不出现在列表响应的 `files` 里，由 `index` 单列统计，前端据此把它置顶为一条虚拟条目。 */
export const INDEX_FILENAME = "MEMORY.md";
export const INDEX_LINE_LIMIT = 200;

/** The selected file is kept in the URL's `file=` param; `file=new` is the create form (valid file names all end in .md, so no clash). / 选中文件记在地址的 `file=` 参数里；`file=new` 是新建表单（合法文件名都以 .md 结尾，不会撞名）。 */
export const MEMORY_FILE_PARAM = "file";
export const NEW_MEMORY_FILE = "new";

/** Same yardstick as `lib/agent/agent_memory_store.py`; local pre-validation only makes duplicates and typos visible in place, the server still decides. / 与 `lib/agent/agent_memory_store.py` 同一把尺子；本地预校验只为让重名与拼错就地可见，判据仍在服务端。 */
const FILENAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._() -]*\.md$/;
const FILENAME_MAX_LENGTH = 100;

export type MemoryEntry =
  | { kind: "index"; name: string; lineCount: number; overLimit: boolean }
  | {
      kind: "topic";
      name: string;
      description: string | null;
      type: AgentMemoryType | null;
      modifiedAt: string;
    };

/** List order: index pinned on top, memory files by modification time, newest first. / 列表顺序：索引置顶，记忆文件按修改时间倒序。 */
export function memoryEntries(overview: AgentMemoryOverview | null): MemoryEntry[] {
  if (!overview) return [];
  const topics = [...overview.files]
    .sort((a, b) => parseIsoTimestamp(b.modified_at).getTime() - parseIsoTimestamp(a.modified_at).getTime())
    .map<MemoryEntry>((file) => ({
      kind: "topic",
      name: file.name,
      description: file.frontmatter?.description ?? null,
      type: file.frontmatter?.type ?? null,
      modifiedAt: file.modified_at,
    }));
  const index: MemoryEntry[] = overview.index.exists
    ? [
        {
          kind: "index",
          name: INDEX_FILENAME,
          lineCount: overview.index.line_count,
          overLimit: overview.index.over_limit,
        },
      ]
    : [];
  return [...index, ...topics];
}

export type MemorySelection = { kind: "file"; entry: MemoryEntry } | { kind: "new"; empty: boolean };

/**
 * Resolve the URL param to a selection: `file=new` is the create form; a file that does not exist (deleted, cleared or misspelled) falls back to the first item;
 * with no files at all, the create form is shown. Delete and clear do not change the URL; the selection settles during render.
 *
 * 地址参数收敛成选中项：`file=new` 是新建表单；指向不存在的文件（已被删除、清空或拼错）时落到第一项；
 * 一个文件都没有时显示新建表单。删除与清空之后不改地址，选中项在渲染期收敛。
 */
export function resolveSelection(param: string | null, entries: MemoryEntry[]): MemorySelection {
  if (param === NEW_MEMORY_FILE) return { kind: "new", empty: entries.length === 0 };
  const entry = entries.find((item) => item.name === param) ?? entries[0];
  return entry ? { kind: "file", entry } : { kind: "new", empty: true };
}

export type NewNameProblem = "invalid" | "duplicate";

/** Duplicate names are case-insensitive: on case-insensitive file systems (the default), creating a name that differs only in case would overwrite the existing file. / 重名不区分大小写：默认不区分大小写的文件系统上，只差大小写的新建会覆盖已有文件。 */
export function checkNewName(name: string, entries: MemoryEntry[]): NewNameProblem | null {
  if (name.length > FILENAME_MAX_LENGTH || !FILENAME_PATTERN.test(name)) return "invalid";
  const folded = name.toLowerCase();
  if (entries.some((entry) => entry.name.toLowerCase() === folded)) return "duplicate";
  return null;
}

/** Body of a new file: a template with frontmatter; `type` is user or project depending on the memory level. / 新建文件的正文：带 frontmatter 的模板，`type` 按记忆层级取 user 或 project。 */
export function newFileTemplate(
  filename: string,
  level: AgentMemoryScope["level"],
  description: string,
  body: string,
): string {
  const name = filename.replace(/\.md$/, "");
  return `---\nname: ${name}\ndescription: ${description}\ntype: ${level}\n---\n\n${body}\n`;
}

/** Full path of the file on the server, for display only. / 文件在服务端的完整路径，只用于展示。 */
export function memoryFilePath(dir: string, filename: string): string {
  return dir ? `${dir.replace(/\/+$/, "")}/${filename}` : filename;
}
