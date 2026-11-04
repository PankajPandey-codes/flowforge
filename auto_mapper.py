"""
auto_mapper.py — LLM-based matching of test cases to captured HTTP flows.

Uses claude-sonnet-4-6 via the Anthropic SDK (single batched call).
Each captured flow (api_id) is used at most once — duplicates are re-assigned
to the next unclaimed occurrence of the same endpoint.
"""

import json
import os
import re
from collections import defaultdict

try:
    from tools.capture.redaction import redacted_json_snippet
except ImportError:
    from redaction import redacted_json_snippet

try:
    from tools.capture.llm_client import (
        MODEL,
        claude_cli_path,
        call_via_claude_cli,
        output_token_limit,
        stream_message_text,
    )
except ImportError:
    from llm_client import (
        MODEL,
        claude_cli_path,
        call_via_claude_cli,
        output_token_limit,
        stream_message_text,
    )


MIN_MAPPING_OUTPUT_TOKEN_LIMIT = 4096
MAPPING_OUTPUT_TOKENS_PER_TEST_CASE = 300
LARGE_CAPTURE_THRESHOLD = 150
RETRY_BATCH_SIZE = 50
CLI_CHUNK_SIZE = 10       # TCs per parallel CLI call — smaller = faster per subprocess
CLI_MAX_WORKERS = 4       # parallel subprocess limit
MAX_CANDIDATES_PER_TC = 12
MAX_SEMANTIC_CANDIDATES_PER_TC = 8
CLI_MAX_CANDIDATES_PER_TC = 6  # fewer candidates for CLI path to keep prompts small
MIN_SEMANTIC_SCORE = 11
REQUEST_SNIPPET_CHARS = 90
RESPONSE_SNIPPET_CHARS = 200

_STOP_WORDS = {
    "and", "are", "but", "can", "for", "from", "has", "have", "into",
    "that", "the", "this", "with", "when", "then", "will", "should",
    "verify", "validated", "validate", "against", "after", "before",
    "case", "test", "api", "http", "response", "request",
}

# Domain synonyms for words with no morphological relationship (the suffix
# stemming in _tokens already bridges disburse/disbursement on its own —
# these groups exist for pairs like disburse/fund that plain stemming can
# never connect). First word in each group is the canonical form.
_SYNONYM_GROUPS = [
    ("disburse", "fund", "release"),
    ("repay", "settle"),
    ("restructure", "modify", "amend"),
    ("approve", "authorise", "authorize"),
    ("reject", "decline"),
    ("holiday", "moratorium", "defer"),
    ("create", "originate"),
    # "cancel"/"close" deliberately excluded: "close" is too generic in this
    # domain (loan-maturity closure vs. application cancellation are
    # different, sometimes opposite, actions) and collapsing them risks a
    # false-positive match dominating the ranking via the path-token bonus.
]
_SYNONYM_MAP = {word: group[0] for group in _SYNONYM_GROUPS for word in group}


def _apply_synonyms(word_text: str) -> str:
    return " ".join(_SYNONYM_MAP.get(word, word) for word in word_text.split())

SYSTEM_PROMPT = (
    "You are matching API test cases to HTTP traffic captured during manual testing. "
    "Test cases were executed in order — test case 1 ran before test case 2. "
    "IMPORTANT: Each captured API id must be used AT MOST ONCE across all your mappings. "
    "IMPORTANT: A later test case must map to a captured API id greater than the previous "
    "mapped test case's API id. Never map test case N+1 to a request captured before test case N. "
    "The same endpoint (path) may appear multiple times in the captured traffic with different ids — "
    "each occurrence is a separate captured call. "
    "Map test case 1 to the earliest matching call, test case 2 to the NEXT occurrence of its "
    "matching endpoint (a different id), and so on. Never reuse the same id for two test cases. "
    "Return only valid JSON."
)


# Auth mode override — set by server.py before calling any mapping function.
# Values: "auto" (default), "api_key" (force SDK), "cli" (force Claude CLI / SSO).
_AUTH_MODE: str = "auto"


def set_auth_mode(mode: str) -> None:
    global _AUTH_MODE
    if mode in ("auto", "api_key", "cli"):
        _AUTH_MODE = mode


def get_llm_status() -> dict:
    """Return whether normal capture auto-mapping can call an LLM."""
    return _get_llm_status(_AUTH_MODE)


