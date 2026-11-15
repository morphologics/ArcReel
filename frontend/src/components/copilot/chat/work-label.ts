import type { TFunction } from "i18next";
import {
  Bot,
  Eye,
  FilePen,
  FilePlus,
  FileText,
  Film,
  FolderSearch,
  Globe,
  ListChecks,
  ListTree,
  MessageCircleQuestion,
  Search,
  Sparkles,
  SquareTerminal,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";
import type { ContentBlock, EpisodeMeta, TodoItem } from "@/types";
import { episodeDisplayName, episodeItemLabel, type ProjectScripts } from "@/utils/episode-display";
import { ARCREEL_TOOL_SUMMARIES } from "./arcreel-tool-summaries";

// ---------------------------------------------------------------------------
// Display layer for work rows: which blocks count as work, work status, and tool calls' display names, icons and one-line summaries.
// Recognized arguments are turned into wording creators can read; only unrecognized ones fall back to truncated raw arguments.
// ---------------------------------------------------------------------------
// 工序行的显示层：哪些块算工序、工序的状态，以及工具调用的显示名、图标与一句摘要。
// 认得的参数翻成创作者能读的话，不认得的才退回截断的参数原文。
// ---------------------------------------------------------------------------

export type ToolInput = Record<string, unknown>;

/** Success gets no mark; running versus stopped is distinguished only by whether the session is still running or has ended. / 成功不加标记；运行中与已停止只在会话仍在跑或已终结时区分。 */
export type WorkStatus = "running" | "ok" | "error" | "stopped";

/** Work segment: consecutive work in a reply round is packed into a compact column; gaps only between body text. / 工序块：一轮回复里连续的工序排成紧凑的一列，正文之间才留段距。 */
export function isWorkBlock(block: ContentBlock): boolean {
  return (
    block.type === "tool_use" ||
    block.type === "tool_result" ||
    block.type === "thinking" ||
    block.type === "skill_invocation" ||
    block.type === "task_progress"
  );
}

export interface BlockSegment {
  work: boolean;
  /** Blocks in the segment and their positions in the original content. / 段内的块与它在原内容里的位置。 */
  items: Array<{ block: ContentBlock; index: number }>;
}

/** Segment by work: consecutive work merges into one segment, other blocks each form their own. / 按工序分段：连续的工序合成一段，其余块各自成段。 */
export function segmentWorkBlocks(blocks: ContentBlock[]): BlockSegment[] {
  const segments: BlockSegment[] = [];
  blocks.forEach((block, index) => {
    const work = isWorkBlock(block);
    const last = segments.at(-1);
    if (work && last?.work) last.items.push({ block, index });
    else segments.push({ work, items: [{ block, index }] });
  });
  return segments;
}

/** Status of a tool_use block: done once it has a result; stopped if the session ended with still no result, so the running status doesn't hang. / tool_use 块的状态：有结果即结束，会话终结仍无结果时是已停止，避免运行状态悬挂。 */
export function toolStatus(block: ContentBlock, sessionDone: boolean): WorkStatus {
  if (block.is_error) return "error";
  if (block.result !== undefined) return "ok";
  return sessionDone ? "stopped" : "running";
}

export type EpisodeRefs = readonly Pick<EpisodeMeta, "episode" | "title" | "script_file">[];

const SUMMARY_JOINER = " · ";
const MAX_LISTED = 3;
const MAX_FALLBACK_CHARS = 120;

function str(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(str).filter(Boolean) : [];
}

function fileName(path: string): string {
  return path.split("/").pop() ?? path;
}

/** Formatting helpers for summary fragments, generated from the current language and the project's episode list. / 摘要片段的格式化工具，按当前语言与项目的集清单生成。 */
export interface SummaryFormat {
  text: (value: unknown) => string;
  /** Keep only the last two path segments. / 路径只留最后两段。 */
  path: (value: unknown) => string;
  /** Episode ID -> episode name (title, or the episode order position when untitled). / 集 ID → 集名（标题，没有标题时是播出位置）。 */
  episode: (value: unknown) => string;
  /** Script file name -> its episode's name; shows the file name when not in the episode list. / 剧本文件名 → 所属集的集名；不在集清单里时显示文件名。 */
  script: (value: unknown) => string;
  /** Entry ID -> "episode name · S01". / 条目 ID → 「集名 · S01」。 */
  item: (value: unknown) => string;
  /** A set of entry IDs: within one episode written as "episode name · S01, S02"; more than three lists only the first three. / 一组条目 ID：同一集时写「集名 · S01、S02」，超过三个只列前三个。 */
  items: (value: unknown) => string;
  /** A set of names; more than three lists only the first three. / 一组名称，超过三个只列前三个。 */
  list: (value: unknown) => string;
  assetType: (value: unknown) => string;
  quote: (value: unknown) => string;
  arrow: (from: unknown, to: unknown) => string;
  revision: (value: unknown) => string;
  version: (value: unknown) => string;
  count: (key: "tool_summary_grids" | "tool_summary_operations", value: unknown) => string;
  projectFields: (input: ToolInput) => string;
  record: (value: unknown) => Record<string, unknown>;
  /** A given field of each object in an array. / 数组里每个对象的某个字段。 */
  field: (value: unknown, key: string) => unknown[];
  join: (...parts: string[]) => string;
}

export function createSummaryFormat(t: TFunction, episodes: EpisodeRefs, scripts: ProjectScripts = {}): SummaryFormat {
  const list = (value: unknown): string => {
    const names = strings(value);
    if (names.length === 0) return "";
    const listed = names.slice(0, MAX_LISTED).join(t("dashboard:tool_summary_separator"));
    if (names.length <= MAX_LISTED) return listed;
    return t("dashboard:tool_summary_more", { list: listed, total: names.length, rest: names.length - MAX_LISTED });
  };
  const episode = (value: unknown): string =>
    typeof value === "number" && Number.isInteger(value) ? episodeDisplayName(episodes, value, t) : "";
  const record = (value: unknown): Record<string, unknown> =>
    value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

  return {
    text: str,
    path: (value) => str(value).split("/").slice(-2).join("/"),
    episode,
    script: (value) => {
      const name = fileName(str(value));
      if (!name) return "";
      const hit = episodes.find((entry) => fileName(entry.script_file) === name);
      return hit ? episode(hit.episode) : name;
    },
    item: (value) => {
      const id = str(value);
      return id ? episodeItemLabel(id, scripts, episodes, t) : "";
    },
    items: (value) => {
      const ids = strings(value);
      if (ids.length === 0) return "";
      const labels = ids.map((id) => episodeItemLabel(id, scripts, episodes, t));
      // Entries in the same episode write the episode name once: "Episode 1 · 1, 2" / 同一集的条目只写一次集名：「第 1 集 · 1、2」
      const prefixes = new Set(labels.map((label) => label.split(SUMMARY_JOINER)[0]));
      if (prefixes.size === 1 && labels.every((label) => label.includes(SUMMARY_JOINER))) {
        const [prefix] = prefixes;
        return `${prefix}${SUMMARY_JOINER}${list(labels.map((label) => label.slice(prefix.length + SUMMARY_JOINER.length)))}`;
      }
      return list(labels);
    },
    list,
    assetType: (value) => {
      const type = str(value);
      return type ? t(`dashboard:task_type_${type}`, { defaultValue: type }) : "";
    },
    quote: (value) => {
      const text = str(value);
      return text ? t("dashboard:tool_summary_quoted", { text }) : "";
    },
    arrow: (from, to) => (str(from) && str(to) ? `${str(from)} → ${str(to)}` : ""),
    revision: (value) => (typeof value === "number" ? t("dashboard:tool_summary_revision", { revision: value }) : ""),
    version: (value) => (typeof value === "number" ? t("dashboard:tool_summary_version", { version: value }) : ""),
    count: (key, value) => (Array.isArray(value) && value.length > 0 ? t(`dashboard:${key}`, { count: value.length }) : ""),
    projectFields: (input) => {
      if (input.overview != null) return t("dashboard:tool_summary_project_overview");
      if (input.settings != null) return t("dashboard:tool_summary_project_settings");
      return "";
    },
    record,
    field: (value, key) => (Array.isArray(value) ? value.map((entry) => record(entry)[key]) : []),
    join: (...parts) => parts.filter(Boolean).join(SUMMARY_JOINER),
  };
}

const ARCREEL_TOOL_PREFIX = /^mcp__arcreel__([a-z0-9_]+)$/;

const BUILTIN_TOOLS: Record<string, { key: string; icon: LucideIcon }> = {
  Read: { key: "tool_builtin_read", icon: FileText },
  Write: { key: "tool_builtin_write", icon: FilePlus },
  Edit: { key: "tool_builtin_edit", icon: FilePen },
  Bash: { key: "tool_builtin_bash", icon: SquareTerminal },
  BashOutput: { key: "tool_builtin_bash_output", icon: SquareTerminal },
  KillBash: { key: "tool_builtin_kill_bash", icon: SquareTerminal },
  Grep: { key: "tool_builtin_grep", icon: Search },
  Glob: { key: "tool_builtin_glob", icon: FolderSearch },
  WebSearch: { key: "tool_builtin_web_search", icon: Globe },
  WebFetch: { key: "tool_builtin_web_fetch", icon: Globe },
  TodoWrite: { key: "tool_builtin_todo_write", icon: ListChecks },
  AskUserQuestion: { key: "tool_builtin_ask_user_question", icon: MessageCircleQuestion },
};

function arcreelToolIcon(id: string): LucideIcon {
  if (id.startsWith("generate") || id.startsWith("edit_images")) return Sparkles;
  if (id.startsWith("inspect") || id.startsWith("get") || id.startsWith("list") || id.startsWith("read")) return Eye;
  if (id.includes("timeline") || id.includes("revision") || id.includes("final_cut") || id.includes("jianying")) return Film;
  if (id.includes("draft") || id.startsWith("patch") || id.startsWith("edit")) return FilePen;
  return Wrench;
}

function builtinSummary(name: string, input: ToolInput, t: TFunction, f: SummaryFormat): string | null {
  switch (name) {
    case "Read":
    case "Write":
    case "Edit":
      return f.path(input.file_path);
    case "Bash":
      return f.text(input.description) || f.text(input.command);
    case "Grep":
      return f.quote(input.pattern);
    case "Glob":
      return f.text(input.pattern);
    case "WebSearch":
      return f.text(input.query);
    case "WebFetch":
      return f.text(input.url);
    case "TodoWrite": {
      const todos = Array.isArray(input.todos) ? (input.todos as TodoItem[]) : [];
      const completed = todos.filter((todo) => todo.status === "completed").length;
      return todos.length > 0 ? t("dashboard:tool_summary_todo", { completed, total: todos.length }) : "";
    }
    case "AskUserQuestion":
      return f.join(...f.field(input.questions, "question").map(str));
    case "BashOutput":
    case "KillBash":
      return "";
    default:
      return null;
  }
}

function fallbackSummary(input: ToolInput): string {
  if (Object.keys(input).length === 0) return "";
  const json = JSON.stringify(input);
  return json.length > MAX_FALLBACK_CHARS ? `${json.slice(0, MAX_FALLBACK_CHARS)}…` : json;
}

export interface WorkLabel {
  name: string;
  summary: string;
  icon: LucideIcon;
}

/** Display name, icon and summary of a tool call. ArcReel tools and built-in tools have localized names; other tools keep their original names. / 工具调用的显示名、图标与摘要。ArcReel 工具与内置工具都有本地化名称，其余工具保留原名。 */
export function toolLabel(
  name: string,
  input: ToolInput | undefined,
  t: TFunction,
  episodes: EpisodeRefs,
  scripts: ProjectScripts = {},
): WorkLabel {
  const args = input ?? {};
  const f = createSummaryFormat(t, episodes, scripts);

  const arcreel = ARCREEL_TOOL_PREFIX.exec(name);
  if (arcreel) {
    const id = arcreel[1];
    const summarize = ARCREEL_TOOL_SUMMARIES[id];
    return {
      name: t(`dashboard:tool_name_${id}`, { defaultValue: id }),
      summary: summarize ? summarize(args, f) : fallbackSummary(args),
      icon: arcreelToolIcon(id),
    };
  }

  const builtin = BUILTIN_TOOLS[name];
  return {
    name: builtin ? t(`dashboard:${builtin.key}`) : name,
    summary: builtinSummary(name, args, t, f) ?? fallbackSummary(args),
    icon: builtin?.icon ?? Wrench,
  };
}

export const SUBAGENT_ICON = Bot;
export const SKILL_ICON = Zap;
export const BACKGROUND_TASK_ICON = ListTree;
export const TOOL_RESULT_ICON = Wrench;
