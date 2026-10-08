import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import server


def _flow(fid):
    return {"id": fid, "method": "GET", "path": "/x", "path_normalized": "/x", "status": 200,
            "request_body": {}, "response_body": {}}


def _tc(num):
    return {"testScenarioNumber": str(num), "testObjective": f"TC {num}", "expectedResponseCode": 200}


def test_pause_requested_is_honored_on_the_next_batch_without_clearing_it_first(monkeypatch):
    """
    STATE["pause_requested"] used to be reset to False unconditionally at the
    top of every /api/retry-mapping call -- including the very next
    paginated batch call after the user clicked Pause. That meant the flag
    could never survive long enough for a response to actually report
    paused=True. It must only be cleared at the start of a fresh run
    (start_index == 0), and a pending pause must be honored (and reported)
    on the very next batch call rather than silently wiped first.
    """
    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not call the LLM while paused")

    # server.py resolves tools.capture.auto_mapper first, falling back to a
    # bare auto_mapper import only if that fails -- patch whichever module
    # object it will actually resolve, or the patch silently no-ops against
    # an unrelated duplicate module instance.
    try:
        import tools.capture.auto_mapper as auto_mapper
    except ImportError:
        import auto_mapper
    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: {"ok": True, "reason": "ready", "message": "", "model": "x"})
    monkeypatch.setattr(auto_mapper, "map_test_cases_with_diagnostics", fail_if_called)

    server.STATE["parsed_flows"] = [_flow(i) for i in range(1, 101)]
    server.STATE["test_cases"] = [_tc(1), _tc(2)]
    server.STATE["pause_requested"] = True  # simulates: user paused after the previous batch

    client = server.app.test_client()
    resp = client.post("/api/retry-mapping", json={
        "existing_mappings": [],
        "start_index": 50,  # a continuation batch, NOT a fresh run
        "batch_size": 50,
    })
    data = resp.get_json()

    assert data["ok"] is True
    assert data["paused"] is True


def test_retry_mapping_persists_progress_so_it_survives_a_server_restart(tmp_path, monkeypatch):
    """
    STATE["mappings"] updated by a retry batch must be written to disk via
    _save_state(), the same way /api/stop-capture and /api/confirm already
    do -- otherwise a server restart mid-retry silently loses everything
    the retry run mapped so far, the same class of bug already fixed for
    append_mode/start_from_case in _STATE_KEYS.
    """
    monkeypatch.setattr(server, "STATE_FILE", str(tmp_path / "state.json"))

    try:
        import tools.capture.auto_mapper as auto_mapper
    except ImportError:
        import auto_mapper

    def fake_map(test_cases, captured_flows, force_candidate_prompt=False):
        return {"mappings": [{"tc": 1, "api_id": 5, "confidence": 90, "reason": "x"}], "warning": None}

    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: {"ok": True, "reason": "ready", "message": "", "model": "x"})
    monkeypatch.setattr(auto_mapper, "map_test_cases_with_diagnostics", fake_map)

    server.STATE["parsed_flows"] = [_flow(i) for i in range(1, 11)]
    server.STATE["test_cases"] = [_tc(1)]
    server.STATE["mappings"] = []

    client = server.app.test_client()
    resp = client.post("/api/retry-mapping", json={"existing_mappings": [], "start_index": 0, "batch_size": 50})
    assert resp.get_json()["ok"] is True

    # Simulate a server restart: fresh STATE, then the startup path reloads
    # whatever was persisted.
    server.STATE["mappings"] = []
    server._load_state()

    assert server.STATE["mappings"] and server.STATE["mappings"][0]["api_id"] == 5


def test_retry_mapping_defaults_start_index_to_a_fresh_run_when_omitted(monkeypatch):
    """
    The only caller (app.js) always sends start_index, but the route
    shouldn't 500 if it's ever omitted -- treat that the same as a fresh
    run (start_index=0) rather than requiring the field.
    """
    try:
        import tools.capture.auto_mapper as auto_mapper
    except ImportError:
        import auto_mapper
    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: {"ok": False, "reason": "missing_api_key", "message": "no key", "model": "x"})

    server.STATE["parsed_flows"] = [_flow(1)]
    server.STATE["test_cases"] = [_tc(1)]

    client = server.app.test_client()
    resp = client.post("/api/retry-mapping", json={"existing_mappings": []})
    data = resp.get_json()

    assert data["ok"] is True
    assert data["batch_start"] == 0


def test_starting_a_fresh_run_clears_a_stale_pause_flag(monkeypatch):
    server.STATE["parsed_flows"] = [_flow(i) for i in range(1, 11)]
    server.STATE["test_cases"] = [_tc(1)]
    server.STATE["pause_requested"] = True  # stale from a previous, unrelated run

    try:
        import tools.capture.auto_mapper as auto_mapper
    except ImportError:
        import auto_mapper
    monkeypatch.setattr(auto_mapper, "get_llm_status", lambda: {"ok": False, "reason": "missing_api_key", "message": "no key", "model": "x"})

    client = server.app.test_client()
    resp = client.post("/api/retry-mapping", json={
        "existing_mappings": [],
        "start_index": 0,  # a fresh run
        "batch_size": 50,
    })
    data = resp.get_json()

    assert data["ok"] is True
    assert data["paused"] is False
