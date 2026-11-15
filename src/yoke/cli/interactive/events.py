"""Wake an input loop when either input or worker output becomes available."""

from queue import Queue
from threading import Event, Thread

from yoke.cli.interactive.common import InputFunc, InputInterrupted


class WakeQueue[T](Queue[T]):
    """A queue sharing a wake event with other event sources."""

    def __init__(self, wake: Event) -> None:
        super().__init__()
        self._wake = wake

    def put(self, item: T, block: bool = True, timeout: float | None = None) -> None:
        """Publish before waking so a consumer cannot miss the item."""
        super().put(item, block, timeout)
        self._wake.set()


def start_basic_input_reader(
    input_func: InputFunc,
    input_queue: Queue[str | None | InputInterrupted],
) -> None:
    def read_input() -> None:
        while True:
            try:
                raw = input_func("› ")
            except KeyboardInterrupt:
                input_queue.put(InputInterrupted())
                return
            except (EOFError, StopIteration):
                input_queue.put(None)
                return
            prompt = raw.strip()
            input_queue.put(prompt)
            if prompt.lower() in {"exit", "quit"}:
                return

    Thread(target=read_input, daemon=True).start()
