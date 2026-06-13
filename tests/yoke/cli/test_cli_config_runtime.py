from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN003, ANN202, D100, D103, F403, F405, S101

from .support import *  # noqa: F403, F405
from yoke.cli.config import format_provider_model_status


def test_build_agent_binds_provider_into_tool_context(
    tmp_path: Path, monkeypatch
) -> None:
    install_builtin_provider(monkeypatch, ConfigOnlyProvider)

    agent = build_agent_from_args(
        CLIArgs(model="codex:gpt-5.4", root=str(tmp_path))
    )

    web_research = agent.tools["web_research"]
    assert web_research._context["provider"] is agent.provider
    assert web_research.context.provider is agent.provider
    assert web_research.context.provider_name == "codex"
    assert web_research.context.model_key == "codex:gpt-5.4"


def test_cli_registration_context_matches_runtime_context(
    tmp_path: Path, monkeypatch
) -> None:
    tools_dir = tmp_path / ".yoke"
    tools_dir.mkdir()
    (tools_dir / "config.json").write_text(
        '{"tools": {"inspect_model": "allow"}}\n',
        encoding="utf-8",
    )
    (tools_dir / "inspect_model.py").write_text(
        """
from yoke.agent.tools import LocalTool


class InspectModelTool(LocalTool):
    name = "inspect_model"
    description = "Inspect registration and runtime model metadata."

    def execute(self) -> dict[str, object]:
        return {
            "ok": True,
            "registration_model": self._context["registration_model"],
            "registration_provider": self._context["registration_provider"],
            "runtime_model": self.context.model_key,
            "runtime_provider": self.context.provider,
        }


def register_tools(context):
    return [
        InspectModelTool.bind(
            registration_model=context.model_key,
            registration_provider=context.provider,
        )
    ]
""".strip(),
        encoding="utf-8",
    )
    install_builtin_provider(monkeypatch, ConfigOnlyProvider)

    agent = build_agent_from_args(
        CLIArgs(model="codex:gpt-5.4", root=str(tmp_path))
    )
    result = agent.tools["inspect_model"].execute()

    assert result == {
        "ok": True,
        "registration_model": "codex:gpt-5.4",
        "registration_provider": agent.provider,
        "runtime_model": "codex:gpt-5.4",
        "runtime_provider": agent.provider,
    }


def test_cli_selects_one_write_tool_from_model_id(
    tmp_path: Path, monkeypatch
) -> None:
    install_builtin_provider(monkeypatch, ConfigOnlyProvider)

    gpt_agent = build_agent_from_args(
        CLIArgs(model="codex:gpt-5.4", root=str(tmp_path))
    )
    non_gpt_agent = build_agent_from_args(
        CLIArgs(model="codex:kimi-k2.7-code", root=str(tmp_path))
    )

    assert "apply_patch" in gpt_agent.tools
    assert "edit" not in gpt_agent.tools
    assert "edit" in non_gpt_agent.tools
    assert "apply_patch" not in non_gpt_agent.tools


def test_build_agent_preserves_agents_file_system_message(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "Always inspect todo.json first.", encoding="utf-8"
    )
    install_builtin_provider(monkeypatch, ConfigOnlyProvider)

    agent = build_agent_from_args(CLIArgs(model="codex:gpt-5.4", root=str(tmp_path)))

    system_messages = agent.context_manager.instructions
    assert len(system_messages) == 3
    assert "You are yoke running in the yoke CLI" in (system_messages[0].content or "")
    assert "Use the `apply_patch` tool" not in (system_messages[0].content or "")
    assert "Always inspect todo.json first." in (system_messages[1].content or "")
    assert "Use the `apply_patch` tool" in (system_messages[2].content or "")
    assert agent.context_manager.max_total_tokens == 400_000


