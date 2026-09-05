# Composed MCP work

The ChatGPT-facing MCP server supports bounded batches, Python composition,
repeatable process observation, binary transfer, and versioned workspace
recipes. These tools do not start a Yoke agent. Yoke's agent SDK, provider
messages, skill instructions, and existing agent-side MCP projection are
unchanged.

## Choose a tool

| Job | Tool |
| --- | --- |
| One local read, search, command, or patch | Existing direct tools |
| Several independent local reads or searches | `batch_read` |
| Dependent reads, downstream calls, or local filtering | `exec_python` with `yoke_mcp` |
| Discover downstream contracts | `mcp_inspect` |
| Retrieve retained output | `result_read` |
| Observe several running commands | `process_read` |
| Write terminal input | `process_io` |
| Stop a process and its managed dispatch | `process_cancel` |
| Search and fetch match windows | `search_then_read` |
| Git status, root instructions, known paths, optional search | `workspace_snapshot` |
| Explicit patch followed by exact checks | `check_patch` |
| Import native ChatGPT file parameters | `import_files` |
| Upload bytes from another program | `write_binary_file` |
| Export exact byte pages to another program | `export_file` |

Existing tool names remain available. New results advertise output schemas and
use `structuredContent` with a short text acknowledgement. Set
`YOKE_MCP_LEGACY_RESULT_TEXT=true` for clients that also require the full JSON
serialized into text. This compatibility setting does not affect Yoke agents.
The MCP command descriptor advertises its effective non-login-shell default.

## Independent reads

```json
{
  "items": [
    {"id": "instructions", "tool": "read_file", "arguments": {"path": "AGENTS.md"}},
    {"id": "matches", "tool": "rg", "arguments": {"raw_args": "-n needle src"}}
  ],
  "max_concurrency": 4,
  "deadline_ms": 30000,
  "max_output_tokens": 8000
}
```

`batch_read` accepts 1–16 typed `read_file`, `rg`, or `fd` operations. It validates
the entire request before starting, rejects duplicate IDs and search execution
switches, and returns outcomes in input order. A missing file does not discard
successful siblings. No nested batch, shell, Python, or generic downstream call
is accepted in this tool.

The batch allows at most four concurrent operations and shares the service's
operation limit. Each item receives a separate output allowance, so a noisy
search cannot hide another item's status. The approximate token budget is four
ASCII JSON bytes per token, capped at 64,000 bytes. Requests must reserve 512
bytes per item, the actual ASCII JSON encoding of each ID, and 1,024 bytes
for the envelope. Oversized payloads become retained-result receipts within
individual items; the batch always preserves its run ID, item IDs and statuses,
operation count, and elapsed time. This is a byte heuristic, not
a measurement of tokens consumed by ChatGPT.

The deadline stops queued work and cancels unfinished items. Composed searches
disable ripgrep configuration hooks, poll cancellation, and cap captured data
at 4 MiB. Internal file reads reject files larger than 4 MiB and return clean
text, source offset, total line count, completeness, and continuation offset.
These limits do not change the direct agent `ReadTool`.

## Python composition

`exec_python` injects `yoke_mcp` into its fresh Python subprocess. Ordinary
Python and shell access retain their existing OS permissions. The helper is
not a sandbox and is not advertised as read-only.

```python
import asyncio
from yoke_mcp import tools, output

async def main():
    index = await tools.call("read_file", {"path": "index.txt"})
    if index.status != "ok":
        output.emit({"error": index.error})
        return
    result = await tools.call("read_file", {"path": index.data["content"].strip()})
    output.emit({"path": result.data["path"], "content": result.data["content"]})

asyncio.run(main())
```

`tools.call` permits local `read_file`, `rg`, `fd`, `skill`, and `result_read`.
`tools.gather([...])` awaits independent calls. Results expose `.status`,
`.data`, and `.error`; malformed requests or denied dispatch raise an exception.
`output.emit(value)` retains the complete JSON value and prints a compact
receipt containing `result_ref` and selected output. Large intermediate values
remain in the subprocess instead of passing through ChatGPT.

`tools.mcp(server, tool, arguments, schema_hash=...)` uses the parent's existing
downstream client. Discovery remains explicit. For downstream calls, either
configure a reviewed, schema-pinned read wrapper or include an exact
`managed_calls` manifest in the outer `exec_python` request:

