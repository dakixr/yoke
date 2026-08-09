---
name: create-skill
description: Scaffold an yoke skill with `yoke skills init` and write predictable instructions.
---

# Create Skill

## Core Process

1. Confirm where the skill should live unless the user already specified it:
   repo-local, global, or custom root.
2. Choose a lowercase kebab-case skill name.
3. Scaffold with `yoke skills init`; do not hand-create the initial files.
4. Edit the generated `SKILL.md` into a predictable skill; follow the writing
   pass below.
5. Report the generated path, whether it is repo-local/global/custom-rooted,
   and any follow-up needed.

## Completion Criteria

- The generated `SKILL.md` has no placeholder description or body text.
- Every instruction sentence passes the no-op test: it changes what the agent
  would do compared with default behavior.

## CLI Scaffolding

`--root` is the workspace root, not the skills directory itself.
`yoke skills init` writes to:

`<root>/.yoke/skills/<skill-name>/SKILL.md`

Use these command shapes:

### Repo-local skill

```bash
yoke skills init <skill-name>
```

Creates `./.yoke/skills/<skill-name>/SKILL.md` from the current working
directory.

### Global skill

```powershell
yoke skills init --root "$HOME" <skill-name>
```

Creates `$HOME/.yoke/skills/<skill-name>/SKILL.md`. Use the shell-expanded home
directory path as `--root`, not the `.yoke` directory itself.

### Custom root

```bash
yoke skills init --root <custom-dir> <skill-name>
```

Creates `<custom-dir>/.yoke/skills/<skill-name>/SKILL.md`.

If the target file already exists, ask before using `--force`.

## Writing The Skill

1. Decide invocation mode. Use a model-invoked skill when yoke or another skill
   must select it autonomously. Use `disable-model-invocation: true` when only
   the user should invoke it explicitly.
2. Write a one-line `description`. For model invocation, state what the skill
   does and give one trigger per genuinely distinct branch. For user invocation,
   write a human-facing summary without trigger lists.
3. Put the required process near the top as numbered steps. End hard steps with
   checkable, exhaustive completion criteria that prevent premature completion.
4. Separate ordered steps from reference material. Inline what every branch
   needs; move branch-only reference into clearly named linked sibling files.
5. Co-locate each concept's definition, rules, and caveats under one heading.
6. Prune sentence by sentence. Delete no-ops, stale material, and duplicated
   meanings; keep one authoritative source for each behavior.
7. Prefer positive target behavior over prohibitions. Keep a prohibition only
   for a hard guardrail, and pair it with what the agent should do instead.
8. Use compact leading words already meaningful to the model when they replace
   repeated explanations and make invocation or execution more predictable.

## Design Checks

- Split by invocation only when a distinct trigger must independently reach a
  new skill; each model-invoked skill adds permanent description context.
- Split by sequence only when visible later steps repeatedly cause the agent to
  rush an irreducibly fuzzy current step.
- Diagnose premature completion by sharpening completion criteria before
  splitting. Diagnose sprawl with progressive disclosure, not arbitrary length.
- Reject sediment: every line must remain relevant to current skill behavior.
