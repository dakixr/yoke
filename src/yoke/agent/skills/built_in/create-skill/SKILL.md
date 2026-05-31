---
name: create-skill
description: Create a new yoke skill by first confirming where it should live, then scaffolding it with the yoke CLI.
---

# Create Skill

Use this skill when the user wants to create a new yoke skill.

## Always ask location first

Before creating anything, ask where the user wants the skill to be created:
- in this repo
- globally
- in a custom directory

If the user already specified the location clearly, do not ask again.

## Use the yoke CLI to scaffold

Always scaffold the skill with the yoke CLI rather than manually creating these files.

`--root` is the workspace root, not the skills directory itself.
`yoke skills init` always writes to:
`<root>/.yoke/skills/<skill-name>/SKILL.md`

Use one of these patterns:

### Repo-local skill
```bash
yoke skills init <skill-name>
```

This uses the current working directory as `<root>`, so it creates:
`./.yoke/skills/<skill-name>/SKILL.md`

### Global skill
```bash
yoke skills init --root ~ <skill-name>
```

To create a global skill at `~/.yoke/skills/<skill-name>/SKILL.md`, pass your home directory as `<root>`.

Do not pass `~/.yoke` as `--root`, because that creates:
`~/.yoke/.yoke/skills/<skill-name>/SKILL.md`

### Custom directory
Use the custom directory as the CLI root, so yoke will create:
`<custom-dir>/.yoke/skills/<skill-name>/SKILL.md`

```bash
yoke skills init --root <custom-dir> <skill-name>
```

## Naming and format rules

Ensure the skill name is lowercase kebab-case.
The generated folder name must match the skill name.
The skill file must be named `SKILL.md`.

After scaffolding, edit the generated `SKILL.md` to replace the placeholder description and add the actual reusable instructions.

## Expected follow-up behavior

After scaffolding:
1. Open the generated `SKILL.md`
2. Fill in a concrete `description`
3. Write the instructions the agent should follow when that skill is active
4. Optionally show the created file path to the user

If the target file already exists, ask before using `--force`.
