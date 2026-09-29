"""Host-side lifetime checks for retained instances and remote command launches."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import sys

from yoke.agent_runs.worker import Worker


class Lifetime:
    """Retain authority until its owner exits or its managed tree is cleaned up."""

    def __init__(
        self,
        *,
        worker: Worker | None = None,
        parent: Lifetime | None = None,
        process_launch: bool = False,
    ) -> None:
        self.worker = worker
        self.parent = parent
        self.process_launch = process_launch
        self.poll: Callable[[], int | None] | None = None
        self.child: Worker | None = None
        self.child_pid: int | None = None
        self.child_group: int | None = None
        self._worker_dead = False
        self._child_dead = False
        self._retired = False

    def attach(self, pid: int, issuer: int) -> None:
        if not self.process_launch or self.worker is None or self.worker.pid != issuer:
            raise ValueError("Invalid launch observer")
        if self.child_pid is not None:
            if self.child_pid != pid and self.child_group != pid:
                raise ValueError("Launch process identity conflict")
            return
        try:
            self.child = Worker.open(pid)
        except ProcessLookupError:
            self._child_dead = True
        self.child_pid = pid
        self.child_group = pid

    @property
    def unattached(self) -> bool:
        return (
            self.process_launch and self.worker is not None and self.child_pid is None
        )

    def observe(self, pid: int) -> None:
        """Recover a lost attachment from an authenticated descendant's first use."""
        if not self.unattached or self.worker is None or pid == self.worker.pid:
            return
        group = os.getpgid(pid) if os.name != "nt" else None
        child = Worker.open(pid)
        self.child, self.child_pid, self.child_group = child, pid, group

    def dead(self) -> bool:
        if self._retired:
            return True
        if self.poll is not None:
            self._retired = self.poll() is not None
        elif self.worker is not None:
            self._worker_dead = self._worker_dead or self.worker.dead()
            if self._worker_dead:
                self.worker.close()
                self._retired = not self.process_launch or self._tree_dead()
        return self._retired

    def _tree_dead(self) -> bool:
        if self.child_pid is None:
            # Nested launches own separate process groups. Ancestor cleanup
            # cannot prove this unobserved child's death. Keep bounded authority
            # until a descendant identifies itself or the host explicitly closes.
            return False
        self._child_dead = self._child_dead or (
            self.child is not None and self.child.dead()
        )
        if not self._child_dead:
            return False
        if os.name == "nt":
            # A process handle cannot prove every unobserved descendant exited.
            # Explicit managed-tree cleanup can still revoke this capability.
            return False
        return self.child_group is not None and not _group_alive(self.child_group)

    def close(self) -> None:
        self._retired = True
        self.poll = None
        self.parent = None
        if self.worker is not None:
            self.worker.close()
        if self.child is not None:
            self.child.close()


def _group_alive(group: int) -> bool:
    if sys.platform.startswith("linux"):
        # A killed orphan can remain a zombie until its reaper runs. It holds no
        # launch authority and must not keep a dead scope in the registry forever.
        try:
            for path in Path("/proc").iterdir():
                if not path.name.isdecimal():
                    continue
                try:
                    fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                        return True
                except (FileNotFoundError, ProcessLookupError):
                    continue
            return False
        except (OSError, ValueError, IndexError):
            pass
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True
