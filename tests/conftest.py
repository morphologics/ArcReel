"""Shared pytest fixtures for the ArcReel test suite.

This file holds only fixtures, collection-time hooks and session-start validation of targeted selection; no module
imports it. Test doubles live in `tests/fakes.py`, test input builders in `tests/factories.py`.

All DB fixtures derive from the single engine construction point `make_test_engine`:

- `session_factory` / `async_session` are dialect-aware and use real PostgreSQL when `DATABASE_URL` points to it;
  `concurrent_session_factory` is dialect-aware too and provides a WAL file DB that allows independent connections
  for SQLite; `file_session_factory` is always file SQLite, consumed by boundary tests marked `sqlite_only`.
  `pytest_collection_modifyitems` injects `uses_db` for these four, forming the selection that needs a database;
  the PostgreSQL compatibility job takes the `uses_db and not sqlite_only` part of it.
- `db_engine` / `db_session` / `db_factory` (in-memory) and `file_db_factory` (file) always use SQLite and carry no
  `uses_db`; they serve the models and repositories unit tests that stay out of that selection.
- The only exception is the PostgreSQL branch of `async_session`: it binds the public schema the CI job already built
  with `alembic upgrade head`, and its isolation primitive is an outer transaction + SAVEPOINT, unlike
  `make_test_engine`'s per-test schema + `create_all`, so it builds its own engine.

本文件只放 fixture、收集期钩子与会话启动期的定向选择校验，不被任何模块 import；
替身实现放在 `tests/fakes.py`，测试输入构造器放在 `tests/factories.py`。

DB fixture 一律派生自唯一的 engine 构造点 `make_test_engine`：

- `session_factory` / `async_session` 方言感知，`DATABASE_URL` 指向 PostgreSQL 时走真实 PostgreSQL；
  `concurrent_session_factory` 同样方言感知，并为 SQLite 提供允许独立连接的 WAL 文件库；
  `file_session_factory` 恒为文件 SQLite，消费方是标了 `sqlite_only` 的边界用例。
  四者由 `pytest_collection_modifyitems` 注入 `uses_db`，构成需要数据库的选择集；
  PostgreSQL 兼容 job 取其中 `uses_db and not sqlite_only` 的部分。
- `db_engine` / `db_session` / `db_factory`（内存）与 `file_db_factory`（文件）固定走 SQLite、
  不带 `uses_db`，供不进该选择集的 models 与 repositories 单测使用。
- 唯一的例外是 `async_session` 的 PostgreSQL 分支：它绑定 CI job 已 `alembic upgrade head` 建好的
  public schema，隔离原语是外层事务 + SAVEPOINT，与 `make_test_engine` 的 per-test schema +
  `create_all` 不同，故自建 engine。
"""

from __future__ import annotations

import os

# YouReel: the production default locale is English; upstream's tests are written against the Chinese default, so the
# suite runs with "zh" (set before any lib.i18n import). tests/unit/lib/i18n/test_youreel_default_locale.py checks "en".
# YouReel：生产默认语言为英文；上游测试按中文默认值编写，测试套件以 "zh" 运行（在 import lib.i18n 之前设置）。
os.environ.setdefault("ARCREEL_DEFAULT_LOCALE", "zh")

import asyncio
import atexit
import ipaddress
import os
import shutil
import socket
import sys
import tempfile
import uuid as _uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# The same entry point pytest uses at collection time to resolve positional command-line arguments and raise
# UsageError for missing ones; `pytest_sessionstart` below reuses it so session-start validation matches collection.
# pytest 收集期解析命令行位置参数、并对缺失项抛 UsageError 的同一入口；
# 下方 `pytest_sessionstart` 复用它，使会话启动期的校验与收集期同源。
from _pytest.main import resolve_collection_argument
from sqlalchemy import event, pool, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from tests.memory_db_guard import install_shared_connection_write_guard

# The module-level engine of `lib.db.engine` binds to `DATABASE_URL` at import time and is never rebuilt in the
# process, so the override must happen before any import below that reaches that module. If not set explicitly it
# lands in `projects/arcreel.db` at the repo root: one file shared by several xdist workers, with the schema built
# only incidentally by whichever test runs first, so tests have hidden ordering dependencies. Pin it to a temp DB
# owned by this process, with the schema built explicitly by `shared_db_schema`. Stay out of it when DATABASE_URL is
# given from outside (the postgres-compat job, alembic tests that monkeypatch it per test).
#
# xdist workers inherit environ from the controller, including the URL written here;
# `_OWNED_DB_MARKER` lets a worker recognize "minted by the tests, not given from outside" and mint its own, so each
# process owns its DB.
# `lib.db.engine` 的模块级 engine 在 import 期就按 `DATABASE_URL` 绑定，进程内不再重建，
# 所以覆写必须发生在下方任何会传染到该模块的 import 之前。未显式指定时它落在仓库根的
# `projects/arcreel.db`：一份文件被 xdist 的多个 worker 共用，且 schema 只由某个先跑到
# 的用例顺带建出——用例间因此存在隐式顺序依赖。钉到本进程独占的临时库上，schema 由
# `shared_db_schema` 显式建立。DATABASE_URL 已由外部给定（postgres-compat job、
# 逐个用例 monkeypatch 的 alembic 用例）时不介入。
#
# xdist 的 worker 从 controller 继承 environ，会连这里写下的 URL 一起带过去；
# `_OWNED_DB_MARKER` 让 worker 认出「这是测试自己铸的、不是外部给的」，各自另铸一份，
# 从而每个进程都独占一个库。
_OWNED_DB_MARKER = "ARCREEL_TEST_OWNED_DB"
_OWNED_TEST_DB_DIR: str | None = None
_OWNED_TEST_DB_OWNER_PID = os.getpid()