```json
{
  "code": "...program using tools.mcp...",
  "managed_calls": [
    {
      "server": "example",
      "tool": "update_record",
      "arguments": {"id": "known-id", "value": "approved-value"},
      "schema_hash": "hash-returned-by-mcp_inspect",
      "max_calls": 1
    }
  ]
}
```

The manifest is part of the outer execution action visible to the client. It
does not create a separate confirmation flow or grant authorization beyond that
action. If a write's arguments depend on fresh judgment, return evidence and
make a separate explicit write call. The bridge cannot add new manifest entries
or retry an exhausted entry. Retrieved text never grants capabilities.

The private Unix socket has one unpredictable capability per execution. Tokens
expire with the Python timeout and are revoked on completion or cancellation.
Each execution has a default budget of 32 bridge requests, configurable up to
64, and a 16 MiB aggregate IPC byte budget. Individual IPC requests are capped
at 4 MiB and replies at 8 MiB. Python admission is separate from child operation
slots, avoiding parent/child slot starvation. Shared runtime process limits
still apply. The optional alternate Python interpreter must be able to import
the installed `yoke` package to use the helper.

## Discovery, results, and media

`mcp_inspect` accepts `server`, exact `tools`, `query`, multiple `queries`,
`include_schemas`, `refresh`, `limit`, and `cursor`. `refresh: true` requests
a fresh downstream catalog. The `servers` array contains the page's
tools. It preserves complete selected JSON schemas, including `$defs`, `$ref`,
and composition constraints. Each tool includes `schema_hash`, any advertised
output schema and annotations, and an explicit unknown-effects marker when
annotations are absent. A catalog change invalidates continuation cursors.

Use `schema_hash` on `mcp_call` to reject a changed contract before dispatch.
Pinned calls refresh downstream schemas and validate arguments under the same
server lease used for execution. This adds a backend catalog request, but no
extra model turn. Calls
to one server serialize; waiting for that server does not hold the global
configuration lock. Existing config hot reload and tool allowlists still apply.
The MCP server reads raw downstream results before its own output projection.
Agent-side discovery and result formatting retain their established contracts.

Oversized results receive an unguessable handle, expiry, byte count, and bounded
preview. `result_read` retrieves ASCII JSON ranges with `cursor` and `limit`, or
selects top-level `fields`. Keep the field selection unchanged while paging.
Handles expire after 15 minutes, and the store caps retained JSON at 64 MiB.
It rejects new retention when full instead of silently evicting live handles.

The present authentication model represents one owner. All authenticated
clients share that owner's handles and processes. A chat ID or process ID is
not an ownership credential. Restarting the service invalidates all ephemeral
handles. Multi-user and per-chat isolation are separate features.

Downstream PNG, JPEG, GIF, and WebP image blocks are decoded and validated, then
forwarded as native MCP images without re-encoding their bytes. MIME mismatches,
oversized images, and decode limits fail explicitly. The total compressed image
budget matches `view_image`. Audio remains in its downstream representation;
this release does not add an audio decoder or an in-chat HTML widget.

## Process observation and recipes

`process_read` accepts up to 16 `sessions`, each with `session_id`, `after_seq`,
and `offset`. Pass each returned `next_cursor` back unchanged. The cursor reads
retained log ranges without consuming terminal output. `wait_ms` is bounded to
30 seconds. `gap` and `truncated_before_seq` report evicted ranges, including
the reduced tail retained after a process completes. The MCP reader decodes
UTF-8 incrementally so pipe boundaries cannot split characters; invalid UTF-8
uses replacement characters. The agent reader is unchanged. Processes are ephemeral.

`search_then_read` selects the first match window from each of at most 16 files.
It does not claim semantic relevance. `workspace_snapshot` reads the root's
`AGENTS.md`, explicit paths, Git status, and optional match windows. Use the
returned paths to load any additional instructions relevant to the task.

`check_patch` takes the exact patch, `expected_hashes` for every touched source
and destination, and named `checks` with exact argv arrays. A null hash means a
path must not exist. Preconditions and patch application share the MCP patch
mutex. This does not lock external editors or make a multi-file transaction.
Patch failure prevents all checks; a failed or timed-out check skips later
checks. No rollback resets the working tree.

Snapshots and check specifications live in a private temporary job file, so
large source files do not expand command-line arguments. The managed Python
runner must acknowledge readiness before the patch is applied. File hashes
are checked again after startup. Failed startup leaves the patch unapplied;
failed patch application cancels the waiting runner. Temporary jobs are removed
after completion or cancellation.

