"""Descriptors for composed actions, keeping the original direct tools available."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.types import Tool, ToolAnnotations
from pydantic import BaseModel

from yoke.mcp_server.execution.bridge import ComposePython
from yoke.mcp_server.execution.models import (
    BatchRead,
    DownstreamCall,
    Inspect,
    ResultEnvelope,
    ResultRead,
)
from yoke.mcp_server.execution.processes import ProcessCancel, ProcessRead
from yoke.mcp_server.recipes.patch import CheckPatch
from yoke.mcp_server.recipes.workspace import SearchThenRead, WorkspaceSnapshot
from yoke.mcp_server.results.contracts import OUTPUTS
from yoke.mcp_server.transfers.files import ExportFile, ImportFiles, WriteBinary


@dataclass(frozen=True)
class Action:
    model: type[BaseModel]
    description: str
    read_only: bool


ACTIONS = {
    "batch_read": Action(
        BatchRead,
        "Read several known files or run independent rg/fd searches in one bounded call. Returns ordered item IDs, individual failures and retained-result handles. No commands or nested batches.",
        True,
    ),
    "mcp_inspect": Action(
        Inspect,
        "Discover downstream tools. Filter by server, exact tools or queries; request complete schemas only for selected tools. Continue with next_cursor. Reuse schema_hash until the contract changes.",
        True,
    ),
    "mcp_call": Action(
        DownstreamCall,
        "Call an inspected downstream tool. Pass schema_hash to detect drift; fields selects top-level result fields. May read or write external services.",
        False,
    ),
    "exec_python": Action(
        ComposePython,
        "Run Python with the yoke_mcp helper library. tools.call reaches local reads; tools.mcp uses shared downstream clients. Declare exact downstream effects in managed_calls unless server policy grants a reviewed read. output.emit retains selected data. Long execution waits are remotely bounded; continue a returned process session with process_read.",
        False,
    ),
    "result_read": Action(
        ResultRead,
        "Read a retained result handle with a repeatable byte cursor or select top-level fields. Handles expire after 15 minutes and belong to this single-user service, not an individual chat.",
        True,
    ),
    "process_read": Action(
        ProcessRead,
        "Wait for and observe several process sessions without consuming output or writing stdin. Each call is bounded below the remote request deadline and returns earlier on output or completion. Repeat next_cursor while continue is true. Use process_io for input and process_cancel to terminate.",
        True,
    ),
    "process_cancel": Action(
        ProcessCancel,
        "Terminate a running process session and cancel its managed child operations. This controls running work.",
        False,
    ),
    "search_then_read": Action(
        SearchThenRead,
        "Version 1 recipe: search a pattern and read bounded windows around the first match in each matching file. Selection is mechanical and capped by max_files.",
        True,
    ),
    "workspace_snapshot": Action(
        WorkspaceSnapshot,
        "Version 1 recipe: collect Git status, AGENTS.md, known paths and optional search excerpts in one call.",
        True,
    ),
    "check_patch": Action(
        CheckPatch,
        "Version 1 execution recipe: apply an explicitly supplied patch only when all expected file hashes match, then run exact named argv checks. Returns patch outcome, check outcomes and diff. Not a transaction; checks may have side effects.",
        False,
    ),
    "import_files": Action(
        ImportFiles,
        "Download supplied ChatGPT files to matching destinations. Requires HTTPS download_url and file_id. Create-only by default; expected_sha256 explicitly permits replacing that version. Optional sha256 verifies bytes. No URL or file contents are echoed.",
        False,
    ),
    "write_binary_file": Action(
        WriteBinary,
        "Binary-transfer fallback for clients without file parameters. Send at most 2 MiB decoded bytes per chunk; continue using transfer_id and next_offset, then final=true. Matching retries are safe. At most 64 MiB per file. Create-only unless expected_sha256 is supplied.",
        False,
    ),
    "export_file": Action(
        ExportFile,
        "Export exact local bytes as bounded base64 pages for programmatic clients. Pin expected_sha256 on later pages. This does not automatically create a ChatGPT attachment; do not manually copy large base64 through the model.",
        True,
    ),
}


def descriptor(name: str, action: Action, defaults: dict[str, Any]) -> Tool:
    schema = action.model.model_json_schema(by_alias=True)
    for key, value in defaults.items():
        if key in schema.get("properties", {}):
            schema["properties"][key]["default"] = value
    return Tool(
        name=name,
        description=action.description,
        input_schema=schema,
        output_schema=OUTPUTS.get(name, ResultEnvelope).model_json_schema(),
        annotations=ToolAnnotations(
            read_only_hint=action.read_only,
            destructive_hint=not action.read_only,
            idempotent_hint=action.read_only,
            open_world_hint=name
            in {"mcp_call", "exec_python", "check_patch", "import_files"},
        ),
        _meta={"openai/fileParams": ["files"]} if name == "import_files" else None,
    )
