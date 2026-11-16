"""The member is using the studio (YouReel hosting).

Route: POST /api/v1/activity → 204, and nothing else happens here. The studio app sends it at most once a minute while
someone actually uses it (a click, a key, a scroll in a visible tab). It is for the host in front of the studio: a
hosted studio is stopped after a while without use and started again on the next visit, and the host can't tell a
person browsing from a tab left open by the requests alone (the app polls in the background either way). A POST is
what it counts.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from server.auth import CurrentUser

router = APIRouter(prefix="/activity", tags=["Activity signal for the host"])


@router.post("", status_code=204, response_class=Response)
async def note_activity(_user: CurrentUser) -> Response:
    return Response(status_code=204)
