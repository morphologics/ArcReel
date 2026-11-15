/**
 * Data of the "Episodes" view: backend `GET /projects/{name}/episodes-view`, one-to-one with the dataclasses in `lib/episode/episode_layout.py`.
 * A segment's `start` / `end` are Unicode code point offsets into the normalized source, not JS string indices.
 *
 * 「分集」视图的数据：后端 `GET /projects/{name}/episodes-view`，与 `lib/episode/episode_layout.py` 的
 * dataclass 一一对应。段的 `start` / `end` 是规范化源文里的 Unicode 码位偏移，不是 JS 字符串下标。
 */

export type EpisodeSourceOrigin = "whole_source" | "own" | "none";

/** Source file type: only drama projects have it. / 源文件类型：只有剧情演绎项目有。 */
export type SourceKind = "novel" | "screenplay";

export interface EpisodesViewSegment {
  kind: "episode" | "unsplit";
  start: number;
  end: number;
  text: string;
  /** Episode ID when kind is episode. / kind 为 episode 时是集 ID。 */
  episode: number | null;
  /** Unsplit segments sort before the last split-out episode by source position (between split-out episodes or before the first one). / 未切分段排在按源文位置最后一个切出集之前（夹在切出集之间或在第一个切出集之前）。 */
  gap: boolean;
  units: number;
  /** The episode segment continues the same episode from the previous file (this episode spans files, starting in an earlier file). / 集段接着上一个文件里的同一集（这一集跨文件，起点在前面的文件里）。 */
  continued: boolean;
  /** The episode segment continues in the next file (this episode spans files, ending in a later file). / 集段在下一个文件里接着（这一集跨文件，终点在后面的文件里）。 */
  continues: boolean;
}

export interface EpisodesViewFile {
  source_file: string;
  name: string;
  /** Original file name at upload; null when no original backup was kept. / 上传时的原始文件名；没有留原件备份时为 null。 */
  original_filename: string | null;
  /** File cannot be read (missing, symlink, non-UTF-8). / 文件读不到（不存在、符号链接、非 UTF-8）。 */
  missing: boolean;
  /** Code point count of the normalized full text, the upper bound of in-file offsets; 0 when unreadable. / 规范化全文的码位数，是文件内偏移的上界；读不到时为 0。 */
  length: number;
  units: number;
  cut_units: number;
  segments: EpisodesViewSegment[];
  /** Null for non-drama projects. / 非剧情演绎项目为 null。 */
  source_kind: SourceKind | null;
  /** The file was changed outside ArcReel and the episode ledger is not updated yet: replace, edit, reorder, change type and split are paused; delete still works. / 文件在 ArcReel 之外被改动过、还没有更新分集账本：替换、编辑、调序、改类型与切分都暂停，删除仍可用。 */
  changed_outside: boolean;
}

/**
 * A file changed outside ArcReel: the list of affected episodes computed by snapshot alignment. Passing `revision` as is to `acceptExternalSourceChange`
 * confirms this list. When the list cannot be computed `impact` and `revision` are null and `problem` is the server-written reason.
 *
 * 一个在 ArcReel 之外被改动过的文件：按快照对齐算出的受影响集清单。`revision` 原样带给 `acceptExternalSourceChange`
 * 即确认这份清单。算不出清单时 `impact` 与 `revision` 为 null，`problem` 是服务端成文的原因。
 */
export interface ExternalSourceChange {
  source_file: string;
  impact: (SourceFileImpact & { text: string }) | null;
  revision: string | null;
  problem: string | null;
}

export interface EpisodesViewEpisode {
  episode: number;
  origin: EpisodeSourceOrigin;
  /** The source text segment appears in the whole source. / 原文段出现在整本源文里。 */
  placed: boolean;
  /** Whole-source file where the source text range starts (placed only). / 原文范围起点所在的整本源文文件（仅 placed）。 */
  source_file: string | null;
  /** Whole-source file where the source text range ends (placed only); same as source_file when not spanning files. / 原文范围终点所在的整本源文文件（仅 placed）；不跨文件时与 source_file 相同。 */
  end_file: string | null;
  /** Null when the source text cannot be read. / 读不到原文时为 null。 */
  units: number | null;
  spoken_seconds: number | null;
  first_sentence: string;
  last_sentence: string;
  /** Episodes with their own source text take their own record, split-out episodes take the file where their source range starts; null for episodes without source text and non-drama projects. / 自带原文的集取自身记录，切出集取原文范围起点所在文件；无原文的集与非剧情演绎项目为 null。 */
  source_kind: SourceKind | null;
}

