"""Run a disposable daemon and exercise its Unix API using curl.

Run from the repository root: uv run python docs/qa/phase4/smoke.py
No client environment dictionaries or real credentials are written to files.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from tests.yoke.http.test_daemon_api_startup import PLUGIN

    sandbox = Path(tempfile.mkdtemp(prefix="yoke-phase4-smoke-"))
    home, workspace = sandbox / "home", sandbox / "workspace"
    providers = home / ".yoke" / "providers"
    providers.mkdir(parents=True)
    workspace.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_RUNTIME_DIR=str(sandbox / "runtime"),
        YOKE_DB_PATH=str(sandbox / "yoke.db"),
        YOKE_SESSION_DIR=str(sandbox / "sessions"),
        YOKE_PROVIDER_LOGS_DIR=str(sandbox / "provider-logs"),
        YOKE_CODEX_LOGS_DIR=str(sandbox / "provider-logs"),
        YOKE_CODEX_WEBSOCKETS_LOGS_DIR=str(sandbox / "provider-logs"),
    )
    for name in ("YOKE_HTTP_TOKEN", "YOKE_ACP_URL", "YOKE_ACP_TOKEN"):
        env.pop(name, None)
    real = next(
        (
            name
            for name in ("YOKE_CODEX_API_KEY", "OPENCODE_API_KEY", "ZAI_API_KEY")
            if env.get(name)
        ),
        None,
    )
    provider = {
        "YOKE_CODEX_API_KEY": "codex",
        "OPENCODE_API_KEY": "opencode-go",
        "ZAI_API_KEY": "zai",
    }.get(real or "", "fixture")
    config: dict[str, object] = {"capabilities": {"shell": "allow"}}
    if real is None:
        (providers / "fixture.py").write_text(PLUGIN)
        config["default_model"] = "fixture:default"
        env["FAKE_API_KEY"] = "fixture-client-key"
    (home / ".yoke" / "config.json").write_text(json.dumps(config))
    print(
        f"Sandbox: {sandbox}\nProvider: {provider}; credential source: {real or 'fake fixture key'}",
        flush=True,
    )
    command = [
        str(ROOT / ".venv" / "bin" / "yoke"),
        "serve",
        "--daemon",
        "--no-idle-exit",
        "--port",
        "0",
    ]
    print("$ " + shlex.join(command), flush=True)
    log_path = sandbox / "daemon.log"
    log = log_path.open("wb")
    daemon = subprocess.Popen(command, cwd=workspace, env=env, stdout=log, stderr=log)
    socket = sandbox / "runtime" / "yoke" / "daemon.sock"
    prefix = [
        "curl",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--unix-socket",
        str(socket),
    ]
    streams: list[tuple[subprocess.Popen[bytes], BinaryIO, Path]] = []

    def curl(method: str, path: str, payload: dict[str, object] | None = None) -> dict:
        args = [*prefix, "-X", method, "http://localhost/api/v1" + path]
        if payload is not None:
            args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        print("$ " + shlex.join(args), flush=True)
        result = subprocess.run(
            args,
            input=json.dumps(payload).encode() if payload is not None else None,
            capture_output=True,
            check=False,
        )
        print(result.stdout.decode(), flush=True)
        if result.returncode:
            raise RuntimeError(result.stderr.decode())
        return json.loads(result.stdout)

    try:
        deadline = time.monotonic() + 15
        while not socket.exists():
            if time.monotonic() > deadline or daemon.poll() is not None:
                raise RuntimeError(
                    "Isolated daemon did not start: " + log_path.read_text()
                )
            time.sleep(0.05)
        health = curl("GET", "/health")
        assert health["data"]["version"] == "3.26.0"
        status = curl("GET", "/daemon")["data"]
        assert status["port"] is not None and status["port"] != 8770
        for session in ("shared", "unrelated"):
            curl(
                "POST",
                "/session",
                {
                    "id": session,
                    "title": session,
                    "location": {"directory": str(workspace)},
                    "selection": {"provider": provider},
                },
            )
        values = {
            "alpha": "phase4-private-alpha-7b9cb9",
            "beta": "phase4-private-beta-09ff11",
        }
        for owner, value in values.items():
            client_env = {**env, "YOKE_SENTINEL_X": value}
            curl(
                "PUT",
                "/session/shared/client",
                {
                    "owner": owner,
                    "kind": "cli",
                    "cwd": str(workspace),
                    "env": client_env,
                },
            )
            path = sandbox / (owner + ".sse")
            handle = path.open("wb")
            args = [
                *prefix,
                "--no-buffer",
                "http://localhost/api/v1/event?sessionID=shared&client=" + owner,
            ]
            print("$ " + shlex.join(args), flush=True)
            process = subprocess.Popen(args, stdout=handle, stderr=subprocess.DEVNULL)
            streams.append((process, handle, path))
        deadline = time.monotonic() + 10
        while not all("server.connected" in path.read_text() for _, _, path in streams):
            if time.monotonic() > deadline:
                raise RuntimeError("SSE streams did not connect")
            time.sleep(0.05)
        curl("PATCH", "/session/unrelated", {"title": "FILTER-NOISE-MUST-NOT-ARRIVE"})
        for owner in values:
            text = f'Prompt from {owner}. Run command_exec with argv ["bash", "-c", "echo $YOKE_SENTINEL_X"] once, then reply exactly smoke-{owner}-ok.'
            curl(
                "POST",
                "/session/shared/prompt",
                {"clientID": owner, "delivery": "queue", "prompt": {"text": text}},
            )
        result = curl("POST", "/session/shared/wait?timeoutMs=300000")
        assert result["data"]["state"] == "idle", result
        curl("DELETE", "/session/shared/client?owner=alpha")
        streams[0][0].terminate()
        streams[0][0].wait(timeout=5)
        streams[0][1].close()
        curl("GET", "/session/shared/client?owner=beta")
        status = curl("GET", "/daemon")["data"]
        assert [lease["client"] for lease in status["leases"]] == ["beta"]
        curl(
            "POST",
            "/session/shared/prompt",
            {
                "clientID": "beta",
                "prompt": {
                    "text": 'Prompt from beta after alpha detached. Run command_exec with argv ["bash", "-c", "echo $YOKE_SENTINEL_X"] once, then reply exactly smoke-detach-ok.'
                },
            },
        )
        result = curl("POST", "/session/shared/wait?timeoutMs=300000")
        assert result["data"]["state"] == "idle", result
        curl("POST", "/session/shared/drain?timeoutMs=10000")
        time.sleep(0.2)
        for process, handle, path in streams:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
            handle.close()
            text = path.read_text()
            assert '"clientID":"alpha"' in text and '"clientID":"beta"' in text
            assert "FILTER-NOISE" not in text and '"sessionID":"unrelated"' not in text
            assert all(value not in text for value in values.values())
            print(f"Raw filtered SSE excerpt: {path.name}", flush=True)
            selected = {
                "server.connected",
                "session.prompt.admitted",
                "session.tool.ended",
                "session.message.updated",
            }
            for block in text.split("\n\n"):
                if any("event: " + name + "\n" in block for name in selected):
                    print(block + "\n", flush=True)
            print(
                "Filter assertions: both owners observed; unrelated session absent; echoed sentinels redacted.",
                flush=True,
            )
        streams.clear()
        curl("POST", "/daemon/stop", {"force": True})
        daemon.wait(timeout=15)
        log.close()
        targets = [
            *sandbox.glob("yoke.db*"),
            *sandbox.glob("provider-logs/*"),
            log_path,
        ]
        args = ["grep", "-aF", "-l"]
        for value in values.values():
            args += ["-e", value]
        args += [str(path) for path in targets if path.is_file()]
        print("$ " + shlex.join(args), flush=True)
        found = subprocess.run(args, capture_output=True, check=False)
        print(found.stdout.decode() + found.stderr.decode(), end="", flush=True)
        print(
            f"grep exit={found.returncode} (1 means no sentinel in DB/WAL/logs)",
            flush=True,
        )
        assert found.returncode == 1
        print("Daemon exit=" + str(daemon.returncode), flush=True)
    finally:
        for process, handle, _ in streams:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            handle.close()
        if daemon.poll() is None:
            daemon.terminate()
            daemon.wait(timeout=15)
        log.close()


if __name__ == "__main__":
    main()
