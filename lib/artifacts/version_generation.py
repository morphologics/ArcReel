"""Which generation call produced each version of a resource: model and cost for the version history.

Version records don't carry the call; the usage ledger records each successful call with the project file it wrote.
A generated version is recorded right after its call finishes, so each version takes the latest not yet taken call
on the same file that finished before it (a little slack for clock rounding) and not long before it. Uploads and
restores have no call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from lib.artifacts.version_manager import MANUAL_UPLOAD_VERSION_SOURCE

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


def attach_version_generations(versions: Iterable[dict[str, Any]], calls: list[Mapping[str, Any]]) -> None:
    """Set ``generation`` ({provider, model, cost_amount, currency} or None) on each version record, in place."""
    taken: set[int] = set()
    for record in sorted(versions, key=lambda r: r.get("version", 0)):
        record["generation"] = None
        created = _parse(record.get("created_at"))
        if (
            created is None
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
            call = calls[match]
            record["generation"] = {
                "provider": call["provider"],
                "model": call["model"],
                "cost_amount": call["cost_amount"],
                "currency": call["currency"],
            }
