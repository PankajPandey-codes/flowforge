"""
early_suggester.py — Early Automation support module.

Handles three tasks:
  1. Parse an OpenAPI 3.0 spec (JSON or YAML) → flat endpoint pool
  2. Scan existing regression suite payloadJSON/expectedJSON → existing API pool
  3. LLM hybrid-map test cases to best match across both pools
  4. Generate skeleton request/response dicts from OpenAPI schemas
  5. Re-suggest a GET row when its linked POST is edited
"""

import csv
import glob
import json
import os
import re

try:
    from tools.capture.redaction import redacted_json_snippet
except ImportError:
    from redaction import redacted_json_snippet

try:
    from tools.capture.llm_client import (
        MODEL,
        claude_cli_path,
        call_via_claude_cli,
        stream_message_text,
    )
except ImportError:
    from llm_client import (
        MODEL,
        claude_cli_path,
        call_via_claude_cli,
        stream_message_text,
    )

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ---------------------------------------------------------------------------
# 1. OpenAPI spec parsing
# ---------------------------------------------------------------------------

def parse_openapi_spec_from_string(spec_text: str) -> list:
    """
    Parse an OpenAPI 3.0 spec from a raw string (JSON or YAML).
    Returns a list of endpoint dicts with resolved request/response schemas.
    """
    spec = _load_spec(spec_text)
    if not spec:
        return []

    components = spec.get("components", {})
    paths = spec.get("paths", {})
    endpoints = []

    for path, path_item in paths.items():
        for method in ("get", "post", "put", "patch", "delete"):
            op = path_item.get(method)
            if not op:
                continue

            req_schema = _extract_request_schema(op, components)
            resp_schema = _extract_response_schema(op, components)

            endpoints.append({
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary", op.get("operationId", "")),
                "request_schema": req_schema,
                "response_schema": resp_schema,
                "components": components,
                "source": "spec",
            })

    return endpoints


def _load_spec(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except Exception:
        return {}


def _resolve_ref(ref: str, components: dict) -> dict:
    """Resolve a $ref like '#/components/schemas/Foo' against components."""
    if not ref.startswith("#/components/schemas/"):
        return {}
    name = ref.split("/")[-1]
    return components.get("schemas", {}).get(name, {})


def _extract_request_schema(op: dict, components: dict) -> dict:
    body = op.get("requestBody", {})
    content = body.get("content", {})
    for ct in ("application/json", "text/json", "*/*"):
        schema = content.get(ct, {}).get("schema")
        if schema:
            return _resolve_if_ref(schema, components)
    return {}


def _extract_response_schema(op: dict, components: dict) -> dict:
    responses = op.get("responses", {})
    for code in ("200", "201", "202"):
        resp = responses.get(code, {})
        content = resp.get("content", {})
        for ct in ("application/json", "text/json", "*/*"):
            schema = content.get(ct, {}).get("schema")
            if schema:
                return _resolve_if_ref(schema, components)
    return {}


def _resolve_if_ref(schema: dict, components: dict) -> dict:
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], components)
        # Inline one level of properties from the resolved schema
        return resolved
    return schema


# ---------------------------------------------------------------------------
# 2. Skeleton generation from OpenAPI schema
# ---------------------------------------------------------------------------

def generate_skeleton(schema: dict, components: dict = None, _depth: int = 0) -> object:
    """
    Recursively build a skeleton dict/list from an OpenAPI schema.
    - Required string fields → "<FILL_ME>"
    - Uses 'example' if present
    - Caps recursion at depth 8 to avoid infinite loops on circular refs
    """
    if _depth > 8 or not schema:
        return ""

    components = components or {}

    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], components)
        return generate_skeleton(resolved, components, _depth + 1)

    if "example" in schema:
        return schema["example"]

    schema_type = schema.get("type", "object")

    if schema_type == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = schema.get("required", [])
        result = {}
        for k, v in props.items():
            child = generate_skeleton(v, components, _depth + 1)
            # Only include required fields + first 10 optional ones
            if k in required or len(result) < 10:
                result[k] = child
        return result if result else {}

    if schema_type == "array":
        items_schema = schema.get("items", {})
        return [generate_skeleton(items_schema, components, _depth + 1)]

    if schema_type in ("integer", "number"):
        return 0

    if schema_type == "boolean":
        return False

    # string — use first enum value if available, otherwise empty string
    if "enum" in schema:
        return schema["enum"][0]
    return ""


