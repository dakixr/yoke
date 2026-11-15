# Provider projection oracle

These fixtures freeze the file-store behavior at `10437d6`, before the shared
daemon/SQLite migration. Every scenario runs through the CLI session helpers and
in-process `RuntimeAgent`, and through the actual FastAPI app with `TestClient`.
The same scripted provider and runtime configuration serve both paths, using
the real CLI base system prompt and public AGENTS instruction loader. No network
provider, private agent state, or hand-built conversation history is used.

Run the oracle:

```sh
uv run pytest -q -x -p no:cacheprovider tests/yoke/provider_projection
```

Regenerate only for an **explicit, justified provider-visible behavior change**:

```sh
YOKE_REGENERATE_PROJECTION_GOLDENS=1 uv run pytest -q -x -p no:cacheprovider tests/yoke/provider_projection
```

Review every changed request and explain the behavior change in the commit/PR.
Storage or transport refactoring alone never justifies changing these goldens.
Regeneration defaults off and cannot bypass the live-versus-resumed comparison.

## Coverage

Each of these 16 scenarios has an `in_process` and `daemon` fixture:

- `chat`: multiple turns, Unicode, punctuation, repeated spaces and newlines.
- `tools`: real rg/read/write/command execution, sequential model iterations.
- `parallel_tools`: the same four tools requested together in one iteration.
- `inline_skill`: `$oracle`, then `$review`, with real skill files.
- `slash_skill`: CLI `/skill` and the corresponding HTTP activation route.
- `images_supported`: CLI-embedded PNG input and an assistant image response.
- `images_unsupported`: text-model projection, plus HTTP's new-image rejection.
- `local_images_supported`: SDK local-image content parts, HTTP upload equivalent.
- `local_images_unsupported`: text projection of SDK local-image parts, HTTP equivalent.
- `manual_compaction`: compaction request and next-turn handoff.
- `automatic_compaction`: threshold crossing and subsequent handoff continuation.
- `overflow_retry`: provider overflow, summary, reset retry and continuation.
- `checkpoint_stop`: durable tool result, interruption during the next model call,
  then continuation without the cancelled response entering history.
- `tree_navigation`: two branches, switch to the other leaf with a branch summary.
- `fork`: persist, fork, continue in the fork.
- `title`: explicit title generation, including its isolated continuity.

After **every** scenario, copy the saved files, record a live continuation, close
the runtime/app, restore those files, build a fresh runtime/app, and submit the
identical continuation. Both projections must match byte-for-byte. The fixture
also stores both continuations, preventing identical live/resume regressions from
passing. Message JSON strings come directly from `Message.model_dump_json()`;
they are never decoded and re-encoded for comparison. Tool definitions, response
JSON, errors, model/image capabilities, cache scope and continuity are recorded.

## Normalization, exhaustively

- The exact temporary workspace prefix becomes `<WORKSPACE>`. The installation
  path embedded in the CLI system prompt becomes `<HARNESS_ROOT>`, and its
  import-time global configuration directory becomes `<GLOBAL_YOKE_DIR>`.
- Random persisted entry IDs become `<ENTRY:ordinal:kind>`, or
  `<FORK_ENTRY:ordinal:kind>` for new identities first seen in a fork. Shared IDs
  keep their original labels, so unrelated parent/fork scopes cannot alias. Only IDs actually read
  from the store are registered. Cache identity relationships remain visible.
- The generated fork session ID becomes `<FORK_SESSION>`.
- An unpersisted 16-hex title-request scope becomes `<TRANSIENT_TITLE_SCOPE:n>`.
  Only title calls qualify; regular unrecognized cache scopes remain literal.
- In command tool results only, random process IDs become consecutive integers.
  The same mapping replaces the first eight bytes of their base64 `pc1_` cursors;
  the remaining sequence/offset bytes and cursor prefix are preserved.

Nothing else is normalized. Timestamps are absent from these provider-bound
requests. Message text, order, whitespace, serialization, tool arguments/results,
usage, image data, cache-scope selection and continuity remain checked.

## Existing path differences

- In-process manual compaction retains runtime state, so its next call uses
  `response_continuity=reset`. HTTP reloads state and sends `continue`.
- In-process title generation reuses the persisted conversation scope. HTTP
  constructs a separate title runtime with a temporary scope. Both use `isolated`.
- CLI navigation generates a `branch_summary` provider call with no request
  context. HTTP accepts the summary from the caller, with no such provider call.
- CLI image input is embedded as an image URL with no `attachment_name`; HTTP
  includes `attachment_name=tiny.png`. Both send the exact same PNG data. The
  additional SDK local-image scenarios preserve actual `local_image` parts on
  the in-process path; HTTP accepts their equivalent through uploads.
- HTTP rejects a new image for a text model before persistence/provider execution.
  In-process projects it to omission text. To also exercise historical omission
  through HTTP, accept the image with the vision model, switch via `/selection`
  to the text model, then continue. The rejection and capability change are frozen.

The in-process driver uses the supported stateful SDK/CLI execution helpers. It
does not exercise terminal rendering, startup discovery, or interactive turn
isolation. Titles are explicit to keep background scheduling out of call order.
These differences are separate expectations, not normalized into false parity.
