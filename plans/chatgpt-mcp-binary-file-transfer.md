# ChatGPT MCP binary file transfer

Implemented in the MCP composition release. The shipped interface uses opaque
transfer IDs, create-only defaults, and explicit overwrite hashes rather than
the tentative truncate flag below. Native file-parameter imports are also
available. See `src/yoke/docs/mcp-composition.md` for the current contract.

## User problem

From a ChatGPT conversation, a generated image can exist as a real file in the
ChatGPT runtime, but the Yoke MCP surface has no direct way to copy those bytes
onto the remote host. The current MCP tools can read UTF-8 text, patch text
files, search, and run commands on the host, but none accepts binary bytes from
the MCP client.

The concrete failure mode is simple: ChatGPT generated an image and the user
asked to save the existing image to a directory on the remote host. The image
must not be regenerated or re-encoded. ChatGPT can access the source file, and
the MCP server can access its own filesystem, but there is no byte-transfer
primitive between those two sides.

This is specifically a **ChatGPT -> Yoke MCP -> remote host** problem. It should be
solved in the ChatGPT-facing MCP server, not by changing the interactive Yoke
CLI tool policy.

## Source provenance

This plan was derived from the deployed Yoke `0.22.4` MCP snapshot and then
merged into the normal Git source tree. Future implementation should happen in
the repository's `src/yoke/mcp_server/` package. Do not implement the feature
only in a deployed release artifact.

## Relevant MCP code

The deployed MCP implementation already has the right extension seams:

- `src/yoke/mcp_server/registry.py` is the explicit external allowlist. It maps
  stable ChatGPT-visible names such as `read_file`, `apply_patch`, and
  `exec_command` to Yoke `LocalTool` classes and MCP annotations.
- `src/yoke/mcp_server/adapter.py` derives each MCP input schema directly from
  `tool_class.model_json_schema(by_alias=True)`, binds the configured root, parses
  arguments with Pydantic, executes the tool, and returns structured MCP output.
  A new `LocalTool` therefore gets a proper ChatGPT MCP schema without adding a
  parallel hand-written schema layer.
- `src/yoke/mcp_server/process_runtime.py` already runs ordinary tool execution
  off the event loop and has narrow coordination for mutations such as
  `apply_patch`.
- `src/yoke/mcp_server/config.py` currently defaults
  `max_request_body_size` to `4 * 1024 * 1024`, and `server.py` passes that limit
  into the Streamable HTTP MCP application. This matters because base64 expands
  binary payloads by roughly one third.
- `src/yoke/agent/tools/image_generation.py` already demonstrates the local
  conventions we need: strict `base64.b64decode(..., validate=True)`, temporary
  files, and `os.replace(...)` for committing generated image bytes.
- `tests/yoke/mcp_server/test_contract.py` asserts the exact MCP tool allowlist,
  schemas, annotations, recoverable validation failures, and Yoke path semantics.
  It is the natural contract-test location for this feature.

## Proposed solution

Add a dedicated MCP binary-ingress tool, tentatively named
`write_binary_file`. Keep it MCP-specific instead of broadening the existing
UTF-8 `WriteTool` or `apply_patch`, because those tools intentionally operate on
text and this feature exists to move opaque bytes from a remote MCP client onto
the server.

### Tool shape

Implement an MCP-local `WorkspaceTool` in a focused module such as
`src/yoke/mcp_server/files.py` with an input contract along these lines:

```text
path: str
data_base64: str
offset: int = 0
truncate: bool = False
final: bool = True
sha256: str | None = None
```

Semantics:

1. Resolve `path` using normal Yoke path semantics so relative paths use the MCP
   root and ordinary absolute paths continue to work consistently with the
   existing MCP file tools.
2. Decode `data_base64` with `base64.b64decode(..., validate=True)`. Invalid
   base64 is a normal `{ok: false, error: ...}` tool result.
3. For a one-call upload, `truncate=true`, `offset=0`, and `final=true` writes the
   complete file. Use a sibling temporary file and `os.replace` so the destination
   appears atomically.
4. For larger files, allow multiple calls using explicit byte offsets. An
   offset-based protocol is retry-safe: retrying a chunk rewrites the same range
   instead of accidentally appending it twice. Keep an incomplete transfer in a
   deterministic sibling temporary file and only replace the requested destination
   when `final=true`.
5. If `sha256` is supplied on the final call, hash the completed temporary file
   and refuse to commit it when the digest does not match. This gives ChatGPT a
   byte-for-byte integrity check for generated images and other opaque files.
6. Return compact metadata only, for example `path`, `bytes_written`,
   `next_offset`, `size`, and final `sha256`. Never echo the base64 payload in the
   tool result or logs.

### Chunk size

The MCP transport currently caps a complete request body at 4 MiB. A 2 MiB raw
chunk becomes about 2.67 MiB of base64, leaving comfortable room for JSON-RPC
and argument overhead. The tool should reject decoded chunks larger than a
conservative constant such as 2 MiB and describe that limit in its schema/tool
description.

Small generated images can remain a single call when their encoded request fits
comfortably below the transport ceiling. Chunking is still the safer general
protocol for arbitrary binary files.

### MCP registration

Add `write_binary_file` to `TOOL_REGISTRY` in
`src/yoke/mcp_server/registry.py` with a mutation annotation. The registry is the
intentional ChatGPT-facing allowlist, so registration there is what makes the
tool discoverable through the Yoke MCP connector.

Do not add this to the CLI's model-aware `file.write` capability solely for this
use case. The desired consumer is the remote MCP client. A CLI/SDK binary-write
capability can be considered separately if there is an independent need for it.

### Concurrency and cleanup

Treat transfer writes as mutations in `ProcessRuntime`. Either serialize
`write_binary_file` with a dedicated upload lock or use per-destination locks so
two MCP calls cannot corrupt the same transfer. Per-destination locking is the
better long-term behavior if parallel uploads are expected.

Temporary transfer files should use a recognizable hidden suffix and be removed
after successful commit or terminal validation failure. A later cleanup policy
can remove abandoned stale transfer files after server restarts.

## Tests

Extend the MCP tests rather than CLI tests:

1. Update `tests/yoke/mcp_server/test_contract.py` to include
   `write_binary_file` in the explicit allowlist and assert mutation annotations
   and the generated Pydantic schema.
2. Upload arbitrary non-UTF-8 bytes and assert the destination matches exactly.
3. Test an absolute destination path as well as a path relative to the MCP root.
4. Test invalid base64 as a recoverable tool error.
5. Test overwrite/truncate behavior.
6. Test two or more offset chunks and retry one chunk to prove the operation is
   idempotent at a fixed offset.
7. Test SHA-256 success and mismatch, including that a mismatch does not replace
   the destination.
8. Test the maximum chunk limit so a tool call stays safely under the MCP
   transport's 4 MiB request-body ceiling.
9. Test concurrent writes to the same destination if per-path locking is added.

## Expected ChatGPT flow after implementation

For the original user request, ChatGPT can then:

1. Use the already-generated image file from its local runtime.
2. Read the image bytes locally and compute SHA-256.
3. Base64-encode one chunk at a time without modifying the image.
4. Call the connector's `write_binary_file` tool with the requested destination.
5. Finalize with the expected SHA-256.
6. Report the remote path only after the MCP tool confirms the final digest.

No image regeneration, lossy conversion, public upload service, or temporary
third-party storage is needed.
