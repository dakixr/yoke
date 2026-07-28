"""Fullscreen command-process inspector."""

from yoke.cli.interactive.process_inspector.app import (
    ProcessInspectorState as ProcessInspectorState,
)
from yoke.cli.interactive.process_inspector.app import (
    open_live_process_inspector as open_live_process_inspector,
)

__all__ = ["ProcessInspectorState", "open_live_process_inspector"]
