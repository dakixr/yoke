"""tools_app module."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated
from typing import cast

import typer
from rich.table import Table
from rich.text import Text

from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.config import build_tool_report
from yoke.cli.config import format_tool_discovery_message
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.tools.policy import PiConfig
from yoke.cli.tools.policy import ToolPolicy

DEFAULT_ROOT = Path.cwd().absolute()

tools_app = typer.Typer(help="Manage dynamically loaded tools.")


def print_tool_inventory_table(stream: OutputStream, report: ToolLoadReport) -> None:
    """print_tool_inventory_table."""
    console = build_console(stream)
    table = Table(title="Tool Inventory", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Location")
    active_names = {entry.tool.name for entry in report.active_tools}
    all_entries = sorted(
        [*report.active_tools, *report.denied_tools],
        key=lambda item: (
            item.tool.name,
            item.source_kind,
            str(item.source_path or ""),
        ),
    )
    for entry in all_entries:
        source = "builtin" if entry.source_path is None else str(entry.source_path)
        if entry.tool.name in active_names:
            status = "active"
        else:
            status = "disabled"
        status_text = Text(status, style="green" if status == "active" else "red")
        table.add_row(entry.tool.name, status_text, entry.source_kind, source)
    console.print(table)


TOOLS_INIT_TEMPLATE = '''from __future__ import annotations

from pydantic import Field

from yoke.agent.tools import WorkspaceTool
from yoke.cli.tools.decorators import class_tool, function_tool


@function_tool
def echo(text: str) -> dict[str, object]:
    """Return the provided text."""

    return {"ok": True, "text": text}


@class_tool
class CountLinesTool(WorkspaceTool):
    name = "count_lines"
    description = "Count lines in a UTF-8 file under the workspace root."

    path: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        try:
            resolved = self._resolve_path(self.path)
            self._ensure_text_file(resolved)
            line_count = len(resolved.read_text(encoding="utf-8").splitlines())
            return self._success(path=self.path, line_count=line_count)
        except Exception as exc:
            return self._error(str(exc), path=self.path)
'''


@tools_app.command("init")
def tools_init(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root where the .yoke plugin file should be created.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = DEFAULT_ROOT,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite the scaffold file if it exists."),
    ] = False,
) -> None:
    """tools_init."""
    target = root / ".yoke" / "tools" / "example_tools.py"
    if target.exists() and not force:
        typer.echo(f"Refusing to overwrite existing file: {target}")
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(TOOLS_INIT_TEMPLATE, encoding="utf-8")
    typer.echo(f"Created tool scaffold at {target}")


def _config_path(*, root: Path, global_scope: bool, repo_scope: bool) -> Path:
    if global_scope and repo_scope:
        typer.echo("Use either --global or --repo, not both.", err=True)
        raise typer.Exit(2)
    if global_scope:
        return Path.home() / ".yoke" / "config.json"
    return root / ".yoke" / "config.json"


def _load_config(path: Path) -> PiConfig:
    if not path.is_file():
        return PiConfig()
    try:
        return PiConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            "Could not update tool policy because "
            f"`{path}` is invalid. Fix or remove that file first. {exc}"
        ) from exc


def _write_tool_policy(path: Path, tool_name: str, policy: ToolPolicy) -> None:
    config = _load_config(path)
    tools = dict(config.tools)
    tools[tool_name] = policy
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            PiConfig(
                tools=tools,
                default_model=config.default_model,
            ).model_dump(mode="json", exclude_none=True),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _set_tool_policy(
    *,
    tool_name: str,
    policy: ToolPolicy,
    root: Path,
    global_scope: bool,
    repo_scope: bool,
) -> None:
    path = _config_path(root=root, global_scope=global_scope, repo_scope=repo_scope)
    try:
        _write_tool_policy(path, tool_name, policy)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Set {tool_name}={policy.value} in {path}")


@tools_app.command("activate")
def tools_activate(
    tool_name: Annotated[str, typer.Argument(help="Tool name or glob pattern.")],
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root whose .yoke/config.json should be updated.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = DEFAULT_ROOT,
    global_scope: Annotated[
        bool,
        typer.Option("--global", help="Update ~/.yoke/config.json."),
    ] = False,
    repo_scope: Annotated[
        bool,
        typer.Option("--repo", help="Update the repo .yoke/config.json."),
    ] = False,
) -> None:
    """tools_activate."""
    _set_tool_policy(
        tool_name=tool_name,
        policy=ToolPolicy.allow,
        root=root,
        global_scope=global_scope,
        repo_scope=repo_scope,
    )


@tools_app.command("deactivate")
def tools_deactivate(
    tool_name: Annotated[str, typer.Argument(help="Tool name or glob pattern.")],
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root whose .yoke/config.json should be updated.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = DEFAULT_ROOT,
    global_scope: Annotated[
        bool,
        typer.Option("--global", help="Update ~/.yoke/config.json."),
    ] = False,
    repo_scope: Annotated[
        bool,
        typer.Option("--repo", help="Update the repo .yoke/config.json."),
    ] = False,
) -> None:
    """tools_deactivate."""
    _set_tool_policy(
        tool_name=tool_name,
        policy=ToolPolicy.deny,
        root=root,
        global_scope=global_scope,
        repo_scope=repo_scope,
    )


@tools_app.command("list")
def tools_list(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root used for repo-local tool discovery.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = DEFAULT_ROOT,
) -> None:
    """tools_list."""
    console = build_console(cast(OutputStream, sys.stdout))
    try:
        report = build_tool_report(root=root)
    except ValueError as exc:
        console.print(Text(f"Tool loading failed: {exc}", style="red"))
        raise typer.Exit(1) from exc
    console.print(Text("Tool loading OK.", style="green"))
    console.print(format_tool_discovery_message(report))
    print_tool_inventory_table(cast(OutputStream, sys.stdout), report)
    if report.config_path is not None:
        console.print(f"Config: {report.config_path}")
    for pattern in report.unmatched_config_patterns:
        console.print(
            Text(
                f"Warning: tool rule did not match any loaded tool: {pattern}",
                style="yellow",
            )
        )
