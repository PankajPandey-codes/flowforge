import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from auto_mapper import _build_user_prompt, _build_candidate_prompt, _flow_score


def _flow(fid, method="POST", path="/loans/disburse", status=200,
          request_body=None, response_body=None, path_normalized=None):
    return {
        "id": fid,
        "method": method,
        "path": path,
        "path_normalized": path_normalized or path,
        "status": status,
        "request_body": request_body or {},
        "response_body": response_body or {},
    }


SENSITIVE_FLOW = _flow(
    1,
    request_body={"borrowerName": "Jane Doe", "loanAccountNumber": "BH38559GH"},
    response_body={"borrowerName": "Jane Doe", "loanAmount": 50000},
)

TEST_CASES = [{"testScenarioNumber": "1", "testObjective": "Disburse loan", "expectedResponseCode": 200}]


def test_build_user_prompt_never_includes_real_captured_values():
    prompt = _build_user_prompt(TEST_CASES, [SENSITIVE_FLOW])

    assert "Jane Doe" not in prompt
    assert "50000" not in prompt
    # Field names must survive — the LLM still needs shape to recognise the endpoint.
    assert "borrowerName" in prompt
    # Identifier values are intentionally preserved -- many structurally
    # identical captured calls are only distinguishable by a value like
    # this, and it's a reference key, not PII/financial data.
    assert "BH38559GH" in prompt


def test_build_candidate_prompt_never_includes_real_captured_values():
    many_test_cases = TEST_CASES * 1  # candidate prompt path is exercised directly regardless of count
    prompt = _build_candidate_prompt(many_test_cases, [SENSITIVE_FLOW])

    assert "Jane Doe" not in prompt
    assert "borrowerName" in prompt


def test_build_candidate_prompt_preserves_response_body_identifier_value():
    """
    Many captured GET .../loanaccount/{opaque-id} calls are structurally
    identical -- the only thing that disambiguates them is an identifier
    value like loanAccountNumber, which lives in the response body. If the
    response snippet is stripped to empty, the candidate prompt is blind to
    it regardless of what redaction's identifier whitelist allows through.
    """
    flow = _flow(
        4, method="GET", path="/loanaccount/12345", status=200,
        response_body={"loanAccountNumber": "BH38560GH", "borrowerName": "Jane Doe"},
    )
    prompt = _build_candidate_prompt(TEST_CASES, [flow])

    assert "Jane Doe" not in prompt
    assert "BH38560GH" in prompt


def test_local_candidate_scoring_still_uses_real_unredacted_values():
    """
    _flow_score never leaves the process (used only to rank candidates
    locally) — redaction must not blind it, or mapping accuracy regresses.
    """
    tc = {"testScenarioNumber": "1", "testObjective": "disburse the loan", "expectedResponseCode": 200}
    flow_with_matching_body = _flow(2, method="GET", path="/unrelated",
                                     request_body={"action": "disburse"})
    flow_without_match = _flow(3, method="GET", path="/unrelated", request_body={"action": "noop"})

    assert _flow_score(tc, flow_with_matching_body) > _flow_score(tc, flow_without_match)
