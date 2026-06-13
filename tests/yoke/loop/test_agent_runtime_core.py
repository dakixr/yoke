from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: D100, D103, F403, F405, S101

from .support import *  # noqa: F403, F405


def write_test_skill(root: Path, name: str, description: str) -> Path:
    skill_dir = root / ".yoke" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse {name}.\n",
        encoding="utf-8",
    )
    return skill_dir


def test_agent_fork_duplicates_runtime_configuration(tmp_path: Path) -> None:
    provider = FakeProvider()

    def before_tool_call(context: BeforeToolCallContext) -> None:
        del context
        return None

    def after_tool_call(context: AfterToolCallContext) -> None:
        del context
        return None

    active_skill = ActiveSkill(
        name="demo",
        description="Demo skill.",
        source_path=str(tmp_path / "skills" / "demo" / "SKILL.md"),
        reload_on_next_use=False,
    )
    available_skill = SkillSpec(
        name="demo",
        description="Demo skill.",
        root=tmp_path / "skills" / "demo",
        skill_md_path=tmp_path / "skills" / "demo" / "SKILL.md",
    )
    agent = RuntimeAgent(
        provider=provider,
        tools=tools(tmp_path),
        max_iterations=7,
        context_manager=ContextManager(instructions=[Message.system("system prompt")]),
        tool_execution="sequential",
        before_tool_call=before_tool_call,
        after_tool_call=after_tool_call,
        available_skills=[available_skill],
        active_skills=[active_skill],
    )

    forked = agent.fork()

    assert forked is not agent
    assert forked.provider is provider
    assert forked.max_iterations == 7
    assert forked.tool_execution == "sequential"
    assert forked.before_tool_call is before_tool_call
    assert forked.after_tool_call is after_tool_call
    assert forked.context_manager is not agent.context_manager
    assert list(forked.tools) == list(agent.tools)
    assert all(forked.tools[name] is not agent.tools[name] for name in agent.tools)
    assert forked.active_skills == agent.active_skills
    assert forked.active_skills is not agent.active_skills
    assert forked.available_skills == agent.available_skills
    assert forked.available_skills is not agent.available_skills

    forked.active_skills.append(
        ActiveSkill(
            name="other",
            description="Other skill.",
            source_path=str(tmp_path / "skills" / "other" / "SKILL.md"),
            reload_on_next_use=False,
        )
    )

    assert [skill.name for skill in agent.active_skills] == ["demo"]


def test_skill_tool_uses_current_context_active_skills(tmp_path: Path) -> None:
    from yoke.agent.skills.registry import load_skill_registry
    from yoke.agent.tools import SkillTool

    write_test_skill(tmp_path, "manual-skill", "Manual skill.")
    write_test_skill(tmp_path, "model-skill", "Model skill.")
    registry = load_skill_registry([tmp_path / ".yoke" / "skills"])

    class SkillLoadingProvider(Provider):
        supports_image_inputs = True
        max_images_per_message = 50

        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del messages, tools
            self.calls += 1
            if self.calls == 1:
                return Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name="skill",
                                arguments='{"load":["model-skill"]}',
                            ),
                        )
                    ],
                )
            return Message.assistant("done")

    skill_tool = SkillTool.bind(skill_registry=registry, active_skills=[])
    manual_skill = registry.activate("manual-skill")
    agent = RuntimeAgent(
        provider=SkillLoadingProvider(),
        tools=[skill_tool],
        skill_registry=registry,
        available_skills=registry.skills,
        active_skills=[manual_skill],
    )

    result = agent.run("use skills")

    assert result.status == "completed"
    assert [skill.name for skill in agent.active_skills] == [
        "manual-skill",
        "model-skill",
    ]


