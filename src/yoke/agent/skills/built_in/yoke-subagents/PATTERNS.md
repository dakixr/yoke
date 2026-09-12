# Orchestration patterns

Start with the smallest case below. Each Python block is a complete script,
with no sibling helpers or task schemas. Save only the one needed under
`.agents_local/`, then run it with the repository's Yoke-enabled Python.
Choose `SELECTION` from local provider status and set `ROOT` to the repository's
absolute path. Replace the example prompts and paths with the actual task.

The guarded entrypoint matters: Yoke's tool subprocesses re-import the launcher.
Run these as files rather than piping them to Python's stdin.

## One worker, with optional follow-ups

Keep trivial work in the parent. Use one delegated agent when its separate
context or perspective helps. Pass one prompt for a single answer, or several
prompts to reuse that agent's conversation sequentially. Save as `ask.py`:

```python
import asyncio
from pathlib import Path
import sys

from yoke.ai import Agent, RunConfig, build_builtin_provider


async def ask(root: Path, selection: str, prompts: list[str]) -> None:
    if not prompts:
        raise ValueError("Supply at least one prompt")
    async with Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=root,
            tools=["file.read", "file.search"],
            sys_prompt="Stay read-only. Cite file:line evidence and report blockers.",
        ),
    ) as worker:
        for prompt in prompts:
            result = await worker.prompt_async(prompt)
            print(result.output, flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: ask.py ROOT SELECTION PROMPT [PROMPT ...]")
    asyncio.run(ask(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3:]))
```

```bash
uv run python "$ROOT/.agents_local/ask.py" "$ROOT" "$SELECTION" \
  "Read parser.py and SPEC.md. Identify contract violations with evidence." \
  "Which finding most needs a regression test? Give the input and expected result."
```

Both prompts use one conversation. The parent checks the answer and runs any
validation. No state file is needed unless this role must survive the process.

## Two independent workers

Prefer complementary prompts for independent questions. Repeat the same question
only when independent replication or disagreement is the point. Save as `audit.py`;
each supplied prompt gets its own agent and provider:

```python
import asyncio
from pathlib import Path
import sys

from yoke.ai import Agent, BatchTask, RunConfig
from yoke.ai import build_builtin_provider, run_many


async def audit(root: Path, selection: str, prompts: list[str]) -> None:
    if not 1 <= len(prompts) <= 4:
        raise ValueError("This small example accepts 1-4 prompts")

    def factory(_task: BatchTask) -> Agent:
        return Agent(
            provider=build_builtin_provider(selection),
            config=RunConfig(
                root=root,
                tools=["file.read", "file.search"],
                sys_prompt="Stay read-only. Cite file:line evidence and report blockers.",
            ),
        )

    batch = await run_many(
        [BatchTask(id=f"audit-{i}", prompt=p) for i, p in enumerate(prompts, 1)],
        agent_factory=factory,
        max_concurrency=2,
        max_attempts=1,
    )
    for item in batch.items:
        print(f"{item.task.id}: {item.status}", flush=True)
        print(
            item.result.output if item.result is not None else repr(item.error),
            flush=True,
        )
    if batch.failed_count:
        raise RuntimeError("Some audits failed; keep successful answers and report gaps")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: audit.py ROOT SELECTION PROMPT [PROMPT ...]")
    asyncio.run(audit(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3:]))
```

```bash
uv run python "$ROOT/.agents_local/audit.py" "$ROOT" "$SELECTION" \
  "Audit parser.py against SPEC.md. Return confirmed defects and their evidence." \
  "Inspect test_parser.py against SPEC.md. Identify missing contract coverage."
```

`run_many()` closes every worker. A completed call can still report a blocker;
the parent reads both answers before synthesizing them. Plain text is sufficient
here. Use `output_type` only when another step needs machine-readable results.

## One durable role

Durable means the same role remembers after this Python process exits. It does
not require a coder/reviewer pair. Save as `durable.py`:

