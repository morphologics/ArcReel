"""
Video project management WebUI - FastAPI main application

How to start:
    cd ArcReel
    uv run uvicorn server.app:app --reload --reload-dir server --reload-dir lib --port 1241

Note: --reload-dir must be used to limit the watched directories, otherwise watchfiles scans over a hundred thousand
      files in node_modules / .venv / .git / .worktrees etc., at 50%+ of one CPU core.

On native Windows do not add --reload: when uvicorn enables reload on Windows it switches to SelectorEventLoop,
which does not support subprocesses, so the built-in Agent cannot launch Claude Code. Use instead:
    uv run uvicorn server.app:app --port 1241

You can also use ``uv run python -m server.app``: the listen address comes from LISTEN_HOST / LISTEN_PORT; on
Windows reload is automatically off, on other platforms reload is on and only server and lib are watched.

视频项目管理 WebUI - FastAPI 主应用

启动方式:
    cd ArcReel
    uv run uvicorn server.app:app --reload --reload-dir server --reload-dir lib --port 1241

注意：必须用 --reload-dir 限定监视目录，否则 watchfiles 会扫描
node_modules / .venv / .git / .worktrees 等十几万个文件，单核 CPU 50%+。

Windows 原生环境不要加 --reload：uvicorn 启用 reload 时在 Windows 上改用
SelectorEventLoop，它不支持子进程，内置 Agent 无法拉起 Claude Code。改用：
    uv run uvicorn server.app:app --port 1241

也可以 ``uv run python -m server.app``：监听地址取 LISTEN_HOST / LISTEN_PORT，
Windows 上自动不开 reload，其他平台开启 reload 并只监视 server 与 lib。
"""

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.datastructures import Headers
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipResponder, IdentityResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lib import PROJECT_ROOT
from lib.agent.agent_session_store import default_sdk_config_dir, session_store_enabled
from lib.agent.agent_session_store.import_local import migrate_local_transcripts_to_store
from lib.agent.agent_session_store.key_alignment import align_session_keys
from lib.agent.agent_session_store.store import DbSessionStore
from lib.catalog.official_sync import start_official_sync, stop_official_sync
from lib.config.env_keys import PROVIDER_SECRET_KEYS
from lib.db import async_session_factory, close_db, init_db
from lib.generation.generation_worker import GenerationWorker
from lib.infra.data_root_layout import DataRootLayout, list_project_dirs
from lib.infra.data_root_layout_migration import migrate_data_root_layout
from lib.infra.env_init import warn_if_env_diverges_from_file
from lib.infra.ffmpeg import log_ffmpeg_status
from lib.infra.httpx_shared import shutdown_http_client, startup_http_client
from lib.infra.logging_config import (
    attach_file_handler,
    migrate_legacy_log_dir,
    setup_logging,
    warn_if_log_dir_env_set,
)
from lib.infra.loop_stall_probe import start_loop_stall_probe, stop_loop_stall_probe
from lib.infra.path_safety import try_safe_join
from lib.project.project_migrations import cleanup_stale_backups, run_project_migrations
from lib.script.source_loader.migration import migrate_project_source_encoding, migrate_project_source_encoding_once
from server.auth import ensure_auth_password, ensure_token_secret, get_current_user, warn_if_auth_disabled
from server.cors_config import resolve_cors_policy
from server.dependencies import require_project_migration_ok, require_valid_project_name
from server.error_handlers import ERROR_RESPONSES, register_error_handlers
from server.remote_mcp import mount_remote_mcp, remote_mcp_host
from server.routers import (
    activity,
    ad_script,
    agent_config,
    agent_llm,
    agent_memory,
    api_keys,
    asset_sheets,
    assets,
    assistant,
    catalog,
    characters,
    cost_estimation,
    custom_endpoints,
    custom_providers,
    derivative_uploads,
    diagnostics,
    edit_timelines,
    editor_text_templates,
    end_frames,
    episode_drafts,
    episode_management,
    episode_planning,
    episodes_view,
    file_view,
    files,
    generate,
    grids,
    market,
    market_submissions,
    official_service,
    onboarding,
    presentations,
    products,
    project_events,
    project_migration,
    project_trash,
    projects,
    prompt_authoring,
    prompt_templates,
    props,
    provider_models,
    providers,
    reference_videos,
    scenes,
    script_plan,
    script_review,
    shot_uploads,
    storyboard_batches,
    system,
    system_config,
    tasks,
    usage,
    user_options,
    versions,
)
from server.routers import auth as auth_router
from server.services.project.episode_id_records import recorded_episode_ids_on
from server.services.project.project_events import ProjectEventService
from server.services.tasks.generation_tasks import execute_generation_task
from server.services.tasks.resume_executor import execute_resume_video_task


def assert_no_provider_secrets_in_environ() -> None:
    """The parent process must not hold any provider secret; violating this fails fast.

    Bash sandbox subprocesses inherit the parent env via fork, so the parent process must move all provider secrets
    offline into the DB, and the SDK's options.env injects them into the subprocess explicitly.

    父进程禁止持有任何 provider 密钥；违反即 fail-fast。

    Bash 沙箱子进程通过 fork 继承父 env，父进程必须把 provider secrets
    全部下线到 DB，由 SDK options.env 显式注入子进程。
    """
    leaked = sorted(k for k in PROVIDER_SECRET_KEYS if os.environ.get(k))
    if leaked:
        raise RuntimeError(
            f"SECURITY: parent process os.environ holds provider secrets: {leaked}. "
            "Enter them on the WebUI system config page and remove those entries from env / .env."
        )


_APPARMOR_USERNS_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
_UNPRIV_USERNS_SYSCTL = Path("/proc/sys/kernel/unprivileged_userns_clone")
_MAX_USER_NS_SYSCTL = Path("/proc/sys/user/max_user_namespaces")


