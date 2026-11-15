"""Public lazy exports and their generated typing stubs share one source."""

from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "package",
    [
        "yoke.agent",
        "yoke.agent.loop",
        "yoke.agent.compaction",
        "yoke.ai",
        "yoke.ai.providers",
    ],
)
def test_public_exports_and_stub_match_lazy_table(package: str) -> None:
    """Resolve every public export and independently check its static import."""
    module = import_module(package)
    assert module.__file__ is not None
    stub = ast.parse(Path(module.__file__).with_suffix(".pyi").read_text())
    imports = {
        alias.asname: (node.module, alias.name)
        for node in stub.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert set(imports) == set(module.__all__)
    for name, target in module._LAZY_EXPORTS.items():
        location, attribute = (target, name) if isinstance(target, str) else target
        assert imports[name] == (location, attribute)
        assert getattr(module, name) is getattr(import_module(location), attribute)
