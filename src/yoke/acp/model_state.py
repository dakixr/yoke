"""Advertise every model's reasoning efforts through ACP session model state.

ACP config options describe only the session's current model, so a client that
lists models from them can show an effort selector for that model alone. The
unstable ``models`` field carries one entry per model; T3 Code reads each
entry's ``_meta`` for its effort choices. agent-client-protocol 0.12 does not
model that field, so these response types add it.
"""

from __future__ import annotations

from typing import Any

from acp import schema as s

from yoke.acp.state import AcpSession


class NewSessionResponse(s.NewSessionResponse):
    models: dict[str, Any] | None = None


class LoadSessionResponse(s.LoadSessionResponse):
    models: dict[str, Any] | None = None


class ResumeSessionResponse(s.ResumeSessionResponse):
    models: dict[str, Any] | None = None


class ForkSessionResponse(s.ForkSessionResponse):
    models: dict[str, Any] | None = None


def model_state(
    catalog: dict[str, dict[str, Any]], selection: AcpSession
) -> dict[str, Any]:
    """Return ACP session model state with per-model reasoning metadata."""
    available = []
    for model in catalog.values():
        efforts = model["reasoningEfforts"]
        current = (
            selection.effort
            if model["id"] == selection.model
            else model["defaultReasoningEffort"]
        )
        meta: dict[str, Any] = (
            {
                "reasoningEfforts": [
                    {"value": effort, "label": effort} for effort in efforts
                ],
                "reasoningEffort": current,
            }
            if efforts
            else {"supportsReasoningEffort": False}
        )
        available.append({"modelId": model["id"], "name": model["name"], "_meta": meta})
    return {"currentModelId": selection.model, "availableModels": available}
