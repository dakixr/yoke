"""Memory-only client attachments and admission-time ownership snapshots."""

from __future__ import annotations

from threading import RLock

from yoke.http.errors import ApiError
from yoke.http.models.prompt import PromptAdmissionRequest, PromptAdmissionReceipt
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.client_session import ClientSession


class SessionClients:
    """Retain independent attachments and the owner of each admitted prompt."""

    def __init__(self) -> None:
        self._clients: dict[str, ClientSession] = {}
        self._inputs: dict[str, ClientSession | None] = {}
        self.fallback: ClientSession | None = None
        self._lock = RLock()

    def attach(self, client: ClientSession) -> None:
        with self._lock:
            previous = self._clients.pop(client.config.owner, None)
            if previous is not None:
                client.latest_plan = previous.latest_plan
            self._clients[client.config.owner] = client
            self.fallback = client.environment_fallback()

    def detach(self, owner: str) -> None:
        with self._lock:
            self._clients.pop(owner, None)

    def select(self, owner: str | None = None) -> ClientSession | None:
        with self._lock:
            if owner is not None:
                client = self._clients.get(owner)
                if client is None:
                    raise ApiError(
                        409,
                        "client_not_attached",
                        "Attach this client before admitting a prompt.",
                    )
                return client
            return next(reversed(self._clients.values()), None)

    def admit(
        self,
        pending: PendingInputService,
        session_id: str,
        request: PromptAdmissionRequest,
    ) -> PromptAdmissionReceipt:
        """Publish durable admission and its private owner under one memory gate."""
        with self._lock:
            client = self.select(request.client_id) or self.fallback
            receipt = pending.admit(session_id, request)
            self._inputs.setdefault(receipt.id, client)
            return receipt

    def for_input(self, input_id: str) -> ClientSession | None:
        with self._lock:
            return self._inputs.get(input_id, self.select() or self.fallback)

    def owners(self) -> list[str]:
        with self._lock:
            return list(self._clients)
