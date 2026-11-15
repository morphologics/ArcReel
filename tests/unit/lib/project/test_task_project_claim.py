"""A task's claim on a project: after project deletion voids the claim, the on-disk recheck for that project
in the task context fails.

任务对项目的认领：项目删除使认领作废后，任务上下文里对该项目的落盘复核失败。"""

import asyncio

import pytest

from lib.project.task_project_claim import (
    ProjectDeletedDuringTaskError,
    claim_task_project,
    ensure_task_project_claim,
    restore_task_project_claims,
    revoke_task_project_claims,
)


async def test_revoked_claim_blocks_only_the_claimed_project_inside_the_task_context():
    # Deletion can happen after the task has been claimed but before it enters the execution context. / 删除可能发生在任务已被认领、但还没进入执行上下文的时候。
    revoke_task_project_claims(["t1"])
    with claim_task_project("t1", "demo") as claim:
        assert claim.revoked
        with pytest.raises(ProjectDeletedDuringTaskError):
            await asyncio.to_thread(ensure_task_project_claim, "demo")
        ensure_task_project_claim("other")
    ensure_task_project_claim("demo")


def test_restored_claim_lets_the_task_write_again():
    # When an overwriting import fails to replace the directory and the project is kept as-is, the voided
    # claim is restored along with it.
    # 覆盖导入替换目录失败、项目原样保留时，作废的认领随之恢复。
    revoke_task_project_claims(["t2"])
    restore_task_project_claims(["t2"])
    with claim_task_project("t2", "demo") as claim:
        assert not claim.revoked
        ensure_task_project_claim("demo")
