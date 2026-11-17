"""Failed manual compaction must release storage maintenance."""

from tests.yoke.storage.test_image_codec import quiet, legacy
from yoke.agent.models import Message
from yoke.session import SessionStore
from yoke.session.events import SessionEventJournal
from yoke.storage.compactor import compact_batch


def test_b8_failed_compaction_closes_busy_marker(tmp_path):
    store = SessionStore(tmp_path, migrate=True)
    store.save("failed", [Message.user("hello"), Message.assistant("answer")])
    legacy(store.database)
    journal = SessionEventJournal(tmp_path)
    journal.append("failed", "session.compaction.started", {"operationID": "op"})
    journal.append("failed", "session.compaction.failed", {"operationID": "op"})
    quiet(store.database)
    with store.database.write() as connection:
        connection.execute("UPDATE events SET time = '2000-01-01'")
    assert compact_batch(store.database).processed > 0