export interface UnregisteredSourceFile {
  name: string;
  size: number;
  /** The file name can be added to the whole source directly (no underscore prefix, and not episode_N.txt). / 文件名能直接加入整本源文（非下划线前缀，也不是 episode_N.txt）。 */
  can_join_whole_source: boolean;
}

export interface EpisodesView {
  /** Unit of measuring size: by character (Chinese etc.) or by word (English, Vietnamese). / 体量的计量单位：按字（中文等）或按词（英文、越南文）。 */
  unit: "chars" | "words";
  units: number;
  cut_units: number;
  files: EpisodesViewFile[];
  episodes: EpisodesViewEpisode[];
  unregistered: UnregisteredSourceFile[];
  /** Replan candidate not yet adopted or discarded; null when none. / 尚未采纳或放弃的重新规划候选；没有时为 null。 */
  replan: ReplanSummary | null;
  /** Files changed outside ArcReel, in file order. / 在 ArcReel 之外被改动过的文件，按文件顺序。 */
  external_changes: ExternalSourceChange[];
}

/** A position in the whole source: the file and the code point offset within it. / 整本源文里的一个位置：文件与文件内的码位偏移。 */
export interface SourcePoint {
  source_file: string;
  offset: number;
}

/** One episode in the candidate. `same_as` is an existing episode with an identical source range; `overlaps` are replaced episodes whose source ranges overlap it. / 候选里的一集。`same_as` 是原文范围一模一样的现有集，`overlaps` 是原文范围与它重叠的被替换集。 */
export interface ReplanCandidateEpisode {
  title: string;
  hook: string;
  source_file: string;
  start: number;
  end: number;
  units: number;
  first_sentence: string;
  last_sentence: string;
  same_as: number | null;
  overlaps: number[];
}

/**
 * "New episode scheme": the candidate generated by replanning and the changes on adoption. The episode ledger stays unchanged until adoption, and episode IDs are those of existing episodes.
 * When `stale` is non-null the episodes or source changed after generation; the candidate can only be discarded and the change list is empty.
 *
 * 「新的分集方案」：重新规划生成的候选与采纳后的变化。分集账本在采纳前不变，集 ID 都是现有集的。
 * `stale` 非 null 时生成之后分集或源文有改动，候选只能放弃，变化清单为空。
 */
export interface ReplanSummary {
  id: string;
  /** Which episode replanning starts from. / 从哪一集开始重新规划。 */
  episode: number;
  instructions: string | null;
  /** Generated up to the end of the whole source. / 已生成到整本源文结尾。 */
  complete: boolean;
  /** Why generation stopped midway: AI could not find a split point, or an error; null when the creator stopped it or it is still generating. / 生成中途停止的原因：AI 找不到切分点、出错；创作者主动停止或还在生成时为 null。 */
  interrupted: "no_cut_point" | "failed" | null;
  stale: "ledger_changed" | "source_changed" | null;
  start: SourcePoint;
  /** Position generated up to. / 已生成到的位置。 */
  end: SourcePoint;
  /** Number of split-out episodes replaced; null when the candidate is stale or there are no episodes yet. / 被替换的切出集数；候选过时或还没有集时为 null。 */
  old_count: number | null;
  new_count: number;
  units: number;
  average_units: number | null;
  /** Already in production; kept as an episode without source text after adoption. / 已开始制作，采纳后保留为无原文的集。 */
  retired: number[];
  /** No artifacts; removed after adoption. / 没有产物，采纳后移除。 */
  removed: number[];
  /** Kept as an episode without source text, and no episode in the new scheme has an identical source range. / 保留为无原文、且新方案里找不到原文范围一模一样的集。 */
  needs_review: number[];
  /** Episodes whose source text lies beyond the new scheme's coverage and are likewise treated as replaced on adoption (when the scheme did not reach the end of the whole source). / 原文超出新方案覆盖范围、采纳时同样按被替换处理的集（方案没有生成到整本源文结尾时）。 */
  uncovered: number[];
  /** Other episodes whose episode order will change. / 播出位置会变的其他集。 */
  moved: { episode: number; from: number; to: number }[];
  episodes: ReplanCandidateEpisode[];
}

