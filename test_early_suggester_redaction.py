import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from early_suggester import _build_get_suggestion_prompt


def test_get_suggestion_prompt_never_includes_real_captured_values():
    post_request = {"borrowerName": "Jane Doe", "loanAccountNumber": "BH38559GH"}

    prompt = _build_get_suggestion_prompt(
        post_path="/loans/disburse",
        post_request=post_request,
        get_objective="Verify the disbursed loan can be retrieved",
    )

    assert "Jane Doe" not in prompt
    assert "borrowerName" in prompt
    # Identifier values are intentionally preserved -- see redaction.py's
    # module docstring for why.
    assert "BH38559GH" in prompt
    assert "/loans/disburse" in prompt
    assert "Verify the disbursed loan can be retrieved" in prompt
