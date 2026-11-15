"""SessionManager sandbox wiring tests: options assembly, hook return format, permission chain order.

Pure rule assertions (path rulings / settings compilation / allowlist predicates / key-stripping transform) have moved to
tests/agent_runtime/test_agent_access_policy.py; this file only tests the wiring on the SDK wrapper side.

SessionManager sandbox 接线测试：options 装配、hook 返回格式、权限链顺序。

纯规则断言（路径裁决 / settings 编译 / 白名单谓词 / 密钥剥离变换）已搬家至
tests/agent_runtime/test_agent_access_policy.py；本文件只测 SDK 封皮侧的接线。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from lib.agent.agent_memory_paths import project_memory_dir
from server.agent_runtime.agent_access_policy import AgentAccessPolicy
from server.agent_runtime.session_manager import ManagedSession, SessionManager
from server.agent_runtime.session_store import SessionMetaStore
from tests.fakes import use_provider_env

# Current user id passed per call (in production it comes from SessionManager's CurrentUser context).
#: 逐调用传入的当前用户 id（生产取自 SessionManager 的 CurrentUser 上下文）。
_USER_ID = "default"


@pytest.fixture
def fs_session_manager(tmp_path: Path) -> SessionManager:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "projects").mkdir()
    meta_store = SessionMetaStore()
    return SessionManager(project_root, meta_store)


@pytest.mark.asyncio
async def test_build_options_includes_sandbox_settings(
    fs_session_manager: SessionManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proj_dir = fs_session_manager.layout.projects_dir / "test_proj"
    proj_dir.mkdir(parents=True)
    (proj_dir / "project.json").write_text('{"title": "t"}', encoding="utf-8")

    async def fake_env():
        return {"ANTHROPIC_API_KEY": "sk", "ARK_API_KEY": ""}

    use_provider_env(monkeypatch, fake_env)

    opts = await fs_session_manager._build_options("test_proj")

    assert opts.sandbox is not None
    assert opts.sandbox.get("enabled") is True
    assert opts.sandbox.get("autoAllowBashIfSandboxed") is True
    # Outside Docker, weakerNested=False by default / 非 Docker 默认 weakerNested=False
    assert opts.sandbox.get("enableWeakerNestedSandbox") is False
    # Outbound explicitly fully allowed: omitting the network key means zero pre-allowance, so new domains would be denied outright in unattended sessions.
    # loopback has its own switch: skill scripts calling the same-origin ArcReel API must go through loopback,
    # and allowedDomains cannot cover it whether it says * or 127.0.0.1.
    # 出站显式全放行：省略 network 键等于零预放行，无人值守会话里新域名会被直接拒。
    # loopback 单列开关——skill 脚本调同源 ArcReel API 必须经 loopback，
    # 而 allowedDomains 无论写 * 还是写 127.0.0.1 都覆盖不到它。
    assert opts.sandbox["network"] == {"allowedDomains": ["*"], "allowLocalBinding": True}
    # Disabling the unsandboxed fallback and the file fence are unaffected by opening the network / 禁 unsandboxed fallback 与文件围栏不受网络放开影响
    assert opts.sandbox.get("allowUnsandboxedCommands") is False
    # filesystem.denyRead injection: kernel-level file read denial inside the sandbox profile
    # filesystem.denyRead 注入：sandbox profile 内核级文件读拒绝
    deny_read = opts.sandbox.get("filesystem", {}).get("denyRead", [])
    assert isinstance(deny_read, list)
    assert str(proj_dir / "project.json") in opts.sandbox["filesystem"]["denyWrite"]


def test_session_manager_wires_env_resolved_roots_into_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SessionManager is responsible for env resolution (ARCREEL_PROFILE_DIR / data_root argument) and feeds the resolved
    root paths to AgentAccessPolicy: when the user moves the data/profile directories anywhere (including outside the repo),
    the deny must follow; logs are in ``<data root>/logs``, and ARCREEL_LOG_DIR does not affect the deny scope.

    SessionManager 负责 env 解析（ARCREEL_PROFILE_DIR / data_root 参数），把 resolve
    后的根路径喂给 AgentAccessPolicy——用户把数据/profile 目录搬到任意位置（含 repo 外）
    时，deny 必须跟着指过去；日志在 ``<数据根>/logs``，ARCREEL_LOG_DIR 不影响 deny 范围。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    external_data = tmp_path / "external_data" / "projects"
    data_logs = external_data / "logs"
    data_logs.mkdir(parents=True)
    (data_logs / "arcreel.log").write_text("secret\n", encoding="utf-8")
    stale_logs = tmp_path / "stale_logs"
    stale_logs.mkdir()
    external_profile = tmp_path / "external_profile"
    (external_profile / ".claude").mkdir(parents=True)

    monkeypatch.setenv("ARCREEL_LOG_DIR", str(stale_logs))
    monkeypatch.setenv("ARCREEL_PROFILE_DIR", str(external_profile))

    sm = SessionManager(repo, SessionMetaStore(), data_root=external_data, sandbox_enabled=True)
    policy = sm.access_policy

    assert policy.agent_profile_root == external_profile.resolve()
    assert policy.data_root == external_data.resolve()
    assert policy.project_root == repo.resolve()
    # End to end: logs under the data root are unreadable by both the built-in read tools and Bash (kernel denyRead)
    # 端到端：数据根下的日志对内置读工具与 Bash（内核 denyRead）都不可读
    cwd = sm.layout.projects_dir / "demo"
    allowed, _ = policy.check_path_access(str(data_logs / "arcreel.log"), "Read", cwd, user_id=_USER_ID)
    assert not allowed
    deny_read = policy.build_sandbox_settings(cwd)["filesystem"]["denyRead"]
    assert str(data_logs.resolve()) in deny_read
    assert policy.is_sensitive_path((external_profile / ".claude" / "settings.json").resolve())
    # Neither the directory ARCREEL_LOG_DIR points to nor repo/logs is the log location; they do not go into denyRead
    # ARCREEL_LOG_DIR 指向的目录与 repo/logs 都不是日志位置，不进 denyRead
    assert str(stale_logs.resolve()) not in deny_read
    assert str((repo / "logs").resolve()) not in deny_read


def test_configure_sandbox_runtime_swaps_policy(tmp_path: Path) -> None:
    """Platform facts are injected at startup: the whole policy is replaced rather than poking private attributes,
    and later settings compilation / hook rulings consume the new rules immediately.

    startup 期注入平台事实：整体换新 policy 而非戳改私有属性，
    后续 settings 编译 / hook 裁决立即消费新规则。"""
    sm = _make_session_manager(tmp_path, sandbox_enabled=True)
    cwd = sm.layout.projects_dir / "demo"
    assert sm.access_policy.build_sandbox_settings(cwd)["enabled"] is True

    sm.configure_sandbox_runtime(in_docker=True, sandbox_enabled=False)

    assert sm.access_policy.sandbox_enabled is False
    assert sm.access_policy.in_docker is True
    assert sm.access_policy.build_sandbox_settings(cwd) == {"enabled": False}


@pytest.mark.asyncio
async def test_bash_env_scrub_hook_wraps_command_with_env_unset(fs_session_manager: SessionManager) -> None:
    """The hook puts the policy's wrapping result into ``updatedInput.command`` and returns no
    permissionDecision: the PreToolUse hook is step 1 of the permission chain, and allow would short-circuit all later steps.
    The wrapped content (``env -u <keys> sh -c '<orig>'``) is covered by
    ``test_wrap_bash_command_unsets_provider_keys`` in test_agent_access_policy.py; here only the wiring is verified.

    hook 把 policy 的包装结果塞进 ``updatedInput.command``，且不返回
    permissionDecision——PreToolUse hook 是权限链第 1 步，allow 会短路后续所有步骤。
    包装内容（``env -u <keys> sh -c '<orig>'``）由 test_agent_access_policy.py 的
    ``test_wrap_bash_command_unsets_provider_keys`` 覆盖，此处只验接线。"""
    result = await fs_session_manager._options_assembler._bash_env_scrub_hook(
        {"tool_name": "Bash", "tool_input": {"command": "env | grep ANTHROPIC"}},
        None,
        None,
    )

    out = result.get("hookSpecificOutput")
    assert out is not None
    assert out["hookEventName"] == "PreToolUse"
    # Carry no permission decision, so the permission chain continues to the allow rules / can_use_tool
    # 不携带权限决策，让权限链继续走到 allow 规则 / can_use_tool
    assert "permissionDecision" not in out
    # The policy's wrapping result is threaded into updatedInput.command (the wrapping shape is asserted by the policy tests)
    # policy 的包装结果被穿进 updatedInput.command（包装形态由 policy 测试断言）
    assert out["updatedInput"]["command"].startswith("env -u ")


@pytest.mark.asyncio
async def test_bash_env_scrub_hook_skips_wrap_when_sandbox_disabled(tmp_path: Path) -> None:
    """Windows fallback: ``env -u``/``sh -c`` are POSIX mechanisms that native Windows cannot execute;
    the hook neither wraps nor gives a permission decision, and the original command falls back to _can_use_tool for allowlist matching
    (a wrapped command starts with ``env -u``, so the allowlist would never match).

    Windows 回退：``env -u``/``sh -c`` 是 POSIX 机制，原生 Windows 不可执行；
    hook 不包装也不给权限决策，原始命令落到 _can_use_tool 做白名单匹配
    （包装后命令以 ``env -u`` 开头，会让白名单永远匹配不上）。"""
    sm = _make_session_manager(tmp_path, sandbox_enabled=False)
    result = await sm._options_assembler._bash_env_scrub_hook(
        {"tool_name": "Bash", "tool_input": {"command": "python .claude/skills/x/scripts/run.py"}},
        None,
        None,
    )
    assert result == {"continue_": True}


@pytest.mark.asyncio
async def test_bash_env_scrub_hook_passthrough_when_no_command(fs_session_manager: SessionManager) -> None:
    """An empty command is allowed directly without wrapping.

    空 command 时直接放行，不做包装。"""
    result = await fs_session_manager._options_assembler._bash_env_scrub_hook(
        {"tool_name": "Bash", "tool_input": {}},
        None,
        None,
    )
    assert result == {"continue_": True}


# ============================================================
# Windows sandbox fallback: the sandbox_enabled=False branch (permission chain wiring) / Windows 沙箱回退：sandbox_enabled=False 分支（权限链接线）
# ============================================================


def _make_session_manager(tmp_path: Path, *, sandbox_enabled: bool) -> SessionManager:
    project_root = tmp_path / "repo"
    project_root.mkdir(exist_ok=True)
    (project_root / "projects").mkdir(exist_ok=True)
    return SessionManager(
        project_root,
        SessionMetaStore(),
        sandbox_enabled=sandbox_enabled,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_enabled", [True, False])
async def test_build_options_bash_in_allowed_tools_by_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sandbox_enabled: bool
) -> None:
    """With the sandbox off, Bash/BashOutput/KillBash are stripped; with it on they are kept.

    sandbox 关闭时剥离 Bash/BashOutput/KillBash，启用时保留。"""
    sm = _make_session_manager(tmp_path, sandbox_enabled=sandbox_enabled)
    proj_dir = sm.layout.projects_dir / "test_proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "project.json").write_text('{"title":"t"}', encoding="utf-8")

    async def fake_env():
        return {"ANTHROPIC_API_KEY": "sk"}

    use_provider_env(monkeypatch, fake_env)
    opts = await sm._build_options("test_proj")

    for tool in AgentAccessPolicy.BASH_TOOLS:
        assert (tool in opts.allowed_tools) is sandbox_enabled
    assert "Read" in opts.allowed_tools
    assert "Skill" in opts.allowed_tools
    assert "WebFetch" in opts.allowed_tools
    if not sandbox_enabled:
        assert opts.sandbox == {"enabled": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "python .claude/skills/adapt-custom-endpoint/scripts/custom_endpoint.py validate",
            "PermissionResultAllow",
        ),
        # `..` inside a file name (not a path segment) does not trigger traversal blocking; legitimate commands are allowed as usual
        # `..` 在文件名内部（非路径段）不触发穿越拦截，合法命令照常放行
        ("python .claude/skills/adapt-custom-endpoint/scripts/my..script.py validate", "PermissionResultAllow"),
        # Normalization tolerance: legitimate commands with quoted script paths or Windows backslash separators are not wrongly denied
        # 归一化容错：带引号的脚本路径、Windows 反斜杠分隔符的合法命令不误拒
        (
            'python ".claude/skills/adapt-custom-endpoint/scripts/custom_endpoint.py" validate',
            "PermissionResultAllow",
        ),
        (
            "python .claude\\skills\\adapt-custom-endpoint\\scripts\\custom_endpoint.py validate",
            "PermissionResultAllow",
        ),
        ("cat /etc/passwd", "PermissionResultDeny"),
        ("ls -la", "PermissionResultDeny"),
        # ffmpeg / ffprobe are not on the allowlist: the Agent cannot run them via Bash; media processing goes through the toolset
        # ffmpeg / ffprobe 不在白名单：Agent 经 Bash 跑不了，媒体处理走工具集
        ("ffmpeg -i in.mp4 out.mp4", "PermissionResultDeny"),
        ("ffprobe in.mp4", "PermissionResultDeny"),
        ("ffmpeg", "PermissionResultDeny"),
    ],
)
async def test_windows_bash_whitelist_matches_main_behavior(tmp_path: Path, command: str, expected: str) -> None:
    """With the sandbox off, allowlisted prefixes are allowed and the rest denied; the deny message is derived from WINDOWS_BASH_PREFIX_WHITELIST.

    sandbox 关闭时白名单 prefix 放行，其余拒；deny 文案派生自 WINDOWS_BASH_PREFIX_WHITELIST。"""
    sm = _make_session_manager(tmp_path, sandbox_enabled=False)
    callback = await sm._build_can_use_tool_callback("test_sid", [None])
    result = await callback("Bash", {"command": command}, None)
    assert type(result).__name__ == expected
    if expected == "PermissionResultDeny":
        assert "Bash 白名单" in result.message
        # The deny message must contain every allowlisted prefix (single source of truth) / deny 文案必须包含所有白名单 prefix（单一真相源）
        for prefix in AgentAccessPolicy.WINDOWS_BASH_PREFIX_WHITELIST:
            assert prefix in result.message


def test_windows_bash_whitelist_has_no_media_tool_prefixes() -> None:
    assert AgentAccessPolicy.WINDOWS_BASH_PREFIX_WHITELIST == ("python .claude/skills/",)
    deny_message = AgentAccessPolicy.format_bash_whitelist_deny_message("ffmpeg -i in.mp4 out.mp4")
    assert "  - ffmpeg" not in deny_message
    assert "  - ffprobe" not in deny_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        # Allowlisted prefix + metachar chain: on Windows the trailing command has no sandbox denyWrite
        # backstop and could write protected JSON directly, so the whole string must be denied
        # 白名单前缀 + metachar 链：尾部命令在 Windows 上无 sandbox denyWrite
        # 兜底，可直写 protected JSON，必须整串拒
        'python .claude/skills/manage-project/scripts/peek_split_point.py; python -c "evil"',
        "python .claude/skills/x/scripts/run.py && python -c \"open('project.json','w')\"",
        "python .claude/skills/x/scripts/run.py | tee scripts/episode_1.json",
        "python .claude/skills/x/scripts/run.py $(evil)",
        "python .claude/skills/x/scripts/run.py `evil`",
        "python .claude/skills/x/scripts/run.py > scripts/episode_1.json",
        "python .claude/skills/x/scripts/run.py < secret.txt",
        "python .claude/skills/x/scripts/run.py\npython -c evil",
        # Command-name prefix collision: python3 is not python / 命令名前缀碰撞：python3 不是 python
        "python3 .claude/skills/x/scripts/run.py",
        # Path traversal: matches the python .claude/skills/ prefix and has no metachar, but .. escapes the
        # skills directory to run arbitrary scripts; the Windows fallback has no sandbox backstop, so it must be denied
        # 路径穿越：满足 python .claude/skills/ 前缀且不含 metachar，但 .. 逃出
        # skills 目录跑任意脚本——Windows 回退无 sandbox 兜底，必须拒
        "python .claude/skills/../../../tmp/evil.py",
        "python .claude/skills/../../arcreel_secrets_dumper.py",
        "python .claude/skills/x/scripts/run.py ../../other_project/secret.json",
        # Path traversal obfuscation bypass: the shell turns ".." / .\. back into .., so it must be denied after normalization
        # 路径穿越混淆绕过：shell 会把 ".." / .\. 还原成 ..，归一化后必须拒
        'python .claude/skills/dir/".."/".."/evil.py',
        "python .claude/skills/dir/'..'/'..'/evil.py",
        "python .claude/skills/dir/.\\./.\\./evil.py",
        'python .claude/skills/x/scripts/run.py ".."/".."/secret.json',
        # .. traversal with Windows backslash separators must be denied too (after normalization ../ matches)
        # Windows 反斜杠分隔符下的 .. 穿越同样要拒（归一化后 ../ 命中）
        "python .claude\\skills\\..\\..\\evil.py",
        # The python entry must be <skill>/scripts/<script>.py: any other file under the skills directory
        # (no scripts/ segment, not .py, or directly at the skill root) is never allowed
        # python 入口必须是 <skill>/scripts/<script>.py：skills 目录下任意其它
        # 文件（无 scripts/ 段、非 .py、或直接挂在 skill 根）一律不放行
        "python .claude/skills/evil.py",
        "python .claude/skills/adapt-custom-endpoint/custom_endpoint.py validate",
        "python .claude/skills/adapt-custom-endpoint/scripts/data.json",
        "python .claude/skills/adapt-custom-endpoint/scripts/sub/run.py",
    ],
)
async def test_windows_bash_whitelist_blocks_metachar_chains(tmp_path: Path, command: str) -> None:
    """Compound commands of an allowlisted prefix + shell metachar (; && | $() ` redirection newline) must be denied;
    command names match on token boundaries, blocking prefix collisions like python3; .. path traversal denies the whole string.

    白名单前缀 + shell metachar（; && | $() ` 重定向 换行）的复合命令必须拒；
    命令名按 token 边界匹配，挡 python3 这类前缀碰撞；.. 路径穿越整串拒。"""
    sm = _make_session_manager(tmp_path, sandbox_enabled=False)
    callback = await sm._build_can_use_tool_callback("test_sid", [None])
    result = await callback("Bash", {"command": command}, None)
    assert type(result).__name__ == "PermissionResultDeny"
    assert "Bash 白名单" in result.message


@pytest.mark.asyncio
async def test_windows_bash_management_tools_allowed(tmp_path: Path) -> None:
    """BashOutput / KillBash are Bash management tools and are allowed directly in fallback mode.

    BashOutput / KillBash 是 Bash 管理工具，回退模式下直接放行。"""
    sm = _make_session_manager(tmp_path, sandbox_enabled=False)
    callback = await sm._build_can_use_tool_callback("test_sid", [None])
    for tool in ("BashOutput", "KillBash"):
        result = await callback(tool, {}, None)
        assert type(result).__name__ == "PermissionResultAllow"


def _init_message(auto_dir: str | None) -> dict[str, object]:
    """Minimal shape of the CLI init system message; ``memory_paths`` lives in SystemMessage.data.

    CLI init 系统消息的最小形状；``memory_paths`` 落在 SystemMessage.data 里。"""
    data: dict[str, object] = {"subtype": "init", "session_id": "sdk-1"}
    if auto_dir is not None:
        data["memory_paths"] = {"auto": auto_dir}
    return {"type": "system", "subtype": "init", "data": data}


def _managed_for(project_name: str) -> ManagedSession:
    return ManagedSession(session_id="sess-1", actor=None, status="running", project_name=project_name)


def test_init_auto_memory_mismatch_logs_error_and_keeps_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The auto memory directory reported by init does not match expectations: log an error; the message is not rewritten and the session opens as usual.

    init 上报的 auto memory 目录与预期不符：记 error，消息不被改写、会话照开。"""
    sm = _make_session_manager(tmp_path, sandbox_enabled=True)
    proj_dir = sm.layout.projects_dir / "demo"
    proj_dir.mkdir(parents=True, exist_ok=True)
    managed = _managed_for("demo")
    msg = _init_message(str(tmp_path / "elsewhere" / "memory"))

    with caplog.at_level(logging.ERROR, logger="server.agent_runtime.session_manager"):
        sm._handle_special_message(managed, msg)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [getattr(r, "expected_auto_memory_dir", None) for r in errors] == [str(project_memory_dir(proj_dir))]
    assert managed.status == "running"
    assert msg == _init_message(str(tmp_path / "elsewhere" / "memory"))


@pytest.mark.parametrize(
    "auto_dir_factory",
    [
        pytest.param(lambda proj_dir: str(project_memory_dir(proj_dir)), id="exact"),
        # CLI 2.1.259 reports it with a trailing slash; compare resolved paths, not strings / CLI 2.1.259 上报带尾斜杠；比较按 resolve 后的路径做，不按字符串
        pytest.param(lambda proj_dir: f"{project_memory_dir(proj_dir)}/", id="trailing-slash"),
        pytest.param(lambda _proj_dir: None, id="field-absent"),
    ],
)
def test_init_auto_memory_match_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, auto_dir_factory
) -> None:
    """No warning when the paths match or the CLI does not report memory_paths (old version / auto memory off).

    路径一致或 CLI 未上报 memory_paths（旧版本 / auto memory 关闭）时不告警。"""
    sm = _make_session_manager(tmp_path, sandbox_enabled=True)
    proj_dir = sm.layout.projects_dir / "demo"
    proj_dir.mkdir(parents=True, exist_ok=True)

    with caplog.at_level(logging.ERROR, logger="server.agent_runtime.session_manager"):
        sm._handle_special_message(_managed_for("demo"), _init_message(auto_dir_factory(proj_dir)))

    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == []
