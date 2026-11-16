# SQLite storage QA

Baseline: `83a7992 merge: integrate daemon plumbing`. Branch:
`t3/fix-sqlite-storage-qa`. No version bump or push. The daemon, ACP, HTTP app
lifecycle and CLI daemon commands are outside this pass.

All runtime data used temporary homes or database paths. Original scratch
repros ran from temporary directories using this worktree's `.venv/bin/python -I`.
The additional probes ran against both an archived copy of HEAD's `src` and the
fixed source. The final regression files against original HEAD produced:

```text
20 failed, 1 passed in 2.77s
```

The passing case was incremental vacuum, already implemented in Phase 2.
The fixed code passed all 21 cases before the final validation runs.

The status column describes the baseline. Every STILL-BROKEN finding below is
fixed by this branch.

| Item | Baseline status | Before / after evidence |
| --- | --- | --- |
| #2 retention race, `r2_prune_toctou.py` | FIXED-BY-P2 | Both runs: `prune returned [] after 1.5s`, `pinned+resumed committed`, `session exists after prune: True`. Selection and deletion already share a write transaction. |
| #3 revision reuse, `r1_rev_reuse_cache.py` | FIXED-BY-P2 | Both runs: `cache entries: 10 db entries: 10`, `cache ids == db ids: True`, `first cached message: NEW-1`. `SessionSnapshots` retains no cache; `read_delta` is gone. A source search found no remaining revision-based delta reader. |
| #5 surrogate TEXT, `r5_surrogates.py` | STILL-BROKEN | Before: workspace and title each raise `UnicodeEncodeError`. After: `non-UTF-8 workspace -> saved OK`, `lone surrogate in title -> saved OK`. Message content remains `'x � y'`. Session metadata and indexed entry fields now share the JSON codec's normalization, including valid surrogate pairs. |
| #6 CLI save guards, `r7_disk_full_not_oserror.py` plus CLI probes | STILL-BROKEN | Raw storage still raises SQLite errors. Original scratch: `escapes except OSError: sqlite3 OperationalError disk I/O error`, integrity `ok`, entries `2`. CLI probe before: `CLI full: escaped OperationalError database or disk is full`. After: `Warning: failed to persist yoke session disk-full: database or disk is full`, `CLI full: returned normally; memory intact: True`. Both CLI guards catch `sqlite3.Error`; actual locked/full writes also verify a successful retry. |
| Pooled read snapshot, `r3_thread_fds.py` | STILL-BROKEN | Before: `same pooled thread sees entries: 2 (db has 4)`. After: `same pooled thread sees entries: 4 (db has 4)`. Streaming reads own separate connections. Their snapshot remains consistent while later short reads see new commits. Closing/exhausting the iterator permits checkpoint `(0, 0, 0)`. |
| Short-lived-thread FD leak hypothesis | DISMISSED | Original `r3`: FDs `10` to `50` after 300 threads. Scale probe: `10`, `43`, `52`, `55` after successively 0, 100, 500 and 1,500 additional threads. Only two live Python SQLite connections remain after GC. SQLite retains database descriptors while shared connections stay open; the probe does not show a per-thread connection leak. The scale script's final FD listing races its own closed directory FD and raises `FileNotFoundError`. |
| Archive versus admission | STILL-BROKEN | Before: `archive: accepted`, `archived: True pending: 1`. After: `archive: session_has_pending_work`, `archived: False pending: 1`. The queue guard, metadata writes and durable event now share one queue transaction. |
| Relocation history under writer lock | STILL-BROKEN | Before: `relocation full load in_write: True`. After: `relocation full load in_write: False`. Metadata validation/update uses a summary; the full-record return contract is preserved by loading after the write transaction exits. |
| Metadata resurrection | STILL-BROKEN | Before: `metadata: accepted`, `deleted session resurrected: True`. After: `metadata: Session not found`, `deleted session resurrected: False`. Title, pin, archive, usage and direct metadata edits require an existing row. Initial provider metadata creation explicitly opts in. |
| Idle incremental vacuum, `r6_pragmas_growth.py` | FIXED-BY-P2 | Original scratch reports `auto_vacuum 2`; it never invokes idle maintenance after deletion. Explicit maintenance probe: `before delete: pages 518 freelist 0`, `after maintenance: pages 30 freelist 0`, `integrity: ok`. Four bounded passes reclaim the orphan blob's pages. |
| Upload rollback | STILL-BROKEN | Pin metadata is FIXED-BY-P2: both probes show `pin rollback durable: 0`. Delete before: `delete rollback row: 1 file: False`. After: `delete rollback row: 1 file: True`. Materialized file deletion now waits for the outer commit; the regression also verifies committed deletion removes the file. |
| Newer-schema CLI refusal | STILL-BROKEN | Both commands exit `1`. Before: a Rich traceback ending with `SchemaTooNewError` and `supports up to 2`. After: one line, `Error: <temporary path>/newer.db uses schema version 999, but this Yoke release supports up to 3. Upgrade Yoke to open it.` |
| Importer versus retention | STILL-BROKEN | Both probes show `import: imported`. Before: `import survives retention: False`. After: `import survives retention: True`. Schema v3 adds an internal hold until the next conversation save or tree edit. Original timestamps stay unchanged; after use, the normal 30-day unpinned policy applies again. |

