# Composed MCP work

This page is for people who drive `yoke-mcp` from ChatGPT or another MCP client
and want fewer round trips. It covers the batch, Python composition, result
handle, recipe and file-transfer tools, with their limits. Start with
[mcp-server.md](mcp-server.md) for setup, the full tool list and security.
None of these tools start a Yoke agent, and they do not change how Yoke's own
agents see MCP.

## Choose a tool

| Job | Tool |
| --- | --- |
| One read, search, command or patch | The direct tools |
| Several independent reads or searches | `batch_read` |
| Dependent reads, downstream calls or local filtering | `python_exec` with `yoke_mcp` |
| Discover downstream tools and schemas | `mcp_inspect` |
| Page through a large retained result | `result_read` |
| Wait on or page several running commands | `process_read` |
| Send terminal input | `process_input` |
| Stop a process and its managed calls | `process_cancel` |
| Search and fetch match windows | `search_then_read` |
| Git status, root instructions, known paths, optional search | `workspace_snapshot` |
| Apply a patch, then run exact checks | `check_patch` |
| Import files ChatGPT passes as file parameters | `import_files` |
| Upload bytes from another program | `write_binary_file` |
| Export exact bytes to another program | `export_file` |

## Independent reads

```json
{
  "items": [
    {"id": "instructions", "tool": "read_file", "arguments": {"path": "AGENTS.md"}},
    {"id": "matches", "tool": "rg", "arguments": {"patterns": ["needle"], "paths": ["src"], "limit": 50}}
  ],
  "max_concurrency": 4,
  "deadline_ms": 30000,
  "max_output_tokens": 8000
}
```

`batch_read` takes 1 to 16 `read_file`, `rg` or `fd` operations. It validates
the whole request before starting, rejects duplicate IDs, and returns results
in input order. One failed item, such as a missing file, does not discard the
others. It accepts no nested batches, shell, Python or downstream calls.

At most four items run at once, and they share the server's operation limit.
`max_output_tokens` accepts 512 to 16,000. The server converts it to bytes at
four ASCII JSON bytes per token, capped at 64,000 bytes. The budget must cover
1,024 bytes for the envelope plus, for each item, 512 bytes and the encoded
length of its ID. Each item gets its own share, so one noisy search cannot hide
another item's status. An item whose output does not fit becomes a retained
result handle. The response always keeps the run ID, item IDs and statuses, the
operation count and the elapsed time. The budget is a byte estimate, not a
count of the tokens the client will spend.

The deadline cancels unfinished items. Searches inside a batch ignore ripgrep
configuration and capture at most 4 MiB. Reads reject files over 4 MiB and
return text, source offset, total line count, a completeness flag and a
continuation offset.

## Python composition

`python_exec` makes a `yoke_mcp` module available in its fresh Python
subprocess. The subprocess has the account's normal OS permissions. It is not
a sandbox and is not read-only.

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

`tools.call` can run `read_file`, `rg`, `fd`, `skill` and `result_read`.
`tools.gather([...])` awaits independent calls. Results have `.status`,
`.data` and `.error`. A malformed request or a denied call raises an exception.
`output.emit(value)` retains the complete JSON value and prints a short receipt
with its `result_ref` and the selected output, so large intermediate values stay
in the subprocess instead of passing through the chat.

`tools.mcp(server, tool, arguments, schema_hash=...)` calls a downstream tool
through the server's existing client. Discover schemas with `mcp_inspect`
first. A downstream call from Python is allowed only through a reviewed
read-only wrapper, or when the outer `python_exec` request lists it exactly in
`managed_calls`:

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

The manifest is part of the action the client approves. It adds no separate
confirmation and grants nothing beyond that action. The script cannot add
entries or reuse an exhausted entry, and text it reads never grants new
capabilities. If a write depends on judgment made after reading, return the
evidence and make a separate write call.

Each execution gets one unguessable capability for the bridge, over a private
Unix socket, or loopback TCP on Windows. The capability expires with the Python
timeout and is revoked when the script finishes or is cancelled. Limits per
execution:

| Limit | Value |
| --- | --- |
| Bridge requests | 32 by default, configurable up to 64 |
| Total bridge traffic | 16 MiB |
| One request | 4 MiB |
| One reply | 8 MiB |

Python admission does not take a child operation slot, so a script cannot
starve its own calls. The server's process limits still apply. An alternate
interpreter must be able to import the installed `yoke` package to use the
helper.

`timeout` is the script's execution deadline in seconds. It is separate from
any wait. A `process_read` deadline leaves the script and its calls running.
`process_cancel` terminates the process tree, revokes the capability and
cancels managed calls, and keeps the final output.

## Discovery, results and media

`mcp_inspect` accepts `server`, exact `tools`, `query`, several `queries`,
`include_schemas`, `refresh`, `limit` and `cursor`. `refresh: true` fetches a
fresh downstream catalog. The `servers` array holds the page's tools with their
complete JSON schemas, including `$defs`, `$ref` and composition keywords. Each
tool has a `schema_hash`, any output schema and annotations, and an explicit
unknown-effects marker when the server gave no annotations. A catalog change
invalidates continuation cursors.

