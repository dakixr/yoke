"""Bounded asynchronous orchestration for independent SDK agents."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
import inspect
import time
from typing import cast
from uuid import uuid4

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.ai.sdk.agent import Agent
from yoke.ai.sdk.async_support import drain_worker
from yoke.ai.sdk.batch_safety import close_attempt_agent
from yoke.ai.sdk.batch_safety import emit_batch_error
from yoke.ai.sdk.batch_safety import prepare_retry
from yoke.ai.sdk.batch_safety import register_agent
from yoke.ai.sdk.batch_safety import RetryPredicate
from yoke.ai.sdk.observability import AgentObserver
from yoke.ai.sdk.observability import BoundObserver
from yoke.ai.sdk.types import BatchItemResult
from yoke.ai.sdk.types import BatchProgress
from yoke.ai.sdk.types import BatchResult
from yoke.ai.sdk.types import BatchTask
from yoke.ai.sdk.types import BatchUsage
from yoke.ai.providers.usage_context import usage_metric_context

type AgentFactory = Callable[[BatchTask], Agent | Awaitable[Agent]]
type ProgressCallback = Callable[[BatchProgress], Awaitable[None] | None]


async def run_many[StructuredT](
    tasks: list[BatchTask],
    *,
    agent_factory: AgentFactory,
    max_concurrency: int = 8,
    output_type: type[StructuredT] | None = None,
    timeout: float | None = None,
    max_attempts: int = 1,
    retry_delay: float = 0,
    should_retry: RetryPredicate | None = None,
    on_progress: ProgressCallback | None = None,
    observer: AgentObserver | None = None,
) -> BatchResult[StructuredT]:
    """Run independent tasks concurrently and return input-ordered outcomes.

    The factory is called once per attempt. Every created agent is closed after
    that attempt, including failures and cooperative timeouts.
    """
    _validate_inputs(
        tasks,
        max_concurrency=max_concurrency,
        timeout=timeout,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )
    started = time.monotonic()
    semaphore = asyncio.Semaphore(max_concurrency)
    progress_lock = asyncio.Lock()
    used_agents: set[Agent] = set()
    used_providers: dict[int, object] = {}
    used_agents_lock = asyncio.Lock()
    progress_errors: list[Exception] = []
    completed = 0

    async def run_index(index: int, task: BatchTask) -> BatchItemResult[StructuredT]:
        nonlocal completed
        async with semaphore:
            item = await _run_task(
                task,
                agent_factory=agent_factory,
                output_type=output_type,
                timeout=timeout,
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                should_retry=should_retry,
                observer=observer,
                used_agents=used_agents,
                used_providers=used_providers,
                used_agents_lock=used_agents_lock,
            )
        async with progress_lock:
            completed += 1
            progress = BatchProgress(
                task_id=task.id,
                index=index,
                completed=completed,
                total=len(tasks),
                status=item.status,
                attempts=item.attempts,
                duration_seconds=item.duration_seconds,
            )
            try:
                await _emit_progress(on_progress, progress)
            except Exception as exc:
                progress_errors.append(exc)
        return item

    workers = [
        asyncio.create_task(run_index(index, task)) for index, task in enumerate(tasks)
    ]
    try:
        items = await asyncio.gather(*workers)
    except BaseException:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return BatchResult(
        items=items,
        usage=_aggregate_usage(items),
        duration_seconds=time.monotonic() - started,
        progress_errors=progress_errors,
    )


async def _run_task[StructuredT](
    task: BatchTask,
    *,
    agent_factory: AgentFactory,
    output_type: type[StructuredT] | None,
    timeout: float | None,
    max_attempts: int,
    retry_delay: float,
    should_retry: RetryPredicate | None,
    observer: AgentObserver | None,
    used_agents: set[Agent],
    used_providers: dict[int, object],
    used_agents_lock: asyncio.Lock,
) -> BatchItemResult[StructuredT]:
    started = time.monotonic()
    last_error: BaseException | None = None
    usage = BatchUsage()
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        agent: Agent | None = None
        baseline_usage = BatchUsage()
        result = None
        attempt_error: Exception | None = None
        attempt_stage = "factory"
        close_candidate = False
        registered = False
        bound_observer = (
            BoundObserver(observer, task.id, attempt) if observer is not None else None
        )
        try:
            created = await _call_factory(agent_factory, task)
            candidate = (
                await cast(Awaitable[Agent], created)
                if inspect.isawaitable(created)
                else created
            )
            if not isinstance(candidate, Agent):
                raise TypeError("agent_factory must return an Agent")
            agent = candidate
            close_candidate = True
            attempt_stage = "registration"
            registration_error = await register_agent(
                agent,
                used_agents,
                used_providers,
                used_agents_lock,
            )
            if registration_error is not None:
                close_candidate = registration_error.close_candidate
                raise registration_error
            registered = True
            baseline_usage = _usage_from_messages(agent.messages)
            attempt_stage = "prompt"
            with usage_metric_context(
                surface="sdk",
                sdk_operation="run_many",
                sdk_run_id=uuid4().hex,
            ):
                result = await agent.prompt_async(
                    task.prompt,
                    images=task.images,
                    image_urls=task.image_urls,
                    output_type=output_type,
                    observer=bound_observer,
                    timeout=timeout,
                )
            usage = _add_usage(
                usage,
                _subtract_usage(_usage_from_messages(result.messages), baseline_usage),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt_error = exc
            if agent is not None and registered:
                attempt_usage = _usage_from_messages(agent.messages)
                usage = _add_usage(
                    usage,
                    _subtract_usage(
                        attempt_usage,
                        baseline_usage,
                    ),
                )
        finally:
            close_error = await close_attempt_agent(agent, close_candidate)
            if close_error is not None and attempt_error is not None:
                emit_batch_error(bound_observer, close_error, stage="cleanup")
            elif close_error is not None:
                attempt_error = close_error
                attempt_stage = "cleanup"
                result = None
        if result is not None:
            return BatchItemResult(
                task=task,
                status="completed",
                attempts=attempt,
                duration_seconds=time.monotonic() - started,
                result=result,
                usage=usage,
            )
        last_error = attempt_error
        if attempt_error is None:
            break
        emit_batch_error(bound_observer, attempt_error, stage=attempt_stage)
        if attempt == max_attempts:
            break
        retry, last_error = await prepare_retry(
            attempt_error,
            should_retry=should_retry,
            retry_delay=retry_delay,
            observer=bound_observer,
        )
        if not retry:
            break
    if last_error is None:
        last_error = RuntimeError("Batch task did not produce a result")
    status = "timed_out" if isinstance(last_error, TimeoutError) else "error"
    return BatchItemResult(
        task=task,
        status=status,
        attempts=attempts,
        duration_seconds=time.monotonic() - started,
        error=last_error,
        usage=usage,
    )


async def _call_factory(
    factory: AgentFactory, task: BatchTask
) -> Agent | Awaitable[Agent]:
    """Call a potentially blocking factory outside the event loop."""
    worker = asyncio.create_task(asyncio.to_thread(factory, task))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        outcome = await drain_worker(worker)
        if isinstance(outcome, Agent):
            with suppress(BaseException):
                await outcome.aclose()
        elif inspect.iscoroutine(outcome):
            outcome.close()
        raise


async def _emit_progress(
    callback: ProgressCallback | None, progress: BatchProgress
) -> None:
    if callback is None:
        return
    outcome = callback(progress)
    if inspect.isawaitable(outcome):
        await outcome


def _aggregate_usage[StructuredT](
    items: list[BatchItemResult[StructuredT]],
) -> BatchUsage:
    return _sum_batch_usage([item.usage for item in items])


def _usage_from_messages(messages: Sequence[Message]) -> BatchUsage:
    usages = [
        usage
        for message in messages
        if (usage := getattr(message, "usage", None)) is not None
    ]
    return BatchUsage(
        calls=len(usages),
        input_tokens=_sum_usage(usages, "input_tokens"),
        output_tokens=_sum_usage(usages, "output_tokens"),
        reasoning_tokens=_sum_usage(usages, "reasoning_tokens"),
        total_tokens=_sum_usage(usages, "total_tokens"),
        cached_input_tokens=_sum_usage(usages, "cached_input_tokens"),
        cache_creation_input_tokens=_sum_usage(usages, "cache_creation_input_tokens"),
    )


def _sum_batch_usage(usages: list[BatchUsage]) -> BatchUsage:
    return BatchUsage(
        calls=sum(usage.calls for usage in usages),
        input_tokens=sum(usage.input_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        reasoning_tokens=sum(usage.reasoning_tokens for usage in usages),
        total_tokens=sum(usage.total_tokens for usage in usages),
        cached_input_tokens=sum(usage.cached_input_tokens for usage in usages),
        cache_creation_input_tokens=sum(
            usage.cache_creation_input_tokens for usage in usages
        ),
    )


def _add_usage(left: BatchUsage, right: BatchUsage) -> BatchUsage:
    return _sum_batch_usage([left, right])


def _subtract_usage(total: BatchUsage, baseline: BatchUsage) -> BatchUsage:
    return BatchUsage(
        calls=max(0, total.calls - baseline.calls),
        input_tokens=max(0, total.input_tokens - baseline.input_tokens),
        output_tokens=max(0, total.output_tokens - baseline.output_tokens),
        reasoning_tokens=max(0, total.reasoning_tokens - baseline.reasoning_tokens),
        total_tokens=max(0, total.total_tokens - baseline.total_tokens),
        cached_input_tokens=max(
            0, total.cached_input_tokens - baseline.cached_input_tokens
        ),
        cache_creation_input_tokens=max(
            0,
            total.cache_creation_input_tokens - baseline.cache_creation_input_tokens,
        ),
    )


def _sum_usage(usages: list[TokenUsage], field: str) -> int:
    return sum(int(getattr(usage, field) or 0) for usage in usages)


def _validate_inputs(
    tasks: list[BatchTask],
    *,
    max_concurrency: int,
    timeout: float | None,
    max_attempts: int,
    retry_delay: float,
) -> None:
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_delay < 0:
        raise ValueError("retry_delay must not be negative")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")
    ids = [task.id for task in tasks]
    if any(not task_id.strip() for task_id in ids):
        raise ValueError("Batch task ids must not be empty")
    if len(ids) != len(set(ids)):
        raise ValueError("Batch task ids must be unique")
