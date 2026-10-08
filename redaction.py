"""
redaction.py — Mask captured request/response body values before they're
embedded in a prompt sent to an external LLM API.

Captured flows can contain real values from a dev-environment banking
transaction (borrower names, addresses, amounts). The mapping LLM mostly
needs shape — field names, nesting, list length — to recognise which
endpoint a call belongs to, so leaf values are replaced with type-preserving
placeholders before anything is serialised into a prompt. This must never be
used on the locally-stored captured flow or on what artifact_generator.py
writes to disk — only on the copy of the data that leaves the process.

Identifier/reference fields (loanAccountNumber, accountNo, id, loanId, ...)
are exempt from masking. Many structurally-identical calls (e.g. dozens of
GET .../loanaccount/{id} requests, same path shape, same method) are only
distinguishable by exactly this kind of value in the response body -- with
it masked, the LLM has no way to tell which captured call belongs to which
test case, and mapping accuracy collapses for every objective that relies
on one of these generic reads. Identifiers are reference keys, not PII or
financial data, and are already visible in URLs, tickets, and screenshots
across the org -- redacting them protects nothing while breaking mapping.
"""

import json

_STRING_PLACEHOLDER = "<redacted>"
_NON_JSON_PLACEHOLDER = "<non-JSON body redacted, {length} chars>"

_IDENTIFIER_KEY_SUBSTRINGS = (
    "identifier", "accountno", "accountnumber", "loanaccountnumber",
    "referencenumber", "loannumber",
)


def _is_identifier_key(key) -> bool:
    if not isinstance(key, str):
        return False
    if key.lower() == "id" or key.endswith("Id") or key.endswith("ID"):
        return True
    lowered = key.lower()
    return any(needle in lowered for needle in _IDENTIFIER_KEY_SUBSTRINGS)


def redact_value(value, _key=None):
    """Recursively replace scalar leaf values, keeping keys/shape intact.

    Values under an identifier-like key (see _is_identifier_key) are left
    untouched -- see the module docstring for why.
    """
    if isinstance(value, dict):
        return {k: redact_value(v, _key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, _key=_key) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if _is_identifier_key(_key):
        return value
    if isinstance(value, (int, float)):
        return 0
    if isinstance(value, str):
        return _STRING_PLACEHOLDER
    return value


def redacted_json_snippet(value, limit: int, empty: str = "") -> str:
    """
    Build a size-limited, redacted snippet of a captured body suitable for
    an outbound LLM prompt. Mirrors keys/shape without exposing real values.
    """
    if isinstance(value, (dict, list)) and value:
        return json.dumps(redact_value(value), separators=(",", ":"))[:limit]
    if isinstance(value, str) and value.strip():
        if limit <= 0:
            return empty
        return _NON_JSON_PLACEHOLDER.format(length=len(value))[:limit]
    return empty
