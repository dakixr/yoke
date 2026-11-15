Keep the documentation at src/yoke/docs up to date when making changes to code in src/yoke.

When adding or changing interactive keyboard shortcuts, update the binding table in
`src/yoke/cli/interactive/prompt/keys.py`. It generates the basic-mode startup help
and both modes' `/shortcuts` notices. Prompt-toolkit startup prints only the
version banner.

Prefer deep modules with clear seams over flat files sharing a prefix. Aim for Python files under `src/yoke` of at most 400 lines; one cohesive class may grow to about 600. Never split code just to meet the limit: mixins, free functions taking an untyped owner, Protocols that list another class's private attributes, and helpers that reach into another object's underscore attributes are worse than a longer module. Split along real seams, where each piece owns its state and has a small public interface.

After changing the Yoke SDK, read `src/yoke/agent/skills/built_in/yoke-subagents/SKILL.md` and update it, plus `PATTERNS.md` and `SDK_SURFACE.md`, when the documented orchestration API or examples are affected.

Provider-bound prompting and cache behavior are stability-critical. Performance work must not change provider message content/order, system or skill instructions, tool-call/result sequencing, image capability projection, compaction handoff semantics, response continuity, or conversation cache scope unless the task explicitly requires that behavior change. Prefer HTTP/index/persistence optimizations over agent-loop prompt-path changes. If a performance change touches provider-bound or compaction code, add differential tests against the established projection across representative tools, skills, checkpoints, images, and branches, and revert the optimization if any provider-visible behavior or cache identity diverges.

Before each commit that changes Yoke, bump `src/yoke/_version.py` according to semantic versioning: patch for backward-compatible fixes/docs/defaults, minor for backward-compatible features, and major for breaking changes. Keep `pyproject.toml` and `uv.lock` synchronized with that version.

Run `uv run ruff check .`, `uv run ruff format .`, `uv run ty check`, and `uv run pyright` before finishing changes.
