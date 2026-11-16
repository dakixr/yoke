"""Credential sanitation belongs to diagnostics and daemon-owned log output."""

from __future__ import annotations

import io
import json
import logging
import logging.config
import os
import sys

import pytest
from fastapi.testclient import TestClient

from yoke.ai.providers.codex.subscription.logging import log_provider_event
from yoke.daemon.log import daemon_logging_config, detached_stdio, sanitized_stdio
from yoke.http.errors import ApiError
from yoke.http.services.privacy import SessionPrivacy
from tests.yoke.http.test_daemon_api_environment import make_app
from tests.yoke.http.test_runtime_lifetime_regressions import _auth, _create_session


@pytest.mark.parametrize(
    "name",
    [
        "MY_API_KEY",
        "ACCESS_TOKEN",
        "MY_SECRET",
        "PASSWORD",
        "MY_CREDENTIAL",
        "MY_AUTH",
        "YOKE_CODEX_API_KEY",
        "OPENCODE_API_KEY",
        "ZAI_API_KEY",
        "YOKE_OPENAI_API_KEY",
        "OPENAI_API_KEY",
    ],
)
def test_only_long_secret_named_environment_values_are_diagnostic_redactions(name):
    privacy = SessionPrivacy()
    privacy.register(
        {
            name: "secret-value-1234",
            "SHORT_TOKEN": "1",
            "SEVEN_SECRET": "1234567",
            "EIGHT_SECRET": "12345678",
            "SHLVL": "1",
            "COLUMNS": "80",
            "LINES": "24",
            "YOKE_SENTINEL_X": "ordinary-sentinel-1234",
            "PATH": "/usr/local/bin",
        }
    )
    text = "1 80 24 1234567 12345678 secret-value-1234 ordinary-sentinel-1234 /usr/local/bin"
    assert privacy.text(text) == (
        "1 80 24 1234567 <redacted> <redacted> ordinary-sentinel-1234 /usr/local/bin"
    )


def test_event_diagnostics_preserve_identity_and_transcript_mirroring_fields():
    privacy = SessionPrivacy()
    secret = "sk-test-event-value"
    privacy.register({"MY_API_KEY": secret})
    payload: dict[str, object] = {
        "inputID": secret,
        "message": secret,
        "error": secret,
        "result": {"output": secret + " 1 80 24"},
        "iteration": 1,
    }
    warning = privacy.event_data("session.warning", payload)
    assert warning["inputID"] == secret
    assert warning["message"] == "<redacted>"
    assert warning["error"] == "<redacted>"
    tool = privacy.event_data("session.tool.ended", payload)
    assert tool == {**payload, "error": "<redacted>"}
    assert payload["error"] == secret
    trace = privacy.trace_data(payload)
    assert trace == {
        **payload,
        "error": "<redacted>",
        "result": {"output": "<redacted> 1 80 24"},
    }


def test_daemon_log_formatters_preserve_shared_records_and_library_handlers(tmp_path):
    privacy = SessionPrivacy()
    secret = "daemon-secret-12345678"
    unrelated = logging.StreamHandler(io.StringIO())
    logger = logging.getLogger(__name__)
    logger.addHandler(unrelated)
    try:
        privacy.register({"MY_API_KEY": secret})
        assert unrelated.filters == []
        config = daemon_logging_config(privacy.text)
        formatters = config["formatters"]
        assert isinstance(formatters, dict)
        formatter = logging.config.DictConfigurator(config).configure_formatter(
            formatters["default"]
        )
        try:
            raise ValueError(secret)
        except ValueError:
            record = logging.LogRecord(
                __name__,
                logging.ERROR,
                __file__,
                1,
                "credential %s; 1 80 24",
                (secret,),
                sys.exc_info(),
            )
        before = record.__dict__.copy()
        rendered = formatter.format(record)
        assert secret not in rendered
        assert "1 80 24" in rendered
        assert "ValueError: <redacted>" in rendered
        assert record.__dict__ == before
        assert secret in logging.Formatter().format(record)
        path = tmp_path / "daemon.log"
        with detached_stdio(path), sanitized_stdio(privacy.text):
            print(secret + " 1 80 24")
            print(secret, file=sys.stderr)
        assert path.read_text() == "<redacted> 1 80 24\n<redacted>\n"
        assert unrelated.filters == []
    finally:
        logger.removeHandler(unrelated)
        unrelated.close()


