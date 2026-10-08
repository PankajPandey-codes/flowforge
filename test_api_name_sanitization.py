import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from artifact_generator import generate, is_valid_api_name


def _step(tc_number=1):
    return {
        "tc_number": tc_number,
        "test_objective": "obj",
        "corrected_status": 200,
        "captured_api": {"method": "GET", "path": "/x", "status": 200, "request_body": {}, "response_body": {}},
        "common_ignore": [],
    }


def _confirmed(api_name):
    return {
        "api_name": api_name,
        "module": "TestModule",
        "manifest": "A",
        "owner": "qa",
        "scenario": "y",
        "steps": [_step()],
        "append_mode": False,
    }


@pytest.mark.parametrize("name", ["TestFlow", "Test_Flow_2", "ABC123"])
def test_is_valid_api_name_accepts_plain_identifiers(name):
    assert is_valid_api_name(name) is True


@pytest.mark.parametrize("name", [
    "../../../../tmp/pwned",
    "foo/bar",
    "foo\\bar",
    "foo.bar",
    "foo bar",
    "",
    None,
])
def test_is_valid_api_name_rejects_anything_not_a_plain_identifier(name):
    assert is_valid_api_name(name) is False


def test_generate_refuses_a_path_traversal_api_name_and_writes_nothing_outside_project_root(tmp_path):
    project_root = str(tmp_path)
    escape_target = tmp_path.parent / "pwned.csv"
    if escape_target.exists():
        escape_target.unlink()

    with pytest.raises(ValueError):
        generate(_confirmed("../pwned"), project_root)

    assert not escape_target.exists()


def test_generate_refuses_a_slash_containing_api_name(tmp_path):
    with pytest.raises(ValueError):
        generate(_confirmed("some/nested/name"), str(tmp_path))
