import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import early_suggester as es


# ---------------------------------------------------------------------------
# 1. OpenAPI spec parsing
# ---------------------------------------------------------------------------

SIMPLE_SPEC = {
    "paths": {
        "/loans": {
            "get": {
                "summary": "List loans",
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "array"}}}}},
            },
            "post": {
                "summary": "Create loan",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"amount": {"type": "number"}}}}}},
                "responses": {"201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Loan"}}}}},
            },
        },
    },
    "components": {
        "schemas": {
            "Loan": {"type": "object", "properties": {"id": {"type": "integer"}}},
        },
    },
}


def test_parse_openapi_spec_from_string_extracts_all_defined_methods():
    endpoints = es.parse_openapi_spec_from_string(json.dumps(SIMPLE_SPEC))

    methods = {(e["method"], e["path"]) for e in endpoints}
    assert methods == {("GET", "/loans"), ("POST", "/loans")}


def test_parse_openapi_spec_from_string_resolves_response_ref():
    endpoints = es.parse_openapi_spec_from_string(json.dumps(SIMPLE_SPEC))

    post_endpoint = next(e for e in endpoints if e["method"] == "POST")
    assert post_endpoint["response_schema"] == {"type": "object", "properties": {"id": {"type": "integer"}}}


def test_parse_openapi_spec_from_string_extracts_request_schema():
    endpoints = es.parse_openapi_spec_from_string(json.dumps(SIMPLE_SPEC))

    post_endpoint = next(e for e in endpoints if e["method"] == "POST")
    assert post_endpoint["request_schema"] == {"type": "object", "properties": {"amount": {"type": "number"}}}


def test_parse_openapi_spec_from_string_invalid_text_returns_empty_list():
    assert es.parse_openapi_spec_from_string("not json and not yaml: [") == []


def test_parse_openapi_spec_from_string_empty_spec_returns_empty_list():
    assert es.parse_openapi_spec_from_string("{}") == []


# ---------------------------------------------------------------------------
# 2. Skeleton generation
# ---------------------------------------------------------------------------

def test_generate_skeleton_object_with_properties():
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}}
    assert es.generate_skeleton(schema) == {"name": "", "age": 0}


def test_generate_skeleton_array():
    schema = {"type": "array", "items": {"type": "integer"}}
    assert es.generate_skeleton(schema) == [0]


def test_generate_skeleton_boolean():
    assert es.generate_skeleton({"type": "boolean"}) is False


def test_generate_skeleton_uses_example_when_present():
    schema = {"type": "string", "example": "ABC123"}
    assert es.generate_skeleton(schema) == "ABC123"


def test_generate_skeleton_uses_first_enum_value():
    schema = {"type": "string", "enum": ["ACTIVE", "CLOSED"]}
    assert es.generate_skeleton(schema) == "ACTIVE"


def test_generate_skeleton_resolves_ref_via_components():
    components = {"schemas": {"Loan": {"type": "object", "properties": {"id": {"type": "integer"}}}}}
    schema = {"$ref": "#/components/schemas/Loan"}
    assert es.generate_skeleton(schema, components) == {"id": 0}


def test_generate_skeleton_empty_schema_returns_empty_string():
    assert es.generate_skeleton({}) == ""
    assert es.generate_skeleton(None) == ""


