"""Compatibility type import for callers migrating to RuntimeAgent.

The custom AgentRunner protocol has been removed. HTTP factory annotations
still import this name until their daemon API migration lands.
"""

from yoke.agent.loop.agent import RuntimeAgent as AgentRunner

__all__ = ["AgentRunner"]
