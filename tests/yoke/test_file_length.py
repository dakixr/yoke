from __future__ import annotations

# ruff: noqa: D100, D103, S101

from pathlib import Path

_YOKE_ROOT = Path(__file__).resolve().parents[3] / "src"
_YOKE_SRC = _YOKE_ROOT / "gentools" / "yoke"
_YOKE_TESTS = _YOKE_ROOT / "tests" / "yoke"
_MAX_LINES = 400

_python_files = sorted([*_YOKE_SRC.rglob("*.py"), *_YOKE_TESTS.rglob("*.py")])


def test_python_files_are_not_longer_than_400_lines() -> None:
    oversized_files = []
    for py_file in _python_files:
        line_count = len(py_file.read_text(encoding="utf-8").splitlines())
        if line_count > _MAX_LINES:
            oversized_files.append(
                f"{py_file.relative_to(_YOKE_ROOT)} has {line_count} lines"
            )

    assert not oversized_files, (
        f"Python files must be {_MAX_LINES} lines or shorter. "
        "Refactor oversized files into deeper modules with clear seams, "
        "or simplify them:\n" + "\n".join(oversized_files)
    )
