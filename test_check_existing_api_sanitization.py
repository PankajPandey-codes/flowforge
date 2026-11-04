import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import server


def test_check_existing_api_rejects_path_traversal_api_name():
    client = server.app.test_client()

    resp = client.get("/api/check-existing-api", query_string={"api_name": "../../../../etc/passwd"})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["found"] is False


def test_check_existing_api_accepts_a_plain_identifier(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT_ROOT", str(tmp_path))

    client = server.app.test_client()
    resp = client.get("/api/check-existing-api", query_string={"api_name": "SomeValidName"})
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["found"] is False  # files don't exist in the empty tmp_path, but the name itself is accepted
    assert "missing" in data
