import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import auto_mapper


# ---------------------------------------------------------------------------
# 1. Status-class mismatches should be penalized, not just unrewarded.
# ---------------------------------------------------------------------------

def test_status_score_exact_match_unchanged():
    assert auto_mapper._status_score(200, 200) == 8


def test_status_score_same_class_unchanged():
    assert auto_mapper._status_score(200, 201) == 2


def test_status_score_class_mismatch_is_penalized():
    score = auto_mapper._status_score(200, 404)
    assert score < 0


def test_status_score_invalid_input_stays_neutral():
    assert auto_mapper._status_score("not-a-number", 200) == 0


# ---------------------------------------------------------------------------
# 2. Domain synonym matching — different-root words the existing suffix
#    stemmer can never bridge (disburse vs fund is not a prefix relationship).
# ---------------------------------------------------------------------------

def _flow(fid, method="POST", path="/loans/1", status=200, request_body=None, response_body=None):
    return {
        "id": fid,
        "method": method,
        "path": path,
        "path_normalized": path,
        "status": status,
        "request_body": request_body or {},
        "response_body": response_body or {},
    }


def test_flow_score_matches_synonym_in_path_not_just_literal_word():
    tc = {"testObjective": "Disburse the loan amount", "expectedResponseCode": 200}
    synonym_flow = _flow(1, method="POST", path="/loans/fund", status=200)
    unrelated_flow = _flow(2, method="POST", path="/loans/notes", status=200)

    assert auto_mapper._flow_score(tc, synonym_flow) > auto_mapper._flow_score(tc, unrelated_flow)


def test_flow_score_matches_synonym_repay_settle():
    tc = {"testObjective": "Repay the outstanding balance", "expectedResponseCode": 200}
    synonym_flow = _flow(1, method="POST", path="/loans/settle", status=200)
    unrelated_flow = _flow(2, method="POST", path="/loans/notes", status=200)

    assert auto_mapper._flow_score(tc, synonym_flow) > auto_mapper._flow_score(tc, unrelated_flow)


# ---------------------------------------------------------------------------
# 3. Reconcile LLM-reported confidence against the local scoring heuristic.
# ---------------------------------------------------------------------------

def test_reconcile_confidences_downgrades_high_confidence_low_score_pick():
    test_cases = [{"testScenarioNumber": "1", "testObjective": "Disburse the loan", "expectedResponseCode": 200}]
    captured_flows = [_flow(5, method="GET", path="/unrelated/endpoint", status=404)]
    mappings = [{"tc": 1, "api_id": 5, "confidence": 90, "reason": "LLM picked this"}]

    reconciled = auto_mapper._reconcile_confidences(mappings, test_cases, captured_flows)

    assert reconciled[0]["confidence"] < 90
    assert "reason" in reconciled[0]


def test_reconcile_confidences_leaves_well_scoring_matches_untouched():
    test_cases = [{"testScenarioNumber": "1", "testObjective": "Disburse the loan", "expectedResponseCode": 200}]
    captured_flows = [_flow(5, method="POST", path="/loans/disburse", status=200)]
    mappings = [{"tc": 1, "api_id": 5, "confidence": 90, "reason": "LLM picked this"}]

    reconciled = auto_mapper._reconcile_confidences(mappings, test_cases, captured_flows)

    assert reconciled[0]["confidence"] == 90


def test_reconcile_confidences_ignores_unmapped_rows():
    test_cases = [{"testScenarioNumber": "1", "testObjective": "Disburse the loan", "expectedResponseCode": 200}]
    captured_flows = [_flow(5)]
    mappings = [{"tc": 1, "api_id": None, "confidence": 0, "reason": "no match"}]

    reconciled = auto_mapper._reconcile_confidences(mappings, test_cases, captured_flows)

    assert reconciled[0]["api_id"] is None
    assert reconciled[0]["confidence"] == 0


def test_reconcile_confidences_catches_a_status_and_method_only_coincidence():
    """
    A flow that happens to share the expected status code and a
    method/verb heuristic match, but has zero actual content overlap with
    the objective, must still be flagged — a bare status+method coincidence
    is not evidence the LLM picked the right resource.
    """
    test_cases = [{"testScenarioNumber": "1", "testObjective": "Create a new loan application", "expectedResponseCode": 201}]
    # POST + "create" satisfies _method_score; 201 satisfies _status_score exactly.
    # The path/body share nothing with the objective.
    captured_flows = [_flow(5, method="POST", path="/totally/unrelated/thing", status=201)]
    mappings = [{"tc": 1, "api_id": 5, "confidence": 85, "reason": "LLM picked this"}]

    reconciled = auto_mapper._reconcile_confidences(mappings, test_cases, captured_flows)

    assert reconciled[0]["confidence"] < 85


def test_reconcile_confidences_does_not_double_penalize_wrong_status_strong_content_match():
    """
    A flow whose status doesn't match but whose path/body strongly overlaps
    with the objective (e.g. a negative/stresser test case expecting an
    error) must not be flagged purely because the status-class penalty
    drags the aggregate _flow_score down -- reconciliation should judge
    content relevance, not the aggregate score.
    """
    test_cases = [{"testScenarioNumber": "1", "testObjective": "Disburse the loan with insufficient funds", "expectedResponseCode": 400}]
    captured_flows = [_flow(5, method="POST", path="/loans/disburse", status=200)]
    mappings = [{"tc": 1, "api_id": 5, "confidence": 85, "reason": "LLM picked this"}]

    reconciled = auto_mapper._reconcile_confidences(mappings, test_cases, captured_flows)

    assert reconciled[0]["confidence"] == 85


# ---------------------------------------------------------------------------
# 4. Synonym false positives — "close" is too generic a word to safely
#    collapse into "cancel"; a loan-maturity-closure objective must not
#    gain match credit against a cancellation endpoint.
# ---------------------------------------------------------------------------

def test_close_and_cancel_are_not_treated_as_synonyms():
    tc = {"testObjective": "Verify the loan account closes correctly at maturity", "expectedResponseCode": 200}
    cancellation_flow = _flow(1, method="POST", path="/applications/cancel", status=200)
    # Genuinely unrelated -- must not share "loan"/"account" substrings with
    # the objective, or a coincidental domain-vocabulary match (unrelated to
    # the synonym fix) would mask what this test is actually checking.
    unrelated_flow = _flow(2, method="POST", path="/system/health", status=200)

    assert auto_mapper._flow_score(tc, cancellation_flow) == auto_mapper._flow_score(tc, unrelated_flow)