def _remove_owned_test_db_dir() -> None:
    """Clean up the temp DB only in the process that minted it.

    The atexit registry is copied to child processes on fork. If a pytest session that mutmut forks per mutant exits
    through the normal path rather than ``os._exit``, it runs the parent's registry; without the pid guard it would
    delete the DB shared by all parallel children, and every child's session-level DB fixture would then fail.

    只在铸造临时库的进程里回收它。

    atexit 登记表随 fork 复制到子进程。mutmut 按 mutant fork 出的 pytest 会话若走了
    正常退出路径而非 ``os._exit``，会带着父进程的登记表跑一遍；不设 pid 守卫时它会
    删掉所有并行子进程共用的库，之后每个子进程的会话级 DB fixture 都报错。
    """
    if _OWNED_TEST_DB_DIR is None or os.getpid() != _OWNED_TEST_DB_OWNER_PID:
        return
    shutil.rmtree(_OWNED_TEST_DB_DIR, ignore_errors=True)


if not os.environ.get("DATABASE_URL", "").strip() or os.environ.get(_OWNED_DB_MARKER) == "1":
    _OWNED_TEST_DB_DIR = tempfile.mkdtemp(prefix="arcreel-test-db-")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_OWNED_TEST_DB_DIR}/arcreel.db"
    os.environ[_OWNED_DB_MARKER] = "1"
    # Cleanup hangs off atexit rather than fixture teardown: `--collect-only` (CI's classification marker gate) and
    # collection-time interruption only import conftest and run no fixtures.
    # 回收挂在 atexit 而非 fixture teardown 上：`--collect-only`（CI 的分类 marker 闸门）
    # 与收集期中断都只 import conftest、不跑 fixture。
    atexit.register(_remove_owned_test_db_dir)

# Same for the data root and the Claude SDK config dir: the defaults are `projects/` at the repo root and the user's
# `~/.claude`, and layout migration moves directories and changes session dirs on them. Importing `server.app` at
# collection time creates the Assistant service singleton and fixes it to the data root at that moment, so they must
# be pinned to process-owned temp dirs before any import; each test is then switched to its own empty data root by
# `isolated_data_root`.
# 数据根与 Claude SDK 配置目录同理：默认值是仓库根下的 `projects/` 与用户的 `~/.claude`，
# 布局迁移会在其上挪目录、改会话目录。收集期 import `server.app` 就会建出 Assistant 服务
# 单例并按当时的数据根固定下来，所以须在任何 import 之前钉到本进程独占的临时目录；
# 每个用例再由 `isolated_data_root` 换成各自的空数据根。
_OWNED_TEST_HOME_DIR = tempfile.mkdtemp(prefix="arcreel-test-home-")
os.environ["ARCREEL_DATA_DIR"] = str(Path(_OWNED_TEST_HOME_DIR) / "data")
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_OWNED_TEST_HOME_DIR) / "claude-config")
os.environ.pop("AI_ANIME_PROJECTS", None)
# A deployment's engine token (server/engine_token.py) would turn every test request into a 401; the tests that need
# it set it themselves.
os.environ.pop("ENGINE_PROXY_TOKEN", None)

# Without AUTH_TOKEN_SECRET, lifespan reads or generates the signing key from the data root at startup and then caches
# it in the process. Tests that issue tokens without lifespan read/write the key file on the event loop the first time,
# and the cache is shared across tests, so which test hits the first read/write depends on execution order. Pin a
# fixed value; tests that verify key persistence delenv it themselves.
# 未设置 AUTH_TOKEN_SECRET 时，签名密钥由 lifespan 在启动期从数据根读出或生成，之后缓存在进程内。
# 不经 lifespan 直接签发 token 的用例会在事件循环上首次读写密钥文件，缓存又跨用例共享，
# 哪条用例撞上首次读写取决于执行顺序。钉一个固定值；验证密钥持久化的用例自行 delenv。
if not os.environ.get("AUTH_TOKEN_SECRET"):
    os.environ["AUTH_TOKEN_SECRET"] = "arcreel-test-token-secret-0123456789abcdef"

# On `import lib`, `lib.infra.env_init` reads .env: with no .env at the project root it walks upward, so a worktree
# under `.worktrees/` reads the main checkout's .env, whose provider keys make lifespan tests get stopped by the
# startup key check. Test results should not depend on a .env inside or outside the checkout, so the whole test
# process (and its children) does not read .env; tests that verify the read rules delenv it themselves.
# `import lib` 时 `lib.infra.env_init` 读取 .env：项目根没有 .env 就向上回溯，`.worktrees/` 下的
# worktree 会读到主检出的 .env，其中的 provider 密钥让走 lifespan 的用例被启动期密钥检查拦下。
# 测试结果不应依赖 checkout 里外的 .env，故整个测试进程（含其子进程）不读 .env；
# 验证读取规则的用例自行 delenv。
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

# lifespan's `ensure_auth_password` generates a password when AUTH_PASSWORD is unset and writes it back to the project
# root's .env. Pin a fixed value so lifespan tests that do not replace this step no longer rewrite the repo's .env;
# tests that verify generation and write-back clear AUTH_PASSWORD themselves and pass an env_path under tmp.
# lifespan 的 `ensure_auth_password` 在 AUTH_PASSWORD 未设置时生成口令并回写项目根的 .env。
# 钉一个固定值，未替换该步骤的 lifespan 用例不再改写仓库里的 .env；验证生成与回写的用例
# 自行清掉 AUTH_PASSWORD 并传入 tmp 下的 env_path。
if not os.environ.get("AUTH_PASSWORD"):
    os.environ["AUTH_PASSWORD"] = "arcreel-test-password"


def _remove_owned_test_home_dir() -> None:
    """Clean up only in the process that created the temp dir (pid guard for the same reason as
    ``_remove_owned_test_db_dir``).

    只在创建临时目录的进程里回收（pid 守卫的理由同 ``_remove_owned_test_db_dir``）。"""
    if os.getpid() == _OWNED_TEST_DB_OWNER_PID:
        shutil.rmtree(_OWNED_TEST_HOME_DIR, ignore_errors=True)


atexit.register(_remove_owned_test_home_dir)

import lib.generation.generation_queue as generation_queue_module
from lib.db.base import Base
from lib.db.engine import register_sqlite_functions
from lib.generation.video_request_facts import VideoRequestFacts, VideoRequestFactsFailure
from server.agent_runtime.session_manager import SessionManager
from server.agent_runtime.session_store import SessionMetaStore

