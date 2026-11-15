"""Child process for storage durability tests: write forever, acknowledge each commit.

Usage: python writer_child.py SESSION_DIR SESSION_ID ITERATION

Each committed write prints one acknowledgement line *after* its transaction
returns, so the parent knows exactly which writes were durable when it sent
SIGKILL.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from yoke.agent.models import ConversationEntry, Message
from yoke.session import SessionStore, SessionTreeIndex
from yoke.session.events import SessionEventJournal
from yoke.session.queue import PersistedPendingInput, prompt_queue_transaction


def ack(*parts: object) -> None:
    sys.stdout.write(" ".join(str(part) for part in parts) + "\n")
    sys.stdout.flush()


def main() -> None:
    directory = Path(sys.argv[1])
    session_id = sys.argv[2]
    iteration = int(sys.argv[3])
    store = SessionStore(directory)
    journal = SessionEventJournal(directory)
    record = store.load(session_id)
    if record.created_at is None:
        record = store.save(session_id, [], root=directory)
    tree = SessionTreeIndex(record.conversation_entries, record.leaf_id)
    ack("ready")
    step = 0
    while True:
        step += 1
        tag = f"{iteration}.{step}"
        entry = ConversationEntry(
            kind="user",
            message=Message(role="user", content=f"message {tag} " + "x" * 2000),
            parent_id=record.leaf_id,
        )
        record = store.save(
            session_id,
            [],
            conversation_entries=[*tree.active_entry_refs(), entry],
            leaf_id=entry.id,
            existing_record=record,
            tree_index=tree,
        )
        ack("entry", entry.id)
        record = store.set_title(session_id, f"title {tag}", existing_record=record)
        ack("title", iteration, step)
        with prompt_queue_transaction(directory, session_id) as transaction:
            queue = transaction.snapshot
            queue.prompts.append(
                PersistedPendingInput(
                    id=f"in-{tag}",
                    prompt=f"queued {tag}",
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
            del queue.prompts[:-5]
            queue.revision += 1
            transaction.commit()
            revision = queue.revision
        ack("queue", revision)
        event = journal.append(session_id, "test.step", {"tag": tag})
        ack("event", event.seq)


if __name__ == "__main__":
    main()
