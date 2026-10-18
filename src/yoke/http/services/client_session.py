"""Client-owned tool policy and human input for an HTTP session."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.loop.types import BeforeToolCallContext, BeforeToolCallResult
from yoke.http.models.client_session import ClientSessionRequest
from yoke.http.services.human_input_service import HumanInputService
from yoke.http.services.event_broker import EventService

if TYPE_CHECKING:
    from yoke.agent.loop.types import BeforeToolCallHook

READ_TOOLS = frozenset(
    {
        "read",
        "extract_file_context",
        "rg",
        "fd",
        "grep",
        "find",
        "ls",
        "web_fetch",
        "web_search",
        "web_research",
        "attach_image",
        "mcp_inspect",
        "process_read",
        "skill",
        "update_plan",
        "request_user_input",
    }
)
EDIT_TOOLS = frozenset({"write", "edit", "apply_patch"})


class ClientSession:
    """Hold only process-local client state, never persisted bearer credentials."""

    def __init__(
        self,
        session_id: str,
        config: ClientSessionRequest,
        human_input: HumanInputService,
        events: EventService,
        root: Path,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.human_input = human_input
        self.events = events
        self.root = root
        self.latest_plan: dict[str, object] | None = None

    def before_tool(
        self,
        original: BeforeToolCallHook | None,
        cancelled: Callable[[], bool],
    ) -> BeforeToolCallHook:
        def check(context: BeforeToolCallContext) -> BeforeToolCallResult | None:
            name = context.tool_call.function.name
            mode = self.config.mode
            allowed = (
                name in READ_TOOLS
                or mode == "full-access"
                or (mode == "auto-accept-edits" and name in EDIT_TOOLS)
            )
            if not allowed:
                if mode == "plan":
                    return BeforeToolCallResult(
                        block=True,
                        reason="Plan mode permits read-only tools. Switch mode before executing changes.",
                    )
                kind = (
                    "edit"
                    if name in EDIT_TOOLS
                    else "execute"
                    if name
                    in {
                        "command_exec",
                        "python_exec",
                        "process_input",
                        "process_cancel",
                    }
                    else "other"
                )
                info = self.human_input.create_permission(
                    self.session_id,
                    permission=name,
                    message=f"Run {name}?",
                    tool_call={
                        "toolCallId": context.tool_call.id,
                        "title": name,
                        "kind": kind,
                        "rawInput": context.arguments,
                    },
                )
                while True:
                    if cancelled():
                        self.human_input.reply_permission(
                            self.session_id, info.id, reply="deny"
                        )
                    try:
                        reply = self.human_input.wait_permission(info.id, 0.1)
                        break
                    except TimeoutError:
                        continue
                if reply.reply != "allow" or cancelled():
                    return BeforeToolCallResult(
                        block=True, reason="Tool permission denied or cancelled"
                    )
            return original(context) if original else None

        return check

    def question(
        self,
        question: str,
        options: list[str],
        multiple: bool,
        cancelled: Callable[[], bool],
    ) -> dict[str, object]:
        info = self.human_input.create_question(
            self.session_id,
            question=question,
            options=options,
            multiple=multiple,
        )
        while True:
            if cancelled():
                self.human_input.reject_question(self.session_id, info.id)
            try:
                reply = self.human_input.wait_question(info.id, 0.1)
                return {
                    "ok": not reply.rejected,
                    "answers": reply.answers,
                    "rejected": reply.rejected,
                }
            except TimeoutError:
                continue

    def plan(
        self, steps: list[dict[str, str]], explanation: str | None
    ) -> dict[str, object]:
        self.latest_plan = {"steps": steps, "explanation": explanation}
        self.events.live(
            "session.plan.updated",
            self.latest_plan,
            session_id=self.session_id,
            location=str(self.root),
        )
        return {"ok": True}