# Event-loop blocking guard: active for sync and async tests throughout; blocking calls not on the exemption list fail
# the test, see tests/blocking_guard.
# Hang diagnostics: with ARCREEL_TEST_HANG_DUMP_DIR set, dump all threads' stacks when the process makes no progress for
# a long time, see tests/hang_watchdog.py.
# 事件循环阻塞护栏：同步与异步用例全程启用，豁免清单外的阻塞调用让用例失败，见 tests/blocking_guard。
# 挂起诊断：设了 ARCREEL_TEST_HANG_DUMP_DIR 时，进程长时间无进展就转储全部线程的调用栈，见 tests/hang_watchdog.py。
pytest_plugins = ["tests.blocking_guard", "tests.hang_watchdog"]


@pytest.fixture
def set_admission_video_request_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[VideoRequestFacts | VideoRequestFactsFailure], None]:
    """Make batch admission consume the video request facts the test provides explicitly.

    让批量准入消费测试显式提供的视频请求事实。"""
    from server.services.admission import video_batch_admission

    def configure(facts: VideoRequestFacts | VideoRequestFactsFailure) -> None:
        monkeypatch.setattr(video_batch_admission, "evaluate_video_request_facts", AsyncMock(return_value=facts))

    return configure


VideoRequestFactsResult = VideoRequestFacts | VideoRequestFactsFailure


@pytest.fixture
def set_video_request_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[VideoRequestFactsResult | Mapping[str, VideoRequestFactsResult]], None]:
    """Make video-capability consumers read fact results built by the test, while evaluation tests still use the real
    resolver.

    With a single result, every bucket gets the same one; with a mapping keyed by task-type bucket (``"i2v"`` /
    ``"r2v"``), answer per bucket, for consumer tests where the two buckets are configured differently.

    让视频能力消费方读取测试构造的事实结果，求值测试仍使用真实解析器。

    传单个结果时所有桶同一份；传按任务类型桶（``"i2v"`` / ``"r2v"``）索引的映射时按桶作答，
    供两桶配置不同的消费方用例。
    """
    from lib.script import script_generator
    from lib.script.reference_video import request_projection
    from server.services.admission import cost_estimation
    from server.services.tasks import video_caps

    def configure(facts: VideoRequestFactsResult | Mapping[str, VideoRequestFactsResult]) -> None:
        if isinstance(facts, Mapping):
            by_bucket = dict(facts)

            async def evaluate(_project, *, generation_type, **_kwargs):
                return by_bucket[generation_type]

            fake = AsyncMock(side_effect=evaluate)
        else:
            fake = AsyncMock(return_value=facts)
        for consumer in (script_generator, request_projection, cost_estimation, video_caps):
            monkeypatch.setattr(consumer, "evaluate_video_request_facts", fake)

    return configure


@pytest.fixture
def video_request_facts(set_video_request_facts) -> None:
    """Provide a fixed i2v tier for consumer tests that do not care about duration branches.

    为无关时长分支的消费方用例提供确定的 i2v 档位。"""
    from tests.factories import make_video_request_facts

    set_video_request_facts(make_video_request_facts(route="reference_video", generation_type="i2v"))


@pytest.fixture
def set_error_handler_sessions(monkeypatch: pytest.MonkeyPatch) -> Callable[[async_sessionmaker[AsyncSession]], None]:
    """Make the app-level error handler use the test's session factory when loading the display-name catalog.

    The error handler gets no dependency injection and opens its own session (see
    ``server.i18n.request_display_names``), which ``dependency_overrides[get_async_session]`` does not reach.

    让 app 级错误处理器加载显示名目录时用测试给定的会话工厂。

    错误处理器拿不到依赖注入，自开会话（见 ``server.i18n.request_display_names``），
    ``dependency_overrides[get_async_session]`` 管不到它。
    """
    from server import i18n

    def configure(sessions: async_sessionmaker[AsyncSession]) -> None:
        monkeypatch.setattr(i18n, "async_session_factory", sessions)

    return configure


def _discard_pooled_connections_in_forked_child() -> None:
    """In a forked child, discard the connections inherited from the parent in the module-level engine pool.

    Each aiosqlite connection is driven by a dedicated worker thread, and fork only copies the calling thread, so in the
    child the pooled connections' worker threads do not exist and any query sent through them never gets a result.
    ``close=False`` only drops the references without closing the connections, leaving the parent's connections intact.
    mutmut forks per mutant to run tests, and the child's first test that goes through ``lib.db.engine`` would hang until
    timeout. If the parent never imported that module, the pool has no connections to drop, and engine creation is not
    triggered in the child either.

    fork 出的子进程丢弃模块级 engine 池里从父进程继承的连接。

    aiosqlite 每个连接由一条专属工作线程驱动，fork 只复制调用线程，子进程里池中
    连接的工作线程不存在，经它发出的任何查询都永远等不到结果。``close=False``
    只丢引用不关连接，父进程那份连接不受影响。mutmut 按 mutant fork 跑测试，
    子进程首个走 ``lib.db.engine`` 的用例会就此挂到超时。父进程从未 import 过
    该模块时池中无连接可丢，也不在子进程里触发 engine 创建。
    """
    engine_module = sys.modules.get("lib.db.engine")
    if engine_module is None:
        return
    engine_module.async_engine.sync_engine.dispose(close=False)


_fork_hook_registered = False