def _get_llm_status(auth_mode: str = "auto") -> dict:
    has_cli = bool(claude_cli_path())

    if auth_mode == "cli":
        if has_cli:
            return {"ok": True, "reason": "cli", "message": "LLM auto-mapping via claude CLI (SSO login).", "model": MODEL}
        return {"ok": False, "reason": "missing_cli", "message": "claude CLI not found — install Claude Code to use SSO auth.", "model": MODEL}

    if auth_mode == "api_key":
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic  # noqa: F401
                return {"ok": True, "reason": "ready", "message": "LLM auto-mapping via Anthropic API key.", "model": MODEL}
            except ImportError:
                return {"ok": False, "reason": "missing_sdk", "message": "anthropic SDK not installed. Run: pip install anthropic", "model": MODEL}
        return {"ok": False, "reason": "missing_api_key", "message": "ANTHROPIC_API_KEY is not set.", "model": MODEL}

    # auto: prefer API key, fall back to CLI
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            msg = "LLM auto-mapping is configured."
            if has_cli:
                msg += " Will fall back to claude CLI on auth failure."
            return {"ok": True, "reason": "ready", "message": msg, "model": MODEL}
        except ImportError:
            if has_cli:
                return {"ok": True, "reason": "cli", "message": "LLM auto-mapping via claude CLI (anthropic SDK not installed).", "model": MODEL}
            return {
                "ok": False,
                "reason": "missing_sdk",
                "message": "The anthropic Python package is not installed. Auto-mapping will be skipped; install it with: pip install anthropic",
                "model": MODEL,
            }

    if has_cli:
        return {"ok": True, "reason": "cli", "message": "LLM auto-mapping via claude CLI (no API key needed).", "model": MODEL}

    return {
        "ok": False,
        "reason": "missing_api_key",
        "message": "ANTHROPIC_API_KEY is not configured and claude CLI was not found. Auto-mapping will be skipped; manual mapping is still available.",
        "model": MODEL,
    }


def _build_user_prompt(test_cases: list, captured_flows: list) -> str:
    lines = ["TEST CASES (from apiInputs CSV):"]
    for tc in test_cases:
        lines.append(
            f"{tc.get('testScenarioNumber', '?')}. {tc.get('testObjective', '')} "
            f"[expected HTTP: {tc.get('expectedResponseCode', 200)}]"
        )

    lines.append("")
    lines.append("CAPTURED API CALLS (chronological — each id is a unique captured call):")
    for flow in captured_flows:
        req_snippet = redacted_json_snippet(flow.get("request_body"), 300, "no body")
        resp_snippet = redacted_json_snippet(flow.get("response_body"), 200)

        lines.append(
            f"id={flow['id']}. {flow['method']} {flow['status']} {flow['path']}"
        )
        lines.append(f"    Request: {req_snippet}")
        if resp_snippet:
            lines.append(f"    Response: {resp_snippet}")

    lines.append("")
    lines.append("Return a JSON array with exactly one object per test case listed above, in test-case order.")
    lines.append("Use api_id=null when a test case has no matching captured request. Each api_id may be used at most once:")
    lines.append('[{"tc": <tc_number>, "api_id": <unique captured id or null>, "confidence": <0-100>, "reason": "<one sentence>"}]')

    return "\n".join(lines)


def _json_snippet(value, limit: int, empty: str = "") -> str:
    if isinstance(value, dict) and value:
        return json.dumps(value, separators=(",", ":"))[:limit]
    if isinstance(value, list) and value:
        return json.dumps(value, separators=(",", ":"))[:limit]
    if isinstance(value, str):
        return value[:limit]
    return empty


def _split_words(text: str) -> str:
    # Turn disburseLoan/getRedrawLimits into searchable words.
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    return re.sub(r"[^A-Za-z0-9]+", " ", spaced).lower()


def _tokens(text: str) -> set:
    words = set()
    for word in _apply_synonyms(_split_words(text)).split():
        if len(word) < 3 or word in _STOP_WORDS:
            continue
        words.add(word)
        if len(word) > 4 and word.endswith("s"):
            words.add(word[:-1])
        if len(word) > 7 and word.endswith("ment"):
            words.add(word[:-4])
        if len(word) > 6 and word.endswith("ing"):
            words.add(word[:-3])
        if len(word) > 5 and word.endswith("ed"):
            words.add(word[:-2])
    return words


def _status_score(expected_status, actual_status) -> int:
    try:
        expected = int(expected_status)
        actual = int(actual_status)
    except (TypeError, ValueError):
        return 0
    if expected == actual:
        return 8
    if expected // 100 == actual // 100:
        return 2
    return -4


