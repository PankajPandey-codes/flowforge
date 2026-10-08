import importlib
import json
import os
import sys

from mitmproxy.test import tflow

sys.path.insert(0, os.path.dirname(__file__))


def _reload_addon(monkeypatch, flows_json, capture_host="dev.example.internal"):
    monkeypatch.setenv("MITM_FLOWS_JSON", str(flows_json))
    monkeypatch.setenv("CAPTURE_HOST", capture_host)
    import mitm_addon
    return importlib.reload(mitm_addon)


def _flow(path="/api/loans/1", method="GET", host="dev.example.internal",
          status=200, request_body=b"{}", response_body=b'{"ok": true}'):
    flow = tflow.tflow(resp=True)
    flow.request.method = method
    flow.request.host = host
    flow.request.path = path
    flow.request.content = request_body
    flow.response.status_code = status
    flow.response.content = response_body
    flow.response.headers["content-type"] = "application/json"
    return flow


def test_response_appends_one_ndjson_line_per_flow_without_rewriting_prior_lines(tmp_path, monkeypatch):
    flows_json = tmp_path / "flows.ndjson"
    addon = _reload_addon(monkeypatch, flows_json)

    addon.response(_flow(path="/api/loans/1"))
    first_write_content = flows_json.read_text()

    addon.response(_flow(path="/api/loans/2"))
    second_write_content = flows_json.read_text()

    # The bytes written for the first flow must be untouched by the second
    # write — proof the addon appends rather than rewriting the whole file.
    assert second_write_content.startswith(first_write_content)

    lines = [line for line in second_write_content.splitlines() if line.strip()]
    assert len(lines) == 2
    entries = [json.loads(line) for line in lines]
    assert [e["path"] for e in entries] == ["/api/loans/1", "/api/loans/2"]
    assert [e["sequence"] for e in entries] == [1, 2]


def test_response_skips_disallowed_host(tmp_path, monkeypatch):
    flows_json = tmp_path / "flows.ndjson"
    addon = _reload_addon(monkeypatch, flows_json)

    addon.response(_flow(host="untrusted.example.com"))

    assert not flows_json.exists()


def test_flows_file_is_created_with_owner_only_permissions(tmp_path, monkeypatch):
    """
    Captured flows contain real, unredacted request/response bodies from a
    dev-environment banking transaction. STATE_FILE (server.py) is already
    locked down to 0o600 -- the flows file must match, not inherit whatever
    the process umask happens to leave it at.
    """
    flows_json = tmp_path / "flows.ndjson"
    addon = _reload_addon(monkeypatch, flows_json)

    addon.response(_flow(path="/api/loans/1"))

    mode = os.stat(flows_json).st_mode & 0o777
    assert mode == 0o600
