// Read model of the file view (backend lib/project/file_view.py and lib/project/reference_index.py).
// 文件视图的读模型（后端 lib/project/file_view.py 与 lib/project/reference_index.py）。

/** Role of the file in the project, determined by references; infrastructure files are not returned. / 文件在项目中的角色，由引用关系判定；基础设施文件不返回。 */
export type FileViewRole =
  | "structure"
  | "referenced"
  | "history"
  | "unreferenced"
  | "derived"
  | "agent_config"
  | "memory";

/** UI object the file belongs to, used by "Go to owning view". / 文件所属的界面对象，供「跳到所属界面」使用。 */
export interface FileViewOwner {
  kind: "asset" | "segment" | "reference_unit" | "grid" | "episode" | "timeline" | "source" | "memory" | "project";
  id?: string;
  episode?: number;
  asset_type?: "character" | "scene" | "prop" | "product";
}

export interface FileViewEntry {
  name: string;
  /** Project-relative POSIX path. / 项目内相对 POSIX 路径。 */
  path: string;
  type: "file" | "directory";
  /** Byte count; for directories, the sum of the visible files inside. / 字节数；目录是其中可见文件的总和。 */
  size: number;
  /** UTC ISO 8601 with time zone offset. / 带时区偏移的 UTC ISO 8601。 */
  modified_at: string;
  /** Only files carry a role. / 只有文件带角色。 */
  role?: FileViewRole;
  owner?: FileViewOwner | null;
  /** Present only on the current-version snapshot among history versions, and always true; the server refuses to delete it. / 历史版本里的当前版本快照才带，且恒为 true；服务端拒绝删除它。 */
  is_current?: true;
}

export interface FileViewListing {
  path: string;
  entries: FileViewEntry[];
}

// Project trash (backend lib/project/project_trash.py). / 项目回收站（后端 lib/project/project_trash.py）。

/** One deletion record. `object.kind` is the deleted object's type; unreferenced files deleted from the file view are `file`. / 一条删除记录。`object.kind` 是被删对象的类型，文件视图删除的未引用文件为 `file`。 */
export interface TrashRecord {
  id: string;
  object: { kind: string; path?: string; [key: string]: unknown };
  /** Project-relative path of the deleted file before deletion. / 被删文件删除前的项目内相对路径。 */
  files: string[];
  schema_version: number;
  /** UTC ISO 8601 with time zone offset. / 带时区偏移的 UTC ISO 8601。 */
  deleted_at: string;
  origin: "user" | "agent";
  size: number;
  has_snapshot: boolean;
  /** A record that cannot be restored to its original structure and was already restored in degraded form: only structure files remain, download only. / 无法按原结构恢复、已降级恢复过的记录：只剩结构文件，只能下载。 */
  download_only: boolean;
}

export interface TrashListing {
  records: TrashRecord[];
  total_size: number;
  retention_days: number;
}

// Deleted projects (backend lib/project/deleted_projects.py). / 已删除项目（后端 lib/project/deleted_projects.py）。

/** A deleted project. `id` is its tombstone name, used to locate it for restore and permanent delete; `name` is the project name before deletion. / 一个已删除项目。`id` 是它的墓碑名，恢复与彻底删除都按它定位；`name` 是删除前的项目名。 */
export interface DeletedProject {
  id: string;
  name: string;
  title: string;
  /** UTC ISO 8601 with time zone offset. / 带时区偏移的 UTC ISO 8601。 */
  deleted_at: string;
  /** Time of automatic purge on expiry, also UTC ISO 8601. / 到期自动清除的时间，同为 UTC ISO 8601。 */
  expires_at: string;
  /** Bytes occupied by the project directory. / 项目目录占用的字节数。 */
  size: number;
}

export interface DeletedProjectListing {
  projects: DeletedProject[];
  retention_days: number;
}

/** Where a file lands after restore: when `as_history` is true it became a history version of its resource. / 恢复后一个文件的落点：`as_history` 为真时它成了所在资源的一条历史版本。 */
export interface RestoredFile {
  original: string;
  path: string;
  as_history: boolean;
}

/** Result of one restore. `object` describes the object added back; it differs from the record when renamed. / 一次恢复的结果。`object` 是加回后的对象描述，换了名时与记录里的不同。 */
export interface RestoreResult {
  object: TrashRecord["object"];
  restored: RestoredFile[];
  /** When true the object could not be restored to its original structure; only its files were added back. / 为真时对象无法按原结构恢复，只加回了其中的文件。 */
  degraded: boolean;
  /** Structure files left in the record, download only. / 留在记录里、只能下载的结构文件。 */
  download_only: string[];
}
