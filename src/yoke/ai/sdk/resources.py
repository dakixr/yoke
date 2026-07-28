"""Shared resource ownership for public SDK agents."""

from __future__ import annotations

import threading

from yoke.ai.providers.base import Provider


class ProviderLease:
    """Reference-count ownership of a provider shared by agent forks."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self._lock = threading.Lock()
        self._references = 1
        self._released = False

    def acquire(self) -> ProviderLease:
        """Add one owner and return this lease."""
        with self._lock:
            if self._released:
                raise RuntimeError("Cannot acquire a released provider lease")
            self._references += 1
        return self

    def release(self) -> None:
        """Release one owner and close the provider after the final owner."""
        with self._lock:
            if self._released:
                return
            self._references -= 1
            if self._references:
                return
            self._released = True
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()
