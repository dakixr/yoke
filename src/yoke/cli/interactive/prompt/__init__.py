"""Prompt-toolkit interactive CLI package."""

from yoke.cli.interactive.prompt.app import (
    patch_prompt_toolkit_win32_executor_shutdown as _patch_win32_shutdown,
)
from yoke.cli.interactive.prompt.app import (
    run_prompt_toolkit_cli as run_prompt_toolkit_cli,
)

patch_prompt_toolkit_win32_executor_shutdown = _patch_win32_shutdown
