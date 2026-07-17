from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101

import os
import time
from pathlib import Path
from typing import Any

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools import ReadTool
from yoke.agent.tools.web import WebFetchTool
from yoke.ai.providers.base import Provider


class FakeResponse:
    def __init__(self, text: str, *, url: str) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.status_code = 200
        self.url = url

    def raise_for_status(self) -> None:
        pass


class NoopProvider(Provider):
    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("done")


def test_web_fetch_saves_complete_content_before_truncation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    html = (
        "<html><body><h1>Install</h1><p>Alpha setup notes.</p><p>"
        + ("Complete archive content. " * 50)
        + "</p><h2>Deploy</h2><p>Beta deploy guide.</p></body></html>"
    )
    final_url = "https://docs.example.test/guide?token=secret"

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(html, url=final_url)

    monkeypatch.setattr("httpx.get", fake_get)
    tool = WebFetchTool.bind(root=workspace, home=home).parse_arguments(
        {"url": final_url, "max_chars": 500}
    )

    result = tool.execute()

    assert result["ok"] is True
    assert result["truncated"] is True
    saved_path = Path(str(result["path"]))
    assert saved_path.is_absolute()
    assert saved_path.parent == home / ".yoke" / "tool-output"
    assert saved_path.suffix == ".md"
    assert "secret" not in saved_path.name
    saved_content = saved_path.read_text(encoding="utf-8")
    assert len(saved_content) > 500
    assert "Alpha setup notes." in saved_content
    assert "Complete archive content." in saved_content
    assert "Beta deploy guide." in saved_content
    read_result = (
        ReadTool.bind(root=workspace)
        .parse_arguments({"path": str(result["path"])})
        .execute()
    )
    assert "Complete archive content." in str(read_result["content"])


def test_agent_initialization_removes_only_expired_global_outputs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    output_directory = home / ".yoke" / "tool-output"
    output_directory.mkdir(parents=True)
    expired_output = output_directory / "expired.md"
    current_output = output_directory / "current.md"
    expired_output.write_text("expired", encoding="utf-8")
    current_output.write_text("current", encoding="utf-8")
    expired_time = time.time() - 8 * 24 * 60 * 60
    os.utime(expired_output, (expired_time, expired_time))

    agent = RuntimeAgent(
        provider=NoopProvider(),
        tools=[WebFetchTool.bind(root=workspace, home=home)],
        tool_home=home,
    )

    assert not expired_output.exists()
    assert current_output.read_text(encoding="utf-8") == "current"

    agent.fork()

    assert current_output.read_text(encoding="utf-8") == "current"
