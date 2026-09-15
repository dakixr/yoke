from __future__ import annotations

import pytest

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.session.workspace import promote_session_turn


class Provider:
    def complete(self, messages, tools):
        return Message.assistant("finished answer")


@pytest.mark.parametrize("deleted", [False, True])
def test_completed_turn_is_preserved_when_only_workspace_refresh_fails(
    tmp_path, deleted, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    primary = RuntimeAgent(Provider(), [], tool_root=root)
    fork = primary.fork()
    fork.run("a completed request")
    expected = fork.conversation_entries

    def refresh(*, force=False):
        assert force
        raise ValueError("tool refresh failure")

    monkeypatch.setattr(primary, "refresh_tools", refresh)
    try:
        if deleted:
            root.rmdir()
            promote_session_turn(primary, fork)
            assert primary.conversation_entries == expected
            assert not root.exists()
        else:
            with pytest.raises(ValueError, match="tool refresh failure"):
                promote_session_turn(primary, fork)
    finally:
        primary.close()
        fork.close()
