"""Local tool classes for agent tool execution."""

from __future__ import annotations

import os
import subprocess
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Iterable
from fnmatch import fnmatch
from pathlib import Path
from typing import ClassVar
from typing import cast

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import PrivateAttr

from yoke.agent.models import AgentContext
from yoke.agent.models import Message
from yoke.agent.tools.context import ToolRuntimeContext

DEFAULT_GLOB = "*"


class LocalTool(BaseModel, ABC):
    """Abstract base class for locally executable agent tools."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: ClassVar[str]
    description: ClassVar[str]
    is_yoke_tool: ClassVar[bool] = False
    execute_in_process: ClassVar[bool] = False
    _context: dict[str, object] = PrivateAttr(default_factory=dict)

    @classmethod
    def bind(cls, **context: object) -> LocalTool:
        """Create a bound tool instance with the given execution context."""
        tool = cls.model_construct()
        tool._bind_context(**context)
        return tool

    def _bind_context(self, **context: object) -> None:
        self._context = dict(context)

    def _inherit_context(self, prototype: LocalTool) -> None:
        self._bind_context(**prototype._context)

    @property
    def context(self) -> ToolRuntimeContext:
        """Return the public runtime context bound by an agent."""
        context = self.runtime_context
        if context is None:
            raise RuntimeError("Tool is not bound to an agent runtime context")
        return context

    @property
    def runtime_context(self) -> ToolRuntimeContext | None:
        """Return the runtime context, or None for a standalone bound tool."""
        context = self._context.get("runtime_context")
        return context if isinstance(context, ToolRuntimeContext) else None

    def bind_runtime_context(self, context: ToolRuntimeContext) -> None:
        """Bind the current public runtime context to this tool."""
        self._context["runtime_context"] = context
        self._context["provider"] = context.provider
        self._context["provider_name"] = context.provider_name
        self._context["model_id"] = context.model_id
        self._context["model_name"] = context.model_name
        self._context["model_key"] = context.model_key
        self._context["reasoning_effort"] = context.reasoning_effort
        self._context["root"] = context.root
        self._context["home"] = context.home
        self._context["cancel_requested"] = context.cancel_requested
        self._context["tool_event"] = context.tool_event
        self._context["recent_messages"] = context.recent_messages

    def _is_cancel_requested(self) -> bool:
        runtime_context = self._context.get("runtime_context")
        callback = (
            runtime_context.cancel_requested
            if isinstance(runtime_context, ToolRuntimeContext)
            else self._context.get("cancel_requested")
        )
        if not callable(callback):
            return False
        callback_fn = cast(Callable[[], object], callback)
        return bool(callback_fn())

    def _emit_tool_event(self, event: str, payload: dict[str, object]) -> None:
        runtime_context = self._context.get("runtime_context")
        callback = self._context.get("tool_event")
        if isinstance(runtime_context, ToolRuntimeContext):
            callback = runtime_context.tool_event or callback
        if not callable(callback):
            return
        callback_fn = cast(Callable[[str, dict[str, object]], object], callback)
        callback_fn(event, payload)

    def to_definition(self) -> dict[str, object]:
        """Return the tool definition dict for the provider API."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.__class__.model_json_schema(by_alias=True),
            },
        }

    def parse_arguments(self, arguments: dict[str, object]) -> LocalTool:
        """Parse the given arguments dict and return a bound tool instance."""
        parsed = self.__class__.model_validate(arguments)
        parsed._inherit_context(self)
        return parsed

    @abstractmethod
    def execute(self) -> dict[str, object]:
        """Execute the tool and return the result dict."""
        raise NotImplementedError

    def apply_result(
        self,
        context: AgentContext,
        result: dict[str, object],
    ) -> None:
        """Apply any side effects of the tool result to the agent context."""
        return None

    def pending_context_messages(
        self,
        result: dict[str, object],
    ) -> list[Message]:
        """Return extra messages to append after the tool result, if any."""
        del result
        return []

    def owned_resources(self) -> tuple[object, ...]:
        """Return closeable resources whose lifetime is owned by this tool."""
        return ()


class WorkspaceTool(LocalTool):
    """A tool that resolves relative paths from a workspace directory."""

    _root: Path = PrivateAttr()

    def _bind_context(self, **context: object) -> None:
        root = context.get("root")
        if root is None:
            raise ValueError("Workspace root is required")
        if isinstance(root, Path):
            candidate = root.resolve()
        elif isinstance(root, str):
            candidate = Path(root).resolve()
        elif isinstance(root, os.PathLike):
            resolved_root = os.fspath(root)
            if not isinstance(resolved_root, str):
                raise ValueError("Workspace root must be a string path")
            candidate = Path(resolved_root).resolve()
        else:
            raise ValueError("Workspace root must be path-like")
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(
                f"Workspace root does not exist or is not a directory: {root}"
            )
        self._context = dict(context)
        self._context["root"] = candidate
        self._root = candidate

    @property
    def root(self) -> Path:
        """Return the resolved workspace root path."""
        return self._root

    def _resolve_path(
        self, raw_path_value: str, *, allow_missing: bool = False
    ) -> Path:
        if not raw_path_value.strip():
            raise ValueError("Path must be a non-empty path")
        raw_path = Path(raw_path_value)
        candidate = (
            raw_path.resolve()
            if raw_path.is_absolute()
            else (self.root / raw_path).resolve()
        )
        if not allow_missing and not candidate.exists():
            raise FileNotFoundError(raw_path_value)
        return candidate

    def _ensure_text_file(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(path)
        if not path.is_file():
            raise ValueError(f"Path is not a regular file: {self._display_path(path)}")

    def _success(self, **payload: object) -> dict[str, object]:
        return {"ok": True, **payload}

    def _error(self, error: str, **payload: object) -> dict[str, object]:
        return {"ok": False, "error": error, **payload}

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def _read_existing_text(self, path: Path) -> str | None:
        if not path.exists() or not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

    def _walk(self, path: Path) -> Iterable[Path]:
        if path.is_file():
            yield path
            return
        yield path
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(entry for entry in dirs if entry != ".git")
            current_root = Path(root)
            for name in sorted(files):
                yield current_root / name
            for name in dirs:
                yield current_root / name

    def _iter_files(self, path: Path, *, glob: str = DEFAULT_GLOB) -> Iterable[Path]:
        if path.is_file():
            if fnmatch(path.name, glob):
                yield path
            return

        git_candidates = self._iter_git_files(path, glob=glob)
        if git_candidates is not None:
            yield from git_candidates
            return

        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(entry for entry in dirs if entry != ".git")
            current_root = Path(root)
            for name in sorted(files):
                if fnmatch(name, glob):
                    yield current_root / name

    def _iter_git_files(self, path: Path, *, glob: str) -> Iterable[Path] | None:
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        try:
            completed = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "git",
                    "-C",
                    str(self.root),
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--full-name",
                    "--",
                    self._git_pathspec(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None

        resolved_path = path.resolve()
        seen: set[Path] = set()
        candidates: list[Path] = []
        for line in completed.stdout.splitlines():
            candidate = (self.root / line).resolve()
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                candidate.relative_to(resolved_path)
            except ValueError:
                if candidate != resolved_path:
                    continue
            if candidate not in seen and fnmatch(candidate.name, glob):
                seen.add(candidate)
                candidates.append(candidate)
        return candidates

    def _git_pathspec(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        if not relative.parts:
            return "."
        return str(relative)