```python
import asyncio
from hashlib import sha256
from pathlib import Path
import sys

from yoke.ai import Agent, RunConfig, build_builtin_provider


async def continue_role(root: Path, selection: str, state: Path, prompt: str) -> None:
    state = state.resolve()
    async with Agent(
        provider=build_builtin_provider(
            selection, session_id=sha256(str(state).encode()).hexdigest()
        ),
        config=RunConfig(
            root=root,
            tools=["file.read", "file.search"],
            sys_prompt="Stay read-only. Retain decisions; re-read files when asked to review changes.",
        ),
        state_path=state,
        autosave=True,
    ) as worker:
        print("Resuming" if worker.has_state else "Starting", state, flush=True)
        result = await worker.prompt_async(prompt)
        print(result.output, flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit("usage: durable.py ROOT SELECTION STATE PROMPT")
    asyncio.run(continue_role(
        Path(sys.argv[1]).resolve(), sys.argv[2], Path(sys.argv[3]), sys.argv[4]
    ))
```

Choose a fresh state path for a new job. Keep this path for its later turns:

```bash
STATE="$ROOT/.agents_local/parser-review-unique/reviewer.json"
uv run python "$ROOT/.agents_local/durable.py" "$ROOT" "$SELECTION" "$STATE" \
  "Review parser.py against SPEC.md. Remember that blank records must be preserved."
# After the parent edits parser.py, run a new process with the same state path:
uv run python "$ROOT/.agents_local/durable.py" "$ROOT" "$SELECTION" "$STATE" \
  "Re-read parser.py. Does the new version satisfy the requirement we discussed?"
```

`state_path` loads an existing snapshot; `autosave` saves after successful turns.
The state-derived session ID also stays stable for OpenCode Go. Use one writer
per state file and the same root, selection, tools, and instructions on resume.
This example starts fresh if the file is absent. When absence must be an error,
use `Agent.load(state, provider=..., config=..., autosave=True)` instead.
Conversation state is not a filesystem snapshot, and interrupted work may not
be saved. One JSON state file is enough; no task manifest or handoff is required.
That file is neither small nor sanitized: it can contain prompts, responses,
tool results, paths, and proprietary source context, and can grow quickly across
turns. Treat it as sensitive conversation data.

## When the larger examples earn their cost

Use [scripts/fan_out.py](scripts/fan_out.py) for structured read-only batches
that need retained results and traces, and [scripts/review_pair.py](scripts/review_pair.py)
for scoped implementation with repeated review and explicit resume contracts.
Read the chosen script before adapting it. Both take explicit `--root` and
`--selection`. Fan-out takes `--tasks` with a JSON list; the pair takes `--task`
with one object:

```json
{
  "id": "parser-fix",
  "request": "Make parser.py satisfy SPEC.md without changing the specification.",
  "scope": ["parser.py", "test_parser.py"],
  "acceptance": ["Preserve blank records and add a regression test."],
  "validation": "Parent runs tests; report the commands to execute.",
  "mode": "codebase"
}
```

In the pair, `scope` lists owned write paths. In an audit it lists source paths;
`mode` chooses local, web, or mixed tools without creating more tasks. Supply
only independent tasks. Implementation fan-out needs an adapted factory with
explicit write tools, non-overlapping paths, and reported changes.

The larger scripts retain results, a short handoff index, trace, and phase log.
The pair also saves a contract, role snapshots, and review history. It caps each
invocation at three reviews and returns unresolved feedback after final rejection.
Repeat its command with the same `--run-id` and `--resume` for the same contract.
Resume starts a new bounded loop, not an exactly-once job. It replaces current
result/history summaries, appends traces, and preserves conversations. Copy an
old summary first if needed. Rejected resume input preserves valid results and
writes `resume-error.json`. Review approval still needs parent validation.

Copy `scripts/` intact when adapting those larger examples; their sibling imports
must work under `__mp_main__`. The small examples above have no such dependencies.
For discovery, planning, or synthesis, adapt the one-worker prompt rather than
adding another workflow. Explicitly pass relevant `RunConfig.skills`; workers
do not inherit the parent's conversation or loaded skills.
