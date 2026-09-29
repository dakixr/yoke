"""Versioned ACP projection of authoritative native agent snapshots."""

from collections.abc import Awaitable, Callable
from typing import Any

from yoke.http.models.agent_run import public_snapshot

Update = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentRoster:
    """Keep the latest prompt run per SDK instance, not per process tool read."""

    def __init__(self, session_id: str, update: Update) -> None:
        self.session_id = session_id
        self.update = update
        self.rows: dict[str, dict[str, Any]] = {}
        self._retired_before: tuple[str, int] | None = None

    async def reconcile(self, items: object) -> None:
        if not isinstance(items, list):
            raise ValueError("Invalid agent roster")
        latest: dict[str, dict[str, Any]] = {}
        for item in items:
            # Unlike the host, a client cannot fill in absent ownership.
            if not isinstance(item, dict) or item.get("sessionID") != self.session_id:
                continue
            info = public_snapshot(item, self.session_id)
            if info is None:
                continue
            agent_id = str(info["agentId"])
            previous = latest.get(agent_id)
            if previous is None or self._order(info) > self._order(previous):
                latest[agent_id] = info
        for agent_id, info in latest.items():
            previous = self.rows.get(agent_id)
            if (
                previous is None
                and self._retired_before is not None
                and self._order(info) <= self._retired_before
                and (info["status"] != "running" or info["observation"] == "lost")
            ):
                continue
            if previous is not None:
                if info["version"] < previous["version"]:
                    continue
                if info["runId"] != previous["runId"]:
                    if self._order(info) <= self._order(previous):
                        continue
                elif info["version"] == previous["version"]:
                    # A local lost observation has no authority to invent a new
                    # native version. A reconnect can restore the same revision.
                    if (
                        previous["observation"] != "lost"
                        or info["observation"] == "lost"
                    ):
                        continue
            await self._publish(info)
        for agent_id, info in list(self.rows.items()):
            if agent_id not in latest and info["status"] == "running":
                await self._publish({**info, "observation": "lost"})
        finished = sorted(
            (
                info
                for info in self.rows.values()
                if info["status"] != "running" or info["observation"] == "lost"
            ),
            key=self._order,
            reverse=True,
        )
        for info in finished[256:]:
            self.rows.pop(info["agentId"], None)
            self._retired_before = max(
                self._retired_before or self._order(info), self._order(info)
            )

    @staticmethod
    def _order(info: dict[str, Any]) -> tuple[str, int]:
        return str(info["startedAt"]), int(info["version"])

    async def lost(self) -> None:
        for info in list(self.rows.values()):
            if info["status"] == "running":
                await self._publish({**info, "observation": "lost"})

    async def _publish(self, info: dict[str, Any]) -> None:
        previous = self.rows.get(info["agentId"])
        if previous == info:
            return
        live = info["observation"] == "live"
        status = info["status"]
        if not live:
            text = (
                f"Agent observation is {info['observation']}; execution is unconfirmed."
            )
        else:
            text = f"Agent {status}."
        await self.update(
            self.session_id,
            {
                "sessionUpdate": "tool_call"
                if previous is None
                else "tool_call_update",
                "toolCallId": "yoke-agent:" + info["agentId"],
                "kind": "other",
                "title": info["name"] or info["provider"],
                "status": "in_progress"
                if live and status == "running"
                else "completed"
                if live and status == "completed"
                else "failed",
                "rawOutput": {"type": "yoke_agent", **info},
                "content": [
                    {"type": "content", "content": {"type": "text", "text": text}}
                ],
            },
        )
        self.rows[info["agentId"]] = info
