"""Read-only SDK agent summaries for both interactive consoles."""

from __future__ import annotations

import unicodedata
from typing import Protocol, cast

from rich.table import Table
from rich.text import Text

from yoke.cli.interactive.process_commands import _safe_text
from yoke.cli.interactive.process_commands import command_process_manager
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render.base import Console


class _Registry(Protocol):
    def snapshots(self) -> list[dict[str, object]]: ...


def print_agent_table(console: Console, agent: object) -> None:
    """Show each SDK instance's latest run without reading process output."""
    manager = command_process_manager(agent)
    registry = getattr(manager, "agent_runs", None)
    if registry is None:
        print_scrollback_notice(
            console, "Agent tracking is unavailable for this agent."
        )
        return
    latest: dict[str, dict[str, object]] = {}
    for run in cast(_Registry, registry).snapshots():
        agent_id = run.get("agentId")
        if not isinstance(agent_id, str):
            continue
        previous = latest.get(agent_id)
        if previous is None or _order(run) > _order(previous):
            latest[agent_id] = run
    if not latest:
        print_scrollback_notice(console, "No SDK agent runs yet.")
        return
    active = sum(
        run.get("status") == "running" and run.get("observation") == "live"
        for run in latest.values()
    )
    console.print(
        Text(
            f"SDK agents: {active} running, {len(latest)} total. Latest run per agent."
        )
    )
    table = Table(box=None, pad_edge=False)
    for column in ("Agent", "ID", "Model", "Status", "Last tool", "Tools"):
        table.add_column(column, overflow="fold")
    for run in sorted(latest.values(), key=_order, reverse=True):
        usage = run.get("typedUsage")
        tool_uses = usage.get("toolUses") if isinstance(usage, dict) else None
        count = str(tool_uses) if type(tool_uses) is int and tool_uses >= 0 else "-"
        model = "/".join(
            value
            for key in ("provider", "model")
            if isinstance(value := run.get(key), str) and value
        )
        table.add_row(
            _text(run.get("name")),
            _text(run.get("agentId")),
            _text(model),
            Text(_status(run)),
            _text(run.get("lastToolName")),
            Text(count),
        )
    console.print(table)


def _order(run: dict[str, object]) -> tuple[str, int, str]:
    started = run.get("startedAt")
    version = run.get("version")
    run_id = run.get("runId")
    return (
        started if isinstance(started, str) else "",
        version if type(version) is int else 0,
        run_id if isinstance(run_id, str) else "",
    )


def _status(run: dict[str, object]) -> str:
    status = run.get("status")
    observation = run.get("observation")
    if not isinstance(observation, str):
        observation = "unobserved"
    if status == "running" and observation != "live":
        return observation if observation in {"stale", "lost"} else "unobserved"
    if not isinstance(status, str) or status not in {
        "running",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }:
        return "unknown"
    return f"{status} / {observation}" if observation in {"stale", "lost"} else status


def _text(value: object) -> Text:
    if not isinstance(value, str) or not value:
        return Text("-")
    # Text prevents Rich markup parsing; escaping controls also protects terminals.
    safe = "".join(
        f"\\u{ord(char):04x}"
        if unicodedata.category(char) in {"Cf", "Zl", "Zp"}
        else char
        for char in _safe_text(value[:160])
    )
    return Text(safe + ("..." if len(value) > 160 else ""))
