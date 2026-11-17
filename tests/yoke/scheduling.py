"""Queue expensive interpreter batches without concurrent daemon startup storms."""

from __future__ import annotations

from pathlib import Path

import pytest


PROCESS_GROUPS = {
    "cli/test_headless_client.py": "daemon-processes",
    "daemon/test_windows_process.py": "daemon-processes",
    "daemon/test_process.py": "daemon-processes",
    "daemon/test_adversarial_process.py": "daemon-processes",
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
