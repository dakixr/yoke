"""Queue expensive interpreter batches without concurrent daemon startup storms."""

from __future__ import annotations

from pathlib import Path

import pytest


PROCESS_GROUPS = {
    "cli/test_headless_client.py": "daemon-processes",
    "cli/test_interactive_daemon_restart.py": "daemon-processes",
    "daemon/test_process.py": "daemon-processes",
    "daemon/test_adversarial_process.py": "daemon-controls",
    "storage/test_concurrency.py": "managed-processes",
    "storage/test_durability.py": "managed-processes",
    "agent_runs/test_lifetimes.py": "managed-processes",
    "http/test_agent_runs_end_to_end.py": "managed-processes",
}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    root = Path(__file__).parent
    for item in items:
        path = item.path.relative_to(root).as_posix()
        if group := PROCESS_GROUPS.get(path):
            item.add_marker(pytest.mark.xdist_group(group))

    # The process groups start first. Short tests fill the other workers;
    # protected projection tests start early enough to avoid a shutdown tail.
    def priority(item: pytest.Item) -> int:
        path = item.path.relative_to(root).as_posix()
        if path in PROCESS_GROUPS:
            return 0
        if path.startswith("provider_projection/"):
            return 1
        return 2

    items.sort(key=priority)
