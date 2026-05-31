"""tool_decorators module."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any
from typing import cast
from typing import overload

from pydantic import create_model

from yoke.agent.tools import LocalTool


@overload
def class_tool[ToolClassT: type[LocalTool]](cls: ToolClassT, /) -> ToolClassT: ...


@overload
def class_tool[ToolClassT: type[LocalTool]](
    *, name: str | None = None, description: str | None = None
) -> Callable[[ToolClassT], ToolClassT]: ...


def class_tool[ToolClassT: type[LocalTool]](
    cls: ToolClassT | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
) -> ToolClassT | Callable[[ToolClassT], ToolClassT]:
    """class_tool."""

    def decorate(tool_cls: ToolClassT) -> ToolClassT:
        if not issubclass(tool_cls, LocalTool):
            raise TypeError("@class_tool can only decorate LocalTool subclasses.")
        typed_tool_cls = cast(type[LocalTool], tool_cls)
        if name is not None:
            typed_tool_cls.name = name
        if description is not None:
            typed_tool_cls.description = description
        typed_tool_cls.is_yoke_tool = True
        return tool_cls

    return decorate if cls is None else decorate(cls)


FunctionToolFunc = Callable[..., dict[str, object]]


def _callable_name(value: Callable[..., object]) -> str:
    """Return a stable display name for a callable."""
    name = getattr(value, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return value.__class__.__name__


@overload
def function_tool(func: FunctionToolFunc, /) -> type[LocalTool]: ...


@overload
def function_tool(
    *, name: str | None = None, description: str | None = None
) -> Callable[[FunctionToolFunc], type[LocalTool]]: ...


def function_tool(
    func: FunctionToolFunc | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
) -> type[LocalTool] | Callable[[FunctionToolFunc], type[LocalTool]]:
    """function_tool."""

    def decorate(tool_func: FunctionToolFunc) -> type[LocalTool]:
        signature = inspect.signature(tool_func)
        field_definitions: dict[str, tuple[object, object]] = {}
        parameter_names: list[str] = []
        for parameter in signature.parameters.values():
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }:
                raise TypeError(
                    "@function_tool only supports named parameters without "
                    "*args or **kwargs."
                )
            if parameter.annotation is inspect.Signature.empty:
                raise TypeError(
                    "@function_tool requires a type annotation for parameter "
                    f"{parameter.name!r}."
                )
            default: object = (
                ...
                if parameter.default is inspect.Signature.empty
                else parameter.default
            )
            field_definitions[parameter.name] = (parameter.annotation, default)
            parameter_names.append(parameter.name)

        function_name = _callable_name(tool_func)
        tool_name = name or function_name
        tool_description = description or inspect.getdoc(tool_func) or tool_name
        class_name = (
            "".join(part.capitalize() for part in tool_name.split("_")) + "Tool"
        )

        def execute(self: LocalTool) -> dict[str, object]:
            result = tool_func(
                **{param: getattr(self, param) for param in parameter_names}
            )
            if not isinstance(result, dict):
                raise TypeError(
                    f"@function_tool function {function_name!r} must "
                    "return dict[str, object]."
                )
            return result

        class _FunctionToolBase(LocalTool):
            name = tool_name
            description = tool_description

            def execute(self) -> dict[str, object]:
                return execute(self)

        tool_model = create_model(
            class_name,
            __base__=_FunctionToolBase,
            **cast(dict[str, Any], field_definitions),
        )
        typed_model = cast(Any, tool_model)
        typed_model.__module__ = tool_func.__module__
        typed_model.__doc__ = inspect.getdoc(tool_func)
        typed_model.is_yoke_tool = True
        return tool_model

    return decorate if func is None else decorate(func)