@pytest.fixture(scope="session", autouse=True)
def discard_pooled_connections_after_fork() -> None:
    """Hook ``_discard_pooled_connections_in_forked_child`` into this process's fork hooks.

    The hook is process-level and cannot be unregistered; mutmut opens several pytest sessions in the same parent before
    forking per mutant, so register only once.

    把 ``_discard_pooled_connections_in_forked_child`` 挂到本进程的 fork 钩子上。

    钩子是进程级、不可注销的；mutmut 会在同一父进程里连开几个 pytest 会话再按 mutant
    fork，故只注册一次。
    """
    global _fork_hook_registered
    if _fork_hook_registered or not hasattr(os, "register_at_fork"):
        return
    os.register_at_fork(after_in_child=_discard_pooled_connections_in_forked_child)
    _fork_hook_registered = True


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Each test uses an empty temp data root by default.

    ``app_data_dir()`` and ``get_project_manager()`` are cached in the process; clear them before and after each test,
    so that after a test setenvs ``ARCREEL_DATA_DIR`` itself, the first call resolves the new value. Autouse fixtures that
    import app modules must declare a dependency on this fixture so that resolution triggered meanwhile lands on the temp
    root.

    每个用例默认使用一个空的临时数据根。

    ``app_data_dir()`` 与 ``get_project_manager()`` 在进程内缓存，用例前后都清掉，
    用例自己 setenv ``ARCREEL_DATA_DIR`` 后首次调用即按新值解析。会 import 应用模块的
    autouse fixture 须声明依赖本 fixture，其间触发的解析才落在临时根上。
    """
    from lib.infra.app_data_dir import reset_for_tests
    from lib.project.project_manager import reset_project_manager_for_tests

    monkeypatch.setenv("ARCREEL_DATA_DIR", str(tmp_path_factory.mktemp("data-root")))
    reset_for_tests()
    reset_project_manager_for_tests()
    yield
    reset_for_tests()
    reset_project_manager_for_tests()


@pytest.fixture(autouse=True)
def stub_sandbox_check(isolated_data_root, monkeypatch, request):
    """Mock ``check_sandbox_available`` to return True so the test machine need not pass the real bwrap probe.

    On the GitHub Actions Ubuntu 24.04 runner, ``apparmor_restrict_unprivileged_userns=1`` makes the bwrap probe of
    ``server.app.check_sandbox_available`` fail to start, taking down every test that goes through the FastAPI lifespan
    (TestClient / lifespan / startup hook integration tests). Tests should not depend on the host being able to run
    unprivileged user namespaces; the function's own contract is covered separately by
    ``tests/unit/server/test_startup_assertions.py`` (with a finer subprocess.run stub). That file needs the real
    function, so this autouse stub is skipped by file name.

    Mock ``check_sandbox_available`` 返回 True，避免测试机不满足真实 bwrap probe。

    GitHub Actions Ubuntu 24.04 runner 上 ``apparmor_restrict_unprivileged_userns=1``
    会让 ``server.app.check_sandbox_available`` 的 bwrap probe 启动失败，连带
    把任何走 FastAPI lifespan 的测试（TestClient / lifespan / startup hook 集成测试）
    全部拖崩。测试本不该依赖 host 能跑非特权 user namespace；该函数本身的契约
    由 ``tests/unit/server/test_startup_assertions.py`` 独立覆盖（用更精细的 subprocess.run
    stub）— 那个文件需要走真实函数，故按文件名跳过此 autouse stub。
    """
    if request.path.name == "test_startup_assertions.py":
        return
    monkeypatch.setattr("server.app.check_sandbox_available", lambda: True)


@pytest.fixture(autouse=True)
def offline_official_catalog_sync(isolated_data_root, monkeypatch):
    """Replace the official model-catalog sync started by the app lifespan with an empty task that sends no requests.

    The real lifespan immediately fetches the manifest from the catalog data repo's raw URL, making test results depend
    on the internet and network timing: offline, the fetch fails at once, writes the sync table in the background and
    changes the in-process sync state; online, it may hang on connecting and hold up shutdown. Only the entry point
    referenced by lifespan is replaced; autouse keeps future lifespan tests from going out too; the empty task is still
    cancelled by ``stop_official_sync``. The sync behavior itself is covered by
    ``tests/integration/lib/catalog/test_official_sync.py`` calling ``lib.catalog.official_sync`` directly, unaffected.

    应用 lifespan 启动的模型目录官方同步换成不发请求的空任务。

    真实 lifespan 会立刻向目录数据仓库的 raw 地址取清单文件，用例结果随之依赖外网与网络时序：
    断网时抓取立即失败、在后台写同步表并改进程内的同步状态，联网时又可能卡在建连上拖住关停。
    这里只换 lifespan 引用的入口，autouse 让以后新增的 lifespan 用例同样不外发；空任务照常由
    ``stop_official_sync`` 取消。同步本身的行为由 ``tests/integration/lib/catalog/test_official_sync.py``
    直接调用 ``lib.catalog.official_sync`` 覆盖，不受影响。
    """

    def start_offline(*args, **kwargs) -> asyncio.Task[bool]:
        return asyncio.get_running_loop().create_task(asyncio.Event().wait(), name="catalog-official-sync-offline")

    monkeypatch.setattr("server.app.start_official_sync", start_offline)


#: The address every hostname resolves to in tests (TEST-NET-3, a public, non-routable range).
#: 测试内主机名统一解析到的地址（TEST-NET-3，公网段、不可路由）。
_OFFLINE_DNS_ADDRESS = "203.0.113.10"


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """The event loop's ``getaddrinfo`` answers ``_OFFLINE_DNS_ADDRESS`` for any hostname and sends no real DNS query.

    The artifact download entry resolves the target host before each request; outbound traffic is intercepted by respx
    at the transport layer, but the resolution step would still hit the local resolver. IP literals and ``localhost``
    (local database, test servers) still resolve for real.

    事件循环的 ``getaddrinfo`` 对主机名一律回 ``_OFFLINE_DNS_ADDRESS``，不发真实 DNS 查询。

    产物下载入口在每次请求前解析目标主机；出站流量由 respx 在 transport 层拦截，解析这一步
    却会落到本机解析器上。IP 字面量与 ``localhost``（本地数据库、测试服务器）仍走真实解析。
    """
    real_getaddrinfo = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def getaddrinfo(self, host, port, *args, **kwargs):
        if host is None or host == "localhost" or _is_ip_literal(host):
            return await real_getaddrinfo(self, host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_OFFLINE_DNS_ADDRESS, port or 0))]

    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", getaddrinfo)


def _is_ip_literal(host: str | bytes) -> bool:
    try:
        ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
    except ValueError:
        return False
    return True


@pytest.fixture(scope="session", autouse=True)
def profile_env(tmp_path_factory):
    """Provide one minimal runtime profile per pytest worker.

    Tests exercising another profile location set ``ARCREEL_PROFILE_DIR`` explicitly.
    """
    profile_dir = tmp_path_factory.mktemp("agent-runtime-profile")
    (profile_dir / "CLAUDE.md").write_text("", encoding="utf-8")

    previous = os.environ.get("ARCREEL_PROFILE_DIR")
    os.environ["ARCREEL_PROFILE_DIR"] = str(profile_dir)
    yield
    if previous is None:
        os.environ.pop("ARCREEL_PROFILE_DIR", None)
    else:
        os.environ["ARCREEL_PROFILE_DIR"] = previous


@pytest.fixture
def fd_count():
    """Return a callable that reports the current process file-descriptor count.

    Returns -1 on platforms where /dev/fd and /proc/self/fd are unavailable.
    """

    def _count() -> int:
        for fd_dir in ("/dev/fd", "/proc/self/fd"):
            try:
                return len(os.listdir(fd_dir))
            except OSError:
                continue
        return -1

    return _count


# ---------------------------------------------------------------------------
# Shared database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def shared_db_schema():
    """Migrate the DB the module-level engine points to up to head so any test can use it directly.

    Only runs on the temp DB this conftest created: when DATABASE_URL is given from outside (postgres-compat job), that
    job's alembic step owns the schema. It uses alembic rather than ``create_all`` because ``lib.db.init_db()`` stamps
    base and then upgrades a DB that "has tables but no alembic_version", and a preset create_all schema would make it
    create tables twice.

    把模块级 engine 所指的库迁到 head，使任何用例都能直接用它。

    只对本 conftest 自建的临时库执行：外部给定 DATABASE_URL 时（postgres-compat job）
    schema 由该 job 的 alembic 步骤负责。走 alembic 而非 ``create_all``，因为
    ``lib.db.init_db()`` 对「有表无 alembic_version」的库会先 stamp base 再 upgrade，
    预置的 create_all schema 会让它重复建表。
    """
    if _OWNED_TEST_DB_DIR is None:
        return

    from alembic.config import Config

    from alembic import command

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "alembic"))
    command.upgrade(cfg, "head")


def _pg_url_from_env() -> str | None:
    """Return DATABASE_URL iff it's a PostgreSQL+asyncpg URL, else None."""
    url = os.environ.get("DATABASE_URL")
    if url and url.startswith("postgresql+asyncpg://"):
        return url
    return None


