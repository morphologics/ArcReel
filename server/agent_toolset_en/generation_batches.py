"""Generation batch query and cancel tool declarations."""

from __future__ import annotations

from server.agent_toolset.declaration import Exempt, ToolDeclaration
from server.tool_runtime import GenerationBatchToolRequest, cancel_generation_batch, get_generation_batch

GET_GENERATION_BATCH = ToolDeclaration(
    name="get_generation_batch",
    description=(
        "Query a generation batch: per-member task status, counts, the suggested polling interval poll_after_seconds, and whether "
        "it has finished (done); "
        "once finished it also carries the terminal generation_result (per-ID outcome: success / failure / blocked). While not "
        "finished, call again at poll_after_seconds "
        "intervals until done=true. A batch that does not belong to this project or does not exist returns "
        "generation_batch_not_found. Read-only, no side effects."
    ),
    request_model=GenerationBatchToolRequest,
    migration=Exempt(
        "Read-only: batch state lives in the task queue, not in project artifacts; batches submitted before a migration failure "
        "must remain queryable."
    ),
    domain_key="generation_batch",
    handler=get_generation_batch,
)

CANCEL_GENERATION_BATCH = ToolDeclaration(
    name="cancel_generation_batch",
    description=(
        "Cancel the members of a generation batch that are still queued; returns the task ids that were cancelled (cancelled), "
        "skipped because already running (skipped_running) and skipped because already "
        "finished (skipped_terminal). Members that have started running cannot be cancelled and run to completion; repeated "
        "calls are safe. "
        "A batch that does not belong to this project or does not exist returns generation_batch_not_found."
    ),
    request_model=GenerationBatchToolRequest,
    migration=Exempt(
        "Only withdraws queued tasks and writes no project artifacts; when migration fails it must still be able to cut losses "
        "by cancelling previously submitted batches."
    ),
    domain_key="generation_batch_cancellation",
    handler=cancel_generation_batch,
)

GENERATION_BATCH_TOOLS = (GET_GENERATION_BATCH, CANCEL_GENERATION_BATCH)

__all__ = [
    "CANCEL_GENERATION_BATCH",
    "GENERATION_BATCH_TOOLS",
    "GET_GENERATION_BATCH",
]
