"""Concurrent commit callbacks must publish durable events in journal order."""

from yoke.http.services.event_broker import EventService, GlobalEventBroker
from yoke.session import SessionStore
from yoke.session.events import SessionEventJournal


def test_later_commit_callback_cannot_overtake_settlement(tmp_path, monkeypatch):
    SessionStore(tmp_path, migrate=True)
    events = EventService(SessionEventJournal(tmp_path), GlobalEventBroker())
    callbacks, published = [], []
    monkeypatch.setattr(events.journal, "after_commit", callbacks.append)
    monkeypatch.setattr(
        events.broker, "publish", lambda event, **kw: published.append(event)
    )
    events.durable(
        "session", "session.prompt.settled", {"inputID": "one", "outcome": "completed"}
    )
    events.durable("session", "session.updated", {"title": "Automatic title"})
    for callback in reversed(callbacks):
        callback()
    assert [event.durable.seq for event in published] == [1, 2]
    assert [event.type for event in published] == [
        "session.prompt.settled",
        "session.updated",
    ]


def test_recreated_session_starts_a_new_publication_cursor(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, migrate=True)
    store.save("session", [])
    events = EventService(SessionEventJournal(tmp_path), GlobalEventBroker())
    published = []
    monkeypatch.setattr(
        events.broker, "publish", lambda event, **kw: published.append(event)
    )
    events.durable("session", "session.created", {})
    events.durable("session", "session.updated", {})
    assert store.delete("session")
    store.save("session", [])
    events.durable("session", "session.created", {})
    assert [event.durable.seq for event in published] == [1, 2, 1]
