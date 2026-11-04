import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import server


def _flow(fid):
    return {"id": fid, "method": "GET", "path": "/x", "path_normalized": "/x", "status": 200,
            "request_body": {}, "response_body": {}}


def _tc(num):
    return {"testScenarioNumber": str(num), "testObjective": f"TC {num}", "expectedResponseCode": 200}


def _auto_mapper():
    # Mirrors the try/except import-resolution order server.py itself uses,
    # so patches land on the exact module object the route will call.
    try:
        import tools.capture.auto_mapper as auto_mapper
    except ImportError:
        import auto_mapper
    return auto_mapper


def test_no_fallback_when_initial_pass_is_already_complete(monkeypatch):
    auto_mapper = _auto_mapper()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not run the 50-flow fallback when the initial pass is already complete")

    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: {"ok": True, "reason": "ready"})
    monkeypatch.setattr(
        auto_mapper, "map_test_cases_with_diagnostics",
        lambda tcs, flows: {"mappings": [{"tc": 1, "api_id": 5, "confidence": 90}], "warning": None},
    )
    monkeypatch.setattr(auto_mapper, "retry_map_test_cases_in_batches", fail_if_called)

    mappings, warning = server._run_initial_auto_mapping([_tc(1)], [_flow(5)])

    assert mappings == [{"tc": 1, "api_id": 5, "confidence": 90}]
    assert warning is None


def test_no_fallback_for_cli_reason_even_when_incomplete(monkeypatch):
    """The CLI path already handles any prompt size in one shot -- batching is irrelevant to it."""
    auto_mapper = _auto_mapper()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not run the 50-flow fallback on the CLI path")

    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: {"ok": True, "reason": "cli"})
    monkeypatch.setattr(
        auto_mapper, "map_test_cases_with_diagnostics",
        lambda tcs, flows: {"mappings": [{"tc": 1, "api_id": None, "confidence": 0}], "warning": "incomplete"},
    )
    monkeypatch.setattr(auto_mapper, "retry_map_test_cases_in_batches", fail_if_called)

    mappings, warning = server._run_initial_auto_mapping([_tc(1)], [_flow(5)])

    assert warning == "incomplete"


def test_fallback_runs_on_sdk_path_when_incomplete_and_wins_if_better(monkeypatch):
    auto_mapper = _auto_mapper()
    fallback_calls = []

    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: {"ok": True, "reason": "ready"})
    monkeypatch.setattr(
        auto_mapper, "map_test_cases_with_diagnostics",
        lambda tcs, flows: {"mappings": [{"tc": 1, "api_id": None, "confidence": 0}, {"tc": 2, "api_id": None, "confidence": 0}], "warning": "incomplete"},
    )

    def fake_retry(test_cases, flows, existing_mappings, batch_size):
        fallback_calls.append(batch_size)
        return {
            "mappings": [{"tc": 1, "api_id": 5, "confidence": 90}, {"tc": 2, "api_id": 6, "confidence": 90}],
            "warning": None,
        }

    monkeypatch.setattr(auto_mapper, "retry_map_test_cases_in_batches", fake_retry)

    mappings, warning = server._run_initial_auto_mapping([_tc(1), _tc(2)], [_flow(5), _flow(6)])

    assert fallback_calls == [50]
    assert warning is None
    assert all(m["api_id"] is not None for m in mappings)


def test_fallback_result_discarded_if_worse_than_initial(monkeypatch):
    auto_mapper = _auto_mapper()

    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: {"ok": True, "reason": "ready"})
    monkeypatch.setattr(
        auto_mapper, "map_test_cases_with_diagnostics",
        lambda tcs, flows: {
            "mappings": [{"tc": 1, "api_id": 5, "confidence": 90}, {"tc": 2, "api_id": None, "confidence": 0}],
            "warning": "incomplete",
        },
    )
    monkeypatch.setattr(
        auto_mapper, "retry_map_test_cases_in_batches",
        lambda tcs, flows, existing_mappings, batch_size: {
            "mappings": [{"tc": 1, "api_id": None, "confidence": 0}, {"tc": 2, "api_id": None, "confidence": 0}],
            "warning": "worse",
        },
    )

    mappings, warning = server._run_initial_auto_mapping([_tc(1), _tc(2)], [_flow(5), _flow(6)])

    # The fallback mapped 0/2, strictly worse than the initial pass's 1/2 --
    # the initial (better) result must win, not be silently discarded.
    assert warning == "incomplete"
    assert mappings[0]["api_id"] == 5
