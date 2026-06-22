from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,ANN202,D100,D103,F405,S101

from threading import Thread

from .support import *  # noqa: F403, F405
from .support import _format_context_usage_text


def test_prompt_compaction_refreshes_context_usage_immediately(
    tmp_path: Path,
) -> None:
    from threading import Lock

    from yoke.cli.interactive.prompt.control import _persist_prompt_compaction
    from yoke.cli.runtime import force_compact_history

    class ManualCompactionProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            if messages[0].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("manual summary")
            return Message.assistant("done")

    agent = RuntimeAgent(
        provider=ManualCompactionProvider(),
        tools=[],
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(
                max_total_tokens=300,
                keep_recent_tokens=1,
            ),
        ),
    )
    messages = [
        Message.user("older request " + ("alpha " * 80)),
        Message.assistant("older response " + ("beta " * 80)),
        Message.user("recent request"),
    ]
    active_session = active_session_for(tmp_path)
    state = PromptCliState(
        messages=messages,
        pending_prompts=[],
        context_usage_text="0% left",
    )
    state_lock = Lock()
    notices: list[None] = []

    compacted = force_compact_history(agent, messages)
    assert compacted is not None

    _persist_prompt_compaction(
        compacted,
        state=state,
        state_lock=state_lock,
        agent=agent,
        active_session=active_session,
        scrollback_console=build_console(CaptureStream()),
        run_in_scrollback=lambda render: notices.append(render()),
    )

    assert state.context_usage_text == _format_context_usage_text(compacted[-1])
    assert state.context_usage_text != "0% left"


def test_prompt_toolkit_context_usage_formats_percent_left() -> None:
    assert _format_context_usage_text({"usage_percent": 27}) == "73% left"
    assert _format_context_usage_text({"usage_percent": 120}) == "0% left"


def test_slash_tree_refreshes_context_usage_after_navigation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from threading import Lock

    import yoke.cli.interactive.prompt.loop as prompt_loop_module

    state = PromptCliState(
        messages=[Message.user("old branch")],
        pending_prompts=[],
        context_usage_text="stale",
    )
    state_lock = Lock()
    initial_session = active_session_for(tmp_path)
    updated_session = active_session_for(tmp_path)
    active_session_ref = {"active_session": initial_session}
    updated_messages = [Message.user("new branch")]
    estimator_seen: dict[str, object] = {}
    invalidations: list[None] = []

    def fake_handle_slash_command(
        command: str,
        *,
        agent,
        active_session,
        messages,
        console,
        pending_images=None,
        on_context_usage=None,
        on_editor_text=None,
    ):
        del agent, console, pending_images, on_context_usage
        assert command == "/tree"
        assert active_session is initial_session
        assert messages == [Message.user("old branch")]
        if on_editor_text is not None:
            on_editor_text("retry draft")
        return True, updated_messages, updated_session

    def estimate_toolbar_context_usage(prompt: str) -> str:
        estimator_seen["prompt"] = prompt
        estimator_seen["messages"] = list(state.messages)
        estimator_seen["active_session"] = active_session_ref["active_session"]
        return "42% left"

    monkeypatch.setattr(
        prompt_loop_module,
        "handle_slash_command",
        fake_handle_slash_command,
    )

    result = prompt_loop_module.process_prompt_toolkit_prompt(
        "/tree",
        state=state,
        agent=FakeAgent(),
        active_session_ref=active_session_ref,
        scrollback_console=build_console(CaptureStream()),
        state_lock=state_lock,
        update_status=lambda _message: None,
        invalidate_prompt=lambda: invalidations.append(None),
        request_exit=lambda: None,
        start_turn=lambda *_args, **_kwargs: Thread(),
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=lambda _payload: None,
        estimate_toolbar_context_usage=estimate_toolbar_context_usage,
        on_editor_text=lambda text: setattr(state, "next_editor_text", text),
    )

    assert result is updated_session
    assert state.messages == updated_messages
    assert active_session_ref["active_session"] is updated_session
    assert state.next_editor_text == "retry draft"
    assert state.context_usage_text == "42% left"
    assert estimator_seen == {
        "prompt": "retry draft",
        "messages": updated_messages,
        "active_session": updated_session,
    }
    assert invalidations == [None]


def test_estimate_context_usage_does_not_append_empty_prompt() -> None:
    class FakeUsageAgent:
        def __init__(self) -> None:
            self.context_manager = ContextManager()
            self.available_skills: list[SkillSpec] = []
            self.active_skills: list[ActiveSkill] = []

    agent = FakeUsageAgent()
    messages = [
        Message.user("Investigate the bug"),
        Message.assistant("I found the issue."),
    ]

    usage = estimate_context_usage(agent, "", messages)

    assert usage is not None
    context = agent.context_manager.initialize(
        "",
        messages,
        append_prompt=False,
        available_skills=agent.available_skills,
        active_skills=agent.active_skills,
    )
    expected_tokens = agent.context_manager.estimate_tokens(
        agent.context_manager.messages_for_provider(context)
    ).input_tokens
    assert usage["input_tokens"] == expected_tokens


