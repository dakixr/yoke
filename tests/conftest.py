from __future__ import annotations

import os
import pwd
import signal
import ssl
import time
from collections.abc import Iterator
from functools import wraps
from pathlib import Path
from uuid import uuid4

import pytest
import httpx
import httpx2

from yoke.agent_runs.server import Server
from tests.yoke.tool_fixture import local_file_tools as local_file_tools
from tests.yoke.http_schema_fixture import (
    http_field_adapters as http_field_adapters,
    http_dependency_graphs as http_dependency_graphs,
    reuse_http_dependency_graphs as reuse_http_dependency_graphs,
    reuse_http_validation_schemas as reuse_http_validation_schemas,
)
from tests.yoke.scheduling import (
    pytest_collection_modifyitems as pytest_collection_modifyitems,
)
from tests.yoke.schema_fixture import (
    empty_schema as empty_schema,
    fresh_database_from_schema as fresh_database_from_schema,
)


class FixtureRunServer(Server):
    """Use a short idle accept tick while preserving peer/auth/frame handling."""

    def _serve(self) -> None:
        self.socket.settimeout(0.005)
        super()._serve()


@pytest.fixture(autouse=True)
def fast_run_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yoke.agent_runs.registry.Server", FixtureRunServer)


@pytest.fixture(scope="session")
def transport_certificates() -> dict[tuple[object, bool, bool, bool], ssl.SSLContext]:
    return {}


@pytest.fixture(autouse=True)
def reuse_transport_certificate_setup(
    monkeypatch: pytest.MonkeyPatch,
    transport_certificates: dict[tuple[object, bool, bool, bool], ssl.SSLContext],
) -> None:
    """Share the default CA bundle, never clients or application state.

    HTTPX constructs a TLS context even for plain HTTP and Unix sockets. CLI
    fixtures create many short-lived clients; loading the same CA bundle for
    each request costs more than the actual request. Pools, sockets, clients
    and application state remain independent. Explicit TLS settings and
    environment-provided certificates still take the normal constructor path.
    """
    contexts = transport_certificates

    def reuse(initializer, create_context):
        @wraps(initializer)
        def initialize(transport, *args, **kwargs):
            if (
                not args
                and kwargs.get("verify", True) is True
                and kwargs.get("cert") is None
                and not os.environ.get("SSL_CERT_FILE")
                and not os.environ.get("SSL_CERT_DIR")
            ):
                # HTTP core sets ALPN on connect. Keep contexts separate when
                # transports advertise different protocol configurations.
                key = (
                    create_context,
                    kwargs.get("trust_env", True),
                    kwargs.get("http1", True),
                    kwargs.get("http2", False),
                )
                if key not in contexts:
                    contexts[key] = create_context(trust_env=key[1])
                kwargs["verify"] = contexts[key]
            initializer(transport, *args, **kwargs)

        return initialize

    for library in (httpx, httpx2):
        for transport in (library.HTTPTransport, library.AsyncHTTPTransport):
            monkeypatch.setattr(
                transport,
                "__init__",
                reuse(transport.__init__, library.create_ssl_context),
            )


@pytest.fixture(autouse=True)
def isolate_yoke_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Ambient DB overrides would make unrelated tests share one database. Tests
    # exercising YOKE_DB_PATH set their own override after this fixture.
    monkeypatch.delenv("YOKE_DB_PATH", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("YOKE_DAEMON_PORT", "0")
    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("YOKE_PROVIDER_LOGS_DIR", str(tmp_path / "provider-logs"))


@pytest.fixture(autouse=True, scope="session")
def forbid_real_database_writes() -> Iterator[None]:
    """Fail the run if any test, or a subprocess it starts, creates ~/.yoke/yoke.db."""
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".yoke" / "yoke.db"
    existed = real.exists()
    yield
    if not existed and real.exists():
        pytest.fail(
            f"A test created the real database {real}; isolate HOME/YOKE_DB_PATH."
        )


def _daemon_processes() -> set[int]:
    found: set[int] = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            if b"serve" in arguments and b"--daemon" in arguments:
                found.add(int(process.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return found


@pytest.fixture(autouse=True, scope="session")
def forbid_surviving_test_daemons() -> Iterator[None]:
    """Detect detached test processes too, without touching pre-existing daemons."""
    before = _daemon_processes()
    marker = uuid4().hex
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("YOKE_TEST_RUN_ID", marker)
        yield
    survivors = []
    for pid in _daemon_processes() - before:
        try:
            environment = dict(
                item.split(b"=", 1)
                for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                if b"=" in item
            )
            if environment.get(b"YOKE_TEST_RUN_ID") == marker.encode():
                # Releasing the lifetime lock precedes interpreter teardown.
                # Allow a stopped daemon to finish exiting before failing.
                deadline = time.monotonic() + 5
                command = Path(f"/proc/{pid}/cmdline")
                while command.read_bytes() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if command.read_bytes():
                    survivors.append(pid)
                    os.kill(pid, signal.SIGKILL)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    if survivors:
        pytest.fail(f"Test-spawned yoke serve --daemon survived: {survivors}")
