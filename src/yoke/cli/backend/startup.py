"""Terminal startup configuration resolved by the daemon's CLI resolver."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from rich.text import Text

from yoke.agent.models import Message
from yoke.cli.backend.connection import DaemonRequestError, SessionConnection
from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.backend.headless import run_headless_prompt
from yoke.cli.config import CLIArgs
from yoke.cli.image_input import resolve_image_path
from yoke.cli.render import OutputStream, build_console, print_error
from yoke.daemon.client import DaemonIncompatible, DaemonUnavailable
from yoke.session.repository.utils import new_session_id
from yoke.session.workspace import require_workspace


def create_session(
    connection: SessionConnection, args: CLIArgs, *, root: Path
) -> dict[str, Any]:
    """Resolve model precedence, skills, diagnostics and private client config."""
    return connection.request(
        "POST",
        "session",
        json={
            "id": connection.session_id,
            "location": {"directory": str(root)},
            "startup": True,
            "headless": args.headless,
            "skills": list(args.skills),
            "selection": {
                "provider": args.provider_name,
                "model": args.model,
                "reasoningEffort": args.reasoning_effort,
            },
            "client": connection.client_config(),
        },
    )


def validate_selection(
    connection: SessionConnection, args: CLIArgs, root: Path
) -> None:
    """Reject malformed explicit selections before creating a session."""
    if not args.model:
        return
    from yoke.cli.providers.selection import parse_cli_model_ref

    ref = parse_cli_model_ref(args.model)
    if ref.reasoning_effort is None:
        return
    models = connection.request(
        "GET", "model", params={"directory": str(root), "provider": ref.provider_name}
    )["data"]
    for model in models:
        if model["id"] == ref.model:
            if ref.reasoning_effort.strip().lower() not in model["reasoningEfforts"]:
                supported = (
                    ", ".join(model["reasoningEfforts"])
                    or "no configurable levels; omit the thinking effort"
                )
                raise ValueError(
                    f"Unsupported thinking effort {ref.reasoning_effort!r} for model {ref.provider_name + ':' + model['id']!r}. Supported: {supported}."
                )
            return


def validate_fork_selection(
    connection: SessionConnection, args: CLIArgs, root: Path
) -> None:
    """Reject strict model selections before creating a durable fork."""
    selection = args.model
    if selection is None and args.headless:
        source = connection.request("GET", f"session/{args.fork_session_id}")["data"][
            "selection"
        ]
        if source.get("provider") and source.get("model"):
            selection = f"{source['provider']}:{source['model']}"
    if not selection:
        return
    from yoke.cli.providers.selection import parse_cli_model_ref

    ref = parse_cli_model_ref(selection)
    models = connection.request(
        "GET", "model", params={"directory": str(root), "provider": ref.provider_name}
    )["data"]
    if models and not any(model["id"] == ref.model for model in models):
        raise ValueError(
            f"Unknown model {ref.model!r} for provider {ref.provider_name!r}."
        )


def run_daemon_headless(
    args: CLIArgs,
    *,
    input_func: Callable[..., str] = input,
    stdout: OutputStream | None = None,
    stderr: OutputStream | None = None,
    resume_id: str | None = None,
    relocate: Path | str | None = None,
) -> int:
    """Run a headless CLI invocation with no local runtime or fallback."""
    output = stdout or sys.stdout
    error = stderr or sys.stderr
    error_console = build_console(error)
    connection = None
    try:
        if relocate is not None and resume_id is None and args.session is None:
            raise ValueError("--relocate requires an explicit session ID.")
        prompt = args.prompt
        if not prompt:
            if sys.stdin.isatty():
                raise ValueError(
                    "Headless mode requires --prompt or prompt text from stdin."
                )
            prompt = input_func().strip()
            if not prompt:
                raise ValueError(
                    "Headless mode requires non-empty prompt text from stdin."
                )
        connection = SessionConnection(
            resume_id or args.session or new_session_id(),
            warning=lambda text: print_error(error_console, f"Warning: {text}"),
        )
        root = (
            require_workspace(args.root)
            if not (resume_id or args.session)
            else Path(args.root).expanduser().resolve()
        )
        if resume_id and relocate is not None:
            connection.session_request(
                "POST", "/relocate", json={"directory": str(Path(relocate).resolve())}
            )
        if resume_id:
            info = connection.session_request("GET")["data"]
            root = Path(info["location"]["directory"])
        elif args.fork_session_id:
            validate_fork_selection(connection, args, root)
            validate_selection(connection, args, root)
            info = connection.request(
                "POST",
                f"session/{args.fork_session_id}/fork",
                json={
                    "id": connection.session_id,
                    "location": {"directory": str(root)},
                },
            )["data"]
            root = Path(info["location"]["directory"])
        elif args.session:
            from yoke.cli.backend.connection import DaemonRequestError

            try:
                root = Path(
                    connection.session_request("GET")["data"]["location"]["directory"]
                )
            except DaemonRequestError as exc:
                if exc.status != 404:
                    raise
        with connection.startup_lease():
            validate_selection(connection, args, root)
            created = create_session(connection, args, root=root)
            connection.start()
        report = created.get("startup")
        if isinstance(report, dict):
            for warning in report.get("warnings", []):
                error_console.print(Text(f"Warning: {warning}", style="yellow"))
        images = tuple(resolve_image_path(value, root=root) for value in args.images)
        return run_headless_prompt(
            connection, prompt, image_paths=images, stdout=output, stderr=error
        )
    except (
        ValueError,
        OSError,
        httpx.HTTPError,
        DaemonUnavailable,
        DaemonIncompatible,
    ) as exc:
        print_error(error_console, str(exc))
        return 1
    finally:
        if connection is not None:
            connection.close()


def run_daemon_interactive(
    args: CLIArgs,
    *,
    input_func: Callable[..., str] = input,
    stdout: OutputStream | None = None,
    stderr: OutputStream | None = None,
    resume_id: str | None = None,
    picker: bool = False,
    relocate: Path | str | None = None,
) -> int:
    """Run basic or prompt-toolkit mode entirely through the daemon."""
    output = stdout or sys.stdout
    error = stderr or sys.stderr
    error_console = build_console(error)
    connection: SessionConnection | None = None
    try:
        if relocate is not None and resume_id is None and args.session is None:
            raise ValueError("--relocate requires an explicit session ID.")
        root = (
            require_workspace(args.root)
            if not (resume_id or args.session or picker)
            else Path(args.root).expanduser().resolve()
        )
        session_id = resume_id or args.session or new_session_id()
        connection = SessionConnection(
            session_id,
            warning=lambda text: print_error(error_console, f"Warning: {text}"),
            cwd=root,
        )
        if picker and resume_id is None and args.session is None:
            sessions = connection.request(
                "GET", "session", params={"directory": str(root), "limit": 50}
            ).get("data", [])
            if not sessions:
                raise ValueError("No sessions found for this workspace.")
            output_console = build_console(output)
            output_console.print("Sessions:")
            for index, item in enumerate(sessions, 1):
                output_console.print(
                    f"{index}. {item.get('title') or 'Untitled session'} ({item.get('id')})"
                )
            choice = input_func("Resume session: ").strip()
            try:
                session_id = sessions[int(choice) - 1]["id"]
            except (ValueError, IndexError, KeyError) as exc:
                raise ValueError("Invalid session selection.") from exc
            connection.session_id = session_id
        if relocate is not None:
            connection.session_request(
                "POST", "/relocate", json={"directory": str(Path(relocate).resolve())}
            )
        validate_selection(connection, args, root)
        if args.fork_session_id:
            validate_fork_selection(connection, args, root)
            existing = connection.request(
                "POST",
                f"session/{args.fork_session_id}/fork",
                json={
                    "id": connection.session_id,
                    "location": {"directory": str(root)},
                },
            )["data"]
        elif resume_id or args.session:
            try:
                existing = connection.session_request("GET")["data"]
                root = Path(existing["location"]["directory"])
            except DaemonRequestError as exc:
                if resume_id is not None or exc.status != 404:
                    raise
                existing = None
        else:
            existing = None
        connection.set_cwd(root)
        with connection.startup_lease():
            validate_selection(connection, args, root)
            created = create_session(connection, args, root=root)
            connection.start()
        args.root = str(root)
        data = created.get("data", {})
        backend = DaemonSessionBackend(connection, data)
        messages = backend.messages()
        if args.prompt:
            messages.append(Message.user(args.prompt))
        report = created.get("startup")
        if isinstance(report, dict):
            for warning in report.get("warnings", []):
                error_console.print(Text(f"Warning: {warning}", style="yellow"))
        from yoke.cli.interactive import run_interactive_cli

        return run_interactive_cli(
            args,
            backend,
            messages,
            active_session=backend.session,
            input_func=input_func,
            stdout=output,
            stderr=error,
            replay_session=resume_id is not None,
            replay_messages=messages if resume_id is not None else None,
        )
    except (
        ValueError,
        OSError,
        httpx.HTTPError,
        DaemonIncompatible,
        DaemonUnavailable,
    ) as exc:
        print_error(error_console, str(exc))
        return 1
    finally:
        if connection is not None:
            connection.close()


def run_daemon_cli(
    args: CLIArgs,
    *,
    input_func: Callable[..., str] = input,
    stdout: OutputStream | None = None,
    stderr: OutputStream | None = None,
) -> int:
    """Run the public Python CLI entry point through the daemon."""
    run = run_daemon_headless if args.headless else run_daemon_interactive
    return run(args, input_func=input_func, stdout=stdout, stderr=stderr)
