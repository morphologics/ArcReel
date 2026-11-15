"""Process isolation for pytest sessions spawned by mutmut: a private basetemp, and no touching macOS system
proxy discovery.

Loaded only by ``[tool.mutmut] pytest_add_cli_args`` in ``pyproject.toml`` as ``-p tests.mutmut_plugin``;
local and CI pytest do not load it.

mutmut forks several pytest sessions per mutant to run in parallel. Two fork-related measures:

- At session teardown they would all clean the ``pytest-current`` symlink under the shared root at once; the
  ``FileNotFoundError`` raised by the race bubbles out of ``pytest.main``, making the child take the normal
  exit path with exit code 1, recorded as killed. An explicitly given basetemp registers no cleanup, so there
  is nothing to do at teardown; the directory is reclaimed by this session itself at unconfigure.
- On macOS, ``urllib.request.getproxies()`` with no ``*_proxy`` variables in the environment calls
  SystemConfiguration via ``_scproxy`` to read the system proxy, and that framework segfaults outright in
  forked multithreaded children. Any case constructing ``openai.OpenAI`` / ``httpx.Client`` reaches this,
  and the whole batch of mutants is recorded as -11. Here the two macOS-only entries are replaced with
  "no system proxy" constant implementations: tests never depend on a real system proxy, and proxies given
  by env vars still take effect as before.

mutmut 拉起的 pytest 会话的进程隔离：私有 basetemp，且不触碰 macOS 的系统代理发现。

只由 ``pyproject.toml`` 的 ``[tool.mutmut] pytest_add_cli_args`` 以 ``-p tests.mutmut_plugin``
加载，本地与 CI 的 pytest 不加载它。

mutmut 按 mutant fork 出多个 pytest 会话并行跑。两处 fork 相关的处理：

- 会话收尾时它们会同时清理共享根下的 ``pytest-current`` 软链，竞争抛出的 ``FileNotFoundError``
  从 ``pytest.main`` 冒出，让子进程走正常退出路径、退出码 1 记成 killed。显式给定的 basetemp
  不登记任何清理，收尾时无事可做；目录由本会话在 unconfigure 时自己回收。
- macOS 上 ``urllib.request.getproxies()`` 在环境里没有 ``*_proxy`` 变量时会经 ``_scproxy``
  调 SystemConfiguration 读系统代理，而该框架在 fork 出的多线程子进程里直接段错误。任何构造
  ``openai.OpenAI`` / ``httpx.Client`` 的用例都会走到这里，整批 mutant 记成 −11。这里把两个
  macOS 专属入口换成「无系统代理」的常量实现：测试从不依赖真实系统代理，环境变量给的代理
  仍按原路生效。
"""

from __future__ import annotations

import shutil
import tempfile
import urllib.request

import pytest


def _no_system_proxies() -> dict[str, str]:
    return {}


def _never_bypass_by_system_settings(host: str) -> bool:
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """``tryfirst`` makes the assignment happen before pytest's own tmpdir plugin reads ``config.option.basetemp``.

    After command-line parsing the option is a str, and what xdist hands to workers is also a str; the same type
    is kept here.

    ``tryfirst`` 使赋值先于 pytest 自带 tmpdir 插件读取 ``config.option.basetemp``。

    该选项经命令行解析后是 str，xdist 派给 worker 的也是 str，这里保持同一类型。
    """
    if hasattr(urllib.request, "getproxies_macosx_sysconf"):
        urllib.request.getproxies_macosx_sysconf = _no_system_proxies
        urllib.request.proxy_bypass_macosx_sysconf = _never_bypass_by_system_settings
    if config.option.basetemp is not None:
        return
    private_basetemp = tempfile.mkdtemp(prefix="arcreel-mutmut-basetemp-")
    config.option.basetemp = private_basetemp
    config.add_cleanup(lambda: shutil.rmtree(private_basetemp, ignore_errors=True))
