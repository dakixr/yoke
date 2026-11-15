"""Process-wide live SSE and finite per-session durable history."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from fastapi.responses import StreamingResponse

from yoke.daemon.lifecycle import DaemonLifecycle
from yoke.http.auth import require_auth
from yoke.http.models.event import HistoryResponse
from yoke.http.services.event_broker import GlobalEventBroker
from yoke.http.services.event_broker import live_event
from yoke.http.services.event_broker import public_event_from_session_event
from yoke.session.events import SessionEventJournal


router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/event", operation_id="streamEvents")
async def stream_events(
    request: Request,
    client: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        description="Name this client; under `yoke serve --daemon` the stream is a lease.",
    ),
) -> StreamingResponse:
    broker: GlobalEventBroker = request.app.state.event_broker
    lifecycle: DaemonLifecycle | None = getattr(request.app.state, "daemon", None)
    subscription = broker.subscribe()
    connected = live_event(
        "server.connected",
        {"serverInstanceID": request.app.state.server_instance_id},
    )

    async def body():  # noqa: ANN202
        # Opened inside the body so the generator's finally always closes it.
        lease = (
            lifecycle.open_lease(client)
            if lifecycle is not None and client is not None
            else None
        )
        try:
            yield _sse(connected)
            while not subscription.closed or not subscription.queue.empty():
                try:
                    event = await asyncio.wait_for(subscription.queue.get(), timeout=20)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    return
                yield _sse(event)
        finally:
            broker.unsubscribe(subscription)
            if lease is not None and lifecycle is not None:
                lifecycle.close_lease(lease)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/session/{session_id}/history",
    response_model=HistoryResponse,
    operation_id="sessionHistory",
)
def session_history(
    request: Request,
    session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> HistoryResponse:
    request.app.state.session_service.get_session(session_id)
    journal: SessionEventJournal = request.app.state.event_journal
    events, has_more = journal.history(session_id, after=after, limit=limit)
    return HistoryResponse(
        data=[public_event_from_session_event(event) for event in events],
        has_more=has_more,
    )


def _sse(event) -> str:  # noqa: ANN001
    data = event.model_dump_json(by_alias=True)
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"
