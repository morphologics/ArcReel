import pytest

from scripts.project_status_sync import derive_status


@pytest.mark.parametrize(
    ("state", "labels", "has_assignee", "has_open_closing_pr", "expected"),
    [
        # Spec / parked clear the status, taking precedence over every other signal / Spec / parked 置空，优先于其余一切信号
        ("OPEN", {"Spec", "ready-for-agent"}, True, True, None),
        ("CLOSED", {"parked"}, False, False, None),
        # closed → Done, takes precedence over an open PR and assignee / closed → Done，优先于 open PR 与 assignee
        ("CLOSED", {"needs-triage"}, True, True, "Done"),
        # Open closing PR → In review, takes precedence over assignee and the ready label
        # open closing PR → In review，优先于 assignee 与 ready 标签
        ("OPEN", {"ready-for-agent"}, True, True, "In review"),
        # assignee → In progress, takes precedence over the ready / needs-info labels / assignee → In progress，优先于 ready / needs-info 标签
        ("OPEN", {"ready-for-human", "needs-info"}, True, False, "In progress"),
        # ready-for-agent takes precedence over ready-for-human, and ready-for-human over needs-info
        # ready-for-agent 优先于 ready-for-human，ready-for-human 优先于 needs-info
        ("OPEN", {"ready-for-agent", "ready-for-human"}, False, False, "Ready · agent"),
        ("OPEN", {"ready-for-human", "needs-info"}, False, False, "Ready · human"),
        ("OPEN", {"needs-info", "needs-triage"}, False, False, "Needs info"),
        # needs-triage and no labels always fall back to Inbox / needs-triage 与无标签一律兜底 Inbox
        ("OPEN", {"needs-triage"}, False, False, "Inbox"),
        ("OPEN", set(), False, False, "Inbox"),
    ],
)
def test_derive_status_priority(state, labels, has_assignee, has_open_closing_pr, expected):
    assert derive_status(state, labels, has_assignee, has_open_closing_pr) == expected
