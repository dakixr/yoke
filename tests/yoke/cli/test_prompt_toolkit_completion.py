from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002, ANN003, D100, D103, F403, F405, S101

from typing import Any
from typing import cast

from prompt_toolkit.completion.base import CompleteEvent
from prompt_toolkit.key_binding.key_processor import KeyPressEvent

from .support import *  # noqa: F403, F405
from .support import _format_bottom_toolbar


def test_prompt_toolkit_toolbar_shows_queued_prompts_above_status() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=False,
        status_message="Thinking",
        pending_prompts=["second prompt", "third prompt"],
        spinner_frame="|",
    )

    assert toolbar == [
        (
            "class:bottom-toolbar",
            " queued 1: second prompt \n"
            " queued 2: third prompt \n"
            " | Thinking · 2 queued ",
        )
    ]


def test_prompt_toolkit_toolbar_labels_steering_prompts() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=True,
        status_message="Thinking",
        pending_prompts=[PendingPrompt("use config.py instead", "steering")],
    )

    assert toolbar == [
        (
            "class:bottom-toolbar",
            " steering 1: use config.py instead \n Stopping current turn... ",
        )
    ]


def test_prompt_toolkit_toolbar_shows_pending_images_above_status() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=False,
        status_message="Thinking",
        pending_prompts=[],
        pending_images=[" image 1: screenshot.png ", " image 2: chart.png "],
        spinner_frame="|",
    )

    assert toolbar == [
        (
            "class:bottom-toolbar",
            " image 1: screenshot.png \n image 2: chart.png \n | Thinking ",
        )
    ]


def test_insert_attachment_reference_wraps_filename_for_prompt() -> None:
    @dataclass
    class FakeDocument:
        char_before_cursor: str | None
        current_char: str | None

    @dataclass
    class FakeBuffer:
        document: FakeDocument
        inserted: list[str] = field(default_factory=list)

        def insert_text(self, text: str) -> None:
            self.inserted.append(text)

    buffer = FakeBuffer(document=FakeDocument("e", "i"))

    insert_attachment_reference(
        buffer,
        ImageAttachment(path=Path("C:/tmp/screenshot.png")),
    )

    assert buffer.inserted == [" [screenshot.png] "]


def test_insert_attachment_reference_avoids_extra_spacing_at_boundaries() -> None:
    @dataclass
    class FakeDocument:
        char_before_cursor: str | None
        current_char: str | None

    @dataclass
    class FakeBuffer:
        document: FakeDocument
        inserted: list[str] = field(default_factory=list)

        def insert_text(self, text: str) -> None:
            self.inserted.append(text)

    buffer = FakeBuffer(document=FakeDocument(" ", None))

    insert_attachment_reference(
        buffer,
        ImageAttachment(path=Path("C:/tmp/chart.png")),
    )

    assert buffer.inserted == ["[chart.png]"]


def test_prompt_toolkit_does_not_enable_mouse_support(
    tmp_path: Path, monkeypatch
) -> None:
    import prompt_toolkit

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
            session_holder["prompt_kwargs"] = kwargs
            return "quit"

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)

    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path)),
        FakeAgent(),
        [],
        active_session=active_session_for(tmp_path),
    )

    assert exit_code == 0
    assert "mouse_support" not in session_holder["prompt_kwargs"]


def test_prompt_toolkit_ctrl_u_removes_last_pending_image() -> None:
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    removed: list[str] = []
    key_bindings = KeyBindings()
    register_prompt_toolkit_key_bindings(
        key_bindings,
        state=PromptCliState(messages=[], pending_prompts=[]),
        stop_active_turn=lambda: False,
        attach_image=lambda _attachment: None,
        remove_last_image=lambda: removed.append("removed"),
        resolve_image_path=lambda raw: Path(raw),
        cycle_thinking_effort=lambda: None,
        update_status=lambda _message: None,
    )

    binding = next(
        item for item in key_bindings.bindings if item.keys == (Keys.ControlU,)
    )

    class FakeEvent:
        pass

    binding.handler(cast(KeyPressEvent, FakeEvent()))

    assert removed == ["removed"]


