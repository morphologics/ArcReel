"""Data root layout: the single source of every entry location under the data root.

The data root (``app_data_dir()``) is the directory where a deployment stores runtime data; projects are just one
kind of data in it. No other code joins entries under the data root itself or infers the data root from a project
dir; locations always come from :class:`DataRootLayout` (ADR 0088).

Projects live under ``projects/`` in the data root; other runtime data sits beside it with unprefixed names.

"What is a project" is answered only by :func:`is_project_dir`: a directory whose name matches the project name rule
and that has a ``project.json``. Any other entry in the data root is not a project.

Zero I/O except :func:`is_project_dir` and :func:`list_project_dirs`: only derives paths, never checks existence or
creates dirs.

数据根布局：数据根下每一类条目位置的唯一来源。

数据根（``app_data_dir()``）是一次部署存放运行数据的目录，项目只是其中一类数据。
代码中其它地方不自行拼接数据根下的条目，也不从项目目录反推数据根，一律经
:class:`DataRootLayout` 取位置（ADR 0088）。

项目收在数据根下的 ``projects/`` 里，其余运行数据以无前缀的名字与之并列。

「什么是项目」只由 :func:`is_project_dir` 回答：名字符合项目名规则、并且带 ``project.json``
的目录。数据根里的其它条目一概不是项目。

除 :func:`is_project_dir` 与 :func:`list_project_dirs` 外零 I/O：只派生路径，不检查存在、不建目录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from lib.agent.agent_memory_paths import MEMORY_DIRNAME, is_valid_memory_user_id
from lib.infra.app_data_dir import app_data_dir

PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
PROJECT_FILENAME = "project.json"


def is_project_dir(path: Path) -> bool:
    """Whether ``path`` is a project: a directory whose name matches :data:`PROJECT_NAME_PATTERN` and that has a ``project.json``.

    ``path`` 是不是一个项目：名字符合 :data:`PROJECT_NAME_PATTERN`、并且带 ``project.json`` 的目录。"""
    return bool(PROJECT_NAME_PATTERN.fullmatch(path.name)) and path.is_dir() and (path / PROJECT_FILENAME).is_file()


def list_project_dirs(projects_dir: Path) -> list[Path]:
    """All projects in the projects dir, sorted by name; empty when the projects dir does not exist. / 项目目录下的全部项目，按名字排序；项目目录不存在时为空。"""
    try:
        children = sorted(projects_dir.iterdir())
    except FileNotFoundError:
        return []
    return [child for child in children if is_project_dir(child)]


@dataclass(frozen=True)
class DataRootLayout:
    """Layout of one data root: gives the location of each kind of entry under it. / 一个数据根的布局：给出其下各类条目的位置。"""

    root: Path

    @classmethod
    def current(cls) -> DataRootLayout:
        """Layout of the data root resolved from the current config. / 按当前配置解析的数据根的布局。"""
        return cls(app_data_dir())

    @classmethod
    def for_project_dir(cls, project_dir: Path) -> DataRootLayout:
        """Get the layout of the data root containing a project dir (Agent skill scripts run with the project dir as cwd).

        由一个项目目录求其所在数据根的布局（Agent skill 脚本以项目目录为 cwd 运行）。"""
        return cls(Path(project_dir).parent.parent)

    @property
    def projects_dir(self) -> Path:
        """Projects dir: each project is stored at ``<项目目录>/<项目名>/`` (projects dir / project name). / 项目目录：各项目以 ``<项目目录>/<项目名>/`` 存放。"""
        return self.root / "projects"

    @property
    def deleted_projects_dir(self) -> Path:
        """Deleted projects: wholly deleted projects are stored at ``<本目录>/<墓碑名>/`` (this dir / tombstone name), restorable as a whole within the retention period.

        已删除项目：被整体删除的项目以 ``<本目录>/<墓碑名>/`` 存放，保留期内可整体恢复。"""
        return self.root / "deleted_projects"

    @property
    def global_assets_dir(self) -> Path:
        """Global asset library; paths in asset records are relative to the data root, prefixed with this dir's name.

        全局资产库；资产记录里的路径以数据根为基准，前缀即本目录名。"""
        return self.root / "global_assets"

    @property
    def users_dir(self) -> Path:
        """Root of per-user data: ``<users_dir>/<user_id>/`` holds that user's memory etc.

        各用户数据的根：``<users_dir>/<user_id>/`` 下放该用户的记忆等。"""
        return self.root / "users"

    def user_memory_dir(self, user_id: str) -> Path:
        """User memory dir.

        ``user_id`` directly forms the dir name and must be a single path segment (see ``is_valid_memory_user_id``),
        otherwise the derived dir would escape the data root; raises ``ValueError``.

        用户记忆目录。

        ``user_id`` 直接构成目录名，须是单个路径段（见 ``is_valid_memory_user_id``），
        否则派生出的目录会逃出数据根，抛 ``ValueError``。
        """
        if not is_valid_memory_user_id(user_id):
            raise ValueError(
                "user_id must be a single path segment: not empty and without path separators, drive colons or "
                f"NUL: {user_id!r}",
            )
        return self.users_dir / user_id / MEMORY_DIRNAME

    @property
    def sqlite_db_path(self) -> Path:
        """Default SQLite main file when ``DATABASE_URL`` is unset; ``-wal`` / ``-shm`` share its dir and prefix.

        未设置 ``DATABASE_URL`` 时的默认 SQLite 主文件；``-wal`` / ``-shm`` 与之同目录同前缀。"""
        return self.root / "arcreel.db"

    @property
    def system_config_json_path(self) -> Path:
        """Legacy system config file, only a one-time import source. / 旧版系统配置文件，只作一次性导入源。"""
        return self.root / ".system_config.json"

    @property
    def log_dir(self) -> Path:
        """File log dir. / 文件日志目录。"""
        return self.root / "logs"

    @property
    def vertex_keys_dir(self) -> Path:
        """Vertex credential dir. / Vertex 凭证目录。"""
        return self.root / "vertex_keys"

    def vertex_credential_path(self, credential_id: int) -> Path:
        """Vertex credential file uploaded per credential id. / 按凭证 id 上传的 Vertex 凭证文件。"""
        return self.vertex_keys_dir / f"vertex_cred_{credential_id}.json"

    @property
    def trial_runs_dir(self) -> Path:
        """Artifact dir of the endpoint "test connection". / 端点「测试连接」的产物目录。"""
        return self.root / "trial_runs"

    @property
    def runtime_dir(self) -> Path:
        """Process-internal state: generation admission locks, migration-done markers, migration error logs, etc.

        进程内部状态：生成准入锁、迁移完成标记、迁移错误日志等。"""
        return self.root / "runtime"

    @property
    def system_dirs(self) -> tuple[Path, ...]:
        """Top-level dirs under the data root registered by the layout, other than the projects dir.

        New system dirs must be registered here: the Agent's Bash sandbox pre-creates these dirs at session start and denies
        reading them; unregistered dirs that appear only after session start are not in ``denyRead`` (built-in read tools
        still deny by data root default).

        数据根下由布局登记的、项目目录以外的顶层目录。

        新增的系统目录须登记于此：Agent 的 Bash 沙箱在会话启动时预建这些目录并拒读，未登记且在
        会话启动之后才出现的目录不在 ``denyRead`` 中（内置读工具仍按数据根默认拒绝）。
        """
        return (
            self.deleted_projects_dir,
            self.global_assets_dir,
            self.users_dir,
            self.log_dir,
            self.vertex_keys_dir,
            self.trial_runs_dir,
            self.runtime_dir,
        )

    @property
    def top_level_entries(self) -> tuple[Path, ...]:
        """All top-level entries under the data root registered by the layout: the projects dir and each kind of runtime data.

        数据根下由布局登记的全部顶层条目：项目目录与各类运行数据。"""
        return (self.projects_dir, *self.system_dirs, self.sqlite_db_path)

    @property
    def generation_admission_locks_dir(self) -> Path:
        """Generation admission lock dir. / 生成准入锁目录。"""
        return self.runtime_dir / "generation-admission-locks"

    @property
    def session_import_marker_path(self) -> Path:
        """Done marker for local SDK session import. / 本地 SDK 会话导入完成标记。"""
        return self.runtime_dir / "session-store-import.done"

    @property
    def layout_migration_marker_path(self) -> Path:
        """Done marker for the data root layout migration; when present, startup skips the migration steps. / 数据根布局迁移完成标记；存在时启动不再执行迁移步骤。"""
        return self.runtime_dir / "data-root-layout-migrated.done"

    @property
    def project_migration_error_log_path(self) -> Path:
        """Error log of project schema migration. / 项目 schema 迁移的错误日志。"""
        return self.runtime_dir / "project-migration-errors.log"

    @property
    def auth_token_secret_path(self) -> Path:
        """JWT signing key auto-generated when ``AUTH_TOKEN_SECRET`` is unset; read/write for owner only.

        未设置 ``AUTH_TOKEN_SECRET`` 时自动生成的 JWT 签名密钥；仅属主可读写。"""
        return self.runtime_dir / "auth-token-secret"

    @property
    def image_thumbnail_cache_dir(self) -> Path:
        """Image thumbnail cache generated by the public media endpoint per ``?w=``; the whole dir can be deleted at any time.

        Kept outside the projects dir: not part of asset fingerprints, version snapshots or project export, and not on the
        public media allowlist.

        公开媒体端点按 ``?w=`` 生成的图片缩略图缓存，可随时整目录删除。

        放在项目目录之外：不进资产指纹、版本快照与项目导出，也不在公开媒体白名单内。
        """
        return self.runtime_dir / "image-thumbnails"