# Test fixtures attribute writes to a small set of fixed user_ids; seed them
# on PG so FK constraints (which SQLite tests bypass via PRAGMA foreign_keys=OFF)
# don't reject inserts.
_PG_TEST_USER_IDS = ("default", "u1", "conformance", "e2e", "crash-recover", "long-turn")


async def _seed_pg_users(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for uid in _PG_TEST_USER_IDS:
            await conn.execute(
                text(
                    "INSERT INTO users (id, username, role, is_active, created_at, updated_at) "
                    "VALUES (:id, :username, 'user', true, NOW(), NOW()) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": uid, "username": uid},
            )


def _register_models() -> None:
    """Register all ORM models in ``Base.metadata``.

    Otherwise which tables get created depends on the import chain of the module under test, and the same fixture builds
    different schemas in different files.

    把全部 ORM 模型登记到 ``Base.metadata``。

    不这么做时建表范围取决于被测模块的 import 链，同一 fixture 在不同文件下建出的
    schema 不同。
    """
    from lib.agent.agent_session_store.models import register_models as register_agent_session_models
    from lib.db.models import register_models as register_db_models

    register_agent_session_models()
    register_db_models()


@asynccontextmanager
async def make_test_engine(*, dialect_aware: bool = True, file_path: Path | None = None) -> AsyncGenerator[AsyncEngine]:
    """Engine construction point for DB fixtures; the only exception is the PostgreSQL branch of `async_session`.

    That branch binds the public schema the CI job built with `alembic upgrade head`, and its isolation primitive is an
    outer transaction + SAVEPOINT, unlike the per-test schema + `create_all` here, so it builds its own engine.

    With ``dialect_aware`` and ``DATABASE_URL`` pointing to PG, create a per-test schema, create tables via search_path,
    seed the user row the FKs depend on, and ``DROP SCHEMA CASCADE`` on exit; this gives real coverage of
    dialect-specific code paths (partial unique index + ON CONFLICT, SELECT ... FOR UPDATE).

    Otherwise build SQLite: an in-memory DB when ``file_path`` is None; with a path, a NullPool file DB for concurrent
    tests that must avoid StaticPool serialization. The in-memory DB gets the concurrent-write check of
    ``tests.memory_db_guard``: when several tasks write at once the test fails and suggests the matching file DB fixture.
    ``dialect_aware=False`` always uses SQLite and is not rerouted even in the postgres-compat job: unit-tier tests
    should not be pulled into running on PG.

    DB fixture 的 engine 构造点，唯一例外是 `async_session` 的 PostgreSQL 分支。

    那条分支绑定 CI job 已 `alembic upgrade head` 建好的 public schema，隔离原语是外层
    事务 + SAVEPOINT，与这里的 per-test schema + `create_all` 不同，故自建 engine。

    ``dialect_aware`` 且 ``DATABASE_URL`` 指向 PG 时建 per-test schema 并按 search_path
    建表、播种 FK 依赖的 user 行，退出时 ``DROP SCHEMA CASCADE``——方言相关代码路径
    （partial unique index + ON CONFLICT、SELECT ... FOR UPDATE）由此被真实覆盖。

    其余情况建 SQLite：``file_path`` 为 None 时用内存库；给了路径则用 NullPool 文件库，
    供必须绕开 StaticPool 串行化的并发用例。内存库挂 ``tests.memory_db_guard`` 的并发写检查：
    多个任务同时写库时用例失败，提示改用对应的文件库夹具。``dialect_aware=False`` 固定走 SQLite，
    postgres-compat job 里也不改道——unit 档用例不该被拉去跑 PG。
    """
    pg_url = _pg_url_from_env() if dialect_aware else None
    if pg_url:
        schema = f"test_{_uuid.uuid4().hex[:12]}"
        engine = create_async_engine(pg_url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        async with engine.begin() as conn:
            _register_models()
            await conn.run_sync(Base.metadata.create_all)
        # PG enforces FK constraints (SQLite tests run with PRAGMA foreign_keys=OFF).
        await _seed_pg_users(engine)
        try:
            yield engine
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await engine.dispose()
        return

    write_guard = None
    if file_path is None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        # All sessions of the in-memory DB share one connection; concurrent writes from several tasks fail right away and point
        # to the matching file DB fixture.
        # 内存库所有 session 共用一个连接，多个任务并发写时直接失败并指向对应的文件库夹具。
        write_guard = install_shared_connection_write_guard(
            engine, file_fixture="concurrent_session_factory" if dialect_aware else "file_db_factory"
        )
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{file_path}", poolclass=pool.NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        register_sqlite_functions(dbapi_conn)
        if file_path is None:
            return
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    async with engine.begin() as conn:
        _register_models()
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
    if write_guard is not None:
        write_guard.raise_if_violated()


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """In-memory SQLite engine: the shared entry for models / repositories unit tests.

    内存 SQLite engine —— models / repositories 单测的共享入口。"""
    async with make_test_engine(dialect_aware=False) as engine:
        yield engine


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """AsyncSession on ``db_engine``.

    ``db_engine`` 上的 AsyncSession。"""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def file_db_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """File SQLite + NullPool factory: for concurrent tests that need independent connections.

    The only difference from ``file_session_factory`` is that it carries no `uses_db`: unit-tier concurrent tests should
    not be pulled into the postgres-compat selection.

    文件 SQLite + NullPool 的 factory —— 需要独立连接的并发用例用它。

    与 ``file_session_factory`` 的差别只在不带 `uses_db`：unit 档的并发用例
    不该被拉进 postgres-compat 选集。
    """
    async with make_test_engine(dialect_aware=False, file_path=tmp_path / "concurrency.db") as engine:
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory on ``db_engine``.

    ``db_engine`` 上的 session factory。"""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def custom_providers_app_session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """The session factory bound by ``custom_providers_app``; tests also use it directly to preset endpoint and model rows.

    ``custom_providers_app`` 绑定的 session factory；用例也直接用它预置端点与模型行。"""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
def custom_providers_app(custom_providers_app_session_factory):
    """A FastAPI app that mounts only the custom provider routes, binds an in-memory DB and skips auth as admin.

    Three test files (protocol-agnostic CRUD, capability overrides, ComfyUI protocol) are split by behavior area and
    share the same app under test. The import sits inside the function body: the root conftest is loaded by the whole
    test session and should not pull the server package into every collection for three files.

    只挂自定义供应商路由、绑内存库、以管理员身份免鉴权的 FastAPI 应用。

    三个测试文件（协议无关的 CRUD、能力覆盖、ComfyUI 协议）按行为域分文件，共用的是同一个
    被测应用。import 放在函数体内：根 conftest 由整个测试会话加载，不该为三个文件把 server
    包拉进每一次收集。
    """
    from fastapi import FastAPI

    from lib.db import get_async_session
    from server.auth import CurrentUserInfo, get_current_user
    from server.error_handlers import register_error_handlers
    from server.routers import custom_providers
    from tests.auth_deps import AUTH_DEPENDENCIES

    app = FastAPI()

    async def _override_session():
        async with custom_providers_app_session_factory() as db_session:
            yield db_session

    app.dependency_overrides[get_async_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="test", sub="test", role="admin")
    app.include_router(custom_providers.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    register_error_handlers(app)
    return app


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Dialect-sensitive session factory: per-test schema under PG, otherwise in-memory SQLite.

    方言敏感的 session factory：PG 下走 per-test schema，否则内存 SQLite。"""
    async with make_test_engine() as engine:
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def concurrent_session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Dialect-sensitive and allows independent connections: real schema under PG, WAL file DB for local SQLite.

    方言敏感且允许独立连接：PG 走真实 schema，本地 SQLite 走 WAL 文件库。"""
    async with make_test_engine(file_path=tmp_path / "concurrency-aware.db") as engine:
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def file_session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """File-backed SQLite with NullPool — each connection is independent.

    Required for concurrency tests that must NOT serialize via StaticPool
    (which is the default for ``sqlite+aiosqlite:///:memory:``).

    Always SQLite regardless of ``DATABASE_URL`` — tests that depend on this
    fixture are SQLite-specific edge cases marked ``@pytest.mark.sqlite_only``.
    """
    async with make_test_engine(dialect_aware=False, file_path=tmp_path / "concurrency.db") as engine:
        yield async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# SessionManager family (used by 3+ test files)
# ---------------------------------------------------------------------------


@pytest.fixture
async def meta_store(db_factory: async_sessionmaker[AsyncSession]) -> SessionMetaStore:
    """Create an async SessionMetaStore backed by in-memory SQLite."""
    return SessionMetaStore(session_factory=db_factory)


@pytest.fixture
async def session_manager(tmp_path: Path, meta_store: SessionMetaStore) -> SessionManager:
    """Create a SessionManager wired to *tmp_path* and *meta_store*."""
    return SessionManager(
        project_root=tmp_path,
        meta_store=meta_store,
    )


# ---------------------------------------------------------------------------
# Collection-time hooks: tier marker injection and boundary checks
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 收集期钩子：档位 marker 注入与边界校验
# ---------------------------------------------------------------------------


TESTS_ROOT = Path(__file__).parent
CLASSIFICATION_MARKS = ("unit", "integration", "e2e")


def _tier_from_path(item: pytest.Item) -> str | None:
    """Name of the tier directory the test lives in (the first segment of `tests/unit|integration|e2e/…`).

    用例所在的档位目录名（`tests/unit|integration|e2e/…` 的第一段）。"""
    try:
        rel = Path(str(item.path)).relative_to(TESTS_ROOT)
    except ValueError:
        return None
    head = rel.parts[0] if rel.parts else ""
    return head if head in CLASSIFICATION_MARKS else None


def _validate_selection_args(config: pytest.Config) -> None:
    """Pass every positional command-line argument to pytest's own resolver; missing paths or modules raise UsageError.

    The criteria come entirely from ``resolve_collection_argument``: path existence, ``--pyargs`` module resolution
    (including whether a namespace package is accepted per ``consider_namespace_packages``), no ``::`` selection segment
    on directories, the position of ``[]`` parametrization, all consistent with collection time.

    把每个命令行位置参数交给 pytest 自身的解析器，缺失的路径或模块抛 UsageError。

    判据完全取自 ``resolve_collection_argument``：路径存在性、``--pyargs`` 的模块解析
    （含 namespace package 是否按 ``consider_namespace_packages`` 接受）、目录不得带
    ``::`` 选择段、``[]`` 参数化的位置，都与收集期一致。
    """
    invocation_path = config.invocation_params.dir
    as_pypath = bool(config.getoption("pyargs"))
    consider_namespace_packages = bool(config.getini("consider_namespace_packages"))
    for index, arg in enumerate(config.args):
        resolve_collection_argument(
            invocation_path,
            arg,
            index,
            as_pypath=as_pypath,
            consider_namespace_packages=consider_namespace_packages,
        )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Raise a usage error right away when a targeted selection contains a nonexistent path or module.

    Without -n, pytest itself reports the same UsageError at collection time and exits with 4; under xdist the
    controller only aggregates results from workers, drops the missing selection together with the real files in the
    same batch, and leaves only "no tests ran" and exit code 5, without any error line. Both modes are unified into
    failing loud before collection.

    定向选择里有不存在的路径或模块时直接报用法错误。

    不带 -n 时 pytest 自己会在收集期报出同样的 UsageError 并以 4 退出；xdist 下
    controller 只从 worker 汇总结果，缺失选择连同同批的真实文件一起丢掉，只留下
    「no tests ran」与退出码 5，没有任何错误行。两种模式统一为收集前 fail loud。
    """
    _validate_selection_args(session.config)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Inject classification markers by directory, then inject `uses_db` by fixture origin, and finally run the
    collection-time checks.

    The classification marker is decided by the test's `tests/unit|integration|e2e/` segment and is not written by hand.
    `uses_db` is injected only when the test consumes this conftest's dialect-sensitive fixtures (`async_session` /
    `session_factory` / `concurrent_session_factory` / `file_session_factory`); when a test locally overrides a fixture
    of the same name with a hard-coded SQLite engine, it is not injected, so the postgres-compat job keeps a real dialect
    signal instead of counting SQLite-only code toward the `postgres` coverage flag.

    按目录注入分类 marker，再按 fixture 来源注入 `uses_db`，最后跑收集期校验。

    分类 marker 由用例所在的 `tests/unit|integration|e2e/` 段决定，不手写。
    `uses_db` 只在用例消费本 conftest 的方言敏感 fixture（`async_session` /
    `session_factory` / `concurrent_session_factory` / `file_session_factory`）时注入；用例在本地覆写同名 fixture
    为硬编码 SQLite engine 时不注入，使 postgres-compat job 保持真实的方言信号，
    而不是把只跑 SQLite 的代码算进 `postgres` 覆盖率标记。
    """
    for item in items:
        tier = _tier_from_path(item)
        if tier is not None:
            item.add_marker(getattr(pytest.mark, tier))

    target_fixtures = {"async_session", "session_factory", "concurrent_session_factory", "file_session_factory"}
    canonical_modules = {"tests.conftest"}
    uses_db = pytest.mark.uses_db
    for item in items:
        info = getattr(item, "_fixtureinfo", None)
        if info is None:
            continue
        fixturenames = set(getattr(item, "fixturenames", ()) or ())
        for fname in target_fixtures & fixturenames:
            # `name2fixturedefs[fname]` is pytest's fixture override chain
            # (general → specific). The last element is the definition that
            # actually wins for this test; only it determines whether the
            # test really hits a dialect-sensitive engine.
            defs = info.name2fixturedefs.get(fname) or ()
            if not defs:
                continue
            active = defs[-1]
            if getattr(active.func, "__module__", "") in canonical_modules:
                item.add_marker(uses_db)
                break

    _enforce_classification_markers(items)
    _skip_local_port_tests_without_bind_permission(items)


def _skip_local_port_tests_without_bind_permission(items: list[pytest.Item]) -> None:
    """When the environment forbids binding 127.0.0.1, mark `local_port` tests as skip with a reason.

    The Agent sandbox may refuse to bind local ports; there such tests would only fail with `PermissionError`, so skip
    them with a reason, prompting a rerun in an environment that allows binding ports.

    环境禁止绑定 127.0.0.1 时，把 `local_port` 用例标成带原因的 skip。

    Agent 沙箱可能拒绝绑定本地端口；这类用例在那里只会以 `PermissionError` 失败，
    跳过并注明原因，提示到允许绑定端口的环境里再跑一次。
    """
    port_items = [item for item in items if item.get_closest_marker("local_port") is not None]
    if not port_items:
        return
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
    except PermissionError:
        skip = pytest.mark.skip(reason="当前环境禁止绑定 127.0.0.1 上的端口；在允许绑定本地端口的环境中运行此用例")
        for item in port_items:
            item.add_marker(skip)


def _enforce_classification_markers(items: list[pytest.Item]) -> None:
    """Enforced at collection time: exactly one classification marker, coming from the directory, and the `unit` tier must
    not touch a real database.

    Classification markers are injected by directory, so "missing" means the test is not under one of the three tier
    directories, and "extra" means the file still has a hand-written marker conflicting with the directory; both are
    stopped at collection time and never reach CI. `uses_db` and `unit` together is the tier boundary itself: the unit
    tier forbids a real DB, and a hit means the test is in the wrong directory.

    收集期强制：分类 marker 恰好一个且来自目录，`unit` 档不得触达真实数据库。

    分类 marker 由目录注入，所以「缺失」等价于用例不在三个档位目录之下，「多标」
    等价于文件里还留着与目录相冲突的手写 marker——两者都拦在收集期，不进 CI。
    `uses_db` ∧ `unit` 是档位边界本身：unit 档禁真实 DB，命中说明用例放错了目录。
    """
    missing = []
    conflicting = []
    db_in_unit = []
    for item in items:
        marks = {m.name for m in item.iter_markers()}
        classify = marks & set(CLASSIFICATION_MARKS)
        if not classify:
            missing.append(item.nodeid)
        elif len(classify) > 1:
            conflicting.append(f"{item.nodeid}（{'/'.join(sorted(classify))}）")
        if "uses_db" in marks and "unit" in classify:
            db_in_unit.append(item.nodeid)
    problems = []
    if missing:
        listing = "\n".join(f"  - {nodeid}" for nodeid in missing)
        problems.append(
            f"{len(missing)} 个测试用例不在档位目录下（分类 marker 由目录注入）：\n{listing}\n"
            "把文件移到 tests/unit|integration|e2e/<源码目录镜像>/ 下。"
        )
    if conflicting:
        listing = "\n".join(f"  - {entry}" for entry in conflicting)
        problems.append(
            f"{len(conflicting)} 个测试用例带多个分类 marker（三者互斥）：\n{listing}\n"
            "删掉用例/类/模块三层中手写的分类 marker，档位只由目录决定。"
        )
    if db_in_unit:
        listing = "\n".join(f"  - {nodeid}" for nodeid in db_in_unit)
        problems.append(
            f"{len(db_in_unit)} 个 unit 档用例触达真实数据库（`uses_db`）：\n{listing}\n"
            "unit 档禁真实 DB，把文件移到 tests/integration/ 下的镜像位置。"
        )
    if problems:
        raise pytest.UsageError("\n".join(problems))


@pytest.fixture
async def async_session():
    """Generic AsyncSession for repository tests.

    PG (DATABASE_URL=postgresql+...): trusts that ``alembic upgrade head`` has
    already created the schema (CI job does this before pytest). Each test
    opens a fresh NullPool engine, an outer transaction, and uses SAVEPOINT
    semantics so any `session.commit()` is contained — teardown ROLLBACKs the
    outer transaction, so data writes never persist.

    SQLite (default): each test gets a fresh in-memory engine + ORM
    ``create_all`` — engine is throwaway, no isolation primitive needed.
    """
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        # The only branch that does not use `make_test_engine`: it binds the public schema the CI job built with alembic,
        # while `make_test_engine` builds a per-test schema with create_all; the two have different isolation primitives.
        # 唯一不走 `make_test_engine` 的分支：它绑定 CI job 已 alembic 建好的 public schema，
        # 而 `make_test_engine` 建 per-test schema 并 create_all，两者的隔离原语不同。
        # Per-test engine with NullPool: avoids cross-event-loop reuse of
        # asyncpg connections (each pytest-asyncio test runs on a fresh loop).
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                outer = await conn.begin()
                try:
                    factory = async_sessionmaker(
                        bind=conn,
                        expire_on_commit=False,
                        join_transaction_mode="create_savepoint",
                    )
                    async with factory() as session:
                        yield session
                finally:
                    await outer.rollback()
        finally:
            await engine.dispose()
        return

    # SQLite in-memory — engine is throwaway, ORM-driven schema.
    async with make_test_engine(dialect_aware=False) as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session


# ---------------------------------------------------------------------------
# GenerationQueue family (used by 2+ test files)
# ---------------------------------------------------------------------------


@pytest.fixture
async def generation_queue(db_factory: async_sessionmaker[AsyncSession]):
    """Create an async GenerationQueue backed by in-memory SQLite.

    Automatically resets the module singleton on teardown.
    """
    queue = generation_queue_module.GenerationQueue(session_factory=db_factory)
    generation_queue_module._QUEUE_INSTANCE = queue
    yield queue
    generation_queue_module._QUEUE_INSTANCE = None


# ---------------------------------------------------------------------------
# Polling / retry clock (used by 2+ test files)
# ---------------------------------------------------------------------------


@pytest.fixture
def poll_clock():
    """Fixture form of ``bounded_poll_clock``: all polling and backoff waits of the whole test use the fake clock.

    Compression semantics and the timeout safety net are identical to the context-manager form; suitable when the whole
    test needs the fake clock.

    ``bounded_poll_clock`` 的 fixture 形态：整条用例的轮询与退避等待都走假表。

    压缩语义与超时兜底与上下文管理器形态完全一致，适用于整条用例都需要假表的场景。
    """
    from tests.fakes import bounded_poll_clock

    with bounded_poll_clock():
        yield


# ---------------------------------------------------------------------------
# Edit timeline projects (final cut and Jianying draft tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def timeline_project(tmp_path: Path):
    """Project ``demo`` in reference-to-video mode: episode 1 has two video units E1U1 and E1U2, no videos yet; returns its
    ProjectManager.

    参考生视频模式的项目 ``demo``：第 1 集含 E1U1、E1U2 两个视频单元，尚无视频；返回其 ProjectManager。"""
    from lib.project.project_manager import ProjectManager

    manager = ProjectManager(tmp_path / "projects")
    manager.create_project("demo")
    manager.create_project_metadata("demo", "Demo", "Anime", "narration")
    manager.update_project("demo", lambda project: project.update({"generation_mode": "reference_video"}))
    manager.save_script(
        "demo",
        {
            "episode": 1,
            "title": "第一集",
            "content_mode": "narration",
            "generation_mode": "reference_video",
            "summary": "摘要",
            "novel": {"title": "小说", "chapter": "第一章"},
            "video_units": [
                {"unit_id": unit_id, "text": f"镜头 {unit_id}", "duration_seconds": 4} for unit_id in ("E1U1", "E1U2")
            ],
        },
        "episode_1.json",
    )
    return manager