/** Scope of replanning: `replaced` are the split-out episodes to be replaced, `started` those among them already in production. / 重新规划的范围：`replaced` 是会被替换的切出集，`started` 是其中已开始制作的集。 */
export interface ReplanPreview {
  status: "preview";
  episode: number;
  source_file: string;
  offset: number;
  /** The source changed after episode planning or there are old split-out episodes, so replanning can only start from the first split-out episode. / 分集规划之后源文有改动或有旧的切出集，只能从第一个切出集起重新规划。 */
  from_beginning: boolean;
  source_replaced: boolean;
  replaced: number[];
  started: number[];
}

/** Consequences of adoption. `text` and `delete_text` appear only while awaiting confirmation; they are server-written confirmation texts. / 采纳的后果。`text` 与 `delete_text` 只在等待确认时出现，是服务端成文的确认文本。 */
export interface ReplanAdoptionImpact {
  candidate: string;
  episode: number;
  old_count: number;
  new_count: number;
  retired: number[];
  removed: number[];
  needs_review: number[];
  uncovered: number[];
  moved: { episode: number; from: number; to: number }[];
  revision: string;
}

export type ReplanAdoptionResponse =
  | { status: "adopted"; episodes: number[]; deleted: number[] }
  | { status: "confirmation_required"; impact: ReplanAdoptionImpact & { text: string; delete_text: string } };

export type AdoptSourceFileTarget =
  | { target: "whole_source" }
  | { target: "episode"; episode?: number | null };

/**
 * Manual split actions: split, divide, move boundary, merge with next episode, clear later splits. Offsets are code point offsets within `source_file`;
 * when `source_file` is omitted, divide uses the file where the episode starts and move boundary the file where it ends.
 *
 * 手工切分的动作：切分、拆分、移动分界、与下一集合并、清除之后的切分。偏移是 `source_file` 内的码位偏移；
 * 缺省 `source_file` 时，拆分取这一集起点所在的文件，移动分界取这一集终点所在的文件。
 */
export type ManualSplitAction =
  | { action: "cut"; source_file: string; end: number; title?: string }
  | { action: "split"; episode: number; at: number; source_file?: string }
  | { action: "move_boundary"; episode: number; at: number; source_file?: string }
  | { action: "merge_next"; episode: number }
  | { action: "clear_after"; episode: number };

/** Episodes (episode IDs) affected by one manual split. / 一次手工切分波及的集（集 ID）。 */
export interface ManualSplitImpact {
  /** Source range changed and has artifacts: marked stale. / 原文范围变了且有产物，标 stale。 */
  restaled: number[];
  /** Has artifacts: becomes an episode without source text and moves to the end of the episode order. / 有产物，转为无原文的集并移到播出顺序末尾。 */
  retired: number[];
  /** No artifacts: removed directly. / 没有产物，直接移除。 */
  removed: number[];
  /** Size (in reading units) of the unsplit source between the two merged episodes when merging with the next; 0 for other actions. / 与下一集合并时并入的两集之间未切分原文的体量（阅读单位）；其余动作为 0。 */
  merged_units: number;
}

export type ManualSplitResponse =
  | { status: "applied"; episode: number | null; impact: ManualSplitImpact }
  /** `impact.text` is the server-written confirmation list. / `impact.text` 是服务端成文的确认清单。 */
  | { status: "confirmation_required"; impact: ManualSplitImpact & { text: string } };

