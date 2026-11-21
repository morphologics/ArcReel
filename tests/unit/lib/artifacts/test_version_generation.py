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
        "duration_ms": 83_000,
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

    assert versions[0]["generation"] == {
        "provider": "runware",
        "model": "seedance",
        "cost_amount": 0.5,
        "currency": "USD",
        "duration_ms": 83_000,
        "call_id": None,
        "task_id": None,
        "exact": False,
    }
    assert versions[1]["generation"] is None
    assert versions[2]["generation"]["model"] == "minimax"
    assert versions[3]["generation"] is None


def test_a_call_long_before_the_version_is_not_its_call():
    versions = [{"version": 1, "created_at": "2026-11-11T12:00:00Z"}]
    attach_version_generations(versions, [_call(1, "seedance", 0.5)])
    assert versions[0]["generation"] is None


def _linked_call(call_id: int, minute: int, model: str, task_id: str) -> dict:
    return {**_call(minute, model, 0.4), "id": call_id, "task_id": task_id}


def test_a_version_that_records_its_call_takes_exactly_that_call():
    """API G16: the link on the version wins over the time match, even when another call finished closer to it.

    API G16：版本上的关联优先于时间匹配，即使另一条调用离它更近。"""
    versions = [
        {
            "version": 1,
            "created_at": "2026-11-11T10:41:00Z",
            "generation_task_id": "T1",
            "generation_call_id": 7,
        }
    ]
    calls = [_linked_call(7, 10, "seedance", "T1"), _linked_call(8, 40, "kling", "T2")]
    attach_version_generations(versions, calls)

    assert versions[0]["generation"]["call_id"] == 7
    assert versions[0]["generation"]["task_id"] == "T1"
    assert versions[0]["generation"]["model"] == "seedance"
    assert versions[0]["generation"]["exact"] is True


def test_old_versions_keep_the_time_match_and_never_take_a_linked_call():
    """Versions written before the link keep the heuristic; a call already linked to a newer version is not theirs.

    关联出现之前写入的版本仍走时间匹配；已被较新版本关联的调用不归它们。"""
    versions = [
        {"version": 1, "created_at": "2026-11-11T10:02:00Z"},
        {"version": 2, "created_at": "2026-11-11T10:03:00Z", "generation_task_id": "T2", "generation_call_id": 9},
    ]
    calls = [_linked_call(8, 1, "seedance", "T1"), _linked_call(9, 2, "kling", "T2")]
    attach_version_generations(versions, calls)

    assert versions[0]["generation"]["call_id"] == 8
    assert versions[0]["generation"]["exact"] is False
    assert versions[1]["generation"]["call_id"] == 9
    assert versions[1]["generation"]["exact"] is True


def test_a_linked_version_whose_call_is_missing_has_no_generation():
    versions = [{"version": 1, "created_at": "2026-11-11T10:02:00Z", "generation_task_id": "T1"}]
    attach_version_generations(versions, [_linked_call(8, 1, "seedance", "T0")])
    assert versions[0]["generation"] is None
