import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import auto_mapper


def _flow(fid, method="GET", path="/loans/1"):
    return {
        "id": fid,
        "method": method,
        "path": path,
        "path_normalized": path,
        "status": 200,
        "request_body": {},
        "response_body": {},
    }


def _tc(num):
    return {"testScenarioNumber": str(num), "testObjective": f"TC {num}", "expectedResponseCode": 200}


OK_STATUS = {"ok": True, "reason": "ready", "message": "ready", "model": "x"}


# ---------------------------------------------------------------------------
# Bug 2 — retry batches must use ranked candidates, not a flat unranked dump,
# and must see everything scanned so far, not a disjoint 50-flow window.
# ---------------------------------------------------------------------------

def test_build_mapping_prompt_forces_candidate_style_regardless_of_flow_count():
    flows = [_flow(1)]  # far below LARGE_CAPTURE_THRESHOLD
    test_cases = [_tc(1)]

    plain = auto_mapper._build_mapping_prompt(test_cases, flows)
    forced = auto_mapper._build_mapping_prompt(test_cases, flows, force_candidate_prompt=True)

    assert "CANDIDATES BY TEST CASE" not in plain
    assert "CANDIDATES BY TEST CASE" in forced


def test_retry_batch_uses_ranked_candidate_prompt_and_the_full_scanned_range(monkeypatch):
    calls = []

    def fake_map(test_cases, captured_flows, force_candidate_prompt=False):
        calls.append({
            "flow_ids": [f["id"] for f in captured_flows],
            "force_candidate_prompt": force_candidate_prompt,
        })
        return {"mappings": [], "warning": None}

    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: OK_STATUS)
    monkeypatch.setattr(auto_mapper, "map_test_cases_with_diagnostics", fake_map)

    flows = [_flow(i) for i in range(1, 101)]
    test_cases = [_tc(i) for i in range(1, 6)]

    auto_mapper.retry_map_test_cases_batch(test_cases, flows, start_index=50, batch_size=50)

    assert len(calls) == 1
    assert calls[0]["force_candidate_prompt"] is True
    # Scanned-so-far means everything up to end_index (1..100), not just the
    # disjoint 51..100 window — a TC whose real match sits earlier must still
    # be visible to the ranker.
    assert calls[0]["flow_ids"][0] == 1
    assert calls[0]["flow_ids"][-1] == 100


def test_retry_in_batches_also_uses_ranked_candidate_prompt(monkeypatch):
    calls = []

    def fake_map(test_cases, captured_flows, force_candidate_prompt=False):
        calls.append({
            "flow_ids": [f["id"] for f in captured_flows],
            "force_candidate_prompt": force_candidate_prompt,
        })
        return {"mappings": [], "warning": None}

    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: OK_STATUS)
    monkeypatch.setattr(auto_mapper, "map_test_cases_with_diagnostics", fake_map)

    flows = [_flow(i) for i in range(1, 101)]
    test_cases = [_tc(i) for i in range(1, 6)]

    auto_mapper.retry_map_test_cases_in_batches(test_cases, flows, batch_size=50)

    assert calls[0]["force_candidate_prompt"] is True
    assert calls[0]["flow_ids"][0] == 1


# ---------------------------------------------------------------------------
# Bug 3 — pause must actually short-circuit before any LLM call.
# ---------------------------------------------------------------------------

def test_retry_batch_short_circuits_when_paused_without_calling_the_llm(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not call the LLM while paused")

    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: OK_STATUS)
    monkeypatch.setattr(auto_mapper, "map_test_cases_with_diagnostics", fail_if_called)

    existing = [{"tc": 1, "api_id": 5, "confidence": 100, "reason": "manual"}]
    flows = [_flow(i) for i in range(1, 101)]
    test_cases = [_tc(1), _tc(2)]

    result = auto_mapper.retry_map_test_cases_batch(
        test_cases, flows, existing_mappings=existing, start_index=50, batch_size=50, paused=True,
    )

    assert result["paused"] is True
    assert result["mappings"] == existing
