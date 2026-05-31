from __future__ import annotations

# ruff: noqa: D100,D102,E501,S603

import json
import os
import shlex
import shutil
import subprocess

from yoke.agent.tools.base import WorkspaceTool
from pydantic import Field


class RipgrepTool(WorkspaceTool):
    """Run ripgrep using a single raw argument string, close to native rg usage."""

    is_yoke_tool = True
    name = "rg"
    description = (
        "Run ripgrep using a single raw_args string containing the exact arguments "
        "you would pass after 'rg'. Prefer rg over other tools for file listing, searching patterns, etc."
    )

    raw_args: str = Field(min_length=1)
    max_output_chars: int = 12_000

    def execute(self) -> dict[str, object]:
        rg_path = self._find_rg_binary()
        user_argv = self._parse_raw_args()
        command = [rg_path]
        if "--json" not in user_argv:
            command.append("--json")
        command.extend(user_argv)
        if not self._has_explicit_path(user_argv):
            command.append(str(self.root))

        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "rg timed out after 20 seconds"}
        except Exception as exc:
            return {"ok": False, "output": str(exc)}

        if completed.returncode not in {0, 1}:
            return self._render_text(completed.stdout, completed.stderr)

        parsed = self._parse_json_output(completed.stdout, command)
        if parsed is None:
            return self._render_text(completed.stdout, completed.stderr)
        return parsed

    def _find_rg_binary(self) -> str:
        rg_path = shutil.which("rg")
        if rg_path:
            return rg_path
        raise FileNotFoundError("ripgrep binary 'rg' was not found on PATH")

    def _parse_raw_args(self) -> list[str]:
        argv = shlex.split(self.raw_args, posix=os.name != "nt")
        if os.name != "nt":
            return argv
        return [self._strip_wrapping_quotes(arg) for arg in argv]

    def _strip_wrapping_quotes(self, value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    def _has_explicit_path(self, argv: list[str]) -> bool:
        saw_pattern = False
        i = 0
        while i < len(argv):
            arg = argv[i]
            if arg == "--":
                return i + 1 < len(argv)
            if arg in {
                "-A",
                "-B",
                "-C",
                "-E",
                "-M",
                "-e",
                "-f",
                "-g",
                "-j",
                "-m",
                "-P",
                "--after-context",
                "--before-context",
                "--context",
                "--encoding",
                "--file",
                "--glob",
                "--iglob",
                "--max-count",
                "--max-depth",
                "--max-filesize",
                "--path-separator",
                "--pre",
                "--pre-glob",
                "--regex-size-limit",
                "--sort",
                "--sortr",
                "--threads",
                "--type",
                "--type-add",
                "--type-clear",
            }:
                i += 2
                continue
            if arg.startswith(
                (
                    "--glob=",
                    "--iglob=",
                    "--max-count=",
                    "--context=",
                    "--type=",
                    "--type-add=",
                    "--type-clear=",
                    "--file=",
                )
            ):
                i += 1
                continue
            if arg.startswith("-") and arg not in {"-", "--"}:
                i += 1
                continue
            if not saw_pattern:
                saw_pattern = True
            else:
                return True
            i += 1
        return False

    def _parse_json_output(
        self, stdout: str, command: list[str]
    ) -> dict[str, object] | None:
        matches: list[dict[str, object]] = []
        truncated = False
        included_match_count = 0
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                return None
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_data = data.get("path") or {}
            path = path_data.get("text") if isinstance(path_data, dict) else ""
            lines_data = data.get("lines") or {}
            text = lines_data.get("text")
            if text is None:
                text = str(lines_data.get("bytes") or "")
            match = {
                "path": path,
                "line": data.get("line_number"),
                "text": str(text).rstrip("\n"),
            }
            candidate_matches = [*matches, match]
            candidate_output: dict[str, object] = {
                "ok": True,
                "command": command,
                "output": candidate_matches,
            }
            if self._serialized_result_size(candidate_output) > self.max_output_chars:
                truncated = True
                break
            matches.append(match)
            included_match_count += 1
        output: dict[str, object] = {
            "ok": True,
            "command": command,
            "output": matches if matches else [],
        }
        if truncated:
            output["truncated"] = True
            output["summary"] = f"showing {included_match_count} matches"
            while (
                matches and self._serialized_result_size(output) > self.max_output_chars
            ):
                matches.pop()
                included_match_count -= 1
                output["summary"] = f"showing {included_match_count} matches"
        return output

    def _serialized_result_size(self, output: dict[str, object]) -> int:
        return len(json.dumps(output, ensure_ascii=False))

    def _render_text(self, stdout: str, stderr: str) -> dict[str, object]:
        output = stdout.rstrip("\n")
        if stderr.strip():
            output = (output + "\n" if output else "") + stderr.rstrip("\n")
        if len(output) > self.max_output_chars:
            line_count = output.count("\n") + (1 if output else 0)
            output = (
                output[: self.max_output_chars]
                + f"\n...[truncated after {line_count} lines]"
            )
        return {"ok": True, "output": output}
