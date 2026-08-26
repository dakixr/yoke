"""Shared constants and helpers for the yoke CLI entry point."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import click
import typer

if TYPE_CHECKING:
    from yoke.cli.config import CLIArgs

CWD = Path.cwd().absolute()
SOURCE_ROOT = Path(__file__).resolve().parents[1]

_SUBCOMMANDS = frozenset(
    {
        "version",
        "login",
        "serve",
        "resume",
        "tools",
        "models",
        "providers",
        "mcp",
        "skills",
    }
)
_OPTIONS_WITH_VALUES = frozenset(
    {
        "--prompt",
        "--session",
        "--fork",
        "--model",
        "--reasoning-effort",
        "--root",
        "--skill",
        "--image",
    }
)


def build_cli_args(
    *,
    prompt: str | None = None,
    headless: bool = False,
    session: str | None = None,
    fork_session_id: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    root: Path,
    skills: tuple[str, ...] = (),
    images: tuple[str, ...] = (),
) -> CLIArgs:
    """Build normalized CLI arguments without importing runtime eagerly."""
    from yoke.cli.config import CLIArgs

    return CLIArgs(
        prompt=prompt,
        headless=headless,
        session=session,
        fork_session_id=fork_session_id,
        model=model,
        reasoning_effort=reasoning_effort,
        root=str(root),
        skills=skills,
        images=images,
    )


def _run_lazy_typer_app(lazy_app: typer.Typer, args: list[str], prog_name: str) -> int:
    try:
        result = lazy_app(
            args=args,
            prog_name=prog_name,
            standalone_mode=False,
        )
    except typer.Exit as exc:
        return int(exc.exit_code)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    if isinstance(result, int):
        return result
    return 0


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _load_source_dotenv(source_root: Path = SOURCE_ROOT) -> None:
    """Load the source `.env` into the current process env."""
    dotenv_path = source_root / ".env"
    if not dotenv_path.is_file():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env_key = key.strip()
        if not env_key:
            continue
        os.environ[env_key] = _strip_matching_quotes(value.strip())


def _inject_prompt_flag(argv: list[str]) -> list[str]:
    """Convert `yoke "message"` → `yoke --prompt "message"` at the entry point."""
    result = list(argv)
    i = 0
    while i < len(result):
        arg = result[i]
        if arg == "--":
            break
        if arg.startswith("-"):
            i += 1
            if "=" not in arg and arg in _OPTIONS_WITH_VALUES:
                i += 1  # skip option value
        else:
            if arg not in _SUBCOMMANDS:
                result.insert(i, "--prompt")
            return result
    return result
