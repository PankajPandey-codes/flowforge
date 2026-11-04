import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import proxy_manager


class _FakeCompletedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode


class _FakePopen:
    def __init__(self, poll_result=None, pid=12345):
        self._poll_result = poll_result
        self.pid = pid

    def poll(self):
        return self._poll_result


def _patch_common(monkeypatch, session_file, poll_result=None, chrome_returncode=0):
    """Stub every subprocess/filesystem/timing side effect proxy_manager touches."""
    monkeypatch.setattr(proxy_manager, "SESSION_FILE", str(session_file))
    monkeypatch.setattr(proxy_manager.time, "sleep", lambda *_: None)
    monkeypatch.setattr(proxy_manager.subprocess, "Popen", lambda *a, **kw: _FakePopen(poll_result))
    monkeypatch.setattr(proxy_manager.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(chrome_returncode))


def test_start_capture_reports_not_ok_when_mitmdump_exits_immediately(tmp_path, monkeypatch):
    """
    A dead mitmdump process (e.g. port 8080 already in use -- the exact
    failure SETUP.md documents as "will fail silently") must not be
    reported as a successful capture start just because Chrome opened.
    """
    _patch_common(monkeypatch, tmp_path / "session.json", poll_result=1, chrome_returncode=0)

    session = proxy_manager.start_capture("/tmp/flows.ndjson")

    assert session["proxy_ok"] is False
    assert session["mitmdump_ok"] is False


def test_start_capture_reports_ok_when_mitmdump_and_chrome_both_succeed(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path / "session.json", poll_result=None, chrome_returncode=0)

    session = proxy_manager.start_capture("/tmp/flows.ndjson")

    assert session["proxy_ok"] is True
    assert session["mitmdump_ok"] is True


def test_start_capture_reports_not_ok_when_chrome_launch_fails_even_if_mitmdump_alive(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path / "session.json", poll_result=None, chrome_returncode=1)

    session = proxy_manager.start_capture("/tmp/flows.ndjson")

    assert session["proxy_ok"] is False
    assert session["mitmdump_ok"] is True


def test_start_capture_writes_session_file(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    _patch_common(monkeypatch, session_file, poll_result=None, chrome_returncode=0)

    proxy_manager.start_capture("/tmp/flows.ndjson", capture_host="dev.example.com")

    with open(session_file) as fh:
        saved = json.load(fh)
    assert saved["flows_file"] == "/tmp/flows.ndjson"
    assert saved["proxy_ok"] is True


def test_get_flow_count_counts_ndjson_lines(tmp_path):
    flows_file = tmp_path / "flows.ndjson"
    flows_file.write_text('{"id": 1}\n{"id": 2}\n{"id": 3}\n')

    assert proxy_manager.get_flow_count(str(flows_file)) == 3


def test_get_flow_count_missing_file_returns_zero(tmp_path):
    assert proxy_manager.get_flow_count(str(tmp_path / "missing.ndjson")) == 0


def test_get_flow_count_skips_blank_lines(tmp_path):
    flows_file = tmp_path / "flows.ndjson"
    flows_file.write_text('{"id": 1}\n\n{"id": 2}\n')

    assert proxy_manager.get_flow_count(str(flows_file)) == 2


def test_stop_capture_reads_session_file_and_adds_stop_time(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"flows_file": "/tmp/x.ndjson"}))
    monkeypatch.setattr(proxy_manager, "SESSION_FILE", str(session_file))
    monkeypatch.setattr(proxy_manager.time, "sleep", lambda *_: None)
    monkeypatch.setattr(proxy_manager.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(0))

    session = proxy_manager.stop_capture()

    assert session["flows_file"] == "/tmp/x.ndjson"
    assert "stop_time" in session


def test_stop_capture_handles_missing_session_file(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_manager, "SESSION_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(proxy_manager.time, "sleep", lambda *_: None)
    monkeypatch.setattr(proxy_manager.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(0))

    session = proxy_manager.stop_capture()

    assert "stop_time" in session