A deliberately retained streaming iterator still owns a WAL snapshot until it is
closed or exhausted. That is necessary for consistent streaming reads and no
longer contaminates unrelated executor work. The importer, the only production
caller, already closes its iterator with `contextlib.closing`, including on early
verification failure.

New tests in `tests/yoke/storage/test_qa_regressions.py`:

- `test_abandoned_iterator_does_not_stale_a_pooled_reader`
- `test_surrogate_text_columns_save_and_update`, root/title/provider/model
- `test_metadata_edit_never_resurrects_deleted_session`, title/pin/archive/context/direct
- `test_surrogate_entry_columns_match_sanitized_blob`
- `test_archive_rechecks_queue_after_concurrent_admission`
- `test_relocation_decodes_history_after_releasing_write_transaction`
- `test_upload_pin_and_delete_rollback_preserve_native_file`
- `test_imported_old_session_retained_until_used_then_normal_retention`
- `test_idle_maintenance_reclaims_orphan_pages`

New tests in `tests/yoke/storage/test_qa_cli.py`:

- `test_turn_save_warns_on_sqlite_lock_and_keeps_memory`
- `test_model_switch_warns_on_sqlite_failure`, full/locked
- `test_turn_save_warns_on_sqlite_full_and_can_retry`
- `test_db_info_newer_schema_is_a_clean_cli_error`

Raw scratch, archived-HEAD failure, extended-probe and schema-command logs are
under `/tmp/yoke-sqlite-qa-tmk_2cwc`; original scratch reruns after the fixes are
under `/tmp/yoke-qa-after-k0njcm8g`. These contain synthetic test data only.

The single full-suite run found one regression in the first archive fix: an
already-queued session was rejected after the title-generation cancellation
callback ran. The final fix preserves the original precheck before that callback
and rechecks the queue under the write lock. Both the side-effect contract and
the concurrent-admission guard pass in the focused follow-up. The full suite was
not repeated, following the one-run instruction.

Full-suite tail:

```text
FAILED tests/yoke/http/test_session_view_regressions.py::test_rejected_multifield_patches_have_no_side_effects
1 failed, 2330 passed, 4 skipped in 294.86s (0:04:54)
```

Focused follow-up command:

```text
uv run pytest -q -p no:cacheprovider tests/yoke/http/test_session_view_regressions.py tests/yoke/http/test_contract.py tests/yoke/storage/test_qa_regressions.py tests/yoke/storage/test_qa_cli.py
```

Literal tail after the final archive fix:

```text
..................................................................       [100%]
66 passed in 7.35s
```

The earlier compatibility run also passed:

```text
99 passed in 34.65s
```

The projection oracle, three storage passes and static checks are repeated on
the final source. Their literal tails follow below.

`uv run pytest -q -p no:cacheprovider tests/yoke/provider_projection`:

```text
.................................                                        [100%]
33 passed in 16.98s
```

`uv run pytest -q -p no:cacheprovider tests/yoke/storage`, three runs:

```text
.........................................                                [100%]
41 passed in 20.33s
```

```text
.........................................                                [100%]
41 passed in 18.68s
```

```text
.........................................                                [100%]
41 passed in 21.00s
```

`uv run ruff check .`:

```text
All checks passed!
```

`uv run ruff format .`:

```text
855 files left unchanged
```

`uv run ty check`:

```text
All checks passed!
```

`uv run pyright`:

```text
0 errors, 0 warnings, 0 informations
WARNING: there is a new pyright version available (v1.1.410 -> v1.1.414).
Please install the new version or set PYRIGHT_PYTHON_FORCE_VERSION to `latest`
```

Provider goldens, `src/yoke/_version.py`, `pyproject.toml` and `uv.lock` have no
diff. `git diff --check` passes. Host `pgrep -af pytest` checks found another
engineer's full run and later a daemon/ACP batch; both were allowed to finish
before the final suite started. No second full suite was launched.
