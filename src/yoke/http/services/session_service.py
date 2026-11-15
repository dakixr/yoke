"""HTTP session application entry points and idle-gate policy."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

from yoke.http.errors import ApiError
from yoke.http.models.session import (
    SessionForkRequest,
    SessionInfo,
    SessionPatchRequest,
)
from yoke.http.services.event_broker import EventService
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.http.services.session_catalog import SessionCatalog
from yoke.http.services.session_forks import SessionForks
from yoke.http.services.session_message_index import SessionMessageIndex
from yoke.http.services.session_messages import SessionMessages
from yoke.http.services.session_read_cache import SessionReadCache
from yoke.http.services.session_read_model import SessionReadModel
from yoke.http.services.session_tree_mutations import SessionTreeMutations
from yoke.http.services.session_tree_reads import SessionTreeReads
from yoke.session import SessionStore
from yoke.session.workspace import workspace_lease


class SessionService(SessionCatalog):
    """Compose catalog, transcript, and tree ownership for HTTP callers."""

    def __init__(
        self,
        store: SessionStore,
        events: EventService | None = None,
        read_cache: SessionReadCache | None = None,
        *,
        message_index: SessionMessageIndex,
    ) -> None:
        self.message_index = message_index
        self.read_cache = read_cache or SessionReadCache(store)
        read_model = SessionReadModel(store, message_index, self.read_cache)
        super().__init__(store, events, read_model)
        messages = SessionMessages(read_model, events)
        trees = SessionTreeReads(read_model, events)
        mutations = SessionTreeMutations(read_model, self)
        self.messages = messages.messages
        self.message = messages.message
        self.context = messages.context
        self.tree = trees.tree
        self.navigation_preview = trees.navigation_preview
        self.navigate_tree = mutations.navigate_tree
        self.set_tree_label = mutations.set_tree_label
        self.forks = SessionForks(store, message_index, read_model, self.publish)

    def close(self) -> None:
        self.message_index.close()

    def fork_session(self, session_id: str, request: SessionForkRequest) -> SessionInfo:
        record = self.forks.fork(session_id, request)
        return self.session_info(self.store.index_entry(record.id) or record)

    async def patch(
        self,
        runtimes: SessionRuntimeRegistry,
        session_id: str,
        body: SessionPatchRequest,
    ) -> SessionInfo:
        fields = body.model_fields_set
        if not fields:
            raise ApiError(400, "empty_mutation", "At least one field is required.")
        # Validate existence before choosing a gate, without projecting a card.
        await asyncio.get_running_loop().run_in_executor(
            runtimes.executor, self.read_model.summary, session_id
        )
        runtime = runtimes.get_or_start(session_id)
        loop = asyncio.get_running_loop()

        def cancel_title() -> None:
            completion: Future[None] = Future()

            def cancel_on_loop() -> None:
                try:
                    runtimes.cancel_automatic_title(session_id)
                    completion.set_result(None)
                except BaseException as exc:
                    completion.set_exception(exc)

            loop.call_soon_threadsafe(cancel_on_loop)
            completion.result()

        def mutate() -> SessionInfo:
            with workspace_lease(self.store, session_id), runtime.persistence.lock:
                return self.patch_session(
                    session_id,
                    title_set="title" in fields,
                    title=body.title,
                    pinned_set="pinned" in fields,
                    pinned=body.pinned,
                    archived_set="archived" in fields,
                    archived=body.archived,
                    before_mutation=cancel_title if "title" in fields else None,
                )

        if body.archived is True:
            return await runtime.idle_mutation(mutate)
        async with runtime.gate:
            return await runtime.run_blocking(mutate)
