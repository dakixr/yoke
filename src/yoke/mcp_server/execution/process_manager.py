"""MCP-specific incremental UTF-8 decoding, without changing agent output."""

from __future__ import annotations

import codecs

from yoke.agent.tools.command_process import _ManagedCommandProcess
from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
)


class UTF8CommandProcess(_ManagedCommandProcess):
    decoder_factory = staticmethod(
        lambda: codecs.getincrementaldecoder("utf-8")(errors="replace")
    )


class MCPProcessManager(CommandProcessManager):
    """Use the normal process lifecycle with a reader that retains whole characters."""

    _managed_process_factory = UTF8CommandProcess
