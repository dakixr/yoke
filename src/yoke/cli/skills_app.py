"""skills_app module."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated
from typing import cast

import typer
from rich.table import Table

from yoke.agent.skills import load_skill_registry
from yoke.cli.config import default_cli_skill_dirs
from yoke.cli.path_display import format_root_label
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console

CWD_PATH = Path.cwd()
skills_app = typer.Typer(
    help=(
        "Manage skills. The CLI discovers built-in skills from the yoke "
        "codebase plus ~/.yoke/skills and <repo>/.yoke/skills by default."
    )
)

SKILL_TEMPLATE = """---
name: {name}
description: TODO describe when this skill should be used.
---

# {title}

Add the reusable instructions for this skill here.
"""


def _resolve_cli_skill_dirs(root: Path) -> list[str]:
    return default_cli_skill_dirs(root)


@skills_app.command(
    "list",
    help=("List discovered skills from built-in and default CLI skill directories."),
)
def skills_list(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root used for repo-local skill discovery.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = CWD_PATH,
) -> None:
    """skills_list."""
    try:
        registry = load_skill_registry(_resolve_cli_skill_dirs(root))
    except ValueError as exc:
        typer.echo(f"Skill loading failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    console = build_console(cast(OutputStream, sys.stdout))
    table = Table(title="Skills", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Path")
    for skill in sorted(registry.skills, key=lambda item: item.name):
        table.add_row(
            skill.name,
            skill.description,
            format_root_label(skill.skill_md_path),
        )
    console.print(table)


@skills_app.command("show", help="Show the full contents of a discovered skill.")
def skills_show(
    name: Annotated[str, typer.Argument(help="Skill name.")],
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root used for repo-local skill discovery.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = CWD_PATH,
) -> None:
    """skills_show."""
    try:
        registry = load_skill_registry(_resolve_cli_skill_dirs(root))
        skill = registry.require(name)
        typer.echo(skill.load_content())
    except (KeyError, ValueError) as exc:
        typer.echo(f"Skill loading failed: {exc}", err=True)
        raise typer.Exit(1) from exc


@skills_app.command(
    "init",
    help="Create a new skill scaffold under .yoke/skills/<name>/SKILL.md.",
)
def skills_init(
    name: Annotated[str, typer.Argument(help="Skill name.")],
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root whose .yoke/skills directory should be used.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = CWD_PATH,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite SKILL.md if it already exists."),
    ] = False,
) -> None:
    """skills_init."""
    target_dir = root / ".yoke" / "skills" / name
    target = target_dir / "SKILL.md"
    if target.exists() and not force:
        typer.echo(f"Refusing to overwrite existing file: {format_root_label(target)}")
        raise typer.Exit(1)
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(
        SKILL_TEMPLATE.format(
            name=name,
            title=name.replace("-", " ").title(),
        ),
        encoding="utf-8",
    )
    typer.echo(f"Created skill scaffold at {format_root_label(target)}")