# ---------------------------------------------------------------------------
# 3. Scan existing regression suite
# ---------------------------------------------------------------------------

def scan_existing_apis(project_root: str) -> list:
    """
    Scan fileFromJson/payloadJSON/*.json and corresponding expectedJSON + apiInputs CSVs.
    Returns a flat list of step dicts — one entry per step per API file.
    LLM will pick the best-matching step.
    """
    payload_dir = os.path.join(project_root, "fileFromJson", "payloadJSON")
    expected_dir = os.path.join(project_root, "fileFromJson", "expectedJSON")
    inputs_dir = os.path.join(project_root, "apiInputs")

    results = []

    for payload_path in sorted(glob.glob(os.path.join(payload_dir, "*.json"))):
        base = os.path.splitext(os.path.basename(payload_path))[0]  # e.g. "RedemptionPast"

        try:
            with open(payload_path, encoding="utf-8") as f:
                payload_steps = json.load(f)
        except Exception:
            continue

        # Load matching expected JSON (same base name, may have suffix _response or _request)
        expected_steps = _load_expected(expected_dir, base)

        # Load matching apiInputs CSV for test objectives
        objectives = _load_objectives(inputs_dir, base)

        for step_entry in payload_steps:
            api_name = step_entry.get("apiName", "")  # e.g. "RedemptionPast1"
            step_num = _extract_step_num(api_name, base)
            method = step_entry.get("apiMethod", "GET").upper()
            path = step_entry.get("apiPath", "")
            payload = step_entry.get("payload", {})
            if isinstance(payload, list) and not payload:
                payload = {}

            # Find matching expected entry
            expected_body = {}
            for exp in expected_steps:
                if exp.get("apiName", "") == api_name:
                    expected_body = exp.get("expected", {})
                    break

            objective = objectives.get(step_num, "")

            results.append({
                "api_name": base,
                "api_name_full": api_name,
                "step": step_num,
                "method": method,
                "path": path,
                "request_body": payload,
                "response_body": expected_body,
                "test_objective": objective,
                "source": "existing",
            })

    return results


