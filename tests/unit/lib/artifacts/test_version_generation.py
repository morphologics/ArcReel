from datetime import UTC, datetime

from lib.artifacts.version_generation import attach_version_generations


def _call(minute: int, model: str, cost: float) -> dict:
    return {
        "finished_at": datetime(2026, 11, 11, 10, minute, tzinfo=UTC),
        "provider": "runware",
        "model": model,
        "cost_amount": cost,
        "currency": "USD",
        "call_type": "video",
    }


def test_each_generated_version_takes_the_call_that_finished_just_before_it():
    versions = [
        {"version": 1, "created_at": "2026-11-11T10:01:05Z"},
        {"version": 2, "created_at": "2026-11-11T10:20:00Z", "source": "manual_upload"},
        {"version": 3, "created_at": "2026-11-11T10:40:30Z"},
        {"version": 4, "created_at": "2026-11-11T10:45:00Z", "restored_from": 1},
    ]
    # the 10:30 call failed to commit a version; v3 belongs to the 10:40 one
    calls = [_call(1, "seedance", 0.5), _call(30, "kling", 0.7), _call(40, "minimax", 0.9)]
    attach_version_generations(versions, calls)

    assert versions[0]["generation"] == {"provider": "runware", "model": "seedance", "cost_amount": 0.5, "currency": "USD"}
    assert versions[1]["generation"] is None
    assert versions[2]["generation"]["model"] == "minimax"
    assert versions[3]["generation"] is None


def test_a_call_long_before_the_version_is_not_its_call():
    versions = [{"version": 1, "created_at": "2026-11-11T12:00:00Z"}]
    attach_version_generations(versions, [_call(1, "seedance", 0.5)])
    assert versions[0]["generation"] is None
