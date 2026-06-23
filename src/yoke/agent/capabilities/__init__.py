"""Agent-owned capability selection and registration."""

from yoke.agent.capabilities.builtin import CommandExecutionCapability
from yoke.agent.capabilities.builtin import DEFAULT_CAPABILITIES
from yoke.agent.capabilities.builtin import FileEditCapability
from yoke.agent.capabilities.builtin import FileContextCapability
from yoke.agent.capabilities.builtin import FileReadCapability
from yoke.agent.capabilities.builtin import FileSearchCapability
from yoke.agent.capabilities.builtin import ImageGenerationCapability
from yoke.agent.capabilities.builtin import ImageInputCapability
from yoke.agent.capabilities.builtin import McpCapability
from yoke.agent.capabilities.builtin import WebCapability
from yoke.agent.capabilities.builtin import default_capabilities
from yoke.agent.capabilities.builtin import model_prefers_apply_patch
from yoke.agent.capabilities.builtin import research_capabilities
from yoke.agent.capabilities.builtin import resolve_builtin_capabilities
from yoke.agent.capabilities.builtin import worker_capabilities
from yoke.agent.capabilities.core import BaseCapability
from yoke.agent.capabilities.core import CapabilityContext
from yoke.agent.capabilities.core import CapabilityInput
from yoke.agent.capabilities.core import CapabilityRegistration
from yoke.agent.capabilities.core import CapabilityResolution
from yoke.agent.capabilities.core import CapabilityResolver
from yoke.agent.capabilities.core import instantiate_capabilities
from yoke.agent.capabilities.legacy import ExplicitToolsCapability
from yoke.agent.capabilities.legacy import RegisterToolsCapability

__all__ = [
    "BaseCapability",
    "CapabilityContext",
    "CapabilityInput",
    "CapabilityRegistration",
    "CapabilityResolution",
    "CapabilityResolver",
    "CommandExecutionCapability",
    "DEFAULT_CAPABILITIES",
    "ExplicitToolsCapability",
    "FileContextCapability",
    "FileEditCapability",
    "FileReadCapability",
    "FileSearchCapability",
    "ImageGenerationCapability",
    "ImageInputCapability",
    "McpCapability",
    "RegisterToolsCapability",
    "WebCapability",
    "default_capabilities",
    "instantiate_capabilities",
    "model_prefers_apply_patch",
    "research_capabilities",
    "resolve_builtin_capabilities",
    "worker_capabilities",
]
