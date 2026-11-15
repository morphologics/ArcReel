"""Import smoke test — catches circular deps and import-time side effects.

Parametrized walk over core submodules under lib/ and server/, one importlib.import_module each.
Any circular dependency, missing dependency or top-level side-effect crash turns this red.

An in-process walk has blind spots; :func:`test_module_imports_first_in_fresh_process` fills them, see that
function for why.

参数化遍历 lib/ 与 server/ 下核心子模块，每个 importlib.import_module 一次。
任何循环依赖、缺失依赖、顶层副作用崩溃都会在此红。

同进程遍历有其盲区，:func:`test_module_imports_first_in_fresh_process` 补上，理由见该函数。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Allowlist of core submodules. Append here when adding packages (instead of using pkgutil.walk_packages,
# to avoid accidentally pulling in lib.i18n.zh/en translation data packages and alembic.versions migration
# scripts).
# Modules involved in module-level mutual imports (lib.config <-> lib.custom_provider, the endpoint definition
# dispatcher <-> each kind's implementer) and base modules required to stay light must also be registered in
# FIRST_IMPORT_MODULES.
# 核心子模块白名单。新增包时请在此追加（而不是用 pkgutil.walk_packages，
# 以避免意外拉起 lib.i18n.zh/en 的翻译数据包和 alembic.versions 迁移脚本）。
# 参与模块级互引的模块（lib.config ↔ lib.custom_provider、端点定义的分派方 ↔ 各 kind 的实现方）
# 及要求保持轻量的基础模块另需登记 FIRST_IMPORT_MODULES。
MODULES = [
    # lib top-level single-file modules
    # lib 顶层单文件模块
    "lib.backends.ark_shared",
    "lib.backends.backend_runtime",
    "arcreel_market_core.video_backend_contract",
    "lib.project.asset_fingerprints",
    "lib.project.data_validator",
    "lib.backends.gemini_shared",
    "lib.generation.generation_queue",
    "lib.generation.generation_queue_client",
    "lib.generation.generation_worker",
    "lib.generation.restart_recovery",
    "lib.generation.task_failure_encoding",
    "lib.generation.video_resume",
    "lib.script.grid.grid_manager",
    "lib.backends.grok_shared",
    "lib.infra.image_utils",
    "lib.infra.logging_config",
    "lib.generation.media_generator",
    "lib.backends.openai_shared",
    "lib.project.project_change_hints",
    "lib.project.project_manager",
    "lib.prompts.prompt_builders",
    "lib.prompts.prompt_builders_script",
    "lib.prompts.prompt_utils",
    "lib.backends.providers",
    "lib.infra.retry",
    "lib.script.script_generator",
    "lib.script.script_models",
    "lib.script.storyboard_sequence",
    "lib.prompts.style_templates",
    "lib.config.system_config",
    "lib.backends.text_generator",
    "lib.infra.thumbnail",
    "lib.artifacts.version_manager",
    # lib subpackages
    # lib 子包
    "lib.config",
    "lib.custom_provider",
    "lib.custom_provider.comfyui",
    "lib.db",
    "lib.db.models",
    "lib.db.repositories",
    "lib.script.grid",
    "lib.backends.image_backends",
    "lib.prompts.prompt_templates",
    "lib.backends.text_backends",
    "lib.backends.video_backends",
    # server
    "server",
    "server.agent_runtime",
    "server.app",
    "server.auth",
    "server.dependencies",
    "server.routers",
    "server.services",
    "server.services.tasks.formal_image_commit",
]


# Modules that must work as the first import, each verified in a fresh interpreter (see the case docstring for
# why).
# 首位导入必须成立的模块，逐个在全新解释器里验证（理由见用例 docstring）。
FIRST_IMPORT_MODULES = [
    "lib.backends.backend_runtime",
    "lib.backends.image_backends.base",
    "arcreel_market_core.video_backend_contract",
    "lib.config.resolver",
    "lib.custom_provider.backends",
    "lib.custom_provider.capabilities",
    "lib.custom_provider.comfyui.comfyui_backend",
    "lib.custom_provider.comfyui.comfyui_client",
    "lib.custom_provider.comfyui.import_shapes",
    "lib.custom_provider.comfyui.inference",
    "lib.custom_provider.comfyui.request_builder",
    "arcreel_market_core.comfyui.validator",
    "lib.custom_provider.discovery",
    "arcreel_market_core.endpoint_definition",
    "lib.custom_provider.endpoint_test",
    "lib.custom_provider.endpoints",
    "lib.custom_provider.factory",
    "lib.custom_provider.loader",
    "server.services.currency.upload_finalize",
    "server.services.tasks.derivative_sheet_tasks",
    "server.services.tasks.formal_image_commit",
    "server.services.tasks.generation_tasks",
    "server.services.tasks.image_edit_tasks",
    "server.services.tasks.reference_video_tasks",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports_cleanly(module_name: str) -> None:
    """Smoke: the module imports by full name, and import-time side effects (config reads, circular imports) do not
    raise.

    冒烟：模块能按全名导入，import 期副作用（配置读取、循环 import）不抛错。"""
    assert importlib.import_module(module_name).__name__ == module_name


@pytest.mark.parametrize("module_name", FIRST_IMPORT_MODULES)
def test_module_imports_first_in_fresh_process(module_name: str) -> None:
    """The module also imports successfully as the first project module imported in the interpreter.

    ``lib.config`` and ``lib.custom_provider`` reference each other (the latter assembles backends, and backends
    read the former's URL tools); the endpoint definition dispatcher and each ``kind``'s implementer also point
    at each other across a package boundary (``endpoint_definition``'s dispatch table imports each kind's
    validation implementation, and ``comfyui.import_shapes`` reaches back for ``kinds`` constants).
    The task execution entry ``generation_tasks`` registers executors at module level, and executors and upload
    finalization share the formal image commit; they may only depend one way on ``formal_image_commit`` and must
    not import the task execution entry back.
    If any one such edge reverts to a module-level import it forms a cycle - and the cycle only blows up when a
    particular module goes first; the in-process smoke walk cannot see it because ``sys.modules`` is already
    warmed by earlier cases. A fresh subprocess is the only way to lock in "each can be imported independently in
    any order".

    该模块作为解释器里第一个被导入的项目模块时也能成功。

    ``lib.config`` 与 ``lib.custom_provider`` 互相引用（后者装配 backend、backend 又读前者的
    URL 工具）；端点定义的分派方与各 ``kind`` 的实现方也隔着包边界互指（``endpoint_definition``
    的分派表 import 各 kind 的校验实现，``comfyui.import_shapes`` 又回头取 ``kinds`` 的常量）。
    任务执行入口 ``generation_tasks`` 在模块级登记各执行器，执行器与上传收尾又共用正式图像提交，
    它们只能单向依赖 ``formal_image_commit``，不得回头导入任务执行入口。
    这类边一旦有一条退回模块级导入就会成环——而环只在特定模块打头时才炸，同进程的冒烟遍历因
    ``sys.modules`` 已被前序用例填热而看不见。全新子进程是唯一能锁定"任意顺序均可独立导入"的手段。
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        # Blocking side effects at import time (network, lock waits) are exactly one of the shapes this case catches;
        # a timeout must turn red explicitly, rather than hanging the CI job until its time limit.
        # 导入期的阻塞式副作用（网络、锁等待）正是本用例要拦的形态之一，超时须显式转红，
        # 而不是把 CI job 挂满时限。
        pytest.fail(f"{module_name} 首位导入超时，疑似存在阻塞式顶层副作用")
    assert result.returncode == 0, f"{module_name} 无法作为首个导入：\n{result.stderr}"


def test_video_backend_contract_first_import_stays_light() -> None:
    """First import of the video contract loads no runtime support, third-party HTTP/ORM or video implementation
    packages.

    视频契约首位导入不加载运行支持、第三方 HTTP/ORM 或视频实现包。"""
    code = """
import sys
import arcreel_market_core.video_backend_contract

forbidden = {
    "httpx",
    "sqlalchemy",
    "lib.backends.backend_runtime",
    "lib.backends.video_backends",
}
assert forbidden.isdisjoint(sys.modules), forbidden & sys.modules.keys()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, f"视频契约首位导入加载了运行依赖：\n{result.stderr}"
