"""Client-provided questions and plans, available only in attached sessions."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, Literal, Protocol, cast

from pydantic import BaseModel, Field

from yoke.agent.tools.base import LocalTool


class ClientActions(Protocol):
    def question(
        self,
        question: str,
        options: list[str],
        multiple: bool,
        cancelled: Callable[[], bool],
    ) -> dict[str, object]: ...
    def plan(
        self, steps: list[dict[str, str]], explanation: str | None
    ) -> dict[str, object]: ...


class PlanStep(BaseModel):
    step: str = Field(min_length=1)
    status: Literal["pending", "in_progress", "completed"]


class UpdatePlanTool(LocalTool):
    name = "update_plan"
    description = "Publish or replace the task plan shown to the user. At most one step may be in progress."
    execute_in_process = True
    plan: list[PlanStep] = Field(max_length=100)
    explanation: str | None = None

    def execute(self) -> dict[str, object]:
        if sum(step.status == "in_progress" for step in self.plan) > 1:
            return {"ok": False, "error": "At most one plan step may be in progress"}
        actions = cast(ClientActions, self._context["client_actions"])
        return actions.plan([step.model_dump() for step in self.plan], self.explanation)


class RequestUserInputTool(LocalTool):
    name = "request_user_input"
    description = "Ask the user a question and wait for their answer. Omit options for a free-text answer."
    execute_in_process = True
    preserve_cancelled_result: ClassVar[bool] = True
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list, max_length=30)
    multiple: bool = False

    def execute(self) -> dict[str, object]:
        actions = cast(ClientActions, self._context["client_actions"])
        return actions.question(
            self.question, self.options, self.multiple, self._is_cancel_requested
        )


def client_tools(actions: ClientActions) -> tuple[LocalTool, ...]:
    return (
        UpdatePlanTool.bind(client_actions=actions),
        RequestUserInputTool.bind(client_actions=actions),
    )
