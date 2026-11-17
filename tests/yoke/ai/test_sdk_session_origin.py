"""In-process SDK state stays portable; its database writes identify SDK usage."""

from pathlib import Path

from tests.yoke.ai.test_usage_attribution import UsageProvider
from yoke.ai.sdk import Agent, RunConfig
from yoke.session import SessionStore


def test_sdk_snapshot_does_not_create_a_mislabelled_daemon_session(tmp_path: Path):
    store = SessionStore(migrate=True)
    agent = Agent(
        provider=UsageProvider(),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    try:
        agent.prompt("hello")
        snapshot = agent.save(tmp_path / "snapshot.json")
        assert snapshot.is_file()
    finally:
        agent.close()
    with store.database.read() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
        assert db.execute("SELECT DISTINCT surface FROM usage_events").fetchall() == [
            ("sdk",)
        ]
