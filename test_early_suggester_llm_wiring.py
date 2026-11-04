import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import early_suggester as es


def test_suggest_get_from_post_routes_through_shared_stream_message_text(monkeypatch):
    """
    suggest_get_from_post used to bypass the shared streaming helper
    entirely, calling client.messages.create(model="claude-sonnet-4-6", ...)
    directly with a hardcoded model string one line below the MODEL
    constant -- exactly the kind of duplicate that drifts silently on a
    model bump. It must now go through stream_message_text like every
    other LLM call in this module, so there's a single place the model
    name and retry/backoff behavior live.
    """
    calls = []

    def fake_stream(client, *, system, user_prompt, max_tokens=None):
        calls.append({"system": system, "max_tokens": max_tokens})
        return '{"suggested_path": "/loans/{id}", "suggested_response": {"id": 0}}'

    monkeypatch.setattr(es, "_ANTHROPIC_KEY", "fake-key-for-test")
    monkeypatch.setattr(es, "stream_message_text", fake_stream)
    monkeypatch.setattr("anthropic.Anthropic", lambda api_key: object(), raising=False)

    result = es.suggest_get_from_post(
        {"path": "/loans", "request_body": {"amount": 100}},
        {"testObjective": "Get the loan"},
    )

    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 1024
    assert result["suggested_path"] == "/loans/{id}"


def test_suggest_get_from_post_cli_path_uses_the_shared_short_timeout(monkeypatch):
    calls = []

    def fake_cli(system, user_prompt, timeout=None):
        calls.append(timeout)
        return '{"suggested_path": "/loans/{id}", "suggested_response": {"id": 0}}'

    monkeypatch.setattr(es, "_ANTHROPIC_KEY", "")
    monkeypatch.setattr(es, "claude_cli_path", lambda: "/usr/bin/claude")
    monkeypatch.setattr(es, "call_via_claude_cli", fake_cli)

    es.suggest_get_from_post(
        {"path": "/loans", "request_body": {"amount": 100}},
        {"testObjective": "Get the loan"},
    )

    assert calls == [es._CLI_TIMEOUT]
