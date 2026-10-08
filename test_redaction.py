import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from redaction import redact_value, redacted_json_snippet


def test_redact_value_masks_strings_and_numbers_but_keeps_keys():
    original = {
        "borrowerName": "Jane Doe",
        "amount": 15000.50,
        "active": True,
        "notes": None,
    }

    redacted = redact_value(original)

    assert set(redacted.keys()) == set(original.keys())
    assert redacted["borrowerName"] == "<redacted>"
    assert redacted["amount"] == 0
    assert redacted["active"] is True
    assert redacted["notes"] is None


def test_redact_value_preserves_identifier_fields_needed_to_disambiguate_calls():
    """
    Real regression found in production use: many structurally-identical
    GET calls (same path shape, same method, e.g. GET /loanaccount/{id})
    are only distinguishable by an identifier value buried in the response
    body (e.g. loanAccountNumber). Blanket-redacting that value made every
    such call look identical to the LLM, and mapping accuracy collapsed to
    0% for every objective that relied on one of these generic GETs.
    Identifiers are reference keys, not PII/financial data -- they're
    already visible in URLs, screenshots, and tickets across the org.
    """
    original = {
        "loanAccountNumber": "BH38559GH",
        "accountNumber": "12345678",
        "accountNo": "ACC-001",
        "externalId": "EXT-9",
        "identifier": 8142,
        "id": 8142,
        "loanId": 8142,
        "borrowerName": "Jane Doe",
        "amount": 15000.50,
    }

    redacted = redact_value(original)

    assert redacted["loanAccountNumber"] == "BH38559GH"
    assert redacted["accountNumber"] == "12345678"
    assert redacted["accountNo"] == "ACC-001"
    assert redacted["externalId"] == "EXT-9"
    assert redacted["identifier"] == 8142
    assert redacted["id"] == 8142
    assert redacted["loanId"] == 8142
    # Genuinely sensitive fields must still be masked.
    assert redacted["borrowerName"] == "<redacted>"
    assert redacted["amount"] == 0


def test_redact_value_identifier_whitelist_is_case_insensitive_and_nested():
    original = {"loan": {"LoanAccountNumber": "BH38560GH", "borrower": "Jane Doe"}}

    redacted = redact_value(original)

    assert redacted["loan"]["LoanAccountNumber"] == "BH38560GH"
    assert redacted["loan"]["borrower"] == "<redacted>"


def test_redact_value_does_not_whitelist_unrelated_fields_containing_id_substring():
    """
    A naive substring match on "id" would also match "avoid", "consider",
    "grid" etc. -- confirms the whitelist only matches genuine identifier
    field names, not any key that happens to contain "id" as a substring.
    """
    original = {"avoidDoubleBooking": "some sensitive note", "considerations": "private detail"}

    redacted = redact_value(original)

    assert redacted["avoidDoubleBooking"] == "<redacted>"
    assert redacted["considerations"] == "<redacted>"


def test_redact_value_recurses_into_nested_dicts_and_lists():
    original = {
        "loan": {"id": 42, "borrower": "Jane Doe"},
        "history": [{"amount": 100}, {"amount": 200}],
    }

    redacted = redact_value(original)

    assert redacted["loan"]["id"] == 42  # identifier key -- preserved, see test_redact_value_preserves_identifier_fields...
    assert redacted["loan"]["borrower"] == "<redacted>"
    assert len(redacted["history"]) == 2
    assert all(entry["amount"] == 0 for entry in redacted["history"])


def test_redacted_json_snippet_hides_real_values_but_keeps_field_names():
    body = {"borrowerName": "Jane Doe", "loanAmount": 50000}

    snippet = redacted_json_snippet(body, limit=300)

    assert "Jane Doe" not in snippet
    assert "50000" not in snippet
    assert "borrowerName" in snippet
    assert "loanAmount" in snippet


def test_redacted_json_snippet_respects_char_limit():
    body = {"field": "x" * 500}

    snippet = redacted_json_snippet(body, limit=20)

    assert len(snippet) <= 20


def test_redacted_json_snippet_empty_body_returns_empty_marker():
    assert redacted_json_snippet({}, limit=300, empty="no body") == "no body"
    assert redacted_json_snippet(None, limit=300, empty="no body") == "no body"


def test_redacted_json_snippet_non_json_string_body_is_not_echoed_verbatim():
    raw_text = "account=12345678&balance=99999"

    snippet = redacted_json_snippet(raw_text, limit=300)

    assert "12345678" not in snippet
    assert "99999" not in snippet
