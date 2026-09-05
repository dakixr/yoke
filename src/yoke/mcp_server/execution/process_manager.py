"""MCP-specific incremental UTF-8 decoding, without changing agent output."""

from __future__ import annotations

import codecs
from collections.abc import Callable

from yoke.agent.tools.command_process import _ManagedCommandProcess
from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
)


class UTF8CommandProcess(_ManagedCommandProcess):
    def _reader_main(self, read_chunk: Callable[[], bytes]) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while raw := read_chunk():
                decoded = decoder.decode(raw)
                if decoded:
                    self._append_output(decoded.encode("utf-8"))
        except OSError:
            pass
        finally:
            tail = decoder.decode(b"", final=True)
            if tail:
                self._append_output(tail.encode("utf-8"))
            self._reader_finished()


class MCPProcessManager(CommandProcessManager):
    """Use the normal process lifecycle with a reader that retains whole characters."""

    _managed_process_factory = UTF8CommandProcess
