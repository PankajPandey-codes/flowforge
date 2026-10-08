"""
mitm_addon.py — mitmproxy addon that writes captured HTTP flows to a file.

Runs inside mitmdump's own Python process, so the Flask server never needs to
import the mitmproxy Python package — only the mitmdump binary is required.

Flows are written as newline-delimited JSON (one captured flow per line),
appended as they're captured, so writing flow N costs O(1) rather than
rewriting all N flows to disk.

proxy_manager.py starts mitmdump with:
    MITM_FLOWS_JSON=/tmp/flows.json CAPTURE_HOST=dev.example.internal \
        mitmdump -s /path/to/mitm_addon.py

If CAPTURE_HOST is set, only flows whose host matches it are recorded.
If unset, a basic blocklist is applied instead.
"""

import json
import os
import re
import time

from mitmproxy import http

_HERE = os.path.dirname(os.path.abspath(__file__))

FLOWS_JSON          = os.environ.get("MITM_FLOWS_JSON", "/tmp/mitm_flows.json")
CAPTURE_HOST        = os.environ.get("CAPTURE_HOST", "").lower().strip()
CAPTURE_IGNORE_FILE = os.environ.get(
    "CAPTURE_IGNORE_FILE",
    os.path.join(_HERE, "ignore.txt"),
)

# Fallback blocklist used only when CAPTURE_HOST is not set
_BLOCKED_HOST_SUBSTRINGS = [
    "google.com", "googleapis.com", "gstatic.com",
    "apple.com", "icloud.com", "cloudflare.com", "akamai",
]
_BLOCKED_METHODS    = {"OPTIONS", "HEAD"}
_LOCALHOST_HOSTS    = {"localhost", "127.0.0.1"}
_BLOCKED_EXTENSIONS = {
    ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".map", ".gif", ".webp", ".eot", ".otf",
}
_IGNORE_CACHE = {
    "mtime": None,
    "contains": [],
}


def _host_allowed(host: str) -> bool:
    h = host.lower().split(":")[0]
    if CAPTURE_HOST:
        return h == CAPTURE_HOST
    # Fallback: accept unless on blocklist
    if h in _LOCALHOST_HOSTS:
        return False
    return not any(sub in h for sub in _BLOCKED_HOST_SUBSTRINGS)


def _path_allowed(path: str) -> bool:
    p = path.split("?")[0]
    _, ext = os.path.splitext(p)
    if ext.lower() in _BLOCKED_EXTENSIONS:
        return False
    return not _ignored_by_file(path)


def _load_ignore_rules() -> dict:
    try:
        mtime = os.path.getmtime(CAPTURE_IGNORE_FILE)
    except OSError:
        _IGNORE_CACHE.update({"mtime": None, "contains": []})
        return _IGNORE_CACHE

    if _IGNORE_CACHE["mtime"] == mtime:
        return _IGNORE_CACHE

    contains = []
    try:
        with open(CAPTURE_IGNORE_FILE, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                contains.append(line.lower())
    except OSError:
        contains = []

    _IGNORE_CACHE.update({
        "mtime": mtime,
        "contains": contains,
    })
    return _IGNORE_CACHE


def _ignored_by_file(path: str) -> bool:
    rules = _load_ignore_rules()
    path_only = path.split("?", 1)[0].lower()
    return any(needle in path_only for needle in rules["contains"])


def _safe_json(data: bytes):
    if not data:
        return {}
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except Exception:
        text = data.decode("utf-8", errors="replace")
        return text if text.strip() else {}


def _normalize_path(path: str):
    path_only = path.split("?")[0]
    segments = path_only.split("/")
    url_param = "NA"
    normalized = []
    for seg in segments:
        if re.match(r"^\d+$", seg):
            url_param = seg
            normalized.append("{id}")
        else:
            normalized.append(seg)
    return "/".join(normalized), url_param


_sequence: int = 0


def response(flow: http.HTTPFlow) -> None:
    global _sequence

    req  = flow.request
    resp = flow.response
    if resp is None:
        return

    method = req.method.upper()
    host   = req.pretty_host or req.host or ""
    path   = req.path or "/"
    status = resp.status_code

    if method in _BLOCKED_METHODS:
        return
    if not _host_allowed(host):
        return
    if not _path_allowed(path):
        return
    if method == "GET" and "text/html" in resp.headers.get("content-type", ""):
        return

    request_body   = _safe_json(req.content)  if req.content  else {}
    response_body  = _safe_json(resp.content) if resp.content else {}
    path_normalized, url_param = _normalize_path(path)
    qs = path.split("?", 1)[1] if "?" in path else ""

    try:
        ts = time.strftime("%H:%M:%S", time.localtime(req.timestamp_start))
    except Exception:
        ts = ""

    _sequence += 1
    entry = {
        "id":              _sequence,
        "sequence":        _sequence,
        "method":          method,
        "host":            host,
        "path":            path.split("?")[0],
        "path_normalized": path_normalized,
        "url_param":       url_param,
        "query_string":    qs,
        "request_body":    request_body,
        "response_body":   response_body,
        "status":          status,
        "timestamp":       ts,
    }

    try:
        # Real, unredacted captured banking data -- lock the file down to
        # the owner like STATE_FILE (server.py) already is, rather than
        # leaving it at whatever the process umask happens to allow.
        fd = os.open(FLOWS_JSON, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
