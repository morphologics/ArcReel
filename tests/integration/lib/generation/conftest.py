"""Shared fixtures for the generation worker integration tests.

生成 worker 集成测试共用的 fixture。"""

from pathlib import Path

import pytest

from tests.fakes import bind_safe_session_factory


@pytest.fixture
async def worker_db(db_factory, monkeypatch):
    """Replace the global session factory that the worker touches directly with an in-memory database.

    ``_requeue_single_task`` and ``CapacityTable.from_db`` bypass the injected queue collaborator and write through
    the ``safe_session_factory`` imported at worker module level; bound to the in-memory database, these two paths
    really run, so assertions can land on row state.

    把 worker 直接触达的全局 session factory 换成内存库。

    ``_requeue_single_task`` 与 ``CapacityTable.from_db`` 绕开注入的 queue 协作者、
    直接经 worker 模块级导入的 ``safe_session_factory`` 落库；绑到内存库后这两条路径真的执行，
    断言因而能落在行状态上。
    """
    bind_safe_session_factory(monkeypatch, db_factory)
    return db_factory


@pytest.fixture
def staged_project(tmp_path, monkeypatch) -> Path:
    """Point the worker's project lookup at the tmp project so staging cleanup lands on the real file system.

    把 worker 的项目定位指向 tmp 项目，让 staging 清理落在真实文件系统上。"""
    project_path = tmp_path / "demo"
    project_path.mkdir()

    class _PM:
        def get_project_path(self, _name: str) -> Path:
            return project_path

    monkeypatch.setattr("lib.generation.video_resume.get_project_manager", lambda: _PM())
    return project_path
