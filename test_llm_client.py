import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import llm_client


class _FakeException(Exception):
    def __init__(self, status_code=None, body=None, message=""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_is_transient_llm_error_true_for_known_retryable_status_codes():
    for code in (429, 500, 502, 503, 504):
        assert llm_client.is_transient_llm_error(_FakeException(status_code=code)) is True


def test_is_transient_llm_error_false_for_client_error():
    assert llm_client.is_transient_llm_error(_FakeException(status_code=400)) is False


def test_is_transient_llm_error_true_for_overload_in_error_body():
    exc = _FakeException(body={"error": {"type": "overloaded_error"}})
    assert llm_client.is_transient_llm_error(exc) is True


def test_output_token_limit_falls_back_to_default_when_client_is_none(monkeypatch):
    monkeypatch.setattr(llm_client, "_OUTPUT_TOKEN_LIMIT_CACHE", None)
    assert llm_client.output_token_limit(None) == llm_client.DEFAULT_OUTPUT_TOKEN_LIMIT


def test_output_token_limit_caches_across_calls(monkeypatch):
    monkeypatch.setattr(llm_client, "_OUTPUT_TOKEN_LIMIT_CACHE", None)
    calls = []

    class FakeModelInfo:
        max_tokens = 64000

    class FakeModels:
        def retrieve(self, model):
            calls.append(model)
            return FakeModelInfo()

    class FakeClient:
        models = FakeModels()

    first = llm_client.output_token_limit(FakeClient())
    second = llm_client.output_token_limit(FakeClient())

    assert first == 64000
    assert second == 64000
    assert len(calls) == 1  # second call served from cache, no second retrieve()


def test_stream_message_text_retries_on_transient_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm_client, "_OUTPUT_TOKEN_LIMIT_CACHE", 1000)

    attempts = {"count": 0}

    class FakeStream:
        def __init__(self, should_fail):
            self._should_fail = should_fail

        def __enter__(self):
            if self._should_fail:
                raise _FakeException(status_code=429)
            return self

        def __exit__(self, *a):
            return False

        @property
        def text_stream(self):
            return iter(["hello"])

    class FakeMessages:
        def stream(self, **kwargs):
            attempts["count"] += 1
            return FakeStream(should_fail=attempts["count"] == 1)

    class FakeClient:
        messages = FakeMessages()

    result = llm_client.stream_message_text(FakeClient(), system="sys", user_prompt="hi")

    assert result == "hello"
    assert attempts["count"] == 2


def test_stream_message_text_raises_immediately_on_non_transient_error(monkeypatch):
    monkeypatch.setattr(llm_client, "_OUTPUT_TOKEN_LIMIT_CACHE", 1000)

    class FakeStream:
        def __enter__(self):
            raise _FakeException(status_code=400)

        def __exit__(self, *a):
            return False

    class FakeMessages:
        def stream(self, **kwargs):
            return FakeStream()

    class FakeClient:
        messages = FakeMessages()

    try:
        llm_client.stream_message_text(FakeClient(), system="sys", user_prompt="hi")
        assert False, "expected the non-transient error to propagate"
    except _FakeException:
        pass


def test_stream_message_text_falls_back_to_cli_when_client_is_none(monkeypatch):
    monkeypatch.setattr(llm_client, "call_via_claude_cli", lambda system, user_prompt: "cli-response")

    result = llm_client.stream_message_text(None, system="sys", user_prompt="hi")

    assert result == "cli-response"
