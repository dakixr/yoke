"""Global live event and durable history transport models."""

from __future__ import annotations

from yoke.http.models.common import ApiModel
from yoke.http.models.common import LocationInfo


class DurableEventInfo(ApiModel):
    aggregate_id: str
    seq: int
    version: int = 1


class PublicEvent(ApiModel):
    id: str
    type: str
    time: str
    session_id: str | None = None
    location: LocationInfo | None = None
    durable: DurableEventInfo | None = None
    data: dict[str, object]


class HistoryResponse(ApiModel):
    data: list[PublicEvent]
    has_more: bool

