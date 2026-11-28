from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yoke.agent.capabilities.builtins import McpCapability
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools.context import (
    ModelIdentity,
    ToolRegistrationContext,
    ToolRegistrationResult,
    ToolSessionContext,
)
from yoke.ai.providers.base import Provider
from yoke.cli.bootstrap.config import resolve_agent_config
from yoke.mcp.config import (
    McpServerConfig,
    McpSessionPolicy,
    McpSessionServerPolicy,
    load_mcp_config,
)


class StubProvider(Provider):
    provider_name = "stub"
    supports_image_inputs = False

    def __init__(self, model: str = "stub-model") -> None:
        self.config = SimpleNamespace(model=model)

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("ok")


def is_available_mcp_servers_message(message: Message) -> bool:
    return message.role == "system" and (message.plain_text_content or "").startswith(
        "Available MCP servers:\n"
    )


def _provider_messages(agent: RuntimeAgent) -> list[Message]:
    context = agent.context_for_run(
        "Open Hacker News",
        user_message=None,
        available_skills=None,
        active_skills=None,
    )
    return agent.context_manager.messages_for_provider(context)


def _write_mcp_json(directory: Path, servers: dict[str, object]) -> None:
    yoke_dir = directory / ".yoke"
    yoke_dir.mkdir(parents=True, exist_ok=True)
    (yoke_dir / "mcp.json").write_text(
        json.dumps({"mcp_servers": servers}),
        encoding="utf-8",
    )


def _registration_context(
    *,
    root: Path,
    home: Path,
    mcp_policy: McpSessionPolicy | None = None,
) -> ToolRegistrationContext:
    provider = StubProvider()
    return ToolRegistrationContext(
        root=root,
        home=home,
        provider=provider,
        model=ModelIdentity(
            provider_name=provider.provider_name,
            model_id=provider.config.model,
            supports_image_inputs=False,
        ),
        session=ToolSessionContext(mcp_policy=mcp_policy),
    )


def test_mcp_config_parses_optional_description(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    root.mkdir()
    _write_mcp_json(
        root,
        {
            "docs": {
                "command": "docs-mcp",
                "description": "  Search   internal docs  ",
            },
            "plain": {
                "command": "plain-mcp",
            },
        },
    )

    config = load_mcp_config(root=root, home=home)
    by_name = {server.name: server for server in config.servers}
    assert by_name["docs"].description == "Search   internal docs"
    assert by_name["plain"].description is None


def test_mcp_capability_emits_compact_catalog_without_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    root.mkdir()
    _write_mcp_json(
        home,
        {
            "github": {
                "command": "github-mcp",
                "description": "Old global description",
            },
            "disabled_server": {
                "command": "disabled-mcp",
                "enabled": False,
                "description": "Should not appear",
            },
            "session_disabled": {
                "command": "session-disabled-mcp",
                "description": "Disabled by session policy",
            },
        },
    )
    _write_mcp_json(
        root,
        {
            "github": {
                "command": "github-mcp",
                "description": "Issues and pull requests",
            },
            "plain": {
                "command": "plain-mcp",
            },
        },
    )

    def _fail_connect(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "MCP client connection should not happen during catalog build"
        )

    monkeypatch.setattr("yoke.mcp.client.create_mcp_client", _fail_connect)

    policy = McpSessionPolicy(
        servers={
            "session_disabled": McpSessionServerPolicy(enabled=False),
        },
        injected_servers=(
            McpServerConfig(
                name="injected",
                transport="stdio",
                command="injected-mcp",
                description="Session injected server",
            ),
        ),
    )
    registration = McpCapability().resolve(
        _registration_context(root=root, home=home, mcp_policy=policy)
    )

    assert [tool.name for tool in registration.tools] == ["mcp_inspect", "mcp_call"]
    agent = RuntimeAgent(provider=StubProvider(), tools=list(registration.tools))
    try:
        (catalog,) = [
            message.plain_text_content
            for message in _provider_messages(agent)
            if is_available_mcp_servers_message(message)
        ]
    finally:
        agent.close()
    assert catalog == (
        "Available MCP servers:\n"
        "If a server matches the task or the user asks to use MCP, call `mcp_inspect` before choosing a tool. "
        "Pass the matching `server` name when known.\n"
        "- github: Issues and pull requests\n"
        "- injected: Session injected server\n"
        "- plain"
    )
    assert "disabled_server" not in catalog
    assert "session_disabled" not in catalog
    assert "Old global description" not in catalog


def test_mcp_catalog_omitted_when_no_enabled_servers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    root.mkdir()
    _write_mcp_json(
        root,
        {
            "off": {
                "command": "off-mcp",
                "enabled": False,
            }
        },
    )

    registration = McpCapability().resolve(_registration_context(root=root, home=home))
    assert registration.tools == ()
    assert registration.system_messages == ()


def test_mcp_catalog_omitted_when_inspect_tool_filtered_by_workspace_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    root.mkdir()
    _write_mcp_json(
        root,
        {
            "github": {
                "command": "github-mcp",
                "description": "Issues and PRs",
            }
        },
    )
    yoke_dir = root / ".yoke"
    (yoke_dir / "config.json").write_text(
        '{"capabilities":{"mcp":"allow"},"tools":{"mcp_inspect":"deny"}}',
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=root,
        home=home,
        provider=StubProvider(),
        include_agents_file=False,
    )
    assert {tool.name for tool in resolved.tools} & {"mcp_inspect", "mcp_call"} == {
        "mcp_call"
    }
    agent = RuntimeAgent(provider=StubProvider(), tools=resolved.tools)
    try:
        assert not any(map(is_available_mcp_servers_message, _provider_messages(agent)))
    finally:
        agent.close()

    (yoke_dir / "config.json").write_text(
        '{"capabilities":{"mcp":"allow"}}',
        encoding="utf-8",
    )
    resolved_with_inspect = resolve_agent_config(
        root=root,
        home=home,
        provider=StubProvider(),
        include_agents_file=False,
    )
    assert {tool.name for tool in resolved_with_inspect.tools} >= {
        "mcp_inspect",
        "mcp_call",
    }
    agent = RuntimeAgent(provider=StubProvider(), tools=resolved_with_inspect.tools)
    try:
        assert any(map(is_available_mcp_servers_message, _provider_messages(agent)))
    finally:
        agent.close()


def test_mcp_catalog_omitted_when_inspect_tool_filtered_by_session_allowlist(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    root.mkdir()
    _write_mcp_json(
        root,
        {
            "github": {
                "command": "github-mcp",
                "description": "Issues and PRs",
            }
        },
    )

    def _factory(context: ToolRegistrationContext) -> ToolRegistrationResult:
        registration = McpCapability().resolve(context)
        return ToolRegistrationResult(
            tools=registration.tools,
            system_messages=registration.system_messages,
        )

    agent = RuntimeAgent(
        provider=StubProvider(),
        tools=[],
        tool_factory=_factory,
        tool_root=root,
        tool_home=home,
    )
    try:
        assert any(
            is_available_mcp_servers_message(message)
            for message in _provider_messages(agent)
        )

        agent.set_session_enabled_tools({"mcp_call"})
        assert list(agent.tools) == ["mcp_call"]
        assert not any(
            is_available_mcp_servers_message(message)
            for message in _provider_messages(agent)
        )

        agent.set_session_enabled_tools({"mcp_inspect", "mcp_call"})
        assert any(
            is_available_mcp_servers_message(message)
            for message in _provider_messages(agent)
        )
    finally:
        agent.close()
