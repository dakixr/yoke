"""Provider-aware capability registry for yoke agents."""

from yoke.agent.capabilities.base import BaseCapability
from yoke.agent.capabilities.base import CapabilityRegistration
from yoke.agent.capabilities.builtins import FileWriteCapability
from yoke.agent.capabilities.builtins import ToolClassCapability
from yoke.agent.capabilities.builtins import bind_tool_class
from yoke.agent.capabilities.builtins import builtin_capabilities
from yoke.agent.capabilities.builtins import create_builtin_capabilities
from yoke.agent.capabilities.builtins import create_builtin_tool_entries
from yoke.agent.capabilities.builtins import (
    known_builtin_capability_ids,
)
from yoke.agent.capabilities.builtins import model_prefers_apply_patch
from yoke.agent.capabilities.builtins import resolve_builtin_capability

__all__ = [
    "BaseCapability",
    "CapabilityRegistration",
    "FileWriteCapability",
    "ToolClassCapability",
    "bind_tool_class",
    "builtin_capabilities",
    "create_builtin_capabilities",
    "create_builtin_tool_entries",
    "known_builtin_capability_ids",
    "model_prefers_apply_patch",
    "resolve_builtin_capability",
]
