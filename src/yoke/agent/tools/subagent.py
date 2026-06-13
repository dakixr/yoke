from __future__ import annotations

# ruff: noqa: D100, D101, D103
from pathlib import Path
from typing import Literal
from typing import cast

from pydantic import BaseModel
from pydantic import Field

from yoke.agent.tools.attach_image import AttachImageTool
from yoke.agent.tools.base import WorkspaceTool
from yoke.agent.tools.document_extract import ExtractFileContextTool
from yoke.agent.tools.python_exec import PythonExecTool
from yoke.agent.tools.read import ReadTool
from yoke.agent.tools.rg import RipgrepTool
from yoke.agent.tools.web import WebFetchTool
from yoke.agent.tools.web import WebResearchTool
from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.ai.providers.base import Provider
from yoke.agent.tools.write import register_write_tool


class SubAgentResponse(BaseModel):
    success: bool = Field(
        description="Whether the sub-agent successfully completed the task"
    )
    response: str = Field(description="The response to the prompt")
    pointers: list[str] = Field(
        description=(
            "List of file-paths/URIs/other-resources that might be "
            "relevant to the response."
        )
    )


class SubagentTool(WorkspaceTool):
    is_yoke_tool = True
    name = "subagent"
    description = "Delegate research or implementation work to a nested agent."
    execute_in_process = True

    prompt: str = Field(min_length=1)
    agent_type: Literal["researcher", "worker"] = "researcher"
    root_dir: Path = Path(".")

    def execute(self) -> dict[str, object]:
        runtime_context = self.runtime_context
        provider = (
            runtime_context.provider
            if runtime_context is not None
            else self._context.get("provider")
        )
        if provider is None:
            return self._error("subagent requires a bound provider")

        read_only_tools = [
            RipgrepTool,
            AttachImageTool,
            ExtractFileContextTool,
            ReadTool,
            WebFetchTool,
            WebResearchTool,
        ]

        if self.agent_type == "researcher":
            tools = read_only_tools
            sys_prompt = (
                "Your task is to explore and gather information based on the "
                "given prompt. Be thorough and provide detailed responses. "
                "Use the available tools to gather information. State facts "
                "and avoid making assumptions or giving opinions. If you "
                "don't know the answer, say you don't know."
            )
            output_type = SubAgentResponse
        elif self.agent_type == "worker":
            from yoke.cli.config.runtime import DEFAULT_SYSTEM_PROMPT

            tools = [*read_only_tools, PythonExecTool]
            sys_prompt = DEFAULT_SYSTEM_PROMPT
            output_type = None
        else:
            return self._error(
                f"Unsupported agent_type: {self.agent_type}",
                agent_type=self.agent_type,
            )

        try:
            agent = Agent(
                provider=cast(Provider, provider),
                config=RunConfig(
                    root=self.root_dir,
                    tools=tools,
                    register_tools=register_write_tool,
                    max_iterations=1_000_000,
                    sys_prompt=sys_prompt,
                ),
            )
            result = agent.prompt(
                prompt=self.prompt,
                output_type=output_type,
            )
        except Exception as exc:
            return self._error(f"Subagent run failed: {exc}")

        if result.structured is None:
            return {"ok": True, "response": result.output}
        return {"ok": True, **result.structured.model_dump()}