def test_skill_tool_reloads_active_skill_without_duplicate(tmp_path: Path) -> None:
    from yoke.agent.skills.registry import load_skill_registry
    from yoke.agent.tools import SkillTool

    write_test_skill(tmp_path, "manual-skill", "Manual skill.")
    registry = load_skill_registry([tmp_path / ".yoke" / "skills"])

    class SkillReloadingProvider(Provider):
        supports_image_inputs = True
        max_images_per_message = 50

        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del messages, tools
            self.calls += 1
            if self.calls == 1:
                return Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name="skill",
                                arguments='{"load":["manual-skill"]}',
                            ),
                        )
                    ],
                )
            return Message.assistant("done")

    skill_tool = SkillTool.bind(skill_registry=registry, active_skills=[])
    manual_skill = registry.activate("manual-skill")
    manual_skill.reload_on_next_use = False
    agent = RuntimeAgent(
        provider=SkillReloadingProvider(),
        tools=[skill_tool],
        skill_registry=registry,
        available_skills=registry.skills,
        active_skills=[manual_skill],
    )

    result = agent.run("reload skill")

    assert result.status == "completed"
    assert [skill.name for skill in agent.active_skills] == ["manual-skill"]
    assert agent.active_skills[0].reload_on_next_use is False
    tool_messages = [message for message in result.messages if message.role == "tool"]
    assert tool_messages
    assert '"reloaded": ["manual-skill"]' in tool_messages[0].text_content()


def test_agent_fork_clones_conversation_state(tmp_path: Path) -> None:
    class EchoProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            return Message.assistant(messages[-1].text_content() or "")

    agent = RuntimeAgent(provider=EchoProvider(), tools=tools(tmp_path))
    first = agent.run("alpha")
    forked = agent.fork()

    second = forked.run("beta")

    assert [message.content for message in first.messages] == ["alpha", "alpha"]
    assert [message.content for message in second.messages] == [
        "alpha",
        "alpha",
        "beta",
        "beta",
    ]
    assert [message.content for message in agent.messages] == ["alpha", "alpha"]


def test_agent_loop_runs_until_final_answer(tmp_path: Path) -> None:
    agent = RuntimeAgent(provider=FakeProvider(), tools=tools(tmp_path))

    result = agent.run("Create a file")

    assert result.output == "done"
    assert result.iterations == 2
    assert (tmp_path / "hello.txt").read_text() == "hello"
    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_subagent_tool_runs_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yoke.agent.tools.search_registration.shutil.which",
        lambda name: "/usr/bin/rg" if name == "rg" else None,
    )
    (tmp_path / "notes.txt").write_text("hello from nested context", encoding="utf-8")
    provider = SubagentProvider()
    subagent = SubagentTool.bind(root=tmp_path, provider=provider)
    agent = RuntimeAgent(provider=provider, tools=[subagent], tool_execution="sequential")

    result = agent.run("delegate")

    assert result.output == "done"
    assert provider.calls == 3
    assert "rg" in provider.nested_tool_names
    assert {"grep", "find", "ls"}.isdisjoint(provider.nested_tool_names)
    assert "nested summary" in result.messages[-2].text_content()


def test_agent_loop_emits_commentary_before_tool_calls(
    tmp_path: Path,
) -> None:
    class CommentaryProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.calls += 1
            if self.calls == 1:
                return Message(
                    role="assistant",
                    content="I will create the file first.",
                    phase="commentary",
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name="edit",
                                arguments='{"path":"hello.txt","new_text":"hello"}',
                            ),
                        )
                    ],
                )
            assert messages[-1].role == "tool"
            return Message.assistant("done", phase="final_answer")

    events: list[tuple[str, dict[str, object]]] = []
    agent = RuntimeAgent(provider=CommentaryProvider(), tools=tools(tmp_path))

    result = agent.run(
        "Create a file",
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert result.output == "done"
    event_names = [event for event, _payload in events]
    assert event_names.index("assistant_message") < event_names.index(
        "tool_execution_start"
    )
    commentary_payload = next(
        payload for event, payload in events if event == "assistant_message"
    )
    assert commentary_payload == {
        "iteration": 1,
        "phase": "commentary",
        "content": "I will create the file first.",
    }


def test_agent_loop_attaches_partial_messages_to_provider_error(
    tmp_path: Path,
) -> None:
    class FailingAfterToolProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.calls += 1
            if self.calls == 1:
                return Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name="edit",
                                arguments=(
                                    '{"path":"side_effect.txt","new_text":"persisted"}'
                                ),
                            ),
                        )
                    ],
                )
            assert messages[-1].role == "tool"
            raise ProviderError("provider unavailable")

    agent = RuntimeAgent(provider=FailingAfterToolProvider(), tools=tools(tmp_path))

    try:
        agent.run("Create a file")
    except ProviderError as exc:
        partial_messages = exc.partial_messages
    else:
        raise AssertionError("Expected provider error")

    assert (tmp_path / "side_effect.txt").read_text() == "persisted"
    assert partial_messages is not None
    assert [message.role for message in partial_messages] == [
        "user",
        "assistant",
        "tool",
    ]