def _method_score(objective: str, method: str) -> int:
    obj = objective.lower()
    method = (method or "").upper()
    if method == "POST" and any(w in obj for w in ("create", "add", "initiat", "disburs", "submit")):
        return 4
    if method in {"PUT", "PATCH"} and any(w in obj for w in ("edit", "update", "change", "modify")):
        return 4
    if method == "DELETE" and any(w in obj for w in ("delete", "remove")):
        return 4
    if method == "GET" and any(w in obj for w in ("get", "view", "fetch", "retrieve")):
        return 2
    return 0


def _flow_content_score(tc: dict, flow: dict) -> int:
    """
    Keyword/synonym overlap between the objective and the flow's path/body
    only -- excludes status-code and method-verb coincidence. Separated out
    so a caller can require genuine content relevance specifically, rather
    than an aggregate score a bare status+method match can clear on its own.
    """
    objective = tc.get("testObjective", "")
    tokens = _tokens(objective)
    path_text = _apply_synonyms(_split_words(flow.get("path", "")))
    body_text = _apply_synonyms(_split_words(
        _json_snippet(flow.get("request_body"), 600)
        + " "
        + _json_snippet(flow.get("response_body"), 400)
    ))

    score = 0
    for token in tokens:
        if token in path_text:
            score += 6
        elif token in body_text:
            score += 1
    return score


def _flow_score(tc: dict, flow: dict) -> int:
    objective = tc.get("testObjective", "")
    score = _status_score(tc.get("expectedResponseCode", 200), flow.get("status"))
    score += _method_score(objective, flow.get("method", ""))
    score += _flow_content_score(tc, flow)
    return score