/** Episodes (episode IDs) affected by changes to a whole-source file (insert, replace, edit, delete, reorder). / 整本源文文件的改动（插入、替换、编辑、删除、调序）波及的集（集 ID）。 */
export interface SourceFileImpact {
  /** Source text unchanged, only shifted position. / 原文没变，只平移位置。 */
  shifted: number[];
  /** Source text changed and has artifacts: marked stale. / 原文有变化、已有产物，标 stale。 */
  changed_with_products: number[];
  /** Source text changed, no artifacts yet. / 原文有变化、还没有产物。 */
  changed_without_products: number[];
  /** Source text entirely deleted and has artifacts: becomes an episode without source text and moves to the end of the episode order. / 原文全部删掉、已有产物，转为无原文的集并移到播出顺序末尾。 */
  retired: number[];
  /** Source text entirely deleted, no artifacts: removed directly. / 原文全部删掉、没有产物，直接移除。 */
  removed: number[];
  /** Source file type changed; the script plan will go stale. / 源文件类型改变，脚本规划会过期。 */
  kind_stale: number[];
}

/**
 * Result of a whole-source file change. With `confirmation_required` nothing was written: `impact.text` is the server-written list of affected episodes,
 * and resubmitting with `revision` executes it; if the list changes meanwhile the server asks for confirmation again.
 *
 * 整本源文文件改动的结果。`confirmation_required` 时没有写入：`impact.text` 是服务端成文的受影响集清单，
 * 带上 `revision` 重新提交即执行；清单在此期间变了时服务端再次要求确认。
 */
export type SourceFileChangeResponse =
  | { status: "applied"; impact: SourceFileImpact }
  | { status: "confirmation_required"; impact: SourceFileImpact & { text: string }; revision: string };

/** Submit result of AI episode planning: the generation batch of the first window. Each later window is queued by the previous window while it runs. / AI 规划分集的提交结果：首窗的生成批次。之后每一窗由执行中的上一窗排进队列。 */
export interface EpisodePlanningResponse {
  batch: { batch_id: string; members: { unit_id: string; task_id: string | null; deduped?: boolean }[] };
}

/** Stop episode planning: `cancelled` are the queued windows cancelled, `running` the window executing that will complete as usual. / 停止分集规划：`cancelled` 是取消掉的排队窗口，`running` 是正在执行、会照常完成的窗口。 */
export interface StopEpisodePlanningResponse {
  cancelled: string[];
  running: string[];
}

/** Result of changing a whole-source file's type: with `needs_confirmation` nothing was written; `affected_episodes` are episodes whose script plans would be judged stale. / 改整本源文文件类型的结果：`needs_confirmation` 时没有写入，`affected_episodes` 是会让脚本规划判 stale 的集。 */
export interface SourceKindChangeResult {
  success: boolean;
  applied: boolean;
  needs_confirmation: boolean;
  affected_episodes: number[];
}

/** Result of the episode page writing the episode source; with `needs_confirmation` neither source text nor type was written. / 集页写本集原文的结果；`needs_confirmation` 时原文与类型都没有写入。 */
export interface EpisodeSourceWriteResult extends SourceKindChangeResult {
  episode: number;
  source_origin: EpisodeSourceOrigin;
}

/** "Plan this unsplit source text": the unsplit source segment ending at this point. `end` is the code point offset within the file. / 「规划这段未切分的原文」：以这一点为终点的那段未切分原文。`end` 是文件内的码位偏移。 */
export interface PlanningGap {
  source_file: string;
  end: number;
}

export interface CreateEpisodeBody {
  /** Insert after which episode (episode ID); placed at the end of the episode order when omitted. / 插在哪一集之后（集 ID）；缺省放在播出顺序末尾。 */
  after?: number | null;
  title?: string;
  hook?: string;
  /** Episode source; blank counts as no source text. / 集原文；空白视同没有原文。 */
  source_text?: string | null;
  /** Source file type of the episode source; only effective for drama projects. / 集原文的源文件类型，只对剧情演绎项目生效。 */
  source_kind?: SourceKind | null;
}

/** Content lost by deleting an episode. `text` appears only while awaiting confirmation; it is the server-written confirmation text. / 删除一集会丢失的内容。`text` 只在等待确认时出现，是服务端成文的确认文本。 */
export interface EpisodeDeletionImpact {
  episode: number;
  /** Deleting loses no content that cannot be rebuilt within the project. / 删除不丢失任何无法在项目内重建的内容。 */
  recoverable: boolean;
  revision: string;
  text?: string;
}

export type EpisodeDeletionResponse =
  | { status: "deleted"; impact: EpisodeDeletionImpact }
  | { status: "confirmation_required"; impact: EpisodeDeletionImpact & { text: string } };
