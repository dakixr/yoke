"""Custom prompt-toolkit completion menu for interactive prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Callable
from typing import cast

from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completion
from prompt_toolkit.filters import has_completions
from prompt_toolkit.filters import is_done
from prompt_toolkit.filters import to_filter
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.base import StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import fragment_list_width
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import UIContent
from prompt_toolkit.layout.controls import UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseEvent
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

if TYPE_CHECKING:
    from prompt_toolkit.filters import FilterOrBool
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent


from yoke.cli.render.theme import ACCENT
from yoke.cli.render.theme import TOOLBAR_STYLE_ENTRIES

COMPLETION_MENU_STYLE = Style.from_dict(
    {
        "yoke-completion-menu": "noinherit bg:",
        "yoke-completion-menu.completion": "noinherit fg:#ffffff bg:",
        "yoke-completion-menu.completion.current": f"noinherit fg:{ACCENT} bold bg:",
        "yoke-completion-menu.meta": "noinherit fg:#9a9a9a bg:",
        "yoke-completion-menu.meta.current": f"noinherit fg:{ACCENT} bg:",
        "yoke-completion-menu scrollbar.background": "noinherit bg:",
        "yoke-completion-menu scrollbar.button": "noinherit bg:#555555",
        "yoke-completion-menu scrollbar.button.end": ("noinherit bg:#555555 underline"),
        **TOOLBAR_STYLE_ENTRIES,
    }
)


class YokeCompletionsMenu(ConditionalContainer):
    """Single-column completion menu with command and description columns."""

    def __init__(
        self,
        *,
        max_height: int = 6,
        extra_filter: FilterOrBool = True,
        z_index: int = 10**8,
    ) -> None:
        extra_filter = to_filter(extra_filter)
        super().__init__(
            content=Window(
                content=YokeCompletionsMenuControl(),
                width=Dimension(min=8),
                height=Dimension(min=1, max=max_height),
                right_margins=[ScrollbarMargin(display_arrows=False)],
                dont_extend_width=True,
                style="class:yoke-completion-menu",
                z_index=z_index,
            ),
            filter=extra_filter & has_completions & ~is_done,
        )


class YokeCompletionsMenuControl(UIControl):
    """Render prompt completions as rows like `/command    description`."""

    MIN_WIDTH = 24
    COLUMN_GAP = 4
    MIN_META_WIDTH = 12

    def preferred_width(self, max_available_width: int) -> int | None:
        complete_state = get_app().current_buffer.complete_state
        if complete_state is None:
            return 0
        if not complete_state.completions:
            return 0
        return min(
            max_available_width,
            max(self.MIN_WIDTH, self._preferred_row_width(complete_state)),
        )

    def preferred_height(
        self,
        width: int,
        max_available_height: int,
        wrap_lines: bool,
        get_line_prefix,
    ) -> int | None:
        del width, max_available_height, wrap_lines, get_line_prefix
        complete_state = get_app().current_buffer.complete_state
        if complete_state is None:
            return 0
        return len(complete_state.completions)

    def create_content(self, width: int, height: int) -> UIContent:
        del height
        complete_state = get_app().current_buffer.complete_state
        if complete_state is None:
            return UIContent()

        completions = complete_state.completions
        if not completions:
            return UIContent()
        selected_index = complete_state.complete_index
        if selected_index is None and completions:
            selected_index = 0
        command_width = self._command_width(width, completions)
        meta_width = max(0, width - command_width - self.COLUMN_GAP)

        def get_line(index: int) -> StyleAndTextTuples:
            completion = completions[index]
            is_current = index == selected_index
            return self._completion_fragments(
                completion,
                is_current=is_current,
                command_width=command_width,
                meta_width=meta_width,
            )

        return UIContent(
            get_line=get_line,
            cursor_position=Point(x=0, y=selected_index or 0),
            line_count=len(completions),
            show_cursor=False,
        )

    def mouse_handler(self, mouse_event: MouseEvent):
        buffer = get_app().current_buffer
        complete_state = buffer.complete_state
        if complete_state is None:
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            index = mouse_event.position.y
            if 0 <= index < len(complete_state.completions):
                buffer.apply_completion(complete_state.completions[index])
        elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            buffer.complete_next(count=3, disable_wrap_around=True)
        elif mouse_event.event_type == MouseEventType.SCROLL_UP:
            buffer.complete_previous(count=3, disable_wrap_around=True)
        return None

    def _preferred_row_width(self, complete_state) -> int:
        completions = complete_state.completions
        command_width = max(
            self.MIN_WIDTH,
            max(fragment_list_width(c.display) for c in completions),
        )
        meta_width = max(
            (fragment_list_width(c.display_meta) for c in completions),
            default=0,
        )
        if meta_width:
            return command_width + self.COLUMN_GAP + meta_width
        return command_width

    def _command_width(
        self,
        available_width: int,
        completions: list[Completion],
    ) -> int:
        widest_command = max(
            fragment_list_width(completion.display) for completion in completions
        )
        widest_command = max(self.MIN_WIDTH, widest_command)
        if available_width <= self.MIN_WIDTH + self.COLUMN_GAP:
            return max(1, available_width)
        max_command_width = max(1, available_width - self.COLUMN_GAP)
        if available_width >= widest_command + self.COLUMN_GAP + self.MIN_META_WIDTH:
            return widest_command
        return min(widest_command, max_command_width)

    def _completion_fragments(
        self,
        completion: Completion,
        *,
        is_current: bool,
        command_width: int,
        meta_width: int,
    ) -> StyleAndTextTuples:
        command_style = (
            "class:yoke-completion-menu.completion.current"
            if is_current
            else "class:yoke-completion-menu.completion"
        )
        meta_style = (
            "class:yoke-completion-menu.meta.current"
            if is_current
            else "class:yoke-completion-menu.meta"
        )
        command_fragments, command_fragment_width = _trim_formatted_text(
            completion.display,
            command_width,
        )
        fragments = to_formatted_text(command_fragments, style=command_style)
        fragments.append(
            (command_style, " " * max(0, command_width - command_fragment_width))
        )
        if meta_width <= 0:
            return fragments
        meta_fragments, meta_fragment_width = _trim_formatted_text(
            completion.display_meta,
            meta_width,
        )
        fragments.append((meta_style, " " * self.COLUMN_GAP))
        fragments.extend(to_formatted_text(meta_fragments, style=meta_style))
        fragments.append((meta_style, " " * max(0, meta_width - meta_fragment_width)))
        return fragments


def register_completion_menu_key_bindings(key_bindings: KeyBindings) -> None:
    """Use vertical arrows for completions and preserve horizontal editing."""

    @key_bindings.add("up")
    def _select_previous_completion(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if _move_completion_selection(buffer, -event.arg):
            _invalidate_event_app(event)
            return
        buffer.auto_up(count=event.arg)

    @key_bindings.add("down")
    def _select_next_completion(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if _move_completion_selection(buffer, event.arg):
            _invalidate_event_app(event)
            return
        buffer.auto_down(count=event.arg)

    @key_bindings.add("left")
    def _move_cursor_left(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.complete_state is not None:
            buffer.go_to_completion(None)
        buffer.cursor_position += buffer.document.get_cursor_left_position(
            count=event.arg
        )

    @key_bindings.add("right")
    def _move_cursor_right(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.complete_state is not None:
            buffer.go_to_completion(None)
        buffer.cursor_position += buffer.document.get_cursor_right_position(
            count=event.arg
        )


def selected_completion(complete_state):
    """Return the selected completion, defaulting to the first menu row."""
    if complete_state is None:
        return None
    current_completion = getattr(complete_state, "current_completion", None)
    if current_completion is not None:
        return current_completion
    completions = getattr(complete_state, "completions", ())
    if completions:
        return completions[0]
    return None


def _move_completion_selection(buffer, count: int) -> bool:
    complete_state = buffer.complete_state
    if complete_state is None or not complete_state.completions:
        return False
    current_index = complete_state.complete_index
    effective_index = 0 if current_index is None else current_index
    next_index = (effective_index + count) % len(complete_state.completions)
    if current_index is not None:
        buffer.go_to_completion(None)
    complete_state.go_to_index(next_index)
    return True


def _invalidate_event_app(event: KeyPressEvent) -> None:
    app = getattr(event, "app", None)
    if app is not None:
        app.invalidate()


def _trim_formatted_text(
    formatted_text,
    max_width: int,
) -> tuple[StyleAndTextTuples, int]:
    fragments = to_formatted_text(formatted_text)
    if max_width <= 0:
        return [], 0
    width = fragment_list_width(fragments)
    if width <= max_width:
        return fragments, width
    if max_width <= 3:
        return [("", "." * max_width)], max_width

    result: StyleAndTextTuples = []
    remaining_width = max_width - 3
    used_width = 0
    for style, text, *rest in explode_text_fragments(fragments):
        char_width = get_cwidth(text)
        if char_width > remaining_width:
            break
        if rest:
            result.append(
                cast(
                    tuple[str, str, Callable[[MouseEvent], object]],
                    (style, text, *rest),
                )
            )
        else:
            result.append((style, text))
        remaining_width -= char_width
        used_width += char_width
    result.append(("", "..."))
    return result, used_width + 3