Pass `schema_hash` to `mcp_call` to reject a changed contract before dispatch.
A pinned call refreshes the downstream schema and validates the arguments under
the same server lease as the call. That costs one extra backend request and no
extra model turn. Waiting for a busy server does not block configuration
reloads.

An oversized result returns an unguessable handle, its expiry, its byte count
and a preview. `result_read` returns ASCII JSON ranges with `cursor` and
`limit`, or selected top-level `fields`. Keep the same `fields` while paging.
Handles expire after 15 minutes. The store holds at most 64 MiB and refuses new
results when full instead of evicting live handles.

Downstream PNG, JPEG, GIF and WebP image blocks are decoded, validated and
forwarded as native MCP images without re-encoding. MIME mismatches, oversized
images and decode limits fail with an error. The total image budget matches
`view_image`. Audio stays in its downstream form.

## Processes

MCP uses the shared [process tools](process-tools.md). The differences from
native Yoke:

- `process_read` waits at most the server's `--max-remote-wait-ms`, never more
  than 240,000 ms, instead of native Yoke's 3,600,000 ms.
- Execution's auto mode waits for the server's `--default-yield-ms`.
- There are no completion notices. Read explicitly.
- The reader decodes UTF-8 incrementally, so a chunk boundary never splits a
  character. Invalid bytes become replacement characters.

## Recipes

The three recipe tools report `recipe_version: 1`.

`search_then_read` returns the first match window from each of at most 16
files. It makes no claim about relevance.

`workspace_snapshot` returns the root's `AGENTS.md`, Git status, up to 16
explicit paths and optional match windows. Read any further instructions the
returned paths point to.

`check_patch` takes the exact patch, `expected_hashes` for every source and
destination it touches, and named `checks` with exact argv arrays. A null hash
means the path must not exist. The steps:

1. The server writes the file snapshot and check list to a private temporary
   job file and starts the managed Python runner.
2. Once the runner reports ready, the server checks the hashes again and
   applies the patch, holding the MCP patch lock.
3. The runner runs the checks in order and emits a final report with each
   check's outcome, the final diff and file hashes.

If the runner fails to start, the patch is not applied. If the patch fails,
no checks run. A failed or timed-out check skips the remaining checks. Nothing
is rolled back. The patch lock does not stop external editors, and a
multi-file patch is not a transaction. The response contains the runner's
process session. Follow it with `process_read`. A timeout can leave a check's
side effects unknown, so do not repeat writes automatically.

## File transfer

`import_files` declares `_meta["openai/fileParams"] = ["files"]`. Each item in
`files` has `download_url` and `file_id`, plus optional `mime_type` and
`file_name`. A matching `destinations` array gives each file's path, an
optional source SHA-256 and an optional `expected_sha256` precondition for
overwriting.

Downloads must be direct HTTPS URLs that resolve to public addresses. The
connection pins the validated address and still verifies the TLS hostname,
unless `YOKE_DISABLE_TLS` turns verification off for the whole process.
Redirects, URL credentials and proxy environment variables are refused. Each
download is capped at 64 MiB and must finish within 60 seconds. Signed URLs
never appear in results or logs. The server writes to a temporary sibling and
renames it into place. Creating is the default, and replacing a file requires
its expected digest. Each file reports its own outcome. Imports are not a
transaction. Whether ChatGPT supplies a download URL for a given file is up to
ChatGPT.

`write_binary_file` uploads from another program. Start at offset 0, keep the
returned `transfer_id`, and send each next chunk at `next_offset`. A repeated
identical chunk is accepted. Gaps and conflicting chunks fail. Finish with
`final: true`, preferably with `sha256`. Limits:

| Limit | Value |
| --- | --- |
| Decoded chunk | 2 MiB |
| File | 64 MiB |
| Staged transfers | 8 |
| Transfer handle lifetime | 15 minutes |

A failed final commit or digest check deletes the staged file and frees the
slot. Restart that upload from offset 0. Repeating a successful final chunk is
accepted. Normal shutdown removes staged files. A crash can leave hidden
`.yoke-upload-*` or `.yoke-import-*` files next to the destination.

`export_file` returns base64 pages with the SHA-256, size and `next_offset`.
Pass `expected_sha256` after the first page to detect changes. It is for
programs moving bytes, not for the model to reproduce base64, and it does not
create a ChatGPT attachment.

## Curated downstream wrappers

Set `YOKE_MCP_WRAPPERS_FILE` to a reviewed JSON list and restart the server.
Each entry has `name`, `server`, `tool`, `description`, `input_schema`,
optional `output_schema`, and `read_only`, which defaults to false. Names must
start with `downstream_`, and the file may hold at most 32 entries. The server
never exports downstream actions on its own. Output schemas keep their own
reference scope, including local `$defs`, anchors, recursive references and
nested IDs.

The declared input schema pins the downstream schema hash, so a changed
contract fails before execution. `read_only: true` is your assertion as the
server owner, and it also lets `tools.mcp` call that wrapper from Python
without a `managed_calls` entry. Review the downstream implementation before
setting it. `mcp_call` and arbitrary Python keep their conservative
annotations.
