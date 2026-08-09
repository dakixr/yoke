"""Windows prompt-toolkit compatibility fixes."""

from __future__ import annotations

import sys
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import TypeVar
from typing import cast

_ExecutorResultT = TypeVar("_ExecutorResultT")


def patch_prompt_toolkit_win32_executor_shutdown() -> None:
    """Suppress prompt-toolkit's benign Win32 input shutdown reschedule."""
    if sys.platform != "win32":
        return
    try:
        from prompt_toolkit.input import win32
    except ImportError:
        return
    original = win32.run_in_executor_with_context
    if getattr(original, "_yoke_shutdown_safe", False):
        return

    def run_in_executor_shutdown_safe(
        func: Callable[..., _ExecutorResultT],
        *args: object,
        loop: Any | None = None,  # noqa: ANN401
    ) -> Awaitable[_ExecutorResultT] | None:
        try:
            return original(func, *args, loop=loop)
        except RuntimeError as exc:
            if str(exc) == "Executor shutdown has been called":
                return None
            raise

    cast(Any, run_in_executor_shutdown_safe)._yoke_shutdown_safe = True
    win32.run_in_executor_with_context = cast(
        Any,
        run_in_executor_shutdown_safe,
    )
