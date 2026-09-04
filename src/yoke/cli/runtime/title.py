"""LLM-generated titles for Yoke CLI sessions."""

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
import logging
from threading import current_thread
from threading import Thread

from yoke.agent.models import Message
from yoke.ai.providers.usage_context import UsageMetricContext
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.session import fallback_session_title
from yoke.session.title import generate_session_title as generate_session_title


LOGGER = logging.getLogger(__name__)


def start_session_title_generation(
    session: ActiveSession,
    agent: object,
    prompt: str,
    *,
    messages: Sequence[Message] | None = None,
) -> Thread | None:
    """Start one background title request for an unnamed session."""
    with session.save_lock:
        if session.title:
            return None
        existing = session.title_worker
        if existing is not None and existing.is_alive():
            return existing
        title_messages = tuple(messages) if messages is not None else None
        worker = Thread(
            target=_generate_session_title,
            args=(session, agent, prompt, title_messages),
            daemon=True,
            name="yoke-session-title",
        )
        session.title_worker = worker
        try:
            worker.start()
        except BaseException:
            session.title_worker = None
            raise
        return worker


def wait_for_session_title(session: ActiveSession) -> None:
    """Wait for this session's in-flight title request, if any."""
    with session.save_lock:
        worker = session.title_worker
    if worker is not None and worker is not current_thread():
        worker.join()


def ensure_session_title(
    session: ActiveSession,
    agent: object,
    prompt: str,
    *,
    messages: Sequence[Message] | None = None,
) -> None:
    """Generate and persist a title for an unnamed session."""
    with session.save_lock:
        if session.title:
            return
        title_messages = (
            [message.model_copy(deep=True) for message in messages]
            if messages is not None
            else session.messages()
        )
    if not title_messages or title_messages[-1].text_content() != prompt:
        title_messages.append(Message.user(prompt))
    with session_usage_metric_context(session, prompt):
        generated = generate_session_title(agent, title_messages)
    title = generated or fallback_session_title(prompt)
    with session.save_lock:
        if session.title:
            return
        _persist_session_title(session, title)


def ensure_local_session_title(session: ActiveSession, prompt: str) -> None:
    """Persist a local fallback title without making a provider request."""
    with session.save_lock:
        if session.title:
            return
        _persist_session_title(session, fallback_session_title(prompt))


def _generate_session_title(
    session: ActiveSession,
    agent: object,
    prompt: str,
    messages: Sequence[Message] | None,
) -> None:
    try:
        ensure_session_title(session, agent, prompt, messages=messages)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Background session title generation failed.", exc_info=True)
    finally:
        with session.save_lock:
            if session.title_worker is current_thread():
                session.title_worker = None


def _persist_session_title(session: ActiveSession, title: str) -> None:
    if session.record.created_at is None:
        return
    try:
        record = session.store.set_title(
            session.id,
            title,
            existing_record=session.record,
        )
    except ValueError:
        # A detached or deleted session can outlive the background title
        # worker. Do not recreate a session just for a title.
        return
    session.record = record
    session.title = title


@contextmanager
def session_usage_metric_context(
    session: ActiveSession,
    prompt: str,
) -> Iterator[UsageMetricContext]:
    """Attribute provider calls to the current CLI session."""
    title = session.title or fallback_session_title(prompt)
    with usage_metric_context(
        surface="cli",
        session_id=session.id,
        session_title=title,
    ) as context:
        yield context
