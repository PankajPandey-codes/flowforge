import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import server


def test_append_mode_survives_a_server_restart(tmp_path, monkeypatch):
    """
    _save_state()/_load_state() exist so the tool survives a restart between
    capture rounds. append_mode must round-trip too, otherwise resuming an
    append round after a restart silently falls back to a fresh capture and
    duplicates the manifest row for an already-recorded api_name.
    """
    monkeypatch.setattr(server, "STATE_FILE", str(tmp_path / "state.json"))

    server.STATE["append_mode"] = True
    server.STATE["start_from_case"] = 25
    server._save_state()

    # Simulate the process restarting: fresh STATE at its hardcoded defaults,
    # then the startup path reloads whatever was persisted.
    server.STATE["append_mode"] = False
    server.STATE["start_from_case"] = 1
    server._load_state()

    assert server.STATE["append_mode"] is True
    assert server.STATE["start_from_case"] == 25
