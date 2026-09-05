from __future__ import annotations

# ruff: noqa: D100,D103,S101

import pytest

from yoke.agent.activity import activity_status_for_event


@pytest.mark.parametrize(
    ("event", "payload", "current", "expected"),
    [
        ("model_start", {}, "Streaming", "Thinking"),
        ("model_end", {}, "Thinking", "Streaming"),
        ("assistant_message", {"phase": "commentary"}, "Thinking", "Streaming"),
        ("tool_execution_start", {}, "Thinking", "Running tool"),
        ("tool_execution_end", {"ok": True}, "Running tool", "Thinking"),
        ("tool_execution_end", {"ok": False}, "Running tool", "Recovering"),
        ("compaction_summary_start", {}, "Thinking", "Compacting"),
        ("compaction_summary_end", {"ok": True}, "Compacting", "Thinking"),
        ("provider_rate_limited", {}, "Thinking", "Rate limited"),
        ("provider_retry", {}, "Rate limited", "Retrying provider"),
        ("provider_recovered", {}, "Retrying provider", "Thinking"),
        ("model_start", {}, "Rate limited", "Rate limited"),
        ("model_start", {}, "Retrying provider", "Retrying provider"),
        ("context_usage", {}, "Thinking", None),
    ],
)
def test_activity_status_matches_cli_status_labels(
    event: str,
    payload: dict[str, object],
    current: str,
    expected: str | None,
) -> None:
    assert activity_status_for_event(event, payload, current=current) == expected
