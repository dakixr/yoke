# Phase 4 implementation proof

Base commit: `2829e85bb662e861e8fec97dffae2c6edd91ae52`.
Feature version: `3.26.0`, synchronized in `_version.py`, `pyproject.toml`, and
`uv.lock` using `uv lock -q`. The implementation commit SHA is in the handoff.
No push was requested or performed.

The implementation covers session-filtered SSE, every audited CLI event,
startup selection/skills/reports, independent client configurations and leases,
visible slot waiting with a default of 16, and memory-only client environments,
cwd and credential resolution. The detailed contract and event mapping are in
[`src/yoke/docs/daemon-client.md`](../../../src/yoke/docs/daemon-client.md).

## Verification limits and exact results

The required full suite ran once. It was not wholly green:

```text
FAILED tests/yoke/test_command_process_cursors.py::test_http_cursor_crosses_completion_without_replaying_output
1 failed, 2372 passed, 4 skipped in 347.29s (0:05:47)
```

The full raw output is [pytest-full.txt](pytest-full.txt). The failing existing
test injects a runtime exposing the original public `process_manager()` method.
The new multi-manager inspector initially reached through `resources`, which
that embedded runtime does not have. The implementation now uses the public
`process_managers()` seam when available and preserves the original injection
interface otherwise. No assertion in that test changed.

Post-full changes also retain the last environment after every attachment
leaves, without retaining the detached client's approval policy, and publish
`tool_execution_output_delta` for the prompt-toolkit tool inspector. The final
focused run covers those changes, the failed cursor test, all new API tests,
client policy tests, SDK process observation, every provider projection oracle,
and the request-byte contract:

```text
84 passed in 42.95s
```

See [pytest-final-targeted.txt](pytest-final-targeted.txt). The full suite was
not rerun after these fixes, respecting the requested one-full-run limit.

The final required static checks are preserved verbatim:

- [ruff check](ruff-check.txt): `All checks passed!`
- [ruff format](ruff-format.txt): formatter output for the final source tree.
- [ty check](ty.txt): `All checks passed!`
- [pyright](pyright.txt): `0 errors, 0 warnings, 0 informations`, followed by its
  installed-version update notice.

[capacity-before-full.txt](capacity-before-full.txt) records `pgrep -af pytest`
before the full launch. Only the launching shell matched; another checkout's
full run had already exited. [test-sandbox.txt](test-sandbox.txt) records the
outer temporary HOME/database/runtime root. The shared fixture supplies a new
HOME and session directory per test, clears ambient DB overrides, and sets a
temporary XDG runtime directory. Tests specifically exercising `YOKE_DB_PATH`
set their own temporary override. The real-database guard assertion is unchanged.

## Changed existing test assertions

Exactly two existing assertions changed, both in
`test_client_modes_gate_actual_file_writes`:

1. Reconfiguration while an approval is outstanding expects HTTP 200 instead
   of 409. Admitted work holds its original policy snapshot.
2. Detaching that client during the outstanding approval expects HTTP 200
   instead of 409. Detach removes its attachment and preserves admitted work.

The original assertions about permission requests, writes before approval,
allow/deny results, cleanup and client isolation remain. No existing assertion
was removed, weakened or replaced with an ignore. The process-cursor failure
was fixed in implementation code. `tests/conftest.py` gained isolation setup;
its existing assertions did not change.

The full-schema equality assertion remains unchanged. With explicit approval,
only the OpenAPI snapshot was regenerated, using the snapshot test's own
`json.dumps(payload, indent=2, sort_keys=True) + "\n"` serialization. No provider
projection or request-byte fixture changed. The diff command for those fixtures
returned exit 0 with no output; [provider-goldens-diff.txt](provider-goldens-diff.txt)
is that empty output.

## OpenAPI schema diff

[openapi-diff.txt](openapi-diff.txt) contains the structural comparison.
No path or existing property was removed.

- `GET /event`: optional `sessionID` query.
- `GET /session/{session_id}/client`: optional `owner` query.
- `POST /session`: retains `data`; adds optional `startup` in the response.
- `SessionCreateRequest`: optional `startup`, `headless`, `skills`, `client`.
- `ClientSessionRequest`: optional `env`, `cwd`, `kind`, default kind `acp`.
- `ClientMcpServer`: optional `envVars`.
- `PromptAdmissionRequest`: optional `clientID`.
- New response models: `SessionCreateResponse`, `SessionStartupReport`.

The browser's existing creation request and unfiltered event stream remain
valid. Additional live event names fall through the controller's recognized
handlers without throwing. CLI attachments use `kind: "cli"` to retain the
established provider tool set. Nine new differential scenarios compare that
attachment path against the established daemon projection without goldens.

## Live smoke

Command:

```sh
uv run python docs/qa/phase4/smoke.py
```

The [self-contained smoke script](smoke.py) starts `yoke serve --daemon` with a
temporary HOME, YOKE_DB_PATH and XDG_RUNTIME_DIR and `--port 0`. It uses `curl
--unix-socket` for every API call. Client environments are sent on stdin and
never saved as request files. [smoke-output.txt](smoke-output.txt) contains the
commands and their raw outputs, including both filtered SSE excerpts.

The run used real `codex` credentials available through `YOKE_CODEX_API_KEY`.
It used `/tmp/yoke-phase4-smoke-mexemzz6/runtime/yoke/daemon.sock`, daemon pid
2941056, and TCP port 38693. No production daemon or systemd unit was contacted.

Both streams observed alpha and beta's prompt admissions. A mutation to session
`unrelated` with `FILTER-NOISE-MUST-NOT-ARRIVE` was excluded. The script checks
both session IDs and the marker. Each real shell ran
`["bash", "-c", "echo $YOKE_SENTINEL_X"]`; public results contain
`"output":"<redacted>\n"`. The concurrency unit test separately verifies the
raw provider-side shell output and each client's conflicting fake API key.

After alpha detached and its stream closed, daemon status contained only beta's
lease. Beta completed a third prompt and tool run. The final real-provider
responses were also read from the disposable database after shutdown; see
[smoke-responses.txt](smoke-responses.txt).

The raw privacy proof at the end of the smoke output is:

```text
$ grep -aF -l -e phase4-private-alpha-7b9cb9 -e phase4-private-beta-09ff11 /tmp/yoke-phase4-smoke-mexemzz6/yoke.db /tmp/yoke-phase4-smoke-mexemzz6/provider-logs/codex-2026-11-16.jsonl /tmp/yoke-phase4-smoke-mexemzz6/daemon.log
grep exit=1 (1 means no sentinel in DB/WAL/logs)
Daemon exit=0
```

The daemon had checkpointed away its WAL by then. The concurrency test checks
DB/WAL files while the application is alive, in addition to history rows,
messages, tool traces and captured logs. New tests also exercise independent MCP
subprocess environments, Python virtualenv selection, dotenv non-mutation,
provider file-log sanitation and validation-error sanitation.

## Work remaining for Phase 5

The terminal CLI intentionally remains in process. Phase 5 connects headless,
basic and prompt-toolkit modes to these endpoints, translates SSE event names,
prints the returned startup report, and reattaches a fresh environment after a
daemon restart. There is no persisted credential recovery. The approved limit
for repository tools/provider plugins that directly read `os.environ` remains;
their module code still sees the daemon process. SDK orchestration APIs and
examples were not changed, so the built-in orchestration skill needed no update.

The final source and proof-bundle diff against the base is in
[diff-stat.txt](diff-stat.txt).
