"""
artifact_generator.py — Generate all 4 regression suite artifacts from confirmed mappings.

Generates:
  apiInputs/<APIName>.csv
  fileFromJson/payloadJSON/<APIName>Request.json
  fileFromJson/expectedJSON/<APIName>Response.json
  apisToBeValidated/APIsToBeValidated_<X>.csv  (row appended)
"""

import csv
import json
import os
import re


_VALID_API_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def is_valid_api_name(name) -> bool:
    """
    api_name becomes a filename (and a CSV apiName prefix) in every path
    built below. It's client-controlled (POST /api/confirm on a server
    bound to 0.0.0.0), so reject anything that isn't a plain identifier
    before it ever reaches os.path.join -- a value like "../../etc/passwd"
    would otherwise let a caller write/read outside the intended
    apiInputs/fileFromJson directories.
    """
    return bool(name) and bool(_VALID_API_NAME.match(name))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _next_manifest_row_number(manifest_path: str) -> int:
    """Read the manifest CSV and return the next row number (last data row + 1)."""
    if not os.path.exists(manifest_path):
        return 1
    try:
        with open(manifest_path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            rows = list(reader)
        # rows[0] is the header; subsequent rows are data
        data_rows = [r for r in rows[1:] if r and r[0].strip().isdigit()]
        if not data_rows:
            return 1
        last_num = int(data_rows[-1][0].strip())
        return last_num + 1
    except Exception:
        return 1


def _full_api_path(flow: dict) -> str:
    path = flow.get("path", "") or ""
    query_string = (flow.get("query_string") or "").lstrip("?")
    if query_string and "?" not in path:
        return f"{path}?{query_string}"
    return path


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tc_sort_key(full_name: str, prefix: str) -> int:
    """Extract the trailing TC number from an '<apiName><tc_number>' string."""
    suffix = full_name[len(prefix):] if full_name.startswith(prefix) else full_name
    return _safe_int(suffix)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(confirmed: dict, project_root: str) -> dict:
    """
    Generate all 4 artifacts from the confirmed mapping dict and run dry-run.

    Parameters
    ----------
    confirmed : dict with keys:
        api_name, module, manifest (A/B/C), owner, scenario, steps
        Each step: tc_number, test_objective, captured_api (flow dict),
                   corrected_status, common_ignore
    project_root : str — absolute path to repo root

    Returns
    -------
    dict with keys:
        files: {api_inputs, payload_json, expected_json, manifest}
        dry_run: str
    """
    api_name = confirmed["api_name"].strip()
    if not is_valid_api_name(api_name):
        raise ValueError(
            f"Invalid api_name {api_name!r} — only letters, digits, and underscores are allowed"
        )
    module = confirmed.get("module", "").strip()
    manifest_letter = confirmed.get("manifest", "A").strip().upper()
    owner = confirmed.get("owner", "").strip()
    business_function = confirmed.get("business_function", "NA").strip() or "NA"
    scenario_flag = confirmed.get("scenario", "y").strip().lower()
    steps = confirmed.get("steps", [])
    append_mode = bool(confirmed.get("append_mode", False))

    # -----------------------------------------------------------------------
    # 1. apiInputs/<APIName>.csv
    # -----------------------------------------------------------------------
    api_inputs_dir = os.path.join(project_root, "apiInputs")
    os.makedirs(api_inputs_dir, exist_ok=True)
    api_inputs_path = os.path.join(api_inputs_dir, f"{api_name}.csv")

    inputs_header = ["testScenarioNumber", "testObjective", "urlParameter", "expectedResponseCode"]
    new_tc_numbers = {str(step["tc_number"]) for step in steps}

    kept_rows = []
    if append_mode and os.path.exists(api_inputs_path):
        with open(api_inputs_path, "r", newline="", encoding="utf-8") as fh:
            existing_rows = list(csv.reader(fh))
        if existing_rows:
            inputs_header = existing_rows[0]
            # Drop any existing row for a TC number this round is re-supplying,
            # so re-capturing/appending never leaves stale duplicate rows behind.
            kept_rows = [r for r in existing_rows[1:] if r and r[0] not in new_tc_numbers]

    new_rows = [
        [
            step["tc_number"],
            step.get("test_objective", ""),
            "NA",
            step.get("corrected_status", step.get("captured_api", {}).get("status", 200)),
        ]
        for step in steps
    ]

    all_rows = kept_rows + new_rows
    all_rows.sort(key=lambda r: _safe_int(r[0]))

    with open(api_inputs_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(inputs_header)
        writer.writerows(all_rows)

    # -----------------------------------------------------------------------
    # 2. fileFromJson/payloadJSON/<APIName>.json
    # -----------------------------------------------------------------------
    payload_dir = os.path.join(project_root, "fileFromJson", "payloadJSON")
    os.makedirs(payload_dir, exist_ok=True)
    payload_path = os.path.join(payload_dir, f"{api_name}.json")

    new_payload_entries = []
    for step in steps:
        tc_num = step.get("tc_number", "")
        flow = step.get("captured_api", {})
        method = flow.get("method", "GET")
        # Keep the raw captured path, including any path/query parameters.
        # urlParameter in apiInputs is always NA, so the runner should use this
        # apiPath as one URL. path_normalized is NOT used here because it
        # replaces IDs with {id} literals that the runner cannot resolve.
        path = _full_api_path(flow)
        request_body = flow.get("request_body")
        if request_body is None:
            request_body = {}
        new_payload_entries.append({
            "apiName": f"{api_name}{tc_num}",
            "apiMethod": method,
            "apiPath": path,
            "payload": request_body,
            "TestStresserFlg": step.get("test_stresser", "N").upper(),
        })

    existing_payload = []
    if append_mode and os.path.exists(payload_path):
        with open(payload_path, "r", encoding="utf-8") as fh:
            existing_payload = json.load(fh)
        if not isinstance(existing_payload, list):
            existing_payload = []

    # Drop any existing entry for a TC this round is re-supplying (e.g. filling
    # in a placeholder from an earlier round) instead of appending a duplicate.
    new_payload_names = {e["apiName"] for e in new_payload_entries}
    kept_payload = [e for e in existing_payload if e.get("apiName") not in new_payload_names]

    merged_payload = kept_payload + new_payload_entries
    merged_payload.sort(key=lambda e: _tc_sort_key(e.get("apiName", ""), api_name))

    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump(merged_payload, fh, indent=2)

    # -----------------------------------------------------------------------
    # 3. fileFromJson/expectedJSON/<APIName>.json
    # -----------------------------------------------------------------------
    expected_dir = os.path.join(project_root, "fileFromJson", "expectedJSON")
    os.makedirs(expected_dir, exist_ok=True)
    expected_path = os.path.join(expected_dir, f"{api_name}.json")

    new_expected_entries = []
    for step in steps:
        tc_num = step.get("tc_number", "")
        flow = step.get("captured_api", {})
        response_body = flow.get("response_body", [])
        if not response_body:
            response_body = []
        common_ignore = step.get("common_ignore", [])
        if not isinstance(common_ignore, list):
            common_ignore = []
        new_expected_entries.append({
            "apiName": f"{api_name}{tc_num}",
            "expected": response_body,
            "ignore": [],
            "commonIgnore": common_ignore,
        })

    existing_expected = []
    if append_mode and os.path.exists(expected_path):
        with open(expected_path, "r", encoding="utf-8") as fh:
            existing_expected = json.load(fh)
        if not isinstance(existing_expected, list):
            existing_expected = []

    # Same upsert-by-TC behaviour as the payload JSON above.
    new_expected_names = {e["apiName"] for e in new_expected_entries}
    kept_expected = [e for e in existing_expected if e.get("apiName") not in new_expected_names]

    merged_expected = kept_expected + new_expected_entries
    merged_expected.sort(key=lambda e: _tc_sort_key(e.get("apiName", ""), api_name))

    with open(expected_path, "w", encoding="utf-8") as fh:
        json.dump(merged_expected, fh, indent=2)

    # -----------------------------------------------------------------------
    # 4. apisToBeValidated/APIsToBeValidated_<X>.csv — append row (skip in append mode)
    # -----------------------------------------------------------------------
    manifest_path = None
    if not append_mode:
        manifest_dir = os.path.join(project_root, "apisToBeValidated")
        os.makedirs(manifest_dir, exist_ok=True)
        manifest_path = os.path.join(manifest_dir, f"APIsToBeValidated_{manifest_letter}.csv")

        next_num = _next_manifest_row_number(manifest_path)

        new_row = [
            str(next_num),
            api_name,
            "NA",          # apiMethod column (kept for compatibility)
            "NA",          # apiPath column
            f"{api_name}.csv",
            "y",
            module,
            "json",
            f"{api_name}.json",
            f"{api_name}.json",
            owner,
            scenario_flag,
            "n",
            business_function,
        ]

        # Append row — create file with header if it doesn't exist
        file_existed = os.path.exists(manifest_path)

        # Ensure the file ends with a newline so the new row starts on its own line
        if file_existed and os.path.getsize(manifest_path) > 0:
            with open(manifest_path, "rb") as fh:
                fh.seek(-1, 2)
                if fh.read(1) not in (b"\n", b"\r"):
                    with open(manifest_path, "ab") as fw:
                        fw.write(b"\n")

        with open(manifest_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if not file_existed:
                writer.writerow([
                    "#", "apiName", "apiMethod", "apiPath", "apiInputsFile",
                    "toBeValidated", "Module", "fileType",
                    "payloadJSON", "expectedJSON",
                    "consentPerson", "scenario", "mysql", "BusinessFunction",
                ])
            writer.writerow(new_row)

    files_result = {
        "api_inputs":   api_inputs_path,
        "payload_json": payload_path,
        "expected_json": expected_path,
    }
    if manifest_path:
        files_result["manifest"] = manifest_path

    return {"files": files_result}
