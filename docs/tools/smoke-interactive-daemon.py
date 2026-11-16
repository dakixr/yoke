"""Manual real-provider PTY smokes. Run with: uv run --with pyte python this_file.

Every daemon, socket, database and config belongs to a temporary directory.
The caller's provider environment is copied into that isolated child only.
"""

from __future__ import annotations

import argparse
import codecs
import fcntl
import importlib
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from yoke._version import __version__

ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


class PtySession:
    def __init__(self, root: Path, output: Path, case: str, model: str) -> None:
        self.root, self.output, self.case = root, output, case
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        home = root / "home"
        (home / ".yoke").mkdir(parents=True)
        (home / ".yoke" / "config.json").write_text(
            json.dumps(
                {
                    "default_model": model,
                    "capabilities": {"shell": "allow"},
                }
            )
        )
        self.environment = {
            **os.environ,
            **{
                "HOME": str(home),
                "USERPROFILE": str(home),
                "YOKE_DB_PATH": str(root / "data" / "yoke.db"),
                "YOKE_SESSION_DIR": str(root / "data"),
                "XDG_RUNTIME_DIR": str(root / "run"),
                "YOKE_DAEMON_PORT": "0",
                "YOKE_DAEMON_IDLE_GRACE": "10",
                "YOKE_PROVIDER_LOGS_DIR": str(root / "provider-logs"),
                "TERM": "xterm-256color",
                "PROMPT_TOOLKIT_NO_CPR": "1",
            },
        }
        for name in ("YOKE_DAEMON_BUILD_ID", "YOKE_DAEMON_PROTOCOL", "YOKE_HTTP_TOKEN"):
            self.environment.pop(name, None)
        self.session_id = f"stage3-smoke-{case}"
        self.socket = root / "run" / "yoke" / "daemon.sock"
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
        self.process = subprocess.Popen(
            [sys.executable, "-m", "yoke", "--session", self.session_id],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.workspace,
            env=self.environment,
            start_new_session=True,
        )
        os.close(slave)
        self.raw = bytearray()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pyte = importlib.import_module("pyte")
        self.screen = pyte.HistoryScreen(120, 36, history=10000)
        self.terminal = pyte.Stream(self.screen)
        self.checkpoints: list[str] = []
        self.http = httpx.Client(
            transport=httpx.HTTPTransport(uds=str(self.socket)),
            base_url="http://yoke/api/v1/",
            timeout=2,
            trust_env=False,
        )
        self.pids: list[int] = []

    def read(self, timeout: float = 0.05) -> None:
        if not select.select([self.master], [], [], timeout)[0]:
            return
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            return
        self.raw.extend(chunk)
        self.terminal.feed(self.decoder.decode(chunk))

    def until(self, probe, *, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read()
            try:
                if probe():
                    return
            except (httpx.HTTPError, FileNotFoundError, json.JSONDecodeError):
                pass
            if self.process.poll() is not None:
                raise AssertionError(
                    f"CLI exited {self.process.returncode}: {self.text()}"
                )
        raise AssertionError(f"Timed out in smoke {self.case}: {self.text()}")

    def request(self, method: str, route: str, **kwargs):
        return (
            self.http.request(method, route, **kwargs).raise_for_status().json()["data"]
        )

    def session(self, method: str, suffix: str = "", **kwargs):
        return self.request(method, f"session/{self.session_id}{suffix}", **kwargs)

    def start(self) -> None:
        self.until(lambda: f"Version {__version__}".encode() in self.raw)
        self.pids.append(self.request("GET", "daemon")["pid"])
        self.send("/info")
        self.until(lambda: "Session info:" in self.text())

    def send(self, text: str) -> None:
        # run_in_terminal temporarily restores canonical input. Wait for the
        # editor to take raw input again so CR is not translated into Ctrl+J.
        self.until(lambda: not termios.tcgetattr(self.master)[3] & termios.ICANON)
        os.write(self.master, (text + "\r").encode())

    def keys(self, text: str) -> None:
        self.until(lambda: not termios.tcgetattr(self.master)[3] & termios.ICANON)
        os.write(self.master, text.encode())

    def text(self) -> str:
        return ANSI.sub("", self.raw.decode("utf-8", "replace")).replace("\r\n", "\n")

    def frame(self, label: str) -> None:
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            self.read()
        print(f"Smoke {self.case}: {label}", flush=True)
        self.checkpoints.append(
            label
            + "\n"
            + "\n".join(line.rstrip() for line in self.screen.display).strip()
            + "\n"
        )

    def answer(self, marker: str) -> None:
        def complete():
            values = self.session(
                "GET", "/message", params={"order": "asc", "limit": 200}
            )
            return any(
                item["type"] == "assistant"
                and any(
                    part.get("text", "").strip() == marker
                    for part in item.get("content", [])
                )
                for item in values
            )

        self.until(complete)
        self.until(
            lambda: (
                self.session("POST", "/wait", params={"timeoutMs": 100})["state"]
                == "idle"
            )
        )
        self.until(lambda: marker in self.text())
        # Settlement precedes the UI's transcript fetch and scrollback batch.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            self.read()

    def running_tool(self) -> None:
        self.until(
            lambda: any(
                item["status"] == "running"
                for item in self.session("GET", "/tool-call", params={"limit": 200})
            )
        )

    def curl(self, method: str, suffix: str, data: dict[str, object]) -> None:
        result = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--fail-with-body",
                "--unix-socket",
                str(self.socket),
                "--request",
                method,
                "--header",
                "Content-Type: application/json",
                "--data-binary",
                json.dumps(data),
                f"http://yoke/api/v1/session/{self.session_id}{suffix}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr + result.stdout

    def finish(self) -> dict[str, object]:
        messages = self.session(
            "GET", "/message", params={"order": "asc", "limit": 200}
        )
        prompts = [
            "".join(part.get("text", "") for part in item["content"])
            for item in messages
            if item["type"] == "user"
        ]
        assert len(prompts) == len(set(prompts)), prompts
        self.send("exit")
        deadline = time.monotonic() + 15
        while self.process.poll() is None and time.monotonic() < deadline:
            self.read()
        assert self.process.wait(timeout=2) == 0
        self.read()
        self.frame("After exit")
        summary = {
            "case": self.case,
            "session": self.session_id,
            "daemon_pids": self.pids,
            "prompt_count": len(prompts),
            "prompts": prompts,
            "exit": self.process.returncode,
        }
        (self.output / f"{self.case}.summary.json").write_text(
            json.dumps(summary, indent=2) + "\n"
        )
        return summary

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        try:
            pid = self.request("GET", "daemon")["pid"]
            self.request("POST", "daemon/stop", json={"force": True})
            deadline = time.monotonic() + 8
            while (
                Path(f"/proc/{pid}/cmdline").exists()
                and Path(f"/proc/{pid}/cmdline").read_bytes()
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if (
                Path(f"/proc/{pid}/cmdline").exists()
                and Path(f"/proc/{pid}/cmdline").read_bytes()
            ):
                os.kill(pid, signal.SIGKILL)
        except (httpx.HTTPError, FileNotFoundError, ProcessLookupError):
            pass
        self.http.close()
        os.close(self.master)
        (self.output / f"{self.case}.ansi.txt").write_bytes(self.raw)
        (self.output / f"{self.case}.raw.txt").write_text(self.text())
        (self.output / f"{self.case}.screens.txt").write_text(
            "\n".join(self.checkpoints)
        )
        log = self.root / "home" / ".yoke" / "run" / "daemon.log"
        if log.exists():
            (self.output / f"{self.case}.daemon.log").write_bytes(log.read_bytes())


def smoke(case: str, output: Path, model: str) -> dict[str, object]:
    with TemporaryDirectory(prefix=f"yk-s3-{case}-") as directory:
        cli = PtySession(Path(directory), output, case, model)
        try:
            cli.start()
            if case == "c":
                cli.send(
                    "Use the bash tool to run: echo pty-$((6*7)). Reply with only its output."
                )
                cli.answer("pty-42")
                cli.send("/info")
                cli.until(lambda: cli.text().count("Session info:") >= 2)
                cli.frame("Tool turn and /info")
                cli.send("/tree")
                cli.until(
                    lambda: "Branch" in cli.text() or "Session Tree" in cli.text()
                )
                cli.frame("/tree selector")
                cli.keys("\r")
                cli.until(lambda: "Already at this point." in cli.text(), timeout=10)
                cli.keys("\x18o")
                cli.until(
                    lambda: (
                        "Tool Inspector" in cli.text() or "Tool inspector" in cli.text()
                    )
                )
                cli.keys("k")
                cli.until(lambda: "Arguments" in cli.text(), timeout=10)
                cli.frame("Tool inspector reading command_exec")
                offset = len(cli.raw)
                cli.keys("q")
                cli.until(lambda: b"\x1b[?1049l" in cli.raw[offset:])
                cli.send(
                    'Run the bash command `sleep 30` using command_exec mode="auto", never background, then say done.'
                )
                cli.running_tool()
                cli.send(
                    "Stop that command. Reply with exactly `STEER-OK` and no punctuation."
                )
                cli.answer("STEER-OK")
                cli.frame("Steering")
                cli.send(
                    'Run the bash command `sleep 30` using command_exec mode="auto", never background, then reply SLEEP-DONE.'
                )
                cli.running_tool()
                cli.keys("\x1b\x1b")
                cli.until(lambda: "Stopped current turn." in cli.text())
                cli.frame("Esc-Esc interruption")
            elif case == "d":
                cli.curl(
                    "PUT",
                    "/client",
                    {
                        "owner": "smoke-second",
                        "kind": "cli",
                        "mode": "full-access",
                        "cwd": str(cli.workspace),
                    },
                )
                cli.curl(
                    "POST",
                    "/prompt",
                    {
                        "id": "second-client-input",
                        "clientID": "smoke-second",
                        "prompt": {"text": "Reply only with SECOND-CLIENT-OK."},
                    },
                )
                cli.answer("SECOND-CLIENT-OK")
                cli.frame("Second client prompt and answer")
            else:
                cli.send("Reply with exactly `BEFORE-RESTART-OK` and no punctuation.")
                cli.answer("BEFORE-RESTART-OK")
                os.kill(cli.pids[0], signal.SIGKILL)
                cli.send("Reply with exactly `AFTER-RESTART-OK` and no punctuation.")
                cli.answer("AFTER-RESTART-OK")
                cli.pids.append(cli.request("GET", "daemon")["pid"])
                assert cli.pids[0] != cli.pids[1]
                cli.frame("SIGKILL and reattachment")
            return cli.finish()
        finally:
            cli.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["c", "d", "e", "all"], default="all")
    parser.add_argument("--model", default="codex:gpt-6.1-sol")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("tmp/phase5-stage3-proof")
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for case in ("c", "d", "e") if args.case == "all" else (args.case,):
        print(
            json.dumps(smoke(case, args.output_dir.resolve(), args.model)), flush=True
        )


if __name__ == "__main__":
    main()
