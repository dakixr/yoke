# ruff: noqa: D100, D101, D103, S101

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from yoke.ai import BatchTask
from yoke.ai import to_jsonable
from yoke.ai import write_json_artifact


class Summary(BaseModel):
    verdict: str


@dataclass(slots=True)
class Handoff:
    task: BatchTask
    summary: Summary
    path: Path
    error: Exception | None = None


def test_to_jsonable_preserves_nested_workflow_structure(tmp_path: Path) -> None:
    payload = Handoff(
        task=BatchTask(id="review", prompt="Review."),
        summary=Summary(verdict="ok"),
        path=tmp_path / "result.json",
        error=RuntimeError("planned"),
    )

    assert to_jsonable(payload) == {
        "task": {
            "id": "review",
            "prompt": "Review.",
            "images": [],
            "image_urls": [],
        },
        "summary": {"verdict": "ok"},
        "path": str(tmp_path / "result.json"),
        "error": {"type": "RuntimeError", "message": "planned"},
    }


def test_to_jsonable_rejects_lossy_unknown_values() -> None:
    with pytest.raises(TypeError, match="not JSON artifact compatible"):
        to_jsonable(object())
    with pytest.raises(TypeError, match="string keys"):
        to_jsonable({1: "value"})


def test_write_json_artifact_is_atomic_by_default(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "handoff.json"

    written = write_json_artifact(target, Summary(verdict="ok"))

    assert written == target.resolve()
    assert json.loads(target.read_text(encoding="utf-8")) == {"verdict": "ok"}
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