def test_prompt_toolkit_enter_accepts_current_completion() -> None:
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    applied: list[str] = []
    handled: list[str] = []
    key_bindings = KeyBindings()
    register_prompt_toolkit_key_bindings(
        key_bindings,
        state=PromptCliState(messages=[], pending_prompts=[]),
        stop_active_turn=lambda: False,
        attach_image=lambda _attachment: None,
        remove_last_image=lambda: None,
        resolve_image_path=lambda raw: Path(raw),
        cycle_thinking_effort=lambda: None,
        update_status=lambda _message: None,
    )

    binding = next(
        item for item in key_bindings.bindings if item.keys == (Keys.ControlM,)
    )

    @dataclass
    class FakeCompletion:
        text: str

    @dataclass
    class FakeCompleteState:
        current_completion: FakeCompletion

    class FakeBuffer:
        complete_state = FakeCompleteState(FakeCompletion("/compact"))

        def apply_completion(self, completion: FakeCompletion) -> None:
            applied.append(completion.text)

        def validate_and_handle(self) -> None:
            handled.append("submitted")

    class FakeEvent:
        current_buffer = FakeBuffer()

    binding.handler(cast(KeyPressEvent, FakeEvent()))

    assert applied == ["/compact"]
    assert handled == []


def test_prompt_toolkit_enter_accepts_first_completion_by_default() -> None:
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    applied: list[str] = []
    key_bindings = KeyBindings()
    register_prompt_toolkit_key_bindings(
        key_bindings,
        state=PromptCliState(messages=[], pending_prompts=[]),
        stop_active_turn=lambda: False,
        attach_image=lambda _attachment: None,
        remove_last_image=lambda: None,
        resolve_image_path=lambda raw: Path(raw),
        cycle_thinking_effort=lambda: None,
        update_status=lambda _message: None,
    )

    binding = next(
        item for item in key_bindings.bindings if item.keys == (Keys.ControlM,)
    )

    @dataclass
    class FakeCompletion:
        text: str

    @dataclass
    class FakeCompleteState:
        completions: list[FakeCompletion]
        current_completion = None

    class FakeBuffer:
        complete_state = FakeCompleteState([FakeCompletion("/model")])

        def apply_completion(self, completion: FakeCompletion) -> None:
            applied.append(completion.text)

        def validate_and_handle(self) -> None:
            raise AssertionError("should not submit while completions are open")

    class FakeEvent:
        current_buffer = FakeBuffer()

    binding.handler(cast(KeyPressEvent, FakeEvent()))

    assert applied == ["/model"]


def test_prompt_toolkit_up_down_navigate_completion_menu() -> None:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.buffer import CompletionState
    from prompt_toolkit.completion import Completion
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    key_bindings = KeyBindings()
    register_prompt_toolkit_key_bindings(
        key_bindings,
        state=PromptCliState(messages=[], pending_prompts=[]),
        stop_active_turn=lambda: False,
        attach_image=lambda _attachment: None,
        remove_last_image=lambda: None,
        resolve_image_path=lambda raw: Path(raw),
        cycle_thinking_effort=lambda: None,
        update_status=lambda _message: None,
    )
    buffer = Buffer(document=Document("/m", cursor_position=2))
    buffer.complete_state = CompletionState(
        Document("/m", cursor_position=2),
        [
            Completion("/model", start_position=-2),
            Completion("/memories", start_position=-2),
        ],
        complete_index=0,
    )

    down = next(item for item in key_bindings.bindings if item.keys == (Keys.Down,))
    up = next(item for item in key_bindings.bindings if item.keys == (Keys.Up,))

    @dataclass
    class FakeEvent:
        current_buffer: Buffer
        arg: int = 1

        class FakeApp:
            def invalidate(self) -> None:
                return None

        app = FakeApp()

    down.handler(cast(KeyPressEvent, FakeEvent(buffer)))
    up.handler(cast(KeyPressEvent, FakeEvent(buffer)))

    assert buffer.text == "/m"
    assert buffer.complete_state is not None
    assert buffer.complete_state.complete_index == 0


def test_prompt_toolkit_left_right_keep_editing_with_completion_menu() -> None:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.buffer import CompletionState
    from prompt_toolkit.completion import Completion
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    key_bindings = KeyBindings()
    register_prompt_toolkit_key_bindings(
        key_bindings,
        state=PromptCliState(messages=[], pending_prompts=[]),
        stop_active_turn=lambda: False,
        attach_image=lambda _attachment: None,
        remove_last_image=lambda: None,
        resolve_image_path=lambda raw: Path(raw),
        cycle_thinking_effort=lambda: None,
        update_status=lambda _message: None,
    )
    buffer = Buffer(document=Document("/m", cursor_position=2))
    buffer.complete_state = CompletionState(
        Document("/m", cursor_position=2),
        [Completion("/model", start_position=-2)],
        complete_index=0,
    )

    left = next(item for item in key_bindings.bindings if item.keys == (Keys.Left,))
    right = next(item for item in key_bindings.bindings if item.keys == (Keys.Right,))

    @dataclass
    class FakeEvent:
        current_buffer: Buffer
        arg: int = 1

    left.handler(cast(KeyPressEvent, FakeEvent(buffer)))
    right.handler(cast(KeyPressEvent, FakeEvent(buffer)))

    assert buffer.text == "/m"
    assert buffer.cursor_position == 2
    assert buffer.complete_state is None


