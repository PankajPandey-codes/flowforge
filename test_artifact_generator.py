import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from artifact_generator import generate


def _step(tc_number, objective="obj", status=200, path="/loans/1", request_body=None, response_body=None):
    return {
        "tc_number": tc_number,
        "test_objective": objective,
        "corrected_status": status,
        "captured_api": {
            "method": "GET",
            "path": path,
            "status": status,
            "request_body": request_body or {},
            "response_body": response_body if response_body is not None else {},
        },
        "common_ignore": [],
    }


def _confirmed(steps, append_mode=False, api_name="TestFlow"):
    return {
        "api_name": api_name,
        "module": "TestModule",
        "manifest": "A",
        "owner": "qa",
        "scenario": "y",
        "steps": steps,
        "append_mode": append_mode,
    }


def test_append_mode_upserts_existing_tc_instead_of_duplicating(tmp_path):
    project_root = str(tmp_path)

    generate(_confirmed([_step("1"), _step("2")], append_mode=False), project_root)

    # Re-capture TC 2 in a later append round with a different payload.
    generate(
        _confirmed([_step("2", objective="updated", request_body={"amount": 500})], append_mode=True),
        project_root,
    )

    api_inputs_path = os.path.join(project_root, "apiInputs", "TestFlow.csv")
    with open(api_inputs_path, newline="", encoding="utf-8") as fh:
        data_rows = list(csv.reader(fh))[1:]

    tc_numbers = [r[0] for r in data_rows]
    assert tc_numbers == ["1", "2"]
    assert dict(zip(tc_numbers, (r[1] for r in data_rows)))["2"] == "updated"

    payload_path = os.path.join(project_root, "fileFromJson", "payloadJSON", "TestFlow.json")
    with open(payload_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert [e["apiName"] for e in payload] == ["TestFlow1", "TestFlow2"]
    assert payload[1]["payload"] == {"amount": 500}

    expected_path = os.path.join(project_root, "fileFromJson", "expectedJSON", "TestFlow.json")
    with open(expected_path, encoding="utf-8") as fh:
        expected = json.load(fh)
    assert [e["apiName"] for e in expected] == ["TestFlow1", "TestFlow2"]


def test_append_mode_sorts_numerically_not_lexicographically(tmp_path):
    project_root = str(tmp_path)

    generate(_confirmed([_step("1"), _step("10")], append_mode=False), project_root)
    generate(_confirmed([_step("2")], append_mode=True), project_root)

    api_inputs_path = os.path.join(project_root, "apiInputs", "TestFlow.csv")
    with open(api_inputs_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))

    assert [r[0] for r in rows[1:]] == ["1", "2", "10"]