def _read_sysctl(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _diagnose_bwrap_failure() -> str:
    """Give the precise fix path for a bwrap failure based on the host sysctl state.

    procfs is shared with the host, so host sysctl values are readable inside containers too, and this diagnosis
    works both inside and outside docker. Priority: Ubuntu 24.04 AppArmor restriction > traditional
    unprivileged_userns_clone > max_user_namespaces > fallback container config.

    根据 host sysctl 状态给出 bwrap 失败的精确修复路径。

    procfs 是宿主机共享的，容器内同样能读到 host sysctl 值，所以这套
    诊断在 docker 内外都能跑。优先级：Ubuntu 24.04 AppArmor 限制 >
    传统 unprivileged_userns_clone > max_user_namespaces > 兜底容器配置。
    """
    parts: list[str] = []

    apparmor_userns = _read_sysctl(_APPARMOR_USERNS_SYSCTL)
    if apparmor_userns == "1":
        parts.append(
            "Detected Ubuntu 24.04+ AppArmor restriction (root cause):\n"
            "  /proc/sys/kernel/apparmor_restrict_unprivileged_userns = 1\n"
            "  Blocks ALL unprivileged user namespaces. `apparmor:unconfined`\n"
            "  in docker compose does NOT bypass this — it is a global LSM\n"
            "  switch, not a per-process profile.\n"
            "  Fix on HOST (not inside the container):\n"
            "    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0\n"
            '    echo "kernel.apparmor_restrict_unprivileged_userns=0" '
            "| sudo tee /etc/sysctl.d/60-arcreel-bwrap.conf"
        )

    userns_clone = _read_sysctl(_UNPRIV_USERNS_SYSCTL)
    if userns_clone == "0":
        parts.append(
            "Unprivileged user namespaces disabled on host:\n"
            "  /proc/sys/kernel/unprivileged_userns_clone = 0\n"
            "  Fix on HOST: sudo sysctl -w kernel.unprivileged_userns_clone=1"
        )

    max_userns = _read_sysctl(_MAX_USER_NS_SYSCTL)
    if max_userns == "0":
        parts.append(
            "User namespace count limit set to 0 on host:\n"
            "  /proc/sys/user/max_user_namespaces = 0\n"
            "  Fix on HOST: sudo sysctl -w user.max_user_namespaces=15000"
        )

    if not parts:
        parts.append(
            "Container likely missing security relaxation. docker compose:\n"
            "  security_opt:\n"
            "    - seccomp:unconfined\n"
            "    - apparmor:unconfined\n"
            "  cap_add:\n"
            "    - NET_ADMIN"
        )

    return "\n".join(parts)


def check_sandbox_available(
    *,
    platform_system: Callable[[], str] | None = None,
    executable_which: Callable[[str], str | None] | None = None,
    subprocess_run: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> bool:
    """Detect sandbox tool availability at startup.

    Returning ``True`` means the sandbox is available and must be enabled; returning ``False`` means the SDK does
    not support the current platform (Windows only; official platform constraints at
    https://code.claude.com/docs/en/sandboxing#platform-and-tool-compatibility): the server can still start but with
    the sandbox off, and the Bash tool falls back to the code allowlist
    ``AgentAccessPolicy.WINDOWS_BASH_PREFIX_WHITELIST``. Missing tools on macOS / Linux still fail hard (no
    degradation allowed on supported platforms).

    启动期检测 sandbox 工具可用性。

    返回 ``True`` 表示沙箱可用且必须启用；返回 ``False`` 表示 SDK 不支持
    当前平台（仅 Windows，官方平台约束见
    https://code.claude.com/docs/en/sandboxing#platform-and-tool-compatibility），server
    仍可启动但 sandbox 关闭，Bash 工具回退到
    ``AgentAccessPolicy.WINDOWS_BASH_PREFIX_WHITELIST`` 代码白名单。
    macOS / Linux 工具缺失仍硬失败（受支持平台禁止降级）。
    """
    system = (platform_system or platform.system)()
    which = executable_which or shutil.which
    run = subprocess_run or subprocess.run
    if system == "Darwin":
        if which("sandbox-exec") is None:
            raise RuntimeError(
                "SANDBOX_UNAVAILABLE on macOS\n"
                "  sandbox-exec: not found in PATH (should be system-installed)\n"
                "Required for ArcReel agent runtime."
            )
        return True
    if system == "Linux":
        # Linux dependencies per https://code.claude.com/docs/en/sandboxing#set-up-linux-and-wsl2: both must be
        # installed (bwrap for process/file isolation, socat for network proxy forwarding).
        # Linux 依赖见 https://code.claude.com/docs/en/sandboxing#set-up-linux-and-wsl2：需同时安装
        # （bwrap 做进程/文件隔离，socat 做网络代理转发）。
        missing = [name for name in ("bwrap", "socat") if which(name) is None]
        if missing:
            raise RuntimeError(
                "SANDBOX_UNAVAILABLE on linux\n"
                f"  missing in PATH: {', '.join(missing)}\n"
                "Required for ArcReel agent runtime. Install:\n"
                "  Ubuntu/Debian: sudo apt install bubblewrap socat\n"
                "  Fedora:        sudo dnf install bubblewrap socat\n"
                "  Arch:          sudo pacman -S bubblewrap socat"
            )
        # bwrap being installed does not mean it can run. Two common failures:
        # 1) Creating a user namespace is denied: blocked by seccomp / apparmor / sysctl
        #    → "No permissions to create new namespace"
        # 2) Loopback config inside a new net namespace is denied: the container lacks CAP_NET_ADMIN
        #    → "loopback: Failed RTM_NEWADDR: Operation not permitted"
        # Do a trial run with unshare arguments close to the SDK's actual call, catching it at startup, so the Agent
        # does not fail mysteriously on its first Bash call.
        # bwrap 装了不代表跑得起来。两类常见失败：
        # 1) 创建 user namespace 被拒：seccomp / apparmor / sysctl 屏蔽
        #    → "No permissions to create new namespace"
        # 2) 新 net namespace 内 loopback 配置被拒：容器缺 CAP_NET_ADMIN
        #    → "loopback: Failed RTM_NEWADDR: Operation not permitted"
        # 用与 SDK 实际调用接近的 unshare 参数试跑，启动期就拦下来，
        # 避免 Agent 第一次调 Bash 才神秘失败。
        probe_cmd = [
            "bwrap",
            "--unshare-user",
            "--unshare-net",
            "--unshare-pid",
            "--ro-bind",
            "/",
            "/",
            "/bin/true",
        ]
        try:
            probe = run(probe_cmd, capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                "SANDBOX_BWRAP_BROKEN on Linux\n"
                f"  bwrap probe failed to execute: {exc}\n"
                "Required for ArcReel agent runtime."
            ) from exc
        if probe.returncode != 0:
            stderr = probe.stderr.decode("utf-8", errors="replace").strip() or "(no stderr)"
            raise RuntimeError(
                "SANDBOX_BWRAP_BROKEN on Linux\n"
                f"  bwrap installed but cannot run: {stderr}\n"
                f"{_diagnose_bwrap_failure()}"
            )
        return True
    logger.warning(
        "SANDBOX_UNSUPPORTED on %s — server 启动 sandbox=disabled，Bash 工具回退到代码白名单"
        "（python .claude/skills/.../scripts/*.py / ffmpeg / ffprobe）。"
        "生产部署推荐 macOS / Linux / Docker；Windows 用户建议使用 WSL2。",
        system,
    )
    return False


_DOCKERENV_PATH = Path("/.dockerenv")
_CGROUP_PATH = Path("/proc/1/cgroup")


def detect_docker_environment(
    *,
    dockerenv_path: Path | None = None,
    cgroup_path: Path | None = None,
) -> bool:
    """Detect once at startup whether we are inside a Docker / Podman container.

    Used to decide whether to enable ``SandboxSettings.enableWeakerNestedSandbox``.

    启动期一次性检测当前是否在 Docker / Podman 容器内。

    用于决定是否启用 ``SandboxSettings.enableWeakerNestedSandbox``。
    """
    docker_marker = dockerenv_path or _DOCKERENV_PATH
    cgroup = cgroup_path or _CGROUP_PATH
    if docker_marker.exists():
        return True
    try:
        content = cgroup.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "docker" in content or "podman" in content


# Initialize logging: at module import only attach the stream handler. The file handler is deferred to lifespan, which
# must first run migrate_legacy_log_dir() to move old logs from the code directory into the data root; otherwise the
# handler would first open a log file with the same name in the new location and the old file could not be moved over.
# This also avoids side effects on the real file system when pytest imports server.app during collection.
# 初始化日志：模块导入期只挂 stream handler。
# file handler 推迟到 lifespan，前面要先跑 migrate_legacy_log_dir()
# 把代码目录下的旧 logs 迁入数据根，否则 handler 先在新位置开出同名日志文件，
# 旧文件就迁不过去；同时也避免 pytest 收集阶段 import server.app 时
# 对真实文件系统产生副作用。
setup_logging(file=False)
logger = logging.getLogger(__name__)


def _log_profile_sync_outcome(stats: dict, *, log: logging.Logger = logger) -> None:
    """Decide between info and warning from the stats returned by ``sync_all_agent_profiles``.

    ``stats["aborted"]`` is a bool, and bool is a subclass of int -- a naive ``isinstance(v, int) and v > 0`` would
    treat ``aborted=True`` as a positive "materialization done" signal, the opposite of the real state. Handle the
    abort signal separately first, then use ``type(v) is int`` (strict type equality) to count only real integer
    counts.

    根据 ``sync_all_agent_profiles`` 返回的 stats 决定打 info 还是 warning。

    ``stats["aborted"]`` 是 bool；而 bool 是 int 的子类——简单的
    ``isinstance(v, int) and v > 0`` 会把 ``aborted=True`` 当成"物化完成"的正向
    信号，与实际状态相反。先单独处理 abort 信号，再用 ``type(v) is int``（严格
    类型相等）仅统计真正的整数计数。
    """
    if stats.get("aborted"):
        log.warning("agent_runtime profile 物化已中止: %s", stats)
        return
    if any(type(v) is int and v > 0 for v in stats.values()):
        log.info("agent_runtime profile 物化完成: %s", stats)


async def _migrate_source_encoding_on_startup(
    projects_dir: Path,
    *,
    migrate_source_encoding: Callable[[Path], Any] | None = None,
) -> dict[str, dict]:
    """Run the idempotent encoding migration on each project. Failures are caught and logged and do not block startup.

    对每个项目执行幂等编码迁移。失败被捕获并写日志，不阻塞启动。"""
    summary: dict[str, dict] = {}
    migrate = migrate_source_encoding or migrate_project_source_encoding
    # one-off existence check at startup, local metadata
    if not projects_dir.exists():  # noqa: ASYNC240 -- 启动期一次性存在性检查，本地元数据
        return summary

    for project_dir in list_project_dirs(projects_dir):
        summary[project_dir.name] = await asyncio.to_thread(
            migrate_project_source_encoding_once, project_dir, migrate=migrate
        )
    return summary


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan management

    应用生命周期管理"""
    # Startup
    # Security red-line checks: first sanitize the parent process env, then sandbox tool availability, then docker
    # detection
    # Startup
    # 安全红线检测：先父进程 env 净化，再 sandbox 工具可用性，再 docker 检测
    assert_no_provider_secrets_in_environ()
    sandbox_enabled = check_sandbox_available()
    # detect_docker_environment only makes sense on platforms where the sandbox is available (Linux path probing);
    # skipped on the Windows fallback to avoid pointless file system calls.
    # detect_docker_environment 仅在 sandbox 可用平台有意义（Linux 路径探测）；
    # Windows 回退时跳过，避免无意义的文件系统调用。
    is_docker = detect_docker_environment() if sandbox_enabled else False
    logger.info("Sandbox runtime: enabled=%s docker=%s", sandbox_enabled, is_docker)

    app.state.in_docker = is_docker
    app.state.sandbox_enabled = sandbox_enabled

    # Self-check the bundled ffmpeg once, caching the verdict in the process; unavailability does not block startup.
    # 随包 ffmpeg 自检一次，结论缓存在进程内；不可用不阻断启动。
    await asyncio.to_thread(log_ffmpeg_status)

    # Log file persistence: first move old logs from the code directory into the data root, then attach the file
    # handler. Order matters -- the handler creates arcreel.log in the new location, and attaching it early would
    # leave the old same-named file behind due to the conflict.
    # 日志文件持久化：先把代码目录下的旧日志迁入数据根，再挂 file handler。
    # 顺序很重要——handler 会在新位置创建 arcreel.log，提前挂会让旧的同名文件因冲突留在原处。
    await asyncio.to_thread(migrate_legacy_log_dir)
    attach_file_handler()
    warn_if_log_dir_env_set()
    warn_if_env_diverges_from_file()

    ensure_auth_password()
    warn_if_auth_disabled()

    # Run Alembic migrations (auto-creates tables on first start)
    await init_db()

    # Data root layout migration: after attaching the file log handler (the migration writes to the log) and Alembic
    # (steps rewrite the DB), and before every step that iterates projects -- those steps find projects by the current
    # layout and must not run on a half-migrated data root. Failure aborts startup.
    # 数据根布局迁移：排在挂文件日志 handler（迁移过程写进日志）与 Alembic（步骤会改写库）之后、
    # 所有遍历项目的步骤之前——那些步骤按当前布局找项目，不能跑在半迁移的数据根上。失败即中止启动。
    layout = DataRootLayout.current()
    await migrate_data_root_layout(
        layout.root,
        session_factory=async_session_factory,
        sdk_config_dir=default_sdk_config_dir(),
    )
    # The signing key may live under runtime/ in the data root and must be resolved after the layout migration: a
    # project named runtime in the old layout is moved into projects/, and a key file written earlier would go into
    # the project directory with it.
    # 签名密钥可能落在数据根的 runtime/ 下，须在布局迁移之后解析：旧布局里名为 runtime 的项目
    # 会被搬进 projects/，提前写入的密钥文件会随它进入项目目录。
    await asyncio.to_thread(ensure_token_secret)

    # Source file encoding migration (idempotent; failure does not block startup). Runs before the schema migration:
    # all sources are first converted to UTF-8, so every chain that reads sources as UTF-8 afterwards (episode
    # planning, derived file reconciliation) gets uniform input.
    # 源文件编码迁移（幂等；失败不阻塞启动）。先于 schema 迁移跑：源文一律先归到 UTF-8，
    # 之后所有按 UTF-8 读源文的链路（分集规划、派生文件对账）才有统一的输入。
    source_migration_summary = await _migrate_source_encoding_on_startup(layout.projects_dir)
    migrated_total = sum(len(s.get("migrated") or []) for s in source_migration_summary.values())
    failed_total = sum(len(s.get("failed") or []) for s in source_migration_summary.values())
    if migrated_total or failed_total:
        logger.info(
            "源文件编码迁移完成：migrated=%d failed=%d projects=%d",
            migrated_total,
            failed_total,
            len(source_migration_summary),
        )

    # Run any pending project.json schema migrations (file-based).
    # Both calls are synchronous filesystem walks — offload to a worker thread
    # so they don't block the event loop during uvicorn startup.
    migration_summary = await asyncio.to_thread(
        run_project_migrations,
        layout.projects_dir,
        recorded_episode_ids=recorded_episode_ids_on(asyncio.get_running_loop()),
    )
    if migration_summary.migrated or migration_summary.failed:
        logger.info(
            "Project migrations: migrated=%s skipped=%d failed=%s",
            migration_summary.migrated,
            len(migration_summary.skipped),
            migration_summary.failed,
        )
    await asyncio.to_thread(cleanup_stale_backups, layout.projects_dir, 7)

    # Session store key alignment (idempotent; failure does not block startup): after the data root moves to a new
    # absolute path, the sessions' Agent context moves under keys derived from the current project directory. After
    # the data root layout migration: the layout migration moves old-layout keys to directories under projects/, and
    # only then are they recomputed here by the current layout. Before the local transcript import: the import lists
    # local sessions by the current key and checks whether they were already imported by the current key; with
    # unaligned keys it would import another copy under the new key, leaving records for the same session under two
    # keys, and alignment would no longer change it.
    # 会话存储键对齐（幂等；失败不阻塞启动）：数据根换了绝对路径后，会话的 Agent 上下文改到当前项目目录派生的键下。
    # 排在数据根布局迁移之后：布局迁移把旧布局的键改到 projects/ 下的目录，这里才按当前布局重算。
    # 排在本地 transcript 导入之前：导入按当前键列本地会话、按当前键判断是否已导入，键没对齐时会在新键下
    # 再导入一份，同一会话两个键下都有记录，对齐就不再改它。
    try:
        await align_session_keys(
            async_session_factory,
            projects_dir=layout.projects_dir,
            sdk_config_dir=default_sdk_config_dir(),
            store_enabled=session_store_enabled(),
        )
    except Exception:
        logger.exception("会话存储键对齐失败（不阻塞启动）")

    # Migrate any pre-existing local SDK jsonl transcripts into the DbSessionStore.
    # Runs once (marker-gated); failures are non-fatal and logged.
    if session_store_enabled():
        try:
            store = DbSessionStore(async_session_factory)
            await migrate_local_transcripts_to_store(store, data_root=layout.root)
        except Exception:
            logger.exception("session-store transcript migration failed (non-fatal)")

    # Migrate legacy .system_config.json → DB (no-op if file doesn't exist or already migrated)
    try:
        from lib.config.migration import migrate_json_to_db

        async with async_session_factory() as session:
            await migrate_json_to_db(session, layout.root)
    except Exception as exc:
        logger.warning("JSON→DB config migration failed (non-fatal): %s", exc)

    # Old task-level text backend keys → tier keys (docs/adr/0051). Placed after the JSON→DB migration: same-named
    # keys in old JSON that landed in the DB via the catch-all can also be absorbed by this migration
    # 旧任务级文本 backend 键 → 档位键（docs/adr/0051）。放在 JSON→DB 迁移之后：
    # 旧 JSON 里的同名键经 catch-all 落库后也能被本迁移收编
    try:
        from lib.config.migration import migrate_text_tier_settings

        async with async_session_factory() as session:
            await migrate_text_tier_settings(session)
    except Exception as exc:
        logger.warning("text tier settings migration failed (non-fatal): %s", exc)

    # Rewrite renamed models in settings to their successor models (retired model mapping). Placed after the tier
    # migration: old task-level keys absorbed into tier keys are rewritten too
    # 设置里已改名的模型改写为后继模型（退役模型映射）。放在档位迁移之后：旧任务级键收编进档位键后一并改写
    try:
        from lib.config.migration import migrate_retired_model_settings

        async with async_session_factory() as session:
            await migrate_retired_model_settings(session)
    except Exception as exc:
        logger.warning("retired model settings migration failed (non-fatal): %s", exc)

    # Official market source seed: insert if absent, sync if the URL constant changed; no fetching / 官方市场源 seed：不存在则插入，地址常量变了就同步；不抓取
    try:
        from lib.market.sources import seed_official_source

        async with async_session_factory() as session:
            await seed_official_source(session)
    except Exception as exc:
        logger.warning("official market source seed failed (non-fatal): %s", exc)

    # Load catalog data fetched by the official sync into catalog queries; if it cannot be read, use the factory
    # snapshot for now until the next sync round
    # 官方同步取回的目录数据装入目录查询；读不到时暂用出厂快照，等下一轮同步
    try:
        from lib.catalog.official_sync import load_synced_data

        async with async_session_factory() as session:
            await load_synced_data(session)
    except Exception:
        logger.exception("loading synced catalog data failed (non-fatal)")

    # Merge copies of model entries modified by the user into catalog queries; if they cannot be read, those entries
    # use the official data for now, the rest as usual
    # 用户修改的模型条目副本并进目录查询；读不到时这些条目暂按官方数据，其余照常
    try:
        from lib.catalog.user_entries import reload_user_entries

        async with async_session_factory() as session:
            await reload_user_entries(session)
    except Exception:
        logger.exception("loading user-modified catalog entries failed (non-fatal)")

    # Merge user-added models under built-in providers into catalog queries; if they cannot be read, those models are
    # unavailable for now, other models as usual
    # 内置供应商下用户添加的模型并进目录查询；读不到时这些模型暂不可用，其余模型照常
    try:
        from server.routers.provider_models import reload_user_added_models

        async with async_session_factory() as session:
            await reload_user_added_models(session)
    except Exception:
        logger.exception("loading user-added built-in provider models failed (non-fatal)")

    # Materialize agent_runtime_profile into existing projects (file I/O → worker thread)
    # 把 agent_runtime_profile 物化到存量项目（文件 I/O → worker 线程）
    from lib.project.project_manager import get_project_manager

    _pm = get_project_manager()
    _profile_sync_stats = await asyncio.to_thread(_pm.sync_all_agent_profiles)
    _log_profile_sync_outcome(_profile_sync_stats)

    # Start the shared httpx client (used for external API calls such as version checks) / 启动共享 httpx 客户端（用于版本检查等外部 API 调用）
    await startup_http_client()

    # Initialize async services
    await assistant.assistant_service.startup(in_docker=is_docker, sandbox_enabled=sandbox_enabled)
    assistant.assistant_service.session_manager.start_patrol()

    logger.info("启动 GenerationWorker...")
    worker = create_generation_worker()
    app.state.generation_worker = worker
    await worker.start()
    logger.info("GenerationWorker 已启动")

    logger.info("启动 ProjectEventService...")
    project_event_service = ProjectEventService(PROJECT_ROOT, data_root=layout.root)
    app.state.project_event_service = project_event_service
    await project_event_service.start()
    logger.info("ProjectEventService 已启动")
    assistant.assistant_service.session_manager.set_autonomous_turn_listener(
        project_event_service.publish_assistant_session_resumed
    )

    # The stall probe goes after the startup steps: synchronous startup work such as migrations does not count as an
    # online stall, avoiding false alarms.
    # 卡顿探针放在启动步骤之后：迁移等启动期同步工作不算线上卡顿，避免误报。
    stall_probe = start_loop_stall_probe()
    # Official sync of the model catalog: check once at startup, then once a day; cancelled on app shutdown
    # 模型目录的官方同步：启动时检查一次，之后每天一次；随应用关停取消
    catalog_sync = start_official_sync(async_session_factory)
    # Expiry cleanup of the project trash and deleted projects: once at startup, then periodically; cancelled on app
    # shutdown
    # 项目回收站与已删除项目的过期清理：启动时清一次，之后定期清理；随应用关停取消
    from server.services.project.trash_expiry import start_trash_expiry, stop_trash_expiry

    trash_expiry = start_trash_expiry(layout)
    # Pre-generation of stream segments for the edit view: scan every timeline once a minute and fill missing segments
    # at idle priority; cancelled on app shutdown
    # 剪辑视图的流片段提前生成：每分钟扫一遍各时间线，按空闲优先级补齐缺的片段；随应用关停取消
    from lib.project.project_manager import get_project_manager
    from server.services.presentation.timeline_stream import start_stream_prebuild, stop_stream_prebuild

    stream_prebuild = start_stream_prebuild(get_project_manager())
    # YouReel: give freed memory back to the system every few minutes (glibc malloc_trim; off with
    # ARCREEL_MALLOC_TRIM_INTERVAL=0); cancelled on app shutdown
    from server.services.memory_trim import start_memory_trim, stop_memory_trim

    memory_trim = start_memory_trim()
    try:
        async with remote_mcp_host.run():
            yield
    finally:
        await stop_memory_trim(memory_trim)
        await stop_stream_prebuild(stream_prebuild)
        await stop_trash_expiry(trash_expiry)
        await stop_official_sync(catalog_sync)
        # Also stop the probe on runtime exceptions or when shutdown is cancelled, so the shutdown phase is not
        # misreported as a stall.
        # 运行期异常或关停被取消时同样停掉探针，免得收尾阶段把关停工作误报成卡顿。
        await stop_loop_stall_probe(stall_probe)

    # Shutdown
    assistant.assistant_service.session_manager.set_autonomous_turn_listener(None)
    project_event_service = getattr(app.state, "project_event_service", None)
    if project_event_service:
        logger.info("正在停止 ProjectEventService...")
        await project_event_service.shutdown()
        logger.info("ProjectEventService 已停止")
    worker = getattr(app.state, "generation_worker", None)
    if worker:
        logger.info("正在停止 GenerationWorker...")
        # Shutdown does not cancel running tasks: worker.stop() waits for running tasks to finish before leaving the
        # main loop.
        # 关停不取消在跑任务：worker.stop() 等在跑任务跑完再退出主循环。
        await worker.stop()
        logger.info("GenerationWorker 已停止")
    # Test-connection runs cannot resume: dying with the event loop would leave the ledger's pending rows forever, so
    # settle them via the cancel path before shutdown.
    # 测试连接的 run 不可续跑：随事件循环消亡会把账本 pending 行永远留下，关停前按取消路径结算。
    from lib.custom_provider.endpoint_test import shutdown_trial_runs

    await shutdown_trial_runs()
    await shutdown_http_client()
    await close_db()


# Create the FastAPI app / 创建 FastAPI 应用
app = FastAPI(
    title="YouReel Studio",
    description="Web workspace for AI video production",
    version="1.0.0",
    lifespan=lifespan,
    dependencies=[Depends(require_valid_project_name)],
    # YouReel (G4): document the shared error body on every operation / 在每个操作上记录共用的错误体
    responses=ERROR_RESPONSES,
)

# CORS config (env-driven, parsed in server/cors_config.py; the remote MCP mount shares the same allowlist). Must be
# computed before register_error_handlers(app): the 500 for unexpected exceptions is sent by the ServerErrorMiddleware
# fallback, bypassing CORSMiddleware (see server/error_handlers.py::_cors_headers_for), and the handler needs this
# config to add the CORS headers by hand.
# CORS 配置（env 驱动，解析见 server/cors_config.py；远程 MCP 挂载共用同一份白名单）。
# 须在 register_error_handlers(app) 之前算出：未预期异常的 500 由
# ServerErrorMiddleware 兜底发送，绕过 CORSMiddleware（见
# server/error_handlers.py::_cors_headers_for），handler 需要这份配置手工补 CORS 头。
_allow_origins, _allow_credentials = resolve_cors_policy()

# App-level exception handlers: the single point for the exception → status code → detail mapping (see
# server/error_handlers.py)
# app 级异常处理器：异常→状态码→detail 映射的单点（见 server/error_handlers.py）
register_error_handlers(app, cors_allow_origins=_allow_origins, cors_allow_credentials=_allow_credentials)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_listen_addr() -> tuple[str, int]:
    """Parse ``LISTEN_HOST`` / ``LISTEN_PORT``, shared by the ``__main__`` block and tests.

    **Scope**: only takes effect when run directly via ``python server/app.py`` (going through ``__main__`` below).
    When started with a standard ASGI CLI like ``uvicorn server.app:app``, the listen address is decided by the
    uvicorn process's own ``--host`` / ``--port`` arguments and this function plays no part -- an ASGI app module
    cannot go back and change the uvicorn process's binding at import time. Deployments such as Docker and systemd
    must pass host/port explicitly as uvicorn CLI arguments.

    The truthy default (``or``) is a fallback that covers a ``.env`` mistakenly set to an empty value (such as
    ``LISTEN_PORT=``).

    解析 ``LISTEN_HOST`` / ``LISTEN_PORT``，供 ``__main__`` 块与测试共用。

    **作用范围**：仅当通过 ``python server/app.py`` 直接执行（走下方 ``__main__``）
    时生效。通过 ``uvicorn server.app:app`` 这种标准 ASGI CLI 启动时，listen 地址
    由 uvicorn 进程自身的 ``--host`` / ``--port`` 参数决定，本函数不参与 —— 因为
    ASGI app 模块在 import 时无法回头改 uvicorn 进程的绑定。Docker、systemd 等
    部署需要把 host/port 作为 uvicorn CLI 参数显式传入。

    truthy 默认（``or``）兜底，覆盖 ``.env`` 误写空值（如 ``LISTEN_PORT=``）的场景。
    """
    host = os.environ.get("LISTEN_HOST") or "0.0.0.0"
    port = int(os.environ.get("LISTEN_PORT") or "1241")
    return host, port


# The frontend polls the endpoints below every 3s for task status; in steady state, successful responses would drown
# out real errors/slow requests, so 2xx + fast responses are lowered to DEBUG, while errors/slow responses still go to
# INFO for observability.
# 前端每 3s 轮询下述接口获取任务状态；稳态下成功响应会把真正的错误/慢请求淹没，
# 所以对 2xx + 快速响应降级到 DEBUG，异常/慢响应仍走 INFO 保证可观测。
_QUIET_POLL_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/tasks"),
        ("GET", "/api/v1/tasks/stats"),
    }
)
_QUIET_SLOW_THRESHOLD_MS = 500.0


@app.middleware("http")
async def request_logging_middleware(
    request: Request, call_next, *, quiet_slow_threshold_ms: float = _QUIET_SLOW_THRESHOLD_MS
):
    start = time.perf_counter()
    path = request.url.path
    _skip_log = path.startswith("/assets") or path == "/health"
    try:
        response: Response = await call_next(request)
    except Exception:
        if not _skip_log:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "%s %s 500 %.0fms (unhandled)",
                request.method,
                path,
                elapsed_ms,
            )
        raise
    if not _skip_log:
        elapsed_ms = (time.perf_counter() - start) * 1000
        is_quiet = (
            (request.method, path) in _QUIET_POLL_ENDPOINTS
            and response.status_code < 400
            and elapsed_ms < quiet_slow_threshold_ms
        )
        log = logger.debug if is_quiet else logger.info
        log(
            "%s %s %d %.0fms",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
        )
    return response


# Register API routes
#
# This block is the single source of truth for auth requirements: routers with dependencies require a Bearer token;
# CurrentUser in an endpoint signature only means "the handler needs the user object" and does not carry
# authorization. The two groups without the dependency are explained separately, see the group comments below.
# 注册 API 路由
#
# 认证要求的唯一真相源就是这个区块：带 dependencies 的 router 要求 Bearer token，
# 端点签名里的 CurrentUser 只表示「处理函数要用用户对象」，不承担授权职责。
# 不挂依赖的两组另有说明，见下方分组注释。
app.include_router(projects.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Projects"])
app.include_router(characters.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Characters"])
app.include_router(scenes.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Scenes"])
app.include_router(props.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Props"])
app.include_router(products.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Products"])
app.include_router(
    presentations.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Presentations"]
)
app.include_router(
    edit_timelines.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Edit timelines"]
)
app.include_router(files.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Files"])
app.include_router(
    file_view.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["File view"],
)
app.include_router(
    project_trash.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Project trash"],
)
app.include_router(
    episodes_view.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Episodes view"]
)
app.include_router(
    episode_management.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Episode management"]
)
app.include_router(
    generate.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Generation"],
)
app.include_router(
    storyboard_batches.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Generation"],
)
app.include_router(
    asset_sheets.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Asset sheets"],
)
app.include_router(
    script_review.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Content review"],
)
app.include_router(
    episode_drafts.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Content review"],
)
app.include_router(
    prompt_authoring.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Prompt authoring"],
)
app.include_router(
    episode_planning.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Episode planning"],
)
app.include_router(
    script_plan.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Script planning"],
)
app.include_router(
    ad_script.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Ad / short scripts"],
)
app.include_router(
    shot_uploads.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Shot uploads"],
)
app.include_router(
    derivative_uploads.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Derivative sheet uploads"],
)
app.include_router(
    end_frames.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Shot end frames"],
)
app.include_router(
    project_migration.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Project migration"]
)
app.include_router(versions.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Versions"])
app.include_router(usage.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Usage"])
app.include_router(auth_router.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Auth"])
app.include_router(
    assistant.router,
    prefix="/api/v1/projects/{project_name}/assistant",
    dependencies=[Depends(get_current_user)],
    tags=["Agent sessions"],
)
app.include_router(
    project_events.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Project events"]
)
app.include_router(tasks.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Task queue"])
app.include_router(providers.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Providers"])
app.include_router(
    provider_models.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Providers"]
)
app.include_router(catalog.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Model catalog"])
app.include_router(
    system_config.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["System config"]
)
app.include_router(system.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["System"])
app.include_router(api_keys.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["API keys"])
app.include_router(
    agent_config.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Agent config"]
)
# The studio agent's model for the editor's AI chat (OpenReel next to the studio)
app.include_router(agent_llm.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
app.include_router(activity.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
# YouReel: errors the studio's browser UI reports (Settings → Options → Diagnostics)
app.include_router(
    diagnostics.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Diagnostics"]
)
app.include_router(
    custom_providers.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Custom providers"]
)
app.include_router(
    custom_endpoints.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Custom endpoints"]
)
app.include_router(market.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Market"])
app.include_router(
    market_submissions.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Market"]
)
app.include_router(
    official_service.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Official service"]
)
app.include_router(
    cost_estimation.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Cost estimation"]
)
app.include_router(
    grids.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Grid storyboards"],
)
app.include_router(
    reference_videos.router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Reference video"],
)
app.include_router(
    assets.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Global asset library"]
)
app.include_router(
    agent_memory.user_router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Agent memory"]
)
app.include_router(
    agent_memory.project_router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_project_migration_ok)],
    tags=["Agent memory"],
)
app.include_router(onboarding.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Onboarding"])
app.include_router(user_options.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["User options"])
app.include_router(editor_text_templates.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["User options"])
app.include_router(
    prompt_templates.router, prefix="/api/v1", dependencies=[Depends(get_current_user)], tags=["Prompt templates"]
)

# Public endpoints: reachable anonymously. The login entry is the prerequisite for getting a token, and static media
# is loaded via <img src> / <video src>.
# 公开端点：匿名可达。登录入口是拿 token 的前提，静态媒体经 <img src> / <video src> 加载。
app.include_router(auth_router.public_router, prefix="/api/v1", tags=["Auth"])
app.include_router(files.public_router, prefix="/api/v1", tags=["Files"])

# Self-authenticating endpoints: native browser download navigation cannot carry an Authorization header, so the
# endpoint checks a short-lived download token via verify_download_token (see docs/adr/0071).
# 自带认证端点：浏览器原生下载导航带不了 Authorization header，
# 端点内 verify_download_token 校验短时效下载 token（见 docs/adr/0071）。
app.include_router(projects.self_auth_router, prefix="/api/v1", tags=["Projects"])
app.include_router(edit_timelines.self_auth_router, prefix="/api/v1", tags=["Edit timelines"])


mount_remote_mcp(app, remote_mcp_host)


def create_generation_worker() -> GenerationWorker:
    return GenerationWorker(executor=execute_generation_task, resume_executor=execute_resume_video_task)


@app.get("/health")
async def health_check():
    """Health check / 健康检查"""
    return {"status": "ok", "message": "YouReel Studio is running"}


@app.get("/agent-installation-guide.md", include_in_schema=False)
async def serve_agent_installation_guide(request: Request) -> Response:
    """动态渲染 Agent 安装指引，将 {{BASE_URL}} 替换为实际服务地址（无需认证）。"""
    from fastapi.responses import PlainTextResponse

    template_path = PROJECT_ROOT / "public" / "agent-installation-guide.md"

    def _read() -> tuple[bool, str]:
        if not template_path.exists():
            return False, ""
        return True, template_path.read_text(encoding="utf-8")

    exists, template = await asyncio.to_thread(_read)
    if not exists:
        return PlainTextResponse("Agent install guide not found", status_code=404)

    # Infer the base URL from the request; only trust x-forwarded-proto (standard reverse proxy header); host uses the
    # connection's actual target address and does not accept x-forwarded-host, which users can forge.
    # 从请求推断 base URL；仅信任 x-forwarded-proto（反向代理标准头），
    # host 使用连接实际目标地址，不接受可被用户伪造的 x-forwarded-host。
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto or request.url.scheme or "http"
    host = request.url.netloc
    base_url = f"{scheme}://{host}"

    content = template.replace("{{BASE_URL}}", base_url)
    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")


#: Resource directory with content hashes in the frontend build output (Vite's default assetsDir). File names change
#: with content, so they can be cached forever; files from public/ at the dist root (favicon, style-thumbnails, etc.)
#: carry no hash and are not included.
#: 前端构建产物里带内容哈希的资源目录（Vite 默认 assetsDir）。文件名随内容变化，可永久缓存；
#: dist 根目录下来自 public/ 的文件（favicon、style-thumbnails 等）不带哈希，不在此列。
_HASHED_FRONTEND_ASSETS_PREFIX = "/assets/"
_SPA_SHELL_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"
_HASHED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


class FrontendCacheHeadersMiddleware:
    """Cache headers for the frontend build output.

    - The SPA entry HTML shell must not be cached by the browser: this covers the response trait (text/html) shared
      by both paths, spa_deep_link and the app.frontend native fallback; otherwise after a redeploy the browser may
      keep the old shell and load old hashed resources that were deleted, causing a white screen -- deciding by
      content-type rather than by route is what covers entries like "/" and "/login" that land on the native
      fallback.
    - Hashed resources under ``/assets/`` (including 304 revalidation responses) are set immutable; failure
      responses such as 404 are not cached.

    Pure ASGI implementation rather than BaseHTTPMiddleware: this is a global middleware acting on every request,
    and BaseHTTPMiddleware's anyio TaskGroup + contextvars copying would add extra overhead to each request.

    前端构建产物的缓存头。

    - SPA 入口 HTML 外壳禁止浏览器缓存：覆盖 spa_deep_link 与 app.frontend 原生 fallback
      两条路径共用的响应特征（text/html），否则重新部署后浏览器可能沿用旧壳加载已被删除的
      旧哈希资源，导致白屏——按 content-type 而非按路由判定，才能同时管住 "/"、"/login" 等
      落在原生 fallback 上的入口。
    - ``/assets/`` 下的哈希资源（含 304 再验证响应）设 immutable；404 等失败响应不缓存。

    纯 ASGI 实现而非 BaseHTTPMiddleware：这是个作用于全部请求的全局中间件，
    BaseHTTPMiddleware 的 anyio TaskGroup + contextvars 复制机制会给每个请求引入额外开销。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_hashed_asset = scope["path"].startswith(_HASHED_FRONTEND_ASSETS_PREFIX)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if headers.get("content-type", "").lower().startswith("text/html"):
                    headers["Cache-Control"] = _SPA_SHELL_CACHE_CONTROL
                elif is_hashed_asset and message["status"] in (200, 206, 304):
                    headers["Cache-Control"] = _HASHED_ASSET_CACHE_CONTROL
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(FrontendCacheHeadersMiddleware)


class ResponseCompressionMiddleware:
    """gzip compression for text responses such as JSON / JS / CSS / HTML.

    Reuses Starlette GZip's default exclusion list (text/event-stream and already compressed media types such as
    image/audio/video); 206 partial responses pass through as is; streaming responses use Z_SYNC_FLUSH per chunk
    without batching. Response headers of excluded types are released immediately by Starlette (>= 1.8), so event
    streams receive headers before the first event. compresslevel uses zlib's default level 6: the size gain on
    JSON/JS is nearly the same as 9, at much lower CPU cost.

    JSON / JS / CSS / HTML 等文本响应的 gzip 压缩。

    沿用 Starlette GZip 的默认排除清单（text/event-stream 与 image/audio/video 等已压缩的
    媒体类型），206 分段响应原样透传；流式响应按块 Z_SYNC_FLUSH，不攒批。排除类型的响应头
    由 Starlette（>= 1.8）即时放行，事件流在首个事件前就能收到响应头。compresslevel 取 zlib 默认档 6：
    JSON/JS 的体积收益与 9 相差无几，CPU 开销低得多。
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        responder: ASGIApp
        if "gzip" in Headers(scope=scope).get("accept-encoding", ""):
            responder = GZipResponder(self.app, minimum_size=500, compresslevel=6)
        else:
            responder = IdentityResponder(self.app, minimum_size=500)
        await responder(scope, receive, send)


# Outermost: the frontend cache header middleware rewrites response headers, and compression does not change
#            content-type, so their order does not matter
# 最外层：前端缓存头中间件改写的是响应头，压缩不改变 content-type，两者顺序互不影响
app.add_middleware(ResponseCompressionMiddleware)


# Frontend build output: SPA static file serving. The fallback only applies to GET/HEAD, so write requests that stray
# onto page paths no longer return the page. The mount condition must check index.html rather than the directory:
# app.frontend validates the fallback file at startup, and an incomplete build output would raise RuntimeError and
# take down the whole app (including all APIs)
# 前端构建产物：SPA 静态文件服务。fallback 仅对 GET/HEAD 生效，写请求误入页面路径不再返回页面。
# 挂载条件必须检查 index.html 而非目录：app.frontend 在启动期校验 fallback 文件，
# 构建产物不完整时会抛 RuntimeError 拖垮整个应用（含全部 API）
frontend_dist_dir = PROJECT_ROOT / "frontend" / "dist"

if (frontend_dist_dir / "index.html").is_file():

    def _spa_deep_link_response(rest: str) -> FileResponse:
        # The last segment of an SPA deep link may carry an extension (such as /app/projects/x/source/chapter1.txt);
        # app.frontend's fallback would treat it as a static resource request and return 404, so explicitly fall back
        # to the SPA shell here. This route is registered before app.frontend, so any request under /app/ reaches here
        # first -- if the build output happens to contain a real static file under dist/app/... (URL path following
        # the same mapping as app.frontend, i.e. the same path relative to the dist root), that file must be returned
        # first so it is not unconditionally shadowed. rest is a user-controlled URL segment: anything escaping the
        # root degrades to the SPA shell, never exposing files outside dist
        # SPA 深链末段可能带扩展名（如 /app/projects/x/source/chapter1.txt），
        # app.frontend 的 fallback 会将其判为静态资源请求返回 404，此处显式兜底回 SPA 外壳。
        # 本路由注册在 app.frontend 之前，/app/ 下任何请求都会先到这里——若构建产物中
        # 恰好存在 dist/app/... 下的真实静态文件（URL 路径与 app.frontend 的映射规则一致，
        # 即相对 dist 根目录同路径），须优先返回该文件，避免被无条件遮蔽。
        # rest 是用户可控的 URL 段：越界一律降级回 SPA 外壳，不暴露 dist 之外的文件
        candidate = try_safe_join(frontend_dist_dir / "app", rest, require_file=True)
        if candidate is not None:
            return FileResponse(candidate)
        return FileResponse(frontend_dist_dir / "index.html")

    @app.get("/app/{_rest:path}", include_in_schema=False)
    async def spa_deep_link(_rest: str) -> FileResponse:
        # Probing the static file and constructing FileResponse (the first guess_type loads the system mime table)
        # both touch disk, so the whole thing is offloaded to a thread.
        # 探测静态文件与 FileResponse 构造（首次 guess_type 会加载系统 mime 表）都触盘，整体卸到线程。
        return await asyncio.to_thread(_spa_deep_link_response, _rest)

    app.frontend("/", directory=frontend_dist_dir, fallback="index.html")
else:
    logger.warning("frontend/dist/index.html 不存在，跳过前端页面挂载（API 不受影响）")


def _main_uvicorn_args(platform_name: str = sys.platform) -> tuple[str | FastAPI, dict[str, Any]]:
    """App and arguments that ``__main__`` hands to ``uvicorn.run``.

    reload only accepts an import string. On Windows, once uvicorn enables reload it switches to SelectorEventLoop,
    which does not support subprocesses, so the built-in Agent cannot launch the Claude Code CLI; hence no reload on
    Windows.

    ``__main__`` 交给 ``uvicorn.run`` 的应用与参数。

    reload 只接受导入字符串。Windows 上 uvicorn 一旦启用 reload 就改用不支持子进程的
    SelectorEventLoop，内置 Agent 拉不起 Claude Code CLI，因此 Windows 不开 reload。
    """
    host, port = _resolve_listen_addr()
    if platform_name == "win32":
        return app, {"host": host, "port": port, "reload": False}
    return "server.app:app", {"host": host, "port": port, "reload": True, "reload_dirs": ["server", "lib"]}


if __name__ == "__main__":
    import uvicorn

    _target, _options = _main_uvicorn_args()
    uvicorn.run(_target, **_options)