def test_agent_loop_can_continue_existing_history(tmp_path: Path) -> None:
    agent = RuntimeAgent(
        provider=HistoryProvider(),
        tools=tools(tmp_path),
        context_manager=ContextManager(
            instructions=[Message.system("system prompt")],
        ),
        history=MessageHistory(
            [
                Message.user("previous task"),
                Message.assistant("previous answer"),
            ]
        ),
    )

    result = agent.run("next task")

    assert result.output == "continued"
    assert [message.role for message in result.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_agent_loop_uses_context_manager_before_provider_boundary(
    tmp_path: Path,
) -> None:
    def transform(messages: list[Message]) -> list[Message]:
        updated = [message.model_copy(deep=True) for message in messages]
        updated[0] = Message.system("transformed system")
        return updated

    agent = RuntimeAgent(
        provider=TransformProvider(),
        tools=tools(tmp_path),
        context_manager=ContextManager(
            instructions=[Message.system("original system")],
            transform_messages=transform,
        ),
    )

    result = agent.run("hello")

    assert result.output == "done"


def test_agent_loop_emits_context_usage_after_tool_results(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    agent = RuntimeAgent(provider=FakeProvider(), tools=tools(tmp_path))

    result = agent.run(
        "Create a file",
        on_event=lambda event, payload: events.append((event, payload)),
    )

    usage_events = [payload for event, payload in events if event == "context_usage"]
    assert result.output == "done"
    assert len(usage_events) == 1
    assert usage_events[0]["reason"] == "tool_results"
    assert usage_events[0]["message_count"] == 3
    assert isinstance(usage_events[0]["input_tokens"], int)


def test_agent_loop_rejects_newest_message_over_provider_image_limit(
    tmp_path: Path,
) -> None:
    image_parts = [
        MessageLocalImageContentPart(
            path=str(tmp_path / f"image-{index}.png"),
            label=f"[Image #{index}]",
        )
        for index in range(1, 52)
    ]
    newest_message = Message.user(
        [MessageTextContentPart(text="Too many images."), *image_parts]
    )
    agent = RuntimeAgent(
        provider=FakeProvider(),
        tools=[],
        history=MessageHistory([newest_message]),
    )

    with pytest.raises(ProviderError, match="exceeds provider image limit"):
        agent.run("", user_message=newest_message)


def test_agent_loop_omits_historical_images_for_text_only_provider(
    tmp_path: Path,
) -> None:
    class TextOnlyProvider(Provider):
        supports_image_inputs = False

        def __init__(self) -> None:
            self.received_messages: list[Message] = []

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.received_messages = messages
            return Message.assistant("done")

    image_message = Message.user(
        [
            MessageTextContentPart(text="Review this screenshot."),
            MessageLocalImageContentPart(
                path=str(tmp_path / "screenshot.png"),
                label="[Image #1]",
            ),
        ]
    )
    provider = TextOnlyProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        history=MessageHistory([image_message, Message.assistant("Reviewed.")]),
    )

    result = agent.run("continue")

    assert result.output == "done"
    assert agent.messages[0].has_image_inputs()
    assert not any(message.has_image_inputs() for message in provider.received_messages)
    first_message_text = provider.received_messages[0].text_content() or ""
    assert "Review this screenshot." in first_message_text
    assert "[Image omitted: [Image #1]" in first_message_text
    assert "does not support image inputs" in first_message_text