def test_cli_rejects_unknown_provider(capsys) -> None:
    exit_code = main(["--model", "other:gpt-test", "hello"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Unsupported provider 'other'" in captured.err
    assert "Supported providers: codex," in captured.err
    assert "opencode-go" in captured.err


def test_build_agent_honors_explicit_opencode_go_provider(
    tmp_path: Path, monkeypatch
) -> None:
    install_builtin_provider(
        monkeypatch,
        ConfigOnlyProvider,
        provider_name="opencode-go",
    )

    agent = build_agent_from_args(
        CLIArgs(model="opencode-go:deepseek-v4-pro", root=str(tmp_path))
    )

    assert agent.provider.__class__.__name__ == "ConfigOnlyProvider"


def test_cli_prefers_opencode_go_when_only_credentials_exist(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("YOKE_COPILOT_AUTH_PATH", raising=False)
    monkeypatch.setattr("yoke.cli.config.providers.Path.home", lambda: tmp_path)
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    install_builtin_provider(
        monkeypatch,
        ConfigOnlyProvider,
        provider_name="opencode-go",
    )

    agent = build_agent_from_args(CLIArgs(root=str(tmp_path)))

    assert agent.provider.__class__.__name__ == "ConfigOnlyProvider"


def test_build_agent_includes_reasoning_effort_in_status(
    tmp_path: Path, monkeypatch
) -> None:
    install_builtin_provider(monkeypatch, ConfigOnlyProvider)

    agent = build_agent_from_args(
        CLIArgs(
            model="codex:gpt-5.4",
            reasoning_effort="high",
            root=str(tmp_path),
        )
    )

    assert format_provider_model_status(agent) == ("ConfigOnlyProvider gpt-5.4 high")


def test_cli_reasoning_effort_value_is_not_treated_as_prompt(
    capsys,
) -> None:
    from yoke.cli.main import _inject_prompt_flag

    argv = _inject_prompt_flag(["--reasoning-effort", "high", "hello"])

    captured = capsys.readouterr()
    assert argv == ["--reasoning-effort", "high", "--prompt", "hello"]
    assert "No such command 'high'" not in captured.err


def test_build_agent_derives_compaction_budget_from_provider_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    install_builtin_provider(monkeypatch, CatalogProvider)

    agent = build_agent_from_args(CLIArgs(model="codex:gpt-5.4", root=str(tmp_path)))

    assert agent.context_manager.max_total_tokens == 200_000
    assert agent.context_manager.compaction_policy.reserved_output_tokens == 32_000
    assert agent.context_manager.compactor.model == "gpt-5.4"


def test_large_context_provider_budget_does_not_compact_near_default_window(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeLargeContextProvider(CatalogProvider):
        provider_name = "opencode-go"
        context_window_tokens = 1_000_000

    install_builtin_provider(monkeypatch, FakeLargeContextProvider)

    agent = build_agent_from_args(CLIArgs(model="codex:gpt-5.4", root=str(tmp_path)))
    messages = [Message.user("x" * (209_000 * 4))]
    estimate = agent.context_manager.estimate_tokens(messages)

    assert agent.context_manager.max_total_tokens == 1_000_000
    assert agent.context_manager.compaction_policy.reserved_output_tokens == 160_000
    assert not agent.context_manager.compactor.should_compact(
        estimate,
        policy=agent.context_manager.compaction_policy,
    )


def test_cli_model_option_can_select_provider(tmp_path: Path, monkeypatch) -> None:
    install_builtin_provider(monkeypatch, ConfigOnlyProvider)

    agent = build_agent_from_args(CLIArgs(model="codex:gpt-5.4", root=str(tmp_path)))

    assert agent.provider.__class__.__name__ == "ConfigOnlyProvider"


def test_custom_provider_receives_reasoning_effort_context(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, context) -> None:
            self.config = type(
                "Config",
                (),
                {
                    "model": context.model,
                    "reasoning_effort": context.reasoning_effort,
                },
            )()
            self.context = context

        def complete(self, messages, tools):
            del messages, tools
            raise AssertionError("not used")

    def fake_create_custom_provider(name: str, **kwargs):
        del name
        captured.update(kwargs)
        context = type("Context", (), kwargs | {"home": tmp_path})()
        return FakeProvider(context)

    monkeypatch.setattr(
        "yoke.cli.config.providers.create_custom_provider",
        fake_create_custom_provider,
    )

    agent = build_agent_from_args(
        CLIArgs(
            model="demo:gpt-demo",
            reasoning_effort="medium",
            root=str(tmp_path),
        )
    )

    assert captured["reasoning_effort"] == "medium"
    assert format_provider_model_status(agent) == "FakeProvider gpt-demo medium"