def _load_expected(expected_dir: str, base: str) -> list:
    for suffix in ("", "_response", "Response", "_Response"):
        path = os.path.join(expected_dir, f"{base}{suffix}.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return []


def _load_objectives(inputs_dir: str, base: str) -> dict:
    """Returns {step_num: testObjective} from apiInputs/<base>.csv."""
    csv_path = os.path.join(inputs_dir, f"{base}.csv")
    if not os.path.exists(csv_path):
        return {}
    objectives = {}
    try:
        with open(csv_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                cleaned = {k.strip(): v.strip() for k, v in row.items() if k}
                num = int(cleaned.get("testScenarioNumber", 0) or 0)
                obj = cleaned.get("testObjective", "")
                if num:
                    objectives[num] = obj
    except Exception:
        pass
    return objectives


def _extract_step_num(api_name_full: str, base: str) -> int:
    """Extract trailing integer from 'RedemptionPast3' → 3."""
    suffix = api_name_full[len(base):]
    try:
        return int(suffix)
    except ValueError:
        return 1


# ---------------------------------------------------------------------------
# 4. LLM hybrid mapping — two-phase (spec-first, then existing)
# ---------------------------------------------------------------------------

_SPEC_PHASE_SYSTEM = (
    "You are deciding which test cases call endpoints defined in an OpenAPI spec.\n\n"
    "Rules:\n"
    "- Evaluate each test case independently against every spec endpoint.\n"
    "- Multiple test cases MAY match the same spec endpoint — this is expected and correct.\n"
    "- Only return a match when the spec endpoint clearly describes the same resource AND action\n"
    "  as the test objective. Path segments must reflect the same domain concept.\n"
    "- A shared HTTP method alone is NOT sufficient — the resource must also match.\n"
    "- Set api_key=null and confidence=0 when no spec endpoint is a genuine match.\n"
    "Return ONLY a valid JSON array — no markdown, no explanation."
)

_EXISTING_PHASE_SYSTEM = (
    "You are matching test cases to the most semantically correct endpoint in an existing\n"
    "API regression suite.\n\n"
    "Rules:\n"
    "1. OBJECTIVE MATCH FIRST: If an endpoint has an 'objective' that is semantically identical\n"
    "   or very close to the test case description, that endpoint is the correct match.\n"
    "   Give such matches confidence >= 85.\n"
    "2. PATH must reflect the same domain concept as the test objective.\n"
    "   A shared HTTP method alone is NOT sufficient.\n"
    "3. Multiple test cases MAY match the same endpoint and step — this is expected.\n"
    "4. PATH SPECIFICITY: Sub-segments like /combinedbatch, /close, /payout, /approve, /disburse\n"
    "   indicate SPECIALISED operations. Only match these when the test objective explicitly\n"
    "   names that specific operation. For a generic 'Create X' objective, prefer the base\n"
    "   resource path (e.g. POST /loanaccount) over POST /loanaccount/combinedbatch.\n"
    "5. Numeric path segments (e.g. /loanaccount/7228) are record IDs — treat as /loanaccount/{id}.\n"
    "6. Set api_key=null and confidence=0 when no endpoint genuinely fits.\n"
    "   Do not force a weak match — null is better than wrong.\n"
    "Return ONLY a valid JSON array — no markdown, no explanation."
)


_CLI_TIMEOUT = 180  # early_suggester's LLM calls are small (mapping/skeleton prompts) — no need for auto_mapper's 600s budget


def _llm_call(system: str, user: str) -> str:
    if _ANTHROPIC_KEY:
        import anthropic
        client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
        raw = stream_message_text(client, system=system, user_prompt=user).strip()
    else:
        raw = call_via_claude_cli(system, user, timeout=_CLI_TIMEOUT).strip()
    return _extract_json_from_response(raw)


def _extract_json_from_response(text: str) -> str:
    """
    Extract the first JSON array or object from an LLM response, handling
    cases where the model wraps JSON in markdown fences or adds explanation text.
    """
    # Try stripping simple leading/trailing fences first
    stripped = re.sub(r"^```[a-z]*\n?", "", text.strip())
    stripped = re.sub(r"\n?```$", "", stripped).strip()

    # If it starts with [ or { it's clean JSON — return as-is
    if stripped.startswith(("[", "{")):
        return stripped

    # Otherwise find the first [ ... ] array in the full text (handles explanation + code block)
    match = re.search(r"(\[[\s\S]*?\])", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Last resort: return stripped and let the caller handle the parse error
    return stripped


def _map_spec_phase(test_cases: list, spec_pool: list) -> dict:
    """
    Phase 1: for every TC check whether it matches a spec endpoint.
    Returns {tc_num: {"api_key": str|None, "confidence": int}}.
    """
    if not spec_pool:
        return {}

    lines = ["SPEC ENDPOINTS:"]
    for ep in spec_pool[:60]:
        key = f"{ep['method']} {ep['path']}"
        summary = ep.get("summary", "")
        lines.append(f"  key={json.dumps(key)} summary={json.dumps(summary)}")

    lines.append("\nTEST CASES:")
    for tc in test_cases:
        num = tc.get("testScenarioNumber", "?")
        obj = tc.get("testObjective", "")
        code = tc.get("expectedResponseCode", "")
        lines.append(f"  {num}. {obj} [expected HTTP {code}]")

    lines.append(
        "\nReturn a JSON array with one object per test case:\n"
        '[{"tc": <number>, "api_key": "<METHOD PATH>"|null, "confidence": <0-100>}]'
    )

    try:
        raw = _llm_call(_SPEC_PHASE_SYSTEM, "\n".join(lines))
        results = json.loads(raw)
        return {r["tc"]: r for r in results if "tc" in r}
    except Exception:
        return {}


def _normalize_path(path: str) -> str:
    """
    Replace hardcoded ID segments in a path with {id} so that
    /loanaccount/6433, /accountstatus/BH37338GH, /accountstatus/2100000071NA
    all collapse to the same normalized form.
    """
    # Pure numeric segments: /6433 → /{id}
    path = re.sub(r'/\d+(?=/|$|\?)', '/{id}', path)
    # Uppercase-start alphanumeric IDs: /BH37338GH, /FAA0280, /FAA0287
    path = re.sub(r'/[A-Z][A-Z0-9]{3,}(?=/|$|\?)', '/{id}', path)
    # Digit-start mixed IDs: /2100000071NA (digits then uppercase letters)
    path = re.sub(r'/\d+[A-Z][A-Z0-9]*(?=/|$|\?)', '/{id}', path)
    return path


def _map_existing_phase(test_cases: list, existing_pool: list) -> dict:
    """
    Phase 2: for each remaining TC find the best existing endpoint.
    Returns {tc_num: {"api_key": str|None, "step": int|None, "confidence": int}}.

    Normalizes hardcoded IDs in paths (e.g. /accountstatus/BH37338GH → /accountstatus/{id})
    so many variants of the same endpoint collapse to one representative.
    The LLM receives the normalized path; server.py resolves the actual path from
    the existing pool when populating bodies.
    """
    if not existing_pool or not test_cases:
        return {}

    # Build one representative per (method, normalized_path).
    # Among entries sharing a normalized path, prefer the one with the richest objective.
    norm_map: dict = {}  # (method, norm_path) → best entry
    for ep in existing_pool:
        norm = _normalize_path(ep["path"])
        key = (ep["method"], norm)
        existing = norm_map.get(key)
        obj = ep.get("test_objective", "")
        if existing is None:
            norm_map[key] = ep
        else:
            ex_obj = existing.get("test_objective", "")
            if len(obj) > len(ex_obj):
                norm_map[key] = ep

    # Sort: entries with non-empty objectives first
    representatives = sorted(
        norm_map.items(),
        key=lambda kv: (0 if kv[1].get("test_objective") else 1),
    )

    lines = ["EXISTING APIS (from regression suite):"]
    for (method, norm_path), ep in representatives[:150]:
        key_str = f"{method} {norm_path}"
        obj = ep.get("test_objective", "")
        req_body = ep.get("request_body") or {}
        body_keys = list(req_body.keys())[:8] if isinstance(req_body, dict) else []
        body_hint = f" fields={body_keys}" if body_keys else ""
        lines.append(
            f"  key={json.dumps(key_str)} "
            f"objective={json.dumps(obj[:120])}{body_hint}"
        )

    lines.append("\nTEST CASES TO MATCH:")
    for tc in test_cases:
        num = tc.get("testScenarioNumber", "?")
        obj = tc.get("testObjective", "")
        code = tc.get("expectedResponseCode", "")
        lines.append(f"  {num}. {obj} [expected HTTP {code}]")

    lines.append(
        "\nInstructions:\n"
        "- For each test case, first check if any endpoint has an 'objective' that closely\n"
        "  matches the test description — if yes, that is the answer (confidence >= 85).\n"
        "- Only use path/method matching when no objective match exists.\n"
        "- For general 'Create X' or 'Get X' objectives, use the base resource path,\n"
        "  NOT specialised sub-paths like /combinedbatch, /approve, /disburse unless\n"
        "  the test objective explicitly names that specific operation.\n"
        "- {id} in a path means a record ID placeholder — treat the path as a template.\n"
        "\nReturn a JSON array with one object per test case:\n"
        '[{"tc": <number>, "api_key": "<METHOD PATH>"|null, "step": <int>|null, "confidence": <0-100>}]'
        "\nUse the exact key string from the list above (including {id} if present)."
    )

    try:
        raw = _llm_call(_EXISTING_PHASE_SYSTEM, "\n".join(lines))
        results = json.loads(raw)
        return {r["tc"]: r for r in results if "tc" in r}
    except Exception:
        return {}


def _llm_available() -> bool:
    return bool(_ANTHROPIC_KEY) or bool(claude_cli_path())


def map_hybrid(test_cases: list, spec_pool: list, existing_pool: list) -> list:
    """
    Two-phase mapping: spec-first for all TCs, then existing for unmatched.
    Multiple TCs may map to the same endpoint — no uniqueness constraint.
    """
    if not _llm_available():
        return _fallback_mappings(test_cases)

    # Phase 1 — spec scan for all TCs
    spec_matches = _map_spec_phase(test_cases, spec_pool)

    # Phase 2 — existing scan only for TCs not confidently matched to spec
    unmatched = [
        tc for tc in test_cases
        if spec_matches.get(int(tc.get("testScenarioNumber", 0) or 0), {}).get("confidence", 0) < 60
    ]
    existing_matches = _map_existing_phase(unmatched, existing_pool) if unmatched else {}

    # Merge results
    results = []
    for tc in test_cases:
        num = int(tc.get("testScenarioNumber", 0) or 0)
        sm = spec_matches.get(num, {})
        if sm.get("confidence", 0) >= 60 and sm.get("api_key"):
            results.append({
                "tc": num, "source": "spec",
                "api_key": sm["api_key"], "step": None,
                "confidence": sm["confidence"], "reason": "Matched spec endpoint",
            })
        else:
            em = existing_matches.get(num, {})
            if em.get("confidence", 0) >= 50 and em.get("api_key"):
                results.append({
                    "tc": num, "source": "existing",
                    "api_key": em["api_key"], "step": em.get("step"),
                    "confidence": em["confidence"], "reason": "Matched existing endpoint",
                })
            else:
                results.append({
                    "tc": num, "source": None, "api_key": None,
                    "step": None, "confidence": 0, "reason": "No confident match — map manually",
                })

    results.sort(key=lambda m: m.get("tc", 0))
    return results


def _fallback_mappings(test_cases: list) -> list:
    return [
        {"tc": int(tc.get("testScenarioNumber", i + 1)), "source": None,
         "api_key": None, "step": None, "confidence": 0,
         "reason": "No ANTHROPIC_API_KEY and claude CLI not found — manual mapping required"}
        for i, tc in enumerate(test_cases)
    ]


# ---------------------------------------------------------------------------
# 4b. Enrich SPEC bodies with test-objective-aware values
# ---------------------------------------------------------------------------

_ENRICH_SYSTEM = (
    "You are filling in API request and response bodies for automated test cases.\n"
    "For each test case you receive:\n"
    "  - test_objective: what the test is verifying\n"
    "  - method + path: the API endpoint\n"
    "  - request_skeleton: field names and types from the OpenAPI spec\n"
    "  - response_skeleton: expected response structure from the OpenAPI spec\n\n"
    "Your job: replace skeleton values with realistic values that reflect the test objective.\n"
    "Rules:\n"
    "  - Keep all field names exactly as given — do not add or remove fields\n"
    "  - Keep null values as null\n"
    "  - Choose values that make sense for what the test objective describes\n"
    "  - For response bodies, reflect what the API would realistically return after the operation\n"
    "  - For string enums, pick the value most appropriate to the test objective\n"
    "Return ONLY a valid JSON array — no markdown, no explanation."
)


def enrich_spec_bodies(suggestions: list, test_cases: list) -> None:
    """
    For SPEC-sourced suggestions, call LLM to fill in request/response bodies
    with values appropriate to each test objective. Modifies suggestions in-place.
    Falls back silently to skeleton values on any error.
    """
    tc_index = {
        int(tc.get("testScenarioNumber", i + 1)): tc
        for i, tc in enumerate(test_cases)
    }

    targets = [s for s in suggestions if s.get("source") == "spec" and s.get("path")]
    if not targets or not _llm_available():
        return

    items = []
    for s in targets:
        tc = tc_index.get(s["tc"], {})
        items.append({
            "tc": s["tc"],
            "test_objective": tc.get("testObjective", ""),
            "method": s.get("method", ""),
            "path": s.get("path", ""),
            "request_skeleton": s.get("request_body", {}),
            "response_skeleton": s.get("response_body", {}),
        })

    user_prompt = (
        "Fill in appropriate values for each test case based on its objective.\n"
        "Input:\n" + json.dumps(items, indent=2) + "\n\n"
        "Return a JSON array with one object per test case:\n"
        '[{"tc": <number>, "request_body": <filled object>, "response_body": <filled object>}]'
    )

    try:
        if _ANTHROPIC_KEY:
            import anthropic
            client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
            raw = stream_message_text(client, system=_ENRICH_SYSTEM, user_prompt=user_prompt).strip()
        else:
            raw = call_via_claude_cli(_ENRICH_SYSTEM, user_prompt, timeout=_CLI_TIMEOUT).strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        enriched = json.loads(raw)

        enriched_index = {e["tc"]: e for e in enriched}
        for s in targets:
            e = enriched_index.get(s["tc"])
            if e:
                if e.get("request_body") is not None:
                    s["request_body"] = e["request_body"]
                if e.get("response_body") is not None:
                    s["response_body"] = e["response_body"]
    except Exception:
        pass  # silently fall back to skeletons


# ---------------------------------------------------------------------------
# 5. POST → GET re-suggestion
# ---------------------------------------------------------------------------

_GET_SYSTEM = (
    "You are helping pre-fill a GET API test case based on a related POST API.\n"
    "The POST creates a resource; the GET retrieves it.\n"
    "Suggest:\n"
    "  - suggested_path: POST path + '/{id}' (replace any trailing literal IDs with {id})\n"
    "  - suggested_response: a skeleton response that mirrors the POST request body fields,\n"
    "    adding 'id' as the first field (value 0).\n"
    "Return ONLY valid JSON: {\"suggested_path\": str, \"suggested_response\": object}"
)


def _build_get_suggestion_prompt(post_path: str, post_request: dict, get_objective: str) -> str:
    return (
        f"POST path: {post_path}\n"
        f"POST request body: {redacted_json_snippet(post_request, 800, '{}')}\n"
        f"GET test objective: {get_objective}\n"
    )


def suggest_get_from_post(post_data: dict, get_tc: dict) -> dict:
    """
    Re-suggest a GET row when its linked POST row is edited.
    Falls back to a simple heuristic if LLM is unavailable.
    """
    post_path = post_data.get("path", "")
    post_request = post_data.get("request_body", {})
    get_objective = get_tc.get("testObjective", "")

    # Heuristic fallback (no LLM needed for simple cases)
    simple_path = _append_id(post_path)
    simple_response = _mirror_response(post_request)

    if not _llm_available():
        return {"suggested_path": simple_path, "suggested_response": simple_response}

    user_prompt = _build_get_suggestion_prompt(post_path, post_request, get_objective)

    try:
        if _ANTHROPIC_KEY:
            import anthropic
            client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
            raw = stream_message_text(client, system=_GET_SYSTEM, user_prompt=user_prompt, max_tokens=1024).strip()
        else:
            raw = call_via_claude_cli(_GET_SYSTEM, user_prompt, timeout=_CLI_TIMEOUT).strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        return {
            "suggested_path": result.get("suggested_path", simple_path),
            "suggested_response": result.get("suggested_response", simple_response),
        }
    except Exception:
        return {"suggested_path": simple_path, "suggested_response": simple_response}


def _append_id(path: str) -> str:
    """Turn '/api/v1/loans' → '/api/v1/loans/{id}'."""
    path = path.rstrip("/")
    # If path already ends with a numeric segment or {id}, leave it
    if re.search(r"/\d+$", path) or path.endswith("/{id}"):
        return path
    return path + "/{id}"


def _mirror_response(request_body: object) -> dict:
    """Create a response skeleton that mirrors request body fields."""
    if not isinstance(request_body, dict):
        return {"id": 0}
    result = {"id": 0}
    for k, v in request_body.items():
        if isinstance(v, dict):
            result[k] = {}
        elif isinstance(v, list):
            result[k] = []
        else:
            result[k] = v
    return result