def test_estimate_context_usage_prefers_provider_usage() -> None:
    class FakeUsageAgent:
        def __init__(self) -> None:
            self.context_manager = ContextManager()
            self.available_skills: list[SkillSpec] = []
            self.active_skills: list[ActiveSkill] = []

    agent = FakeUsageAgent()
    messages = [
        Message.user("Investigate the bug"),
        Message(
            role="assistant",
            content="I found the issue.",
            usage=TokenUsage(
                input_tokens=1234,
                output_tokens=20,
                reasoning_tokens=7,
                total_tokens=1254,
            ),
        ),
    ]

    usage = estimate_context_usage(agent, "", messages)

    assert usage is not None
    assert usage["input_tokens"] == 1234
    assert usage["provider_reported_input_tokens"] == 1234
    assert usage["reasoning_tokens"] == 7
    assert usage["accounting_source"] == "provider"


def test_estimate_context_usage_uses_conversation_entries_when_available() -> None:
    class FakeUsageAgent:
        def __init__(self) -> None:
            self.context_manager = ContextManager(
                compaction_policy=CompactionPolicy(
                    max_total_tokens=300,
                    keep_recent_tokens=80,
                )
            )
            self.available_skills: list[SkillSpec] = []
            self.active_skills: list[ActiveSkill] = []

    agent = FakeUsageAgent()
    context = agent.context_manager.initialize(
        "",
        [
            Message.user("older"),
            Message.assistant("older answer " + ("alpha " * 200)),
            Message.user("recent"),
            Message.assistant("recent answer"),
        ],
        append_prompt=False,
        available_skills=agent.available_skills,
        active_skills=agent.active_skills,
    )
    preparation = agent.context_manager.prepare_compaction(context, reason="forced")
    assert preparation is not None
    agent.context_manager.apply_compaction(
        context,
        preparation,
        summary_text="summary of older work",
    )

    conversation_entries = [
        entry.model_copy(deep=True) for entry in context.conversation_log.entries
    ]
    transcript_messages = context.messages

    usage_from_transcript = estimate_context_usage(
        agent,
        "",
        transcript_messages,
    )
    usage_from_entries = estimate_context_usage(
        agent,
        "",
        transcript_messages,
        conversation_entries=conversation_entries,
    )

    assert usage_from_transcript is not None
    assert usage_from_entries is not None
    assert usage_from_transcript["usage_percent"] == 100
    assert usage_from_entries["usage_percent"] < 100


def test_prompt_toolkit_hydrates_context_usage_from_conversation_entries(
    tmp_path: Path, monkeypatch
) -> None:
    import importlib
    import prompt_toolkit

    run_in_terminal_module = importlib.import_module(
        "prompt_toolkit.application.run_in_terminal"
    )

    class FakeUsageAgent:
        supports_message_history = True
        supports_user_message = False

        def __init__(self) -> None:
            self.context_manager = ContextManager(
                compaction_policy=CompactionPolicy(
                    max_total_tokens=300,
                    keep_recent_tokens=80,
                )
            )
            self.available_skills: list[SkillSpec] = []
            self.active_skills: list[ActiveSkill] = []

        def run(
            self,
            prompt: str,
            messages: list[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
            conversation_entries: Any = None,
        ) -> AgentResult:
            del prompt, messages, on_event, stop_requested, conversation_entries
            return AgentResult(output="ok", messages=[], iterations=1)

    agent = FakeUsageAgent()
    context = agent.context_manager.initialize(
        "",
        [
            Message.user("older"),
            Message.assistant("older answer " + ("alpha " * 200)),
            Message.user("recent"),
            Message.assistant("recent answer"),
        ],
        append_prompt=False,
        available_skills=agent.available_skills,
        active_skills=agent.active_skills,
    )
    preparation = agent.context_manager.prepare_compaction(context, reason="forced")
    assert preparation is not None
    agent.context_manager.apply_compaction(
        context,
        preparation,
        summary_text="summary of older work",
    )

    conversation_entries = [
        entry.model_copy(deep=True) for entry in context.conversation_log.entries
    ]
    transcript_messages = context.messages
    expected_usage = estimate_context_usage(
        agent,
        "",
        transcript_messages,
        conversation_entries=conversation_entries,
    )
    assert expected_usage is not None
    expected_text = _format_context_usage_text(expected_usage)
    assert expected_text is not None

    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    store.save(
        "compacted",
        transcript_messages,
        conversation_entries=conversation_entries,
        root=tmp_path,
    )
    active_session = create_active_session(
        CLIArgs(root=str(tmp_path), session="compacted"),
        root=tmp_path,
    )

    session_holder: dict[str, Any] = {}

    class FakeLoop:
        def call_soon_threadsafe(self, callback) -> None:
            callback()

    class FakeApp:
        def __init__(self) -> None:
            self.loop = FakeLoop()

        def invalidate(self) -> None:
            return None

    class FakePromptSession:
        def __init__(self, *args, **kwargs) -> None:
            self.app = FakeApp()

        def prompt(self, *_args, **kwargs) -> str:
            session_holder["toolbar"] = kwargs["bottom_toolbar"]()
            return "quit"

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)
    monkeypatch.setattr(
        run_in_terminal_module,
        "run_in_terminal",
        lambda func, *args, **kwargs: func(),
    )

    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path)),
        agent,
        active_session.record.messages,
        active_session=active_session,
    )

    assert exit_code == 0
    toolbar_text = "".join(t for _s, t in session_holder["toolbar"])
    assert expected_text in toolbar_text
    assert "0% left" not in toolbar_text