def test_provider_diagnostics_redact_before_truncation(tmp_path):
    privacy = SessionPrivacy()
    secret = "sk-test-" + "x" * 400
    privacy.register({"MY_API_KEY": secret})
    with privacy.scope():
        log_provider_event(tmp_path, "fixture", "failed", error=secret)
    payload = json.loads(next(tmp_path.glob("fixture-*.jsonl")).read_text())
    assert payload["error"] == "<redacted>"


def test_zai_debug_logs_sanitize_a_copy_without_changing_request_content(tmp_path):
    from yoke.ai.providers.zai.models import ZAIConfig
    from yoke.ai.providers.zai.recovery import ZAIMessageRecovery

    privacy = SessionPrivacy()
    secret = "sk-test-zai-diagnostic-1234"
    privacy.register({"ZAI_API_KEY": secret})
    path = tmp_path / "zai-debug.jsonl"
    recovery = ZAIMessageRecovery(ZAIConfig(api_key=secret, debug_log_path=str(path)))
    payload = {"messages": [{"role": "user", "content": secret + " 1 80 24"}]}
    with privacy.scope():
        recovery.log_debug_event("failed", **payload)
    assert secret not in path.read_text()
    assert (
        json.loads(path.read_text())["messages"][0]["content"] == "<redacted> 1 80 24"
    )
    assert payload["messages"][0]["content"] == secret + " 1 80 24"


@pytest.mark.parametrize(
    "malformed",
    [
        {"cwd": "private-invalid-cwd"},
        {"kind": "invalid-kind"},
        {"owner": ""},
        {"mcpServers": [{"name": "a"}]},
        {"mcpServers": [{"name": "a", "command": "a"}] * 2},
    ],
)
def test_api_errors_and_invalid_attachments_never_echo_env_mapping(tmp_path, malformed):
    app = make_app(tmp_path, [])
    secret = "sk-test-unprinted-12345678"
    env = {
        **os.environ,
        "MY_API_KEY": secret,
        "ENV_MAPPING_ONLY": "mapping-private-1234",
    }

    def fail():
        raise ApiError(400, "fixture_error", "failed " + secret, {"reason": secret})

    app.add_api_route("/fixture-error", fail)
    with TestClient(app) as client:
        _create_session(client, tmp_path, "one")
        payload = {"owner": "terminal", "env": env, "cwd": str(tmp_path), **malformed}
        for path, body in (
            ("/api/v1/session/one/client", payload),
            (
                "/api/v1/session",
                {"client": payload, "location": {"directory": str(tmp_path)}},
            ),
        ):
            response = (
                client.put(path, headers=_auth(), json=body)
                if path.endswith("/client")
                else client.post(path, headers=_auth(), json=body)
            )
            assert response.status_code == 422
            assert secret not in response.text
            assert "mapping-private-1234" not in response.text
            assert "private-invalid-cwd" not in response.text
            assert json.dumps(env) not in response.text
            for error in response.json()["error"]["details"]["errors"]:
                assert set(error) == {"loc", "type", "msg"}
        client.put(
            "/api/v1/session/one/client",
            headers=_auth(),
            json={"owner": "terminal", "env": env, "cwd": str(tmp_path)},
        ).raise_for_status()
        response = client.get("/fixture-error")
        assert response.status_code == 400
        assert secret not in response.text
        assert response.json()["error"]["message"] == "failed <redacted>"
        assert response.json()["error"]["details"] == {"reason": "<redacted>"}