def test_generate_skeleton_caps_recursion_depth_without_infinite_loop():
    # A schema that refers to itself -- must terminate, not recurse forever.
    components = {"schemas": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"}}}}}
    schema = {"$ref": "#/components/schemas/Node"}

    result = es.generate_skeleton(schema, components)

    # Just needs to return without raising RecursionError / hanging.
    assert isinstance(result, dict)


def test_generate_skeleton_recursion_cap_terminates_by_depth_eight():
    """Pins the actual cutoff -- the docstring previously said 4, the code says 8."""
    components = {"schemas": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"}}}}}
    schema = {"$ref": "#/components/schemas/Node"}

    result = es.generate_skeleton(schema, components)

    depth = 0
    node = result
    while isinstance(node, dict) and "child" in node:
        node = node["child"]
        depth += 1

    assert depth <= 8
    assert node == ""


def test_generate_skeleton_keeps_all_required_fields_beyond_ten():
    properties = {f"required_{i}": {"type": "integer"} for i in range(15)}
    schema = {"type": "object", "properties": properties, "required": list(properties.keys())}

    result = es.generate_skeleton(schema)

    assert set(result.keys()) == set(properties.keys())


def test_generate_skeleton_truncates_optional_fields_at_ten():
    properties = {f"optional_{i}": {"type": "integer"} for i in range(15)}
    schema = {"type": "object", "properties": properties, "required": []}

    result = es.generate_skeleton(schema)

    assert len(result) == 10


# ---------------------------------------------------------------------------
# 3. Scan existing regression suite
# ---------------------------------------------------------------------------

def _write_existing_api(project_root, api_name, payload_steps, expected_steps=None, objectives=None):
    payload_dir = os.path.join(project_root, "fileFromJson", "payloadJSON")
    expected_dir = os.path.join(project_root, "fileFromJson", "expectedJSON")
    inputs_dir = os.path.join(project_root, "apiInputs")
    for d in (payload_dir, expected_dir, inputs_dir):
        os.makedirs(d, exist_ok=True)

    with open(os.path.join(payload_dir, f"{api_name}.json"), "w") as fh:
        json.dump(payload_steps, fh)

    if expected_steps is not None:
        with open(os.path.join(expected_dir, f"{api_name}.json"), "w") as fh:
            json.dump(expected_steps, fh)

    if objectives is not None:
        with open(os.path.join(inputs_dir, f"{api_name}.csv"), "w") as fh:
            fh.write("testScenarioNumber,testObjective,urlParameter,expectedResponseCode\n")
            for num, obj in objectives.items():
                fh.write(f"{num},{obj},NA,200\n")


def test_scan_existing_apis_joins_payload_expected_and_objective(tmp_path):
    project_root = str(tmp_path)
    _write_existing_api(
        project_root,
        "RedemptionPast",
        payload_steps=[{"apiName": "RedemptionPast1", "apiMethod": "get", "apiPath": "/x", "payload": {}}],
        expected_steps=[{"apiName": "RedemptionPast1", "expected": {"status": "ok"}}],
        objectives={1: "Verify redemption"},
    )

    results = es.scan_existing_apis(project_root)

    assert len(results) == 1
    entry = results[0]
    assert entry["method"] == "GET"
    assert entry["response_body"] == {"status": "ok"}
    assert entry["test_objective"] == "Verify redemption"
    assert entry["step"] == 1


def test_scan_existing_apis_handles_missing_expected_and_objectives(tmp_path):
    project_root = str(tmp_path)
    _write_existing_api(
        project_root,
        "NoExtras",
        payload_steps=[{"apiName": "NoExtras1", "apiMethod": "get", "apiPath": "/x", "payload": {}}],
    )

    results = es.scan_existing_apis(project_root)

    assert len(results) == 1
    assert results[0]["response_body"] == {}
    assert results[0]["test_objective"] == ""


def test_scan_existing_apis_matches_expected_file_suffix_variants(tmp_path):
    project_root = str(tmp_path)
    payload_dir = os.path.join(project_root, "fileFromJson", "payloadJSON")
    expected_dir = os.path.join(project_root, "fileFromJson", "expectedJSON")
    os.makedirs(payload_dir)
    os.makedirs(expected_dir)
    with open(os.path.join(payload_dir, "SuffixTest.json"), "w") as fh:
        json.dump([{"apiName": "SuffixTest1", "apiMethod": "GET", "apiPath": "/x", "payload": {}}], fh)
    with open(os.path.join(expected_dir, "SuffixTest_response.json"), "w") as fh:
        json.dump([{"apiName": "SuffixTest1", "expected": {"ok": True}}], fh)

    results = es.scan_existing_apis(project_root)

    assert results[0]["response_body"] == {"ok": True}


def test_scan_existing_apis_ignores_malformed_payload_json(tmp_path):
    project_root = str(tmp_path)
    payload_dir = os.path.join(project_root, "fileFromJson", "payloadJSON")
    os.makedirs(payload_dir)
    with open(os.path.join(payload_dir, "Broken.json"), "w") as fh:
        fh.write("not valid json {{{")

    assert es.scan_existing_apis(project_root) == []


# ---------------------------------------------------------------------------
# 4. Path normalization
# ---------------------------------------------------------------------------

def test_normalize_path_pure_numeric_id():
    assert es._normalize_path("/loanaccount/6433") == "/loanaccount/{id}"


def test_normalize_path_uppercase_alnum_id():
    assert es._normalize_path("/accountstatus/BH37338GH") == "/accountstatus/{id}"


def test_normalize_path_digit_start_mixed_id():
    assert es._normalize_path("/accountstatus/2100000071NA") == "/accountstatus/{id}"


def test_normalize_path_leaves_non_id_segments_alone():
    assert es._normalize_path("/loanaccount/combinedbatch") == "/loanaccount/combinedbatch"


def test_normalize_path_handles_query_string_boundary():
    assert es._normalize_path("/loanaccount/6433?foo=bar") == "/loanaccount/{id}?foo=bar"


# ---------------------------------------------------------------------------
# 5. Hybrid mapping fallback (no LLM available)
# ---------------------------------------------------------------------------

def test_map_hybrid_falls_back_to_manual_when_no_llm_available(monkeypatch):
    monkeypatch.setattr(es, "_llm_available", lambda: False)
    test_cases = [{"testScenarioNumber": "1", "testObjective": "Do a thing"}]

    result = es.map_hybrid(test_cases, spec_pool=[], existing_pool=[])

    assert result == [{
        "tc": 1, "source": None, "api_key": None, "step": None, "confidence": 0,
        "reason": "No ANTHROPIC_API_KEY and claude CLI not found — manual mapping required",
    }]


def test_map_hybrid_prefers_spec_match_over_existing_when_both_confident(monkeypatch):
    monkeypatch.setattr(es, "_llm_available", lambda: True)
    monkeypatch.setattr(es, "_map_spec_phase", lambda tcs, pool: {1: {"api_key": "GET /spec", "confidence": 90}})
    monkeypatch.setattr(es, "_map_existing_phase", lambda tcs, pool: {1: {"api_key": "GET /existing", "confidence": 90, "step": 1}})

    result = es.map_hybrid([{"testScenarioNumber": "1", "testObjective": "x"}], spec_pool=["dummy"], existing_pool=["dummy"])

    assert result[0]["source"] == "spec"
    assert result[0]["api_key"] == "GET /spec"


def test_map_hybrid_falls_back_to_existing_when_spec_confidence_too_low(monkeypatch):
    monkeypatch.setattr(es, "_llm_available", lambda: True)
    monkeypatch.setattr(es, "_map_spec_phase", lambda tcs, pool: {1: {"api_key": "GET /spec", "confidence": 10}})
    monkeypatch.setattr(es, "_map_existing_phase", lambda tcs, pool: {1: {"api_key": "GET /existing", "confidence": 90, "step": 2}})

    result = es.map_hybrid([{"testScenarioNumber": "1", "testObjective": "x"}], spec_pool=["dummy"], existing_pool=["dummy"])

    assert result[0]["source"] == "existing"
    assert result[0]["api_key"] == "GET /existing"
    assert result[0]["step"] == 2


def test_map_hybrid_no_confident_match_anywhere(monkeypatch):
    monkeypatch.setattr(es, "_llm_available", lambda: True)
    monkeypatch.setattr(es, "_map_spec_phase", lambda tcs, pool: {})
    monkeypatch.setattr(es, "_map_existing_phase", lambda tcs, pool: {})

    result = es.map_hybrid([{"testScenarioNumber": "1", "testObjective": "x"}], spec_pool=["dummy"], existing_pool=["dummy"])

    assert result[0]["source"] is None
    assert result[0]["confidence"] == 0


# ---------------------------------------------------------------------------
# 6. POST -> GET heuristic fallback
# ---------------------------------------------------------------------------

def test_append_id_adds_placeholder_to_plain_path():
    assert es._append_id("/api/v1/loans") == "/api/v1/loans/{id}"


def test_append_id_leaves_path_already_ending_in_numeric_id():
    assert es._append_id("/api/v1/loans/42") == "/api/v1/loans/42"


def test_append_id_leaves_path_already_ending_in_id_placeholder():
    assert es._append_id("/api/v1/loans/{id}") == "/api/v1/loans/{id}"


def test_mirror_response_mirrors_request_shape():
    request_body = {"name": "x", "tags": ["a"], "meta": {"k": "v"}, "amount": 5}

    result = es._mirror_response(request_body)

    assert result == {"id": 0, "name": "x", "tags": [], "meta": {}, "amount": 5}


def test_mirror_response_non_dict_request_returns_bare_id():
    assert es._mirror_response("not a dict") == {"id": 0}


def test_suggest_get_from_post_uses_heuristic_when_no_llm_available(monkeypatch):
    monkeypatch.setattr(es, "_llm_available", lambda: False)

    result = es.suggest_get_from_post(
        {"path": "/loans", "request_body": {"amount": 100}},
        {"testObjective": "Get the loan"},
    )

    assert result["suggested_path"] == "/loans/{id}"
    assert result["suggested_response"] == {"id": 0, "amount": 100}
