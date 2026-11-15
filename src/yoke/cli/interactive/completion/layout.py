"""Install the custom completion menu in a prompt-toolkit session."""

from yoke.cli.interactive.completion.menu import YokeCompletionsMenu


def configure_prompt_session_completion_menu(prompt_session) -> None:
    """Replace prompt-toolkit's default popup with yoke's completion menu."""
    try:
        from prompt_toolkit.filters import has_focus
        from prompt_toolkit.layout.containers import Float

        default_buffer_window = prompt_session.layout.current_window
        default_buffer_window.content.menu_position = lambda: 0
        completion_filter = has_focus(prompt_session.default_buffer)
        prompt_wrapper = prompt_session.layout.container.children[0]
        float_container = prompt_wrapper.alternative_content
        floats = float_container.floats
        floats[:2] = [
            Float(
                xcursor=True,
                ycursor=True,
                transparent=True,
                content=YokeCompletionsMenu(
                    max_height=6,
                    extra_filter=completion_filter,
                ),
            )
        ]
        default_buffer_window.height = prompt_session._get_default_buffer_control_height
    except (AttributeError, IndexError, TypeError):
        return
