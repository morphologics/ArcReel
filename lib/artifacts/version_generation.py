"""Which generation call produced each version of a resource: model and cost for the version history.

Versions committed by a queue task since API G16 record their task and ledger call (``generation_task_id`` /
``generation_call_id``, ``lib.artifacts.version_manager``) and are matched to that call exactly. Older versions don't
carry the call; the usage ledger records each successful call with the project file it wrote. A generated version is
recorded right after its call finishes, so each such version takes the latest not yet taken call on the same file that
finished before it (a little slack for clock rounding) and not long before it. Uploads and restores have no call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from lib.artifacts.version_manager import (
    GENERATION_CALL_VERSION_FIELD,
    GENERATION_TASK_VERSION_FIELD,
    MANUAL_UPLOAD_VERSION_SOURCE,
)

# version timestamps have whole seconds; the call is finalised just before the version is committed
_AFTER_SLACK = timedelta(seconds=120)
# the version is committed right after the call; a call this much older belongs to an earlier, failed commit
_MAX_GAP = timedelta(minutes=30)


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _generation(call: Mapping[str, Any], *, exact: bool) -> dict[str, Any]:
    return {
        "provider": call["provider"],
        "model": call["model"],
        "cost_amount": call["cost_amount"],
        "currency": call["currency"],
        # YouReel (owner 2026-11-21): the time the generation took (ms; None when not recorded)
        "duration_ms": call.get("duration_ms"),
        # API G16: the ledger call and its task; ``exact`` = linked by the version record, not matched by time
        "call_id": call.get("id"),
        "task_id": call.get("task_id"),
        "exact": exact,
    }


def attach_version_generations(versions: Iterable[dict[str, Any]], calls: list[Mapping[str, Any]]) -> None:
    """Set ``generation`` ({provider, model, cost_amount, currency, duration_ms, call_id, task_id, exact} or None) on
    each version record, in place."""
    records = sorted(versions, key=lambda r: r.get("version", 0))
    index_by_id = {call["id"]: index for index, call in enumerate(calls) if call.get("id") is not None}
    taken: set[int] = set()
    linked: set[int] = set()
    # Exact links first, so the time match below never hands a linked call to an older, unlinked version.
    for position, record in enumerate(records):
        record["generation"] = None
        if record.get(GENERATION_TASK_VERSION_FIELD) is None:
            continue
        # A linked version is never matched by time: without its call (taskless retry, pruned ledger) it has none.
        linked.add(position)
        index = index_by_id.get(record.get(GENERATION_CALL_VERSION_FIELD))
        if index is not None and index not in taken:
            taken.add(index)
            record["generation"] = _generation(calls[index], exact=True)
    for position, record in enumerate(records):
        created = _parse(record.get("created_at"))
        if (
            position in linked
            or created is None
            or record.get("source") == MANUAL_UPLOAD_VERSION_SOURCE
            or record.get("restored_from") is not None
        ):
            continue
        match = None
        for index, call in enumerate(calls):
            finished = call["finished_at"]
            if index in taken or finished > created + _AFTER_SLACK:
                continue
            if finished >= created - _MAX_GAP:
                match = index
        if match is not None:
            taken.add(match)
            record["generation"] = _generation(calls[match], exact=False)