Checks run in that managed job. The response contains its process session;
observe it with `process_read`. The child emits a final retained
report with individual check outcomes, final diff, and file hashes. A timeout
can leave check side effects unknown. Do not repeat writes automatically.
All three recipes report `recipe_version: 1`.

## File transfer

`import_files` advertises `_meta["openai/fileParams"] = ["files"]`. Each top-level
file-array item declares `download_url`, `file_id`, optional `mime_type`, and
optional `file_name`; only the first two are required. A matching `destinations`
array supplies explicit paths, optional source SHA-256 digests, and optional
`expected_sha256` overwrite preconditions.

Downloads require direct HTTPS URLs resolving to public addresses. The
connection pins a validated address while retaining TLS hostname verification.
Redirects, URL credentials, and environment proxies are disabled. Each download
has a 64 MiB cap, network timeouts, and a 60-second streaming deadline. Signed
URLs never appear in result messages or logs. Temporary sibling files are
committed atomically. Create-only is the default; replacing a file requires its
expected digest. Imports return individual outcomes and are not a transaction.

The schema and HTTP transport are tested. Whether ChatGPT supplies a download
URL for a particular generated or uploaded file remains a client capability;
the metadata cannot make an inaccessible file available.

`write_binary_file` is the programmatic fallback. Start at offset zero, retain
the returned `transfer_id`, and send subsequent chunks at `next_offset`.
Matching retries are accepted; gaps and conflicting retries fail. Finalize with
`final: true` and preferably `sha256`. Chunks contain at most 2 MiB decoded data,
files at most 64 MiB, and the service permits eight staged transfers. Handles
expire after 15 minutes; normal service shutdown cleans staged files. A failed
final commit or digest check removes its staged file and releases its slot.
Restart a failed finalized upload from offset zero; successful final retries
remain supported. A crash
can leave hidden `.yoke-upload-*` or `.yoke-import-*` sibling files for cleanup.

`export_file` returns bounded base64 pages, SHA-256, size, and `next_offset`.
Pin `expected_sha256` after the first page to detect edits. This is an API for
programmatic byte transport, not a request for the language model to reproduce
base64 or a promise of a ChatGPT attachment.

## Curated downstream wrappers

Set `YOKE_MCP_WRAPPERS_FILE` to a reviewed JSON list, then restart the MCP
service. Each entry supplies `name`, `server`, `tool`, `description`,
`input_schema`, optional `output_schema`, and `read_only`, defaulting to false.
Names must start with `downstream_`; at most 32 entries are accepted. No
downstream actions are exported automatically. Embedded output schemas keep
their own reference scope, including local `$defs`, anchors, recursive
references, and nested schema IDs.

The declared input schema pins the downstream schema hash. A changed contract
fails before execution. `read_only: true` is a server-owner policy assertion
and also enables that pinned operation through `tools.mcp` without an exact
manifest. Review the implementation and effects before setting it. The generic
gateway and arbitrary Python retain conservative execution annotations.

## Validation

Tests cover the expanded catalog, schemas, partial batches, result budgets,
retention expiry, repeatable process cursors, cancellation, dependent Python
reads with one operation slot, shared downstream clients, manifest rejection,
stale schemas and catalogs, binary retries, create-only commits, digest
failures, signed-URL redaction, and native images. Differential fixtures freeze
the prior agent MCP projection across text, skills, checkpoints, images,
resources, errors, and oversized results.

Measure complete tasks when evaluating performance. Count outer calls,
sequential model turns, backend operations, bytes, actual client-visible tokens,
latency distributions, success, and confirmation behavior separately. Six
independent reads in one envelope still perform six reads, and a client may
already execute direct calls in parallel.

A 20-run in-memory MCP SDK sample on Mooncake compared six parallel direct reads
with one batch over the same six small files:

| Mode | Outer calls | File reads | Median response bytes | p50 | p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Parallel direct | 6 | 6 | 5,964 | 9.848 ms | 147.256 ms |
| Batch | 1 | 6 | 3,402 | 4.402 ms | 60.438 ms |

These are local transport measurements, including initialization effects in the
sample. They do not measure ChatGPT latency or token consumption. Reproduce
with `uv run python scripts/benchmark_mcp_composition.py --repeats 20`.
