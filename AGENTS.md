Keep the documentation at src/yoke/docs up to date when making changes to code in src/yoke.

When adding or changing interactive keyboard shortcuts, update the startup shortcuts message in `src/yoke/cli/interactive/prompt/rendering.py` and the `/shortcuts` notice in `src/yoke/cli/interactive/common.py`.

Prefer deep modules with clear seams over flat files sharing a prefix. New Python files under `src/yoke` must stay at or below 400 lines. Do not increase an existing oversized module; split it into deeper modules when modifying it substantially.

After changing the Yoke SDK, read `src/yoke/agent/skills/built_in/yoke-subagents/SKILL.md` and update it, plus `PATTERNS.md` and `SDK_SURFACE.md`, when the documented orchestration API or examples are affected.

Before each commit that changes Yoke, bump `src/yoke/_version.py` according to semantic versioning: patch for backward-compatible fixes/docs/defaults, minor for backward-compatible features, and major for breaking changes. Keep `pyproject.toml` and `uv.lock` synchronized with that version.

Run `uv run ruff check .`, `uv run ruff format .`, `uv run ty check`, and `uv run pyright` before finishing changes.
