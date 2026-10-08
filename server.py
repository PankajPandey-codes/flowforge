"""
server.py — Flask app serving the 4-stage API capture UI.

Usage:
    python3 tools/flowforge/server.py
    # Browser opens automatically at http://localhost:5050
"""

import csv
import io
import json
import os
import tempfile
import threading
import time
import webbrowser

from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Path resolution — use os.path only (avoids macOS TCC issues with pathlib)
# ---------------------------------------------------------------------------

# server.py lives at tools/flowforge/server.py — project root is 2 levels up
_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))

# ---------------------------------------------------------------------------
# App initialisation — relative folder names so Flask resolves from _HERE
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    root_path=_HERE,
    instance_path=os.path.join(_HERE, "instance"),
    template_folder=os.path.join(_HERE, "templates"),
    static_folder=os.path.join(_HERE, "static"),
)

# ---------------------------------------------------------------------------
# State persistence — survives server restarts
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(tempfile.gettempdir(), f"capture_server_state_{os.getuid()}.json")
_STATE_KEYS = ["flows_file", "test_cases", "parsed_flows", "mappings",
               "api_name", "module", "manifest", "owner", "auth_mode",
               "append_mode", "start_from_case"]


def _save_state():
    try:
        payload = {k: STATE.get(k) for k in _STATE_KEYS}
        fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"[state] save failed: {e}", flush=True)


def _load_state():
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        for k in _STATE_KEYS:
            if k in saved and saved[k] is not None:
                STATE[k] = saved[k]
        if STATE.get("parsed_flows"):
            print(
                f"[state] restored {len(STATE['parsed_flows'])} flows, "
                f"{len(STATE.get('mappings', []))} mappings from previous session",
                flush=True,
            )
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[state] restore failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Global app state (simple dict — no sessions needed)
# ---------------------------------------------------------------------------

STATE = {
    "capturing": False,
    "flows_file": None,
    "test_cases": [],
    "api_name": None,
    "module": None,
    "manifest": "A",
    "owner": None,
    "parsed_flows": [],
    "mappings": [],
    "presets": {},
    "last_flow_count": 0,
    # Early automation state
    "early_automation": False,
    "early_spec_pool": [],
    "early_existing_pool": [],
    "early_suggestions": [],
    # Append mode state
    "append_mode": False,
    "start_from_case": 1,
    # Auth + mapping control
    "auth_mode": "auto",        # "auto" | "api_key" | "cli"
    "pause_requested": False,   # set True by /api/pause-mapping
}

# ---------------------------------------------------------------------------
# Startup: compute presets
# ---------------------------------------------------------------------------

def _init_presets():
    try:
        from tools.capture.common_ignore_builder import get_presets
    except ImportError:
        from common_ignore_builder import get_presets
    STATE["presets"] = get_presets(PROJECT_ROOT)
    _load_state()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload-csv", methods=["POST"])
