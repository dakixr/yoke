"""Optional client fields must leave established provider requests unchanged."""

import pytest

from yoke.agent.models import Message
from yoke.agent.tools.client import client_tools
from yoke.agent.tools.context import ToolRegistrationResult, normalize_tool_registration
from yoke.http.runtime import client_tools as module
from yoke.session import SessionStore

from .drivers import Daemon
from .golden import Normalizer, encode
from .recording import Tape
from .runtime import World
from .test_oracle import scenario, turn


def established_factory(original, initial):
    """The pre-3.46 projection when no optional client fields were supplied."""

    def register(context):
        base = normalize_tool_registration(original(context) if original else initial)
        client = context.session.client_session
        if client is None or client.config.kind != "acp":
            return base
        tools = list(base.tools)
        names = {tool.name for tool in tools}
        tools.extend(
            tool
            for tool in client_tools(client.turn_actions())
            if tool.name not in names
        )
        instructions = list(base.system_messages)
        if client.config.mode == "plan":
            instructions.append(
                Message.system(
                    "The client selected plan mode. Inspect files and develop a plan. File mutations, command execution, and unclassified MCP calls are blocked until the client switches mode."
                )
            )
        return ToolRegistrationResult(tools=tools, system_messages=instructions)

    return register


@pytest.mark.parametrize("kind", ["cli", "web", "acp"])
@pytest.mark.parametrize(
    "name",
    [
        "tools",
        "inline_skill",
        "images_supported",
        "manual_compaction",
        "checkpoint_stop",
        "tree_navigation",
    ],
)
def test_client_without_optional_fields_preserves_provider_requests(
    tmp_path, monkeypatch, kind, name
):
    projections = []
    current_factory = module.client_tool_factory
    for legacy in (True, False):
        directory = tmp_path / ("established" if legacy else "current")
        monkeypatch.setenv("YOKE_SESSION_DIR", str(directory / "sessions"))
        monkeypatch.setattr(
            module,
            "client_tool_factory",
            established_factory if legacy else current_factory,
        )
        tape = Tape()
        world = World(directory / "workspace", directory / "sessions", name, tape)
        normalizer = Normalizer(world.root)
        driver = Daemon(world)
        try:
            driver.request(
                f"/session/{driver.session_id}/client",
                {"owner": "client", "kind": kind},
                method="PUT",
            )
            scenario(driver, name, normalizer)
            turn(driver, "Continue with exactly the same provider context.")
            normalizer.register(SessionStore(world.directory).load(driver.session_id))
            projections.append(encode(normalizer.normalize(tape.calls)))
            assert not tape.steps
        finally:
            driver.close()
    assert projections[0] == projections[1]