def _chronological_window(flows: list, tc_index: int, tc_count: int) -> list:
    if not flows:
        return []
    avg = max(1, len(flows) // max(1, tc_count))
    window = max(24, avg * 2)
    center = int((tc_index + 0.5) * len(flows) / max(1, tc_count))
    start = max(0, center - (window // 2))
    end = min(len(flows), start + window)
    return flows[start:end]


def _candidate_flows_for_tc(tc: dict, tc_index: int, tc_count: int, captured_flows: list, max_candidates: int = MAX_CANDIDATES_PER_TC) -> list:
    max_semantic = min(MAX_SEMANTIC_CANDIDATES_PER_TC, max_candidates)
    scored = [
        (_flow_score(tc, flow), flow)
        for flow in captured_flows
    ]
    scored.sort(key=lambda item: (-item[0], item[1].get("id", 0)))

    selected = []
    selected_ids = set()

    def add(flow):
        fid = flow.get("id")
        if fid in selected_ids:
            return
        selected.append(flow)
        selected_ids.add(fid)

    for score, flow in scored:
        if score < MIN_SEMANTIC_SCORE or len(selected) >= max_semantic:
            break
        add(flow)

    window = _chronological_window(captured_flows, tc_index, tc_count)
    window_scored = sorted(
        ((_flow_score(tc, flow), flow) for flow in window),
        key=lambda item: (-item[0], item[1].get("id", 0)),
    )
    for _, flow in window_scored:
        if len(selected) >= max_candidates:
            break
        add(flow)

    for score, flow in scored:
        if score <= 0 or len(selected) >= max_candidates:
            break
        add(flow)

    return sorted(selected, key=lambda flow: flow.get("id", 0))


def _build_candidate_prompt(test_cases: list, captured_flows: list, max_candidates: int = MAX_CANDIDATES_PER_TC) -> str:
    lines = [
        "TEST CASES (from apiInputs CSV):",
    ]
    for tc in test_cases:
        lines.append(
            f"{tc.get('testScenarioNumber', '?')}. {tc.get('testObjective', '')} "
            f"[expected HTTP: {tc.get('expectedResponseCode', 200)}]"
        )

    lines.extend([
        "",
        f"CAPTURED API CALLS were pre-ranked from {len(captured_flows)} total captured requests.",
        f"For each test case below, choose the best api_id from that test case's candidate list, or null.",
        "Each id is still a unique chronological captured call. Never reuse the same api_id for two test cases.",
        "A later test case must use an api_id greater than the previous mapped test case's api_id.",
        "",
        "CANDIDATES BY TEST CASE:",
    ])

    tc_count = len(test_cases)
    for idx, tc in enumerate(test_cases):
        tc_num = tc.get("testScenarioNumber", idx + 1)
        lines.append("")
        lines.append(f"TC {tc_num}: {tc.get('testObjective', '')}")
        candidates = _candidate_flows_for_tc(tc, idx, tc_count, captured_flows, max_candidates=max_candidates)
        if not candidates:
            lines.append("  no candidates")
            continue
        for flow in candidates:
            req_snippet = redacted_json_snippet(flow.get("request_body"), REQUEST_SNIPPET_CHARS, "no body")
            resp_snippet = redacted_json_snippet(flow.get("response_body"), RESPONSE_SNIPPET_CHARS)
            lines.append(f"  id={flow['id']}. {flow['method']} {flow['status']} {flow['path']}")
            lines.append(f"      Request: {req_snippet}")
            if resp_snippet:
                lines.append(f"      Response: {resp_snippet}")

    lines.append("")
    lines.append("Return ONLY a JSON array with exactly one object per test case listed above, in test-case order. No markdown, no prose:")
    lines.append("Use api_id=null when a test case has no matching captured request.")
    lines.append('[{"tc": <tc_number>, "api_id": <unique captured id from candidate list or null>, "confidence": <0-100>, "reason": "<one sentence>"}]')
    return "\n".join(lines)


def _build_mapping_prompt(
    test_cases: list,
    captured_flows: list,
    max_candidates: int = MAX_CANDIDATES_PER_TC,
    force_candidate_prompt: bool = False,
) -> str:
    if force_candidate_prompt or len(captured_flows) > LARGE_CAPTURE_THRESHOLD:
        return _build_candidate_prompt(test_cases, captured_flows, max_candidates=max_candidates)
    return _build_user_prompt(test_cases, captured_flows)


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def _parse_json_array(text: str) -> list:
    cleaned = _strip_markdown_fences(text)
    decoder = json.JSONDecoder()
    empty_array = None
    for idx, char in enumerate(cleaned):
        if char != "[":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[idx:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, list):
            continue
        if not value:
            empty_array = value
            continue
        if any(isinstance(item, dict) and ("tc" in item or "api_id" in item) for item in value):
            return value
    if empty_array is not None:
        return empty_array
    raise json.JSONDecodeError("No JSON array found", cleaned, 0)


def _mapping_output_token_limit(client, test_case_count: int) -> int:
    requested = max(
        MIN_MAPPING_OUTPUT_TOKEN_LIMIT,
        int(test_case_count or 1) * MAPPING_OUTPUT_TOKENS_PER_TEST_CASE,
    )
    return min(output_token_limit(client), requested)


def _llm_error_detail(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    status_code = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)

    error_type = None
    message = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error_type = error.get("type")
            message = error.get("message")
        request_id = request_id or body.get("request_id")

    parts = [type(exc).__name__]
    if status_code:
        parts.append(f"HTTP {status_code}")
    if error_type:
        parts.append(str(error_type))
    if message:
        parts.append(str(message))
    elif str(exc):
        parts.append(str(exc)[:300])

    detail = ": ".join(parts)
    if request_id:
        detail += f"; request_id={request_id}"
    return detail


def _is_auth_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 401


def _stream_mapping_text(client, *, system: str, user_prompt: str, test_case_count: int) -> str:
    return stream_message_text(
        client,
        system=system,
        user_prompt=user_prompt,
        temperature=0,
        max_tokens=_mapping_output_token_limit(client, test_case_count),
    )


def _ordered_mappings(mappings: list, test_cases=None) -> list:
    if not test_cases:
        return sorted(
            mappings,
            key=lambda m: (_mapping_tc(m) is None, _mapping_tc(m) or 0),
        )

    by_tc = defaultdict(list)
    for mapping in mappings:
        by_tc[_mapping_tc(mapping)].append(mapping)

    ordered = []
    seen_tcs = set()
    for idx, test_case in enumerate(test_cases):
        tc_num = _tc_num(test_case, idx + 1)
        seen_tcs.add(tc_num)
        if by_tc.get(tc_num):
            ordered.append(by_tc[tc_num].pop(0))

    extra_tcs = sorted(
        tc_num for tc_num in by_tc
        if tc_num is not None and tc_num not in seen_tcs
    )
    for tc_num in extra_tcs:
        ordered.extend(by_tc[tc_num])
    ordered.extend(by_tc.get(None, []))
    return ordered


def _deduplicate(mappings: list, captured_flows: list, test_cases=None) -> list:
    """
    Guarantee each api_id appears at most once and that mapped ids increase
    with test case order. For a duplicate or backwards id, find the next
    unclaimed flow with the same endpoint after the previous mapped id.
    If none is available, set api_id to null.
    """
    flow_by_id = {f["id"]: f for f in captured_flows}

    # (method, path_normalized) → ascending list of flow ids
    by_endpoint: dict = defaultdict(list)
    for f in sorted(captured_flows, key=lambda x: x["id"]):
        key = (f["method"], f.get("path_normalized", f["path"]))
        by_endpoint[key].append(f["id"])

    used: set = set()
    last_api_id = -1
    result = []

    for m in _ordered_mappings(mappings, test_cases):
        if not isinstance(m, dict):
            continue
        api_id = _mapped_api_id(m)
        if api_id != m.get("api_id"):
            m = dict(m)
            m["api_id"] = api_id

        if api_id is None or api_id not in flow_by_id:
            result.append(m)
            continue

        if api_id not in used and api_id > last_api_id:
            used.add(api_id)
            last_api_id = api_id
            result.append(m)
            continue

        # Already claimed or captured before the previous test case — find the
        # next unclaimed occurrence of the same endpoint after last_api_id.
        flow = flow_by_id[api_id]
        key = (flow["method"], flow.get("path_normalized", flow["path"]))
        next_id = next(
            (fid for fid in by_endpoint[key] if fid not in used and fid > last_api_id),
            None,
        )

        new_m = dict(m)
        if next_id is not None:
            new_m["api_id"] = next_id
            used.add(next_id)
            previous_id = last_api_id
            last_api_id = next_id
            if api_id in used:
                why = f"id={api_id} already used by an earlier test case"
            else:
                why = f"id={api_id} is before previous mapped request id={previous_id}"
            new_m["reason"] = (
                f"Re-assigned to id={next_id} ({why}). "
                + new_m.get("reason", "")
            )
        else:
            new_m["api_id"] = None
            new_m["confidence"] = 0
            new_m["reason"] = (
                f"No unclaimed occurrence of {flow['method']} {flow['path']} found after request id={last_api_id}."
            )
        result.append(new_m)

    return result


def _tc_num(test_case: dict, fallback: int = 0) -> int:
    try:
        return int(test_case.get("testScenarioNumber", fallback))
    except (TypeError, ValueError):
        return fallback


def _mapping_tc(mapping: dict):
    if not isinstance(mapping, dict):
        return None
    try:
        return int(mapping.get("tc"))
    except (TypeError, ValueError):
        return None


def _mapped_api_id(mapping: dict):
    if not isinstance(mapping, dict):
        return None
    api_id = mapping.get("api_id")
    if api_id is None or api_id == "":
        return None
    try:
        return int(api_id)
    except (TypeError, ValueError):
        return api_id


def _complete_mappings_for_test_cases(mappings: list, test_cases: list) -> list:
    if not test_cases:
        return mappings

    by_tc = {}
    for mapping in mappings or []:
        tc_num = _mapping_tc(mapping)
        if tc_num is not None and tc_num not in by_tc:
            by_tc[tc_num] = mapping

    completed = []
    for idx, test_case in enumerate(test_cases):
        tc_num = _tc_num(test_case, idx + 1)
        completed.append(
            by_tc.get(tc_num, {
                "tc": tc_num,
                "api_id": None,
                "confidence": 0,
                "reason": "Auto-mapping did not return a match for this test case.",
            })
        )
    return completed


RECONCILE_CONFIDENCE_THRESHOLD = 50
RECONCILE_CONFIDENCE_CAP = 40


def _reconcile_confidences(mappings: list, test_cases: list, captured_flows: list) -> list:
    """
    Cross-check the LLM's self-reported confidence against local keyword/
    synonym overlap between the objective and the flow it picked. The LLM
    and the heuristic never talk to each other otherwise, so a confidently-
    wrong (hallucinated) match can reach the CSV/JSON with nothing to flag
    it. Deliberately checks content overlap alone (_flow_content_score),
    not the full _flow_score -- a bare status-code/method-verb coincidence
    is not evidence the LLM found the right resource, and a flow with
    strong content overlap but a mismatched status (e.g. a negative test
    case expecting an error) shouldn't be flagged just because the status
    penalty drags the aggregate score down.
    A high-confidence pick with zero content overlap is downgraded so it
    surfaces for manual review (confidence < 50 renders red in the mapping
    table) instead of being trusted at face value.
    """
    tc_by_num = {_tc_num(tc, idx + 1): tc for idx, tc in enumerate(test_cases)}
    flow_by_id = {f["id"]: f for f in captured_flows}

    reconciled = []
    for mapping in mappings:
        tc_num = _mapping_tc(mapping)
        api_id = _mapped_api_id(mapping)
        confidence = mapping.get("confidence", 0) if isinstance(mapping, dict) else 0
        tc = tc_by_num.get(tc_num)
        flow = flow_by_id.get(api_id) if api_id is not None else None

        if tc is None or flow is None or confidence < RECONCILE_CONFIDENCE_THRESHOLD:
            reconciled.append(mapping)
            continue

        score = _flow_content_score(tc, flow)
        if score > 0:
            reconciled.append(mapping)
            continue

        adjusted = dict(mapping)
        adjusted["confidence"] = min(confidence, RECONCILE_CONFIDENCE_CAP)
        adjusted["reason"] = (
            f"No keyword overlap between the test objective and this flow's path/body "
            f"despite LLM confidence {confidence} — please double-check this mapping. "
            + mapping.get("reason", "")
        ).strip()
        reconciled.append(adjusted)

    return reconciled


def retry_map_test_cases_batch(
    test_cases: list,
    captured_flows: list,
    existing_mappings=None,
    start_index: int = 0,
    batch_size: int = RETRY_BATCH_SIZE,
    paused: bool = False,
) -> dict:
    """
    Process one retry-mapping batch. The caller can invoke this repeatedly
    from the UI to show progressive mapping updates after every 50 flows.
    """
    preserved_mappings = existing_mappings or []
    total_count = len(test_cases or [])
    flow_count = len(captured_flows or [])
    batch_size = max(1, int(batch_size or RETRY_BATCH_SIZE))
    start_index = max(0, int(start_index or 0))
    end_index = min(flow_count, start_index + batch_size)
    batch_number = (start_index // batch_size) + 1
    total_batches = (flow_count + batch_size - 1) // batch_size if flow_count else 0

    def empty_response(warning=None, is_paused=False):
        mapped_count = sum(1 for m in preserved_mappings if _mapped_api_id(m) is not None)
        return {
            "mappings": preserved_mappings,
            "warning": warning,
            "mapped_count": mapped_count,
            "total_count": total_count,
            "new_mapped_count": 0,
            "batch_start": start_index,
            "batch_end": end_index,
            "batch_size": batch_size,
            "batch_number": batch_number,
            "total_batches": total_batches,
            "next_start_index": end_index,
            "complete": bool(total_count) and mapped_count == total_count,
            "done": end_index >= flow_count or (bool(total_count) and mapped_count == total_count),
            "paused": is_paused,
        }

    if paused:
        return empty_response(is_paused=True)

    if not test_cases or not captured_flows:
        return empty_response()

    status = get_llm_status()
    if not status["ok"]:
        return empty_response(status["message"])

    test_case_order = [_tc_num(tc, idx + 1) for idx, tc in enumerate(test_cases)]
    mapped_by_tc = {}
    used_api_ids = set()

    for mapping in preserved_mappings:
        tc_num = _mapping_tc(mapping)
        api_id = _mapped_api_id(mapping)
        if tc_num is None or api_id is None:
            continue
        mapped = dict(mapping)
        mapped["tc"] = tc_num
        mapped["api_id"] = api_id
        mapped_by_tc[tc_num] = mapped
        used_api_ids.add(api_id)

    previous_mapped_count = len(mapped_by_tc)
    remaining_cases = [
        tc for idx, tc in enumerate(test_cases)
        if _tc_num(tc, idx + 1) not in mapped_by_tc
    ]
    # Everything scanned so far, not a disjoint start_index:end_index window —
    # a remaining TC's real match may sit earlier than this round's window,
    # and _build_candidate_prompt (forced below) scales with test-case count
    # rather than flow count, so there's no size reason to withhold it.
    batch = [
        flow for flow in captured_flows[:end_index]
        if flow.get("id") not in used_api_ids
    ]

    warning = None
    if remaining_cases and batch:
        result = map_test_cases_with_diagnostics(remaining_cases, batch, force_candidate_prompt=True)
        if result.get("warning"):
            warning = result["warning"]
        else:
            batch_flow_ids = {flow.get("id") for flow in batch}
            for mapping in result.get("mappings", []):
                tc_num = _mapping_tc(mapping)
                api_id = _mapped_api_id(mapping)
                if tc_num is None or api_id is None:
                    continue
                if tc_num in mapped_by_tc or api_id in used_api_ids:
                    continue
                if api_id not in batch_flow_ids:
                    continue
                mapped = dict(mapping)
                mapped["tc"] = tc_num
                mapped["api_id"] = api_id
                mapped_by_tc[tc_num] = mapped
                used_api_ids.add(api_id)

    final_mappings = []
    for idx, tc in enumerate(test_cases):
        tc_num = test_case_order[idx]
        final_mappings.append(
            mapped_by_tc.get(tc_num, {
                "tc": tc_num,
                "api_id": None,
                "confidence": 0,
                "reason": "Retry mapping has not found a captured request yet.",
            })
        )

    final_mappings = _deduplicate(final_mappings, captured_flows, test_cases)
    mapped_count = sum(1 for m in final_mappings if _mapped_api_id(m) is not None)

    return {
        "mappings": final_mappings,
        "warning": warning,
        "mapped_count": mapped_count,
        "total_count": total_count,
        "new_mapped_count": max(0, mapped_count - previous_mapped_count),
        "batch_start": start_index,
        "batch_end": end_index,
        "batch_size": batch_size,
        "batch_number": batch_number,
        "total_batches": total_batches,
        "next_start_index": end_index,
        "complete": mapped_count == total_count,
        "done": end_index >= flow_count or mapped_count == total_count,
        "paused": False,
    }


def retry_map_test_cases_in_batches(
    test_cases: list,
    captured_flows: list,
    existing_mappings=None,
    batch_size: int = RETRY_BATCH_SIZE,
) -> dict:
    """
    Retry mapping by sending all remaining test cases with 50 captured requests
    at a time. Stops early once every test case has a mapped api_id.
    """
    preserved_mappings = existing_mappings or []
    preserved_count = sum(1 for m in preserved_mappings if _mapped_api_id(m) is not None)
    total_count = len(test_cases or [])

    if not test_cases or not captured_flows:
        return {
            "mappings": preserved_mappings,
            "warning": None,
            "mapped_count": preserved_count,
            "total_count": total_count,
            "batches_processed": 0,
            "complete": bool(total_count) and preserved_count == total_count,
        }

    status = get_llm_status()
    if not status["ok"]:
        return {
            "mappings": preserved_mappings,
            "warning": status["message"],
            "mapped_count": preserved_count,
            "total_count": total_count,
            "batches_processed": 0,
            "complete": preserved_count == total_count,
        }

    test_case_order = [_tc_num(tc, idx + 1) for idx, tc in enumerate(test_cases)]
    mapped_by_tc = {}
    used_api_ids = set()

    for mapping in existing_mappings or []:
        tc_num = _mapping_tc(mapping)
        api_id = _mapped_api_id(mapping)
        if tc_num is None or api_id is None:
            continue
        mapped = dict(mapping)
        mapped["tc"] = tc_num
        mapped["api_id"] = api_id
        mapped_by_tc[tc_num] = mapped
        used_api_ids.add(api_id)

    warnings = []
    batches_processed = 0
    batch_size = max(1, int(batch_size or RETRY_BATCH_SIZE))

    for start in range(0, len(captured_flows), batch_size):
        remaining_cases = [
            tc for idx, tc in enumerate(test_cases)
            if _tc_num(tc, idx + 1) not in mapped_by_tc
        ]
        if not remaining_cases:
            break

        # Everything scanned so far, not a disjoint start:start+batch_size
        # window — see the matching comment in retry_map_test_cases_batch.
        batch = [
            flow for flow in captured_flows[:start + batch_size]
            if flow.get("id") not in used_api_ids
        ]
        if not batch:
            continue

        batches_processed += 1
        result = map_test_cases_with_diagnostics(remaining_cases, batch, force_candidate_prompt=True)
        if result.get("warning"):
            warnings.append(result["warning"])
            continue

        batch_flow_ids = {flow.get("id") for flow in batch}
        for mapping in result.get("mappings", []):
            tc_num = _mapping_tc(mapping)
            api_id = _mapped_api_id(mapping)
            if tc_num is None or api_id is None:
                continue
            if tc_num in mapped_by_tc or api_id in used_api_ids:
                continue
            if api_id not in batch_flow_ids:
                continue
            mapped = dict(mapping)
            mapped["tc"] = tc_num
            mapped["api_id"] = api_id
            mapped_by_tc[tc_num] = mapped
            used_api_ids.add(api_id)

    final_mappings = []
    for idx, tc in enumerate(test_cases):
        tc_num = test_case_order[idx]
        final_mappings.append(
            mapped_by_tc.get(tc_num, {
                "tc": tc_num,
                "api_id": None,
                "confidence": 0,
                "reason": "Retry mapping did not find a confident captured request.",
            })
        )

    final_mappings = _deduplicate(final_mappings, captured_flows, test_cases)
    mapped_count = sum(1 for m in final_mappings if _mapped_api_id(m) is not None)
    warning = None
    if mapped_count < total_count:
        warning = (
            f"Retry mapping processed {batches_processed} batch(es) of up to {batch_size} captured requests "
            f"and mapped {mapped_count}/{total_count} test cases. Please map the remaining cases manually."
        )
        if warnings:
            warning += f" Last LLM warning: {warnings[-1]}"

    return {
        "mappings": final_mappings,
        "warning": warning,
        "mapped_count": mapped_count,
        "total_count": total_count,
        "batches_processed": batches_processed,
        "complete": mapped_count == total_count,
    }


def _map_via_cli_parallel(test_cases: list, captured_flows: list, force_candidate_prompt: bool = False) -> list:
    """
    Split test cases into CLI_CHUNK_SIZE groups, call claude CLI in parallel,
    then merge. Faster than a single large call for 25+ test cases.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    chunks = [
        test_cases[i: i + CLI_CHUNK_SIZE]
        for i in range(0, len(test_cases), CLI_CHUNK_SIZE)
    ]

    if len(chunks) == 1:
        prompt = _build_mapping_prompt(test_cases, captured_flows, force_candidate_prompt=force_candidate_prompt)
        raw = call_via_claude_cli(SYSTEM_PROMPT, prompt)
        return _parse_json_array(raw)

    def _map_chunk(chunk):
        prompt = _build_mapping_prompt(
            chunk, captured_flows, max_candidates=CLI_MAX_CANDIDATES_PER_TC, force_candidate_prompt=force_candidate_prompt
        )
        raw = call_via_claude_cli(SYSTEM_PROMPT, prompt)
        return _parse_json_array(raw)

    all_mappings = []
    with ThreadPoolExecutor(max_workers=min(CLI_MAX_WORKERS, len(chunks))) as pool:
        futures = {pool.submit(_map_chunk, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            result = future.result()  # raises on error — caller handles
            if isinstance(result, list):
                all_mappings.extend(result)

    return all_mappings


def map_test_cases(test_cases: list, captured_flows: list) -> list:
    """
    Match test cases to captured flows via a single Claude API call, then
    post-process to ensure each api_id is used at most once.

    Returns [] if ANTHROPIC_API_KEY is missing or the API call fails.
    """
    return map_test_cases_with_diagnostics(test_cases, captured_flows).get("mappings", [])


def map_test_cases_with_diagnostics(test_cases: list, captured_flows: list, force_candidate_prompt: bool = False) -> dict:
    """
    Match test cases to captured flows and return mappings plus a user-facing
    warning when LLM auto-mapping was unavailable or failed.
    """
    if not test_cases or not captured_flows:
        return {"mappings": [], "warning": None}

    status = get_llm_status()
    if not status["ok"]:
        return {"mappings": [], "warning": status["message"]}

    client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        except ImportError:
            return {"mappings": [], "warning": get_llm_status()["message"]}

    user_prompt = _build_mapping_prompt(test_cases, captured_flows, force_candidate_prompt=force_candidate_prompt)

    def _attempt(use_client):
        if use_client is None:
            # CLI path: parallel chunks for speed
            mappings = _map_via_cli_parallel(test_cases, captured_flows, force_candidate_prompt=force_candidate_prompt)
        else:
            raw = _stream_mapping_text(
                use_client,
                system=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                test_case_count=len(test_cases),
            )
            mappings = _parse_json_array(raw)
        if not isinstance(mappings, list):
            raise ValueError("LLM response was not a JSON array")
        mappings = _deduplicate(mappings, captured_flows, test_cases)
        mappings = _complete_mappings_for_test_cases(mappings, test_cases)
        mappings = _reconcile_confidences(mappings, test_cases, captured_flows)
        return {"mappings": mappings, "warning": None}

    try:
        return _attempt(client)
    except json.JSONDecodeError:
        if client is not None and claude_cli_path():
            try:
                return _attempt(None)
            except Exception:
                pass
        return {
            "mappings": [],
            "warning": "Auto-mapping failed because the LLM response was not valid JSON. Please map manually.",
        }
    except Exception as e:
        # On auth failure, silently retry via claude CLI before surfacing the error
        if client is not None and _is_auth_error(e) and claude_cli_path():
            try:
                return _attempt(None)
            except Exception as cli_e:
                return {
                    "mappings": [],
                    "warning": f"Auto-mapping failed: SDK auth error and CLI fallback also failed ({cli_e}). Please map manually.",
                }
        return {
            "mappings": [],
            "warning": f"Auto-mapping failed while calling Anthropic ({_llm_error_detail(e)}). Please map manually.",
        }