def upload_csv():
    """Accept multipart CSV upload, parse test cases, return list."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    # utf-8-sig strips the UTF-8 BOM (﻿) automatically if present
    content = file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))

    test_cases = []
    for row in reader:
        # Normalise key names: strip whitespace and any residual BOM
        cleaned = {k.strip().lstrip("﻿"): v.strip() for k, v in row.items() if k}
        test_cases.append(cleaned)

    STATE["test_cases"] = test_cases
    return jsonify({"test_cases": test_cases, "count": len(test_cases)})


@app.route("/api/llm-status", methods=["GET"])
def llm_status():
    """Return whether Anthropic-backed auto-mapping is configured."""
    try:
        try:
            from tools.capture.auto_mapper import get_llm_status
        except ImportError:
            from auto_mapper import get_llm_status
        return jsonify(get_llm_status())
    except Exception as e:
        return jsonify({
            "ok": False,
            "reason": "status_check_failed",
            "message": f"Could not check LLM auto-mapping status ({type(e).__name__}). Auto-mapping may be unavailable.",
            "model": None,
        })


@app.route("/api/start-capture", methods=["POST"])
def start_capture():
    """Start mitmdump + set proxy + launch Chrome."""
    data = request.get_json() or {}
    STATE["api_name"] = data.get("api_name", "UnnamedAPI")
    STATE["module"] = data.get("module", "")
    STATE["manifest"] = data.get("manifest", "A")
    STATE["owner"] = data.get("owner", "")
    STATE["capture_host"] = data.get("capture_host", "").strip()
    STATE["append_mode"]     = bool(data.get("append_mode", False))
    STATE["start_from_case"] = int(data.get("start_from_case", 1))
    STATE["auth_mode"] = data.get("auth_mode", "auto")
    STATE["pause_requested"] = False
    try:
        from tools.capture.auto_mapper import set_auth_mode
    except ImportError:
        from auto_mapper import set_auth_mode
    set_auth_mode(STATE["auth_mode"])

    # In append mode, drop test cases that are already automated
    if STATE["append_mode"] and STATE["start_from_case"] > 1:
        STATE["test_cases"] = [
            tc for tc in STATE["test_cases"]
            if int(tc.get("testScenarioNumber", 0)) >= STATE["start_from_case"]
        ]

    flows_file = f"/tmp/mitm_flows_{int(time.time())}.json"
    STATE["flows_file"] = flows_file
    STATE["capturing"] = True
    STATE["parsed_flows"] = []
    STATE["mappings"] = []
    STATE["last_flow_count"] = 0

    try:
        try:
            from tools.capture.proxy_manager import start_capture as _start
        except ImportError:
            from proxy_manager import start_capture as _start
        session = _start(flows_file, capture_host=STATE["capture_host"])
        proxy_ok = session.get("proxy_ok", True)
        return jsonify({
            "ok": True,
            "flows_file": flows_file,
            "proxy_ok": proxy_ok,
        })
    except Exception as e:
        STATE["capturing"] = False
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def status():
    """Return current capture status and flow count."""
    flow_count = 0
    if STATE["capturing"] and STATE["flows_file"]:
        try:
            try:
                from tools.capture.proxy_manager import get_flow_count
            except ImportError:
                from proxy_manager import get_flow_count
            flow_count = get_flow_count(STATE["flows_file"])
            STATE["last_flow_count"] = flow_count
        except Exception:
            flow_count = STATE["last_flow_count"]

    return jsonify({
        "capturing": STATE["capturing"],
        "flow_count": flow_count,
    })


def _count_mapped(mappings: list) -> int:
    return sum(1 for m in mappings if isinstance(m, dict) and m.get("api_id") not in (None, ""))


def _run_initial_auto_mapping(test_cases: list, flows: list):
    """
    Run the initial LLM auto-mapping pass, then fall back to a 50-flow-batch
    retry pass if the SDK path came back incomplete. Batching is irrelevant
    to the CLI path -- it already handles any prompt size in one shot -- so
    the fallback never runs there. Returns (mappings, warning).
    """
    try:
        from tools.capture.auto_mapper import (
            get_llm_status,
            map_test_cases_with_diagnostics,
            retry_map_test_cases_in_batches,
        )
    except ImportError:
        from auto_mapper import (
            get_llm_status,
            map_test_cases_with_diagnostics,
            retry_map_test_cases_in_batches,
        )

    result = map_test_cases_with_diagnostics(test_cases, flows)
    mappings = result.get("mappings", [])
    mapping_warning = result.get("warning")
    mapped_count = _count_mapped(mappings)
    total_count = len(test_cases)

    llm_reason = get_llm_status().get("reason", "")
    if llm_reason == "cli" or (not mapping_warning and mapped_count >= total_count):
        return mappings, mapping_warning

    print(
        "[stop-capture] initial auto-map incomplete: "
        f"mapped={mapped_count}/{total_count}; warning={mapping_warning or 'none'}; "
        "running 50-flow fallback",
        flush=True,
    )
    retry_result = retry_map_test_cases_in_batches(test_cases, flows, existing_mappings=[], batch_size=50)
    retry_mappings = retry_result.get("mappings", [])
    retry_mapped_count = _count_mapped(retry_mappings)
    print(
        "[stop-capture] fallback auto-map complete: "
        f"mapped={retry_mapped_count}/{total_count}; warning={retry_result.get('warning') or 'none'}",
        flush=True,
    )
    if retry_mapped_count >= mapped_count:
        return retry_mappings, retry_result.get("warning")
    return mappings, mapping_warning


@app.route("/api/stop-capture", methods=["POST"])
def stop_capture():
    """Stop proxy, parse flows, run LLM mapper. Returns flows + mappings + presets."""
    STATE["capturing"] = False

    try:
        try:
            from tools.capture.proxy_manager import stop_capture as _stop
        except ImportError:
            from proxy_manager import stop_capture as _stop
        _stop()
    except Exception:
        pass

    # Parse flows
    flows = []
    if STATE["flows_file"]:
        try:
            try:
                from tools.capture.flow_parser import parse_flows
            except ImportError:
                from flow_parser import parse_flows
            flows = parse_flows(STATE["flows_file"])
        except Exception:
            flows = []
    STATE["parsed_flows"] = flows

    # LLM auto-mapping (errors → return empty list, never crash)
    mappings = []
    mapping_warning = None
    if flows and STATE["test_cases"]:
        try:
            mappings, mapping_warning = _run_initial_auto_mapping(STATE["test_cases"], flows)
        except Exception as e:
            mappings = []
            mapping_warning = (
                f"Auto-mapping failed before the LLM response was processed ({type(e).__name__}). "
                "Please map manually."
            )
    STATE["mappings"] = mappings
    _save_state()

    return jsonify({
        "flows": flows,
        "mappings": mappings,
        "mapping_warning": mapping_warning,
        "presets": STATE["presets"],
        "ok": True,
    })


@app.route("/api/retry-mapping", methods=["POST"])
def retry_mapping():
    """Retry auto-mapping using already parsed captured flows."""
    data = request.get_json() or {}
    flows = STATE.get("parsed_flows") or []
    test_cases = STATE.get("test_cases") or []
    if "existing_mappings" in data:
        existing_mappings = data.get("existing_mappings") or []
    else:
        existing_mappings = STATE.get("mappings") or []

    if not flows:
        return jsonify({
            "ok": False,
            "error": "No captured flows are available. Stop capture first, then retry mapping.",
        }), 400
    if not test_cases:
        return jsonify({
            "ok": False,
            "error": "No test cases are loaded. Upload the apiInputs CSV first.",
        }), 400

    # Apply auth mode for this retry run
    retry_from_tc = max(1, int(data.get("retry_from_tc") or 1))
    try:
        from tools.capture.auto_mapper import set_auth_mode as _set_auth_mode
    except ImportError:
        from auto_mapper import set_auth_mode as _set_auth_mode
    _set_auth_mode(STATE.get("auth_mode", "auto"))

    try:
        batch_size = int(data.get("batch_size") or 50)
        start_index = max(0, int(data.get("start_index") or 0))
        # Only clear a pending pause at the start of a fresh run
        # (start_index == 0). Clearing it unconditionally on every
        # paginated batch call meant the very next batch after the user
        # clicked Pause would wipe the flag before its own response ever
        # got a chance to report it -- Pause could never actually stop
        # anything on a busy loop.
        if start_index == 0:
            STATE["pause_requested"] = False
        if STATE.get("pause_requested"):
            return jsonify({
                "ok": True,
                "mappings": existing_mappings,
                "mapping_warning": None,
                "mapped_count": sum(
                    1 for m in existing_mappings
                    if isinstance(m, dict) and m.get("api_id") not in (None, "")
                ),
                "total_count": len(test_cases),
                "new_mapped_count": 0,
                "batches_processed": 0,
                "batch_start": start_index,
                "batch_end": start_index,
                "batch_size": batch_size,
                "next_start_index": start_index,
                "complete": False,
                "done": False,
                "paused": True,
            })
        end_index = min(len(flows), start_index + max(1, batch_size))
        batch = flows[start_index:end_index]
        first_flow_id = batch[0].get("id") if batch else None
        last_flow_id = batch[-1].get("id") if batch else None
        existing_count = sum(
            1 for mapping in existing_mappings
            if isinstance(mapping, dict) and mapping.get("api_id") not in (None, "")
        )
        print(
            "[retry-mapping] batch "
            f"{(start_index // max(1, batch_size)) + 1}: "
            f"indices {start_index}-{max(start_index, end_index - 1)} "
            f"flow_ids {first_flow_id}-{last_flow_id}; "
            f"existing_mapped={existing_count}/{len(test_cases)}",
            flush=True,
        )
        try:
            from tools.capture.auto_mapper import retry_map_test_cases_batch
        except ImportError:
            from auto_mapper import retry_map_test_cases_batch
        # Exclude TCs below retry_from_tc so the LLM doesn't match them
        batch_test_cases = [
            tc for tc in test_cases
            if int(tc.get("testScenarioNumber", 0)) >= retry_from_tc
        ] if retry_from_tc > 1 else test_cases
        result = retry_map_test_cases_batch(
            batch_test_cases,
            flows,
            existing_mappings=existing_mappings,
            start_index=start_index,
            batch_size=batch_size,
        )
        STATE["mappings"] = result.get("mappings", [])
        _save_state()
        print(
            "[retry-mapping] batch "
            f"{result.get('batch_number')}/{result.get('total_batches')}: "
            f"new_mapped={result.get('new_mapped_count', 0)} "
            f"mapped={result.get('mapped_count', 0)}/{result.get('total_count', len(test_cases))} "
            f"complete={result.get('complete', False)} "
            f"warning={result.get('warning') or 'none'}",
            flush=True,
        )
        return jsonify({
            "ok": True,
            "mappings": STATE["mappings"],
            "mapping_warning": result.get("warning"),
            "mapped_count": result.get("mapped_count", 0),
            "total_count": result.get("total_count", len(test_cases)),
            "new_mapped_count": result.get("new_mapped_count", 0),
            "batches_processed": result.get("batch_number", 0),
            "batch_start": result.get("batch_start", start_index),
            "batch_end": result.get("batch_end", end_index),
            "batch_size": result.get("batch_size", batch_size),
            "batch_number": result.get("batch_number", 0),
            "total_batches": result.get("total_batches", 0),
            "next_start_index": result.get("next_start_index", end_index),
            "complete": result.get("complete", False),
            "done": result.get("done", False),
            "paused": STATE.get("pause_requested", False),
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"Retry mapping failed ({type(e).__name__}): {e}",
        }), 500


@app.route("/api/pause-mapping", methods=["POST"])
def pause_mapping():
    """Signal the active retry-mapping run to stop after the current batch."""
    STATE["pause_requested"] = True
    return jsonify({"ok": True, "message": "Pause requested — mapping will stop after current batch."})


@app.route("/api/save-state", methods=["POST"])
def save_state_route():
    """Manually persist current flows + mappings so restart is safe."""
    _save_state()
    return jsonify({
        "ok": True,
        "flows": len(STATE.get("parsed_flows", [])),
        "mappings": len(STATE.get("mappings", [])),
        "state_file": STATE_FILE,
    })


_ALLOWED_FLOWS_DIR = os.path.realpath(tempfile.gettempdir())


def _is_safe_flows_path(path: str) -> bool:
    """Return True only if path resolves within /tmp and ends with .json."""
    if not path:
        return False
    real = os.path.realpath(path)
    return real.startswith(_ALLOWED_FLOWS_DIR + os.sep) and real.endswith(".json")


@app.route("/api/restore-from-file", methods=["POST"])
def restore_from_file():
    """Reload parsed flows from a /tmp flows file after a restart."""
    data = request.get_json() or {}
    flows_file = data.get("flows_file", "")
    if not _is_safe_flows_path(flows_file):
        return jsonify({"ok": False, "error": "Invalid flows file path"}), 400
    real_path = os.path.realpath(flows_file)
    if not os.path.isfile(real_path):
        return jsonify({"ok": False, "error": f"File not found: {flows_file}"}), 400
    try:
        try:
            from tools.capture.flow_parser import parse_flows
        except ImportError:
            from flow_parser import parse_flows
        flows = parse_flows(real_path)
        STATE["flows_file"] = real_path
        STATE["parsed_flows"] = flows
        STATE["mappings"] = []
        _save_state()
        return jsonify({"ok": True, "flow_count": len(flows), "flows_file": real_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/presets", methods=["GET"])
def presets():
    """Return commonIgnore presets."""
    return jsonify(STATE["presets"])


@app.route("/proxy.pac")
def proxy_pac():
    """
    Serve a PAC file that routes only the capture host through mitmdump.
    Chrome fetches and evaluates PAC files directly, bypassing any proxy
    extension that might be overriding system proxy settings.
    """
    host = STATE.get("capture_host", "").strip()
    if host:
        pac = (
            "function FindProxyForURL(url, host) {\n"
            f'    if (host === "{host}") return "PROXY 127.0.0.1:8080";\n'
            '    return "DIRECT";\n'
            "}"
        )
    else:
        pac = 'function FindProxyForURL(url, host) { return "PROXY 127.0.0.1:8080"; }'
    return pac, 200, {"Content-Type": "application/x-ns-proxy-autoconfig"}


@app.route("/api/check-existing-api", methods=["GET"])
def check_existing_api():
    """Check whether the 3 artifact files for an API name already exist and return entry counts."""
    api_name = request.args.get("api_name", "").strip()
    if not api_name:
        return jsonify({"found": False, "missing": [], "error": "api_name required"}), 400

    try:
        from tools.capture.artifact_generator import is_valid_api_name
    except ImportError:
        from artifact_generator import is_valid_api_name
    if not is_valid_api_name(api_name):
        return jsonify({"found": False, "missing": [], "error": "Invalid api_name"}), 400

    payload_path  = os.path.join(PROJECT_ROOT, "fileFromJson", "payloadJSON",  f"{api_name}.json")
    expected_path = os.path.join(PROJECT_ROOT, "fileFromJson", "expectedJSON", f"{api_name}.json")
    inputs_path   = os.path.join(PROJECT_ROOT, "apiInputs",   f"{api_name}.csv")

    missing = [
        p.replace(PROJECT_ROOT + os.sep, "")
        for p in (payload_path, expected_path, inputs_path)
        if not os.path.exists(p)
    ]
    if missing:
        return jsonify({"found": False, "missing": missing})

    def _count_json(path):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            return len(d) if isinstance(d, list) else 0
        except Exception:
            return 0

    def _count_csv(path):
        try:
            with open(path, encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            return max(0, len(rows) - 1)
        except Exception:
            return 0

    return jsonify({
        "found": True,
        "files": {
            "payload":  payload_path.replace(PROJECT_ROOT + os.sep, ""),
            "expected": expected_path.replace(PROJECT_ROOT + os.sep, ""),
            "inputs":   inputs_path.replace(PROJECT_ROOT + os.sep, ""),
        },
        "entry_counts": {
            "payload":  _count_json(payload_path),
            "expected": _count_json(expected_path),
            "inputs":   _count_csv(inputs_path),
        },
    })


@app.route("/api/manifest-options", methods=["GET"])
def manifest_options():
    """Return unique Module, consentPerson, and BusinessFunction values from all manifest CSVs."""
    modules: set = set()
    person: set = set()
    business_functions: set = set()

    for letter in ("A", "B", "C"):
        path = os.path.join(
            PROJECT_ROOT, "apisToBeValidated", f"APIsToBeValidated_{letter}.csv"
        )
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    m  = (row.get("Module") or "").strip()
                    p  = (row.get("consentPerson") or "").strip()
                    bf = (row.get("BusinessFunction") or "").strip()
                    if m  and m  != "NA": modules.add(m)
                    if p  and p  != "NA": person.add(p)
                    if bf and bf != "NA": business_functions.add(bf)
        except Exception:
            pass

    return jsonify({
        "modules":            sorted(modules),
        "person":           sorted(person),
        "business_functions": sorted(business_functions),
    })


@app.route("/api/early-start", methods=["POST"])
def early_start():
    """Skip capture; parse spec + existing suite; LLM-map test cases. Returns suggestions + presets."""
    data = request.get_json() or {}

    STATE["api_name"]     = data.get("api_name", "UnnamedAPI")
    STATE["module"]       = data.get("module", "")
    STATE["manifest"]     = data.get("manifest", "A")
    STATE["owner"]        = data.get("owner", "")
    STATE["capture_host"] = data.get("capture_host", "")
    STATE["early_automation"] = True
    STATE["capturing"] = False
    STATE["parsed_flows"] = []
    STATE["mappings"] = []

    try:
        try:
            from tools.capture.early_suggester import (
                parse_openapi_spec_from_string, scan_existing_apis,
                map_hybrid, generate_skeleton, enrich_spec_bodies,
            )
        except ImportError:
            from early_suggester import (
                parse_openapi_spec_from_string, scan_existing_apis,
                map_hybrid, generate_skeleton, enrich_spec_bodies,
            )

        spec_pool = []
        spec_content = data.get("spec_content")
        if spec_content:
            spec_pool = parse_openapi_spec_from_string(spec_content)
        STATE["early_spec_pool"] = spec_pool

        existing_pool = scan_existing_apis(PROJECT_ROOT)
        STATE["early_existing_pool"] = existing_pool

        suggestions = map_hybrid(STATE["test_cases"], spec_pool, existing_pool)

        # Enrich suggestions with actual request/response bodies
        spec_index = {f"{ep['method']} {ep['path']}": ep for ep in spec_pool}
        existing_index = {}
        for ep in existing_pool:
            key = (f"{ep['method']} {ep['path']}", ep["step"])
            existing_index[key] = ep

        for s in suggestions:
            source = s.get("source")
            api_key = s.get("api_key") or ""
            step = s.get("step")

            if source == "spec":
                ep = spec_index.get(api_key)
                if ep:
                    components = ep.get("components", {})
                    s["request_body"] = generate_skeleton(ep.get("request_schema", {}), components)
                    s["response_body"] = generate_skeleton(ep.get("response_schema", {}), components)
                    s["method"] = ep["method"]
                    s["path"] = ep["path"]
                else:
                    s["request_body"] = {}
                    s["response_body"] = {}
                    s["method"] = ""
                    s["path"] = api_key.split(" ", 1)[-1] if api_key else ""

            elif source == "existing":
                ep = existing_index.get((api_key, step))
                if not ep:
                    # Exact path match without step
                    ep = next((e for e in existing_pool
                                if f"{e['method']} {e['path']}" == api_key), None)
                if not ep:
                    # Normalized path match — LLM may return path with {id} placeholders
                    try:
                        from tools.capture.early_suggester import _normalize_path
                    except ImportError:
                        from early_suggester import _normalize_path
                    norm_key = _normalize_path(api_key.split(" ", 1)[-1]) if " " in api_key else api_key
                    method_part = api_key.split(" ", 1)[0] if " " in api_key else ""
                    ep = next(
                        (e for e in existing_pool
                         if e["method"] == method_part and _normalize_path(e["path"]) == norm_key),
                        None,
                    )
                if ep:
                    s["request_body"] = ep["request_body"]
                    s["response_body"] = ep["response_body"]
                    s["method"] = ep["method"]
                    s["path"] = ep["path"]
                else:
                    s["request_body"] = {}
                    s["response_body"] = {}
                    parts = api_key.split(" ", 1)
                    s["method"] = parts[0] if parts else ""
                    s["path"] = parts[1] if len(parts) > 1 else ""
            else:
                s["request_body"] = {}
                s["response_body"] = {}
                s["method"] = ""
                s["path"] = ""

        # For SPEC matches, enrich bodies with test-objective-aware values
        enrich_spec_bodies(suggestions, STATE["test_cases"])

        # Mark POST→GET linkages
        _mark_post_get_links(suggestions)

        STATE["early_suggestions"] = suggestions
        return jsonify({"ok": True, "suggestions": suggestions, "presets": STATE["presets"]})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _mark_post_get_links(suggestions: list):
    """If TC[n] is GET and TC[n-1] is POST/PUT, link them bidirectionally."""
    for i in range(1, len(suggestions)):
        prev = suggestions[i - 1]
        curr = suggestions[i]
        if (curr.get("method", "").upper() == "GET" and
                prev.get("method", "").upper() in ("POST", "PUT", "PATCH")):
            prev["linked_get_tc"] = curr["tc"]
            curr["linked_post_tc"] = prev["tc"]


@app.route("/api/early-update-get", methods=["POST"])
def early_update_get():
    """Re-suggest a GET row when its linked POST row is edited."""
    data = request.get_json() or {}
    post_data = data.get("post_data", {})
    get_tc = data.get("get_tc", {})

    try:
        try:
            from tools.capture.early_suggester import suggest_get_from_post
        except ImportError:
            from early_suggester import suggest_get_from_post
        suggestion = suggest_get_from_post(post_data, get_tc)
        return jsonify({"ok": True, "suggestion": suggestion})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/confirm", methods=["POST"])
def confirm():
    """Accept confirmed mappings, generate artifacts, run dry-run."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    # Build confirmed dict
    # data should match the artifact_generator.generate() expected input
    try:
        try:
            from tools.capture.artifact_generator import generate
        except ImportError:
            from artifact_generator import generate
        data["append_mode"]     = STATE.get("append_mode", False)
        data["start_from_case"] = STATE.get("start_from_case", 1)
        result = generate(data, PROJECT_ROOT)
        result["ok"] = True
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _open_browser():
    """Open browser after a short delay (gives Flask time to start)."""
    time.sleep(1.5)
    webbrowser.open("http://localhost:5050")


if __name__ == "__main__":
    _init_presets()
    t = threading.Thread(target=_open_browser, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5050, debug=False)