def test_yoke_completion_menu_formats_command_and_description() -> None:
    from yoke.cli.interactive.completion_menu import YokeCompletionsMenuControl
    from prompt_toolkit.completion import Completion
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    control = YokeCompletionsMenuControl()
    fragments = control._completion_fragments(
        Completion(
            "/model",
            display="/model",
            display_meta="choose what model and reasoning effort to use",
        ),
        is_current=True,
        command_width=12,
        meta_width=48,
    )

    assert fragment_list_to_text(fragments) == (
        "/model          choose what model and reasoning effort to use   "
    )
    assert fragments[0][0].strip() == ("class:yoke-completion-menu.completion.current")


def test_prompt_toolkit_replaces_default_completion_menu() -> None:
    from yoke.cli.interactive.completion_menu import YokeCompletionsMenu
    from yoke.cli.interactive.prompt_loop import (
        configure_prompt_session_completion_menu,
    )
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    session = PromptSession(input=DummyInput(), output=DummyOutput())

    configure_prompt_session_completion_menu(session)

    prompt_wrapper = cast(Any, session.layout.container).children[0]
    floats = prompt_wrapper.alternative_content.floats

    assert len(floats) == 2
    assert isinstance(floats[0].content, YokeCompletionsMenu)
    assert cast(Any, session.layout.current_control).menu_position() == 0


def test_slash_command_completer_suggests_matching_commands() -> None:
    from prompt_toolkit.document import Document

    completer = SlashCommandCompleter()
    completions = list(
        completer.get_completions(Document("/co"), cast(CompleteEvent, object()))
    )
    skill_completions = list(
        completer.get_completions(Document("/sk"), cast(CompleteEvent, object()))
    )
    image_completions = list(
        completer.get_completions(Document("/i"), cast(CompleteEvent, object()))
    )
    shortcut_completions = list(
        completer.get_completions(Document("/sh"), cast(CompleteEvent, object()))
    )

    assert hasattr(completer, "get_completions_async")
    assert [completion.text for completion in completions] == ["/compact"]
    assert [completion.text for completion in skill_completions] == ["/skill"]
    assert [completion.text for completion in image_completions] == ["/image"]
    assert [completion.text for completion in shortcut_completions] == ["/shortcuts"]
    assert str(completions[0].display_meta_text).startswith("Summarize")
    assert current_slash_token("please /co") is None
    assert current_slash_token("/image ") is None


def test_slash_command_completer_suggests_skill_names(tmp_path: Path) -> None:
    from prompt_toolkit.document import Document

    skills = [
        SkillSpec(
            name="code-review",
            description="Review code.",
            root=tmp_path / "code-review",
            skill_md_path=tmp_path / "code-review" / "SKILL.md",
        ),
        SkillSpec(
            name="create-skill",
            description="Create skills.",
            root=tmp_path / "create-skill",
            skill_md_path=tmp_path / "create-skill" / "SKILL.md",
        ),
    ]
    completer = SlashCommandCompleter(skills=skills)

    completions = list(
        completer.get_completions(Document("/skill co"), cast(CompleteEvent, object()))
    )
    assert [completion.text for completion in completions] == ["code-review"]
    assert str(completions[0].display_meta_text) == "Review code."
    assert current_skill_name_token("/skill") is None
    assert current_skill_name_token("/skill ") == ""
    assert current_skill_name_token("/skill co") == "co"
    assert current_skill_name_token("/skill code-review extra") is None


def test_prompt_toolkit_toolbar_shows_provider_model_when_idle() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=False,
        stop_pending=False,
        status_message="",
        pending_prompts=[],
        context_usage="73% left",
        provider_model="FakeProvider gpt-test",
        root_label=r"~\dev\ScriptsCommon",
    )

    assert toolbar == [
        (
            "class:bottom-toolbar",
            r" FakeProvider gpt-test · 73% left · ~\dev\ScriptsCommon ",
        )
    ]
    assert "Ready" not in toolbar[0][1]


def test_prompt_toolkit_toolbar_shows_provider_model_with_context() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=False,
        status_message="Thinking",
        pending_prompts=[],
        context_usage="90% left",
        provider_model="FakeProvider gpt-test",
        root_label=r"~\dev\ScriptsCommon",
        spinner_frame="|",
    )

    assert toolbar == [
        (
            "class:bottom-toolbar",
            r" | Thinking · FakeProvider gpt-test · 90% left · "
            r"~\dev\ScriptsCommon ",
        )
    ]
