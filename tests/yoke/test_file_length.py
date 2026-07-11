from __future__ import annotations

# ruff: noqa: D100, D103, S101

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_YOKE_SRC = _REPO_ROOT / "src" / "yoke"
_YOKE_TESTS = _REPO_ROOT / "tests" / "yoke"
_DEFAULT_MAX_LINES = 400

# These modules predate the size guard. Keep their current size as a ceiling so
# they cannot grow while allowing them to be split down over time. Remove an
# entry when its file is deleted or reduced to the default limit.
_LEGACY_LINE_BUDGETS = {
    "src/yoke/agent/context/manager.py": 408,
    "src/yoke/agent/loop/agent.py": 526,
    "src/yoke/agent/tools/command_process.py": 533,
    "src/yoke/agent/tools/web/fetch.py": 524,
    "src/yoke/agent/tools/web/research.py": 453,
    "src/yoke/ai/providers/codex/subscription.py": 2301,
    "src/yoke/ai/providers/codex/websockets.py": 1035,
    "src/yoke/ai/providers/openai_compat/provider.py": 444,
    "src/yoke/ai/providers/opencode_go.py": 737,
    "src/yoke/ai/providers/zai.py": 741,
    "src/yoke/cli/interactive/basic.py": 408,
    "src/yoke/cli/interactive/mcp_menu.py": 646,
    "src/yoke/cli/interactive/prompt/__init__.py": 403,
    "src/yoke/cli/interactive/renderer.py": 445,
    "src/yoke/cli/interactive/slash_commands.py": 536,
    "src/yoke/cli/interactive/tools/inspector_render.py": 571,
    "src/yoke/cli/main.py": 785,
    "src/yoke/cli/runtime/cli.py": 464,
    "src/yoke/cli/runtime/selector/ui.py": 408,
    "src/yoke/cli/runtime/session.py": 484,
    "src/yoke/cli/session.py": 680,
    "src/yoke/mcp/client.py": 676,
    "tests/yoke/ai/providers/test_codex_subscription.py": 468,
    "tests/yoke/ai/providers/test_codex_websockets.py": 1135,
    "tests/yoke/ai/test_sdk_redesign.py": 481,
    "tests/yoke/bootstrap/test_bootstrap_config.py": 446,
    "tests/yoke/cli/test_cli_model_switching.py": 522,
    "tests/yoke/cli/test_cli_resume_terminal.py": 439,
    "tests/yoke/cli/test_cli_slash_runtime_tools.py": 670,
    "tests/yoke/cli/test_cli_tool_policies.py": 438,
    "tests/yoke/cli/test_prompt_toolkit_completion.py": 551,
    "tests/yoke/cli/test_tool_inspector.py": 857,
    "tests/yoke/loop/test_agent_control.py": 456,
    "tests/yoke/loop/test_agent_runtime_core.py": 539,
    "tests/yoke/test_attach_image_tool.py": 564,
    "tests/yoke/test_context.py": 445,
    "tests/yoke/test_mcp.py": 621,
    "tests/yoke/test_provider_openai_compatible.py": 460,
    "tests/yoke/test_tools.py": 475,
    "tests/yoke/test_web_tools.py": 858,
}


def test_python_files_do_not_exceed_their_line_budget() -> None:
    python_files = sorted([*_YOKE_SRC.rglob("*.py"), *_YOKE_TESTS.rglob("*.py")])
    relative_files = {str(path.relative_to(_REPO_ROOT)): path for path in python_files}
    stale_budgets = sorted(set(_LEGACY_LINE_BUDGETS) - relative_files.keys())
    violations = []
    for relative_path, py_file in relative_files.items():
        line_count = len(py_file.read_text(encoding="utf-8").splitlines())
        budget = _LEGACY_LINE_BUDGETS.get(relative_path, _DEFAULT_MAX_LINES)
        if line_count > budget:
            violations.append(
                f"{relative_path} has {line_count} lines (budget: {budget})"
            )

    assert not stale_budgets, (
        "Remove stale entries from _LEGACY_LINE_BUDGETS:\n" + "\n".join(stale_budgets)
    )
    assert not violations, (
        "Python files must stay within their line budgets. Split oversized "
        "files into modules with clear seams, simplify them, or reduce a "
        "legacy budget after refactoring:\n" + "\n".join(violations)
    )
