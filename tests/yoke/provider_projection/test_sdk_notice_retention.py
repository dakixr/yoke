"""SDK correction prompts keep their request bytes without becoming user intent."""

from contextlib import closing

from tests.yoke.ai.test_sdk_redesign import RecordingProvider, Summary
from yoke.agent.context import ContextManager
from yoke.agent.models import Message
from yoke.ai import Agent, RunConfig


def test_structured_retry_only_changes_retained_user_prompts(tmp_path, monkeypatch):
    results = []
    for legacy in (True, False):
        provider = RecordingProvider(
            Message.assistant("not json"),
            Message.assistant('{"verdict":"pass","risks":[]}'),
        )
        with monkeypatch.context() as patch:
            if legacy:

                def append_as_user(context, message, *, metadata):
                    ContextManager.append_message(context, message)

                patch.setattr(
                    "yoke.ai.sdk.prompt_runner.append_control_message", append_as_user
                )
            with closing(
                Agent(
                    provider=provider,
                    config=RunConfig(
                        root=tmp_path,
                        tools=[],
                        include_agents_file=False,
                    ),
                )
            ) as agent:
                result = agent.prompt("hello", output_type=Summary)
                assert result.structured == Summary(verdict="pass", risks=[])
                assert result.conversation_entries is not None
                manager = ContextManager()
                context = manager.initialize(
                    "",
                    append_prompt=False,
                    conversation_entries=result.conversation_entries,
                )
                preparation = manager.prepare_compaction(context, reason="forced")
                assert preparation is not None
                results.append(
                    (
                        [
                            ([m.model_dump_json() for m in messages], tools)
                            for messages, tools in provider.calls
                        ],
                        result.conversation_entries,
                        preparation.kept_messages,
                    )
                )
    before, after = results
    assert after[0] == before[0]
    retry = "Retry with corrected structured output."
    assert before[1][-2].kind == "user"
    assert after[1][-2].kind == "control"
    assert after[1][-2].message == Message.user(retry)
    assert before[2] == [*after[2], Message.user(retry)]
    assert len(after[2]) == 1
    assert (after[2][0].text_content() or "").startswith("hello")
