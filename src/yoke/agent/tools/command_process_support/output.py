"""Bounded output ownership for active and completed command processes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from yoke.agent.tools.command_process_types import MAX_RETAINED_OUTPUT_BYTES
from yoke.agent.tools.command_process_types import CommandProcessOutputChunk
from yoke.agent.tools.command_process_types import CommandProcessOutputPage
from yoke.agent.tools.command_process_types import CommandProcessSnapshot
from yoke.agent.tools.command_process_types import decode_command_output_chunk


@dataclass(slots=True, frozen=True)
class _OutputRecord:
    seq: int
    raw: bytes
    suppress_leading_lf: bool

    def decoded(self, *, include_previous: bool) -> str:
        text = decode_command_output_chunk(self.raw)
        if include_previous and self.suppress_leading_lf and text.startswith("\n"):
            text = text[1:]
        return text.replace("\r\n", "\n").replace("\r", "\n")


class RetainedProcessOutput:
    """Own the consumable queue and one bounded cursor/history ring."""

    def __init__(self) -> None:
        self._pending: deque[_OutputRecord] = deque()
        self._pending_bytes = 0
        self._pending_original_bytes = 0
        self._retained: deque[_OutputRecord] = deque()
        self._retained_bytes = 0
        self._next_seq = 1
        self._truncated_before_seq = 0
        self._original_bytes = 0
        self._previous_ended_cr = False
        self._last_consumed_seq = 0

    def append(self, raw: bytes) -> None:
        decoded = decode_command_output_chunk(raw)
        record = _OutputRecord(
            seq=self._next_seq,
            raw=raw,
            suppress_leading_lf=self._previous_ended_cr and decoded.startswith("\n"),
        )
        self._previous_ended_cr = decoded.endswith("\r")
        self._next_seq += 1
        self._original_bytes += len(raw)

        self._pending.append(record)
        self._pending_bytes += len(raw)
        self._pending_original_bytes += len(raw)
        while self._pending_bytes > MAX_RETAINED_OUTPUT_BYTES:
            dropped = self._pending.popleft()
            self._pending_bytes -= len(dropped.raw)

        self._retained.append(record)
        self._retained_bytes += len(raw)
        while self._retained_bytes > MAX_RETAINED_OUTPUT_BYTES:
            dropped = self._retained.popleft()
            self._retained_bytes -= len(dropped.raw)
            self._truncated_before_seq = dropped.seq

    def consume(self) -> tuple[str, int]:
        records = tuple(self._pending)
        output = _decode_records(
            records,
            first_has_previous=bool(
                records and records[0].seq == self._last_consumed_seq + 1
            ),
        )
        original_bytes = self._pending_original_bytes
        if records:
            self._last_consumed_seq = records[-1].seq
        self._pending.clear()
        self._pending_bytes = 0
        self._pending_original_bytes = 0
        return output, original_bytes

    def tail(self) -> str:
        return _decode_records(tuple(self._retained), first_has_previous=False)

    def page(self, *, after_seq: int, limit: int) -> CommandProcessOutputPage:
        records = tuple(record for record in self._retained if record.seq > after_seq)[
            :limit
        ]
        return CommandProcessOutputPage(
            chunks=_page_chunks(records, after_seq=after_seq),
            latest_seq=self.latest_seq,
            truncated_before_seq=self._truncated_before_seq,
        )

    def freeze(self) -> tuple[_OutputRecord, ...]:
        return tuple(self._retained)

    @property
    def original_bytes(self) -> int:
        return self._original_bytes

    @property
    def retained_bytes(self) -> int:
        return self._retained_bytes

    @property
    def latest_seq(self) -> int:
        return self._next_seq - 1

    @property
    def truncated_before_seq(self) -> int:
        return self._truncated_before_seq


@dataclass(slots=True, frozen=True)
class CompletedCommandProcess:
    """One final snapshot and its exact bounded output sequence history."""

    snapshot: CommandProcessSnapshot
    output_records: tuple[_OutputRecord, ...]

    def output_page(self, *, after_seq: int, limit: int) -> CommandProcessOutputPage:
        records = tuple(
            record for record in self.output_records if record.seq > after_seq
        )[:limit]
        return CommandProcessOutputPage(
            chunks=_page_chunks(records, after_seq=after_seq),
            latest_seq=self.snapshot.latest_output_seq,
            truncated_before_seq=self.snapshot.truncated_before_seq,
        )


def _page_chunks(
    records: tuple[_OutputRecord, ...], *, after_seq: int
) -> tuple[CommandProcessOutputChunk, ...]:
    return tuple(
        CommandProcessOutputChunk(
            seq=record.seq,
            text=record.decoded(
                include_previous=index > 0 or record.seq == after_seq + 1
            ),
        )
        for index, record in enumerate(records)
    )


def _decode_records(
    records: tuple[_OutputRecord, ...], *, first_has_previous: bool
) -> str:
    return "".join(
        record.decoded(include_previous=index > 0 or first_has_previous)
        for index, record in enumerate(records)
    )
