"""Shared resource ownership for public SDK agents."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import field

from yoke.ai.providers.base import Provider


@dataclass(slots=True)
class CloseAttempt:
    """Completion and error shared by callers joining one close attempt."""

    completed: threading.Event = field(default_factory=threading.Event)
    owner_thread_id: int = field(default_factory=threading.get_ident)
    error: BaseException | None = None

    def finish(self, error: BaseException | None) -> None:
        """Publish this attempt's result to waiting close callers."""
        self.error = error
        self.completed.set()

    def wait(self) -> None:
        """Wait for this attempt and reproduce its failure for the caller."""
        self.completed.wait()
        if self.error is not None:
            raise self.error


class ProviderLease:
    """Reference-count ownership of a provider shared by agent forks."""

    _registry_lock = threading.Lock()
    _registry: dict[int, ProviderLease] = {}

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self._references = 1
        self._released = False

    @classmethod
    def claim(cls, provider: Provider) -> ProviderLease:
        """Claim shared ownership for one provider identity."""
        identity = id(provider)
        with cls._registry_lock:
            existing = cls._registry.get(identity)
            if existing is not None and existing.provider is provider:
                if existing._released:
                    raise RuntimeError("Cannot claim a released provider lease")
                existing._references += 1
                return existing
            lease = cls(provider)
            cls._registry[identity] = lease
            return lease

    def acquire(self) -> ProviderLease:
        """Add one owner and return this lease."""
        with self._registry_lock:
            if self._released:
                raise RuntimeError("Cannot acquire a released provider lease")
            self._references += 1
        return self

    def release(self) -> None:
        """Release one owner and close the provider after the final owner."""
        with self._registry_lock:
            if self._released:
                return
            self._references -= 1
            if self._references:
                return
            self._released = True
            identity = id(self.provider)
            if self._registry.get(identity) is self:
                del self._registry[identity]
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()
