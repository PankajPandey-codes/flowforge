"""
Replay captured flows through the active capture proxy.

Example:
    REPLAY_AUTHORIZATION='Bearer ...' \
    python3 tools/flowforge/replay_flows.py \
      --flows /tmp/mitm_flows_1778086804.json \
      --execute

Extra headers can be passed with repeated --header "Name: value" flags or as
JSON in REPLAY_EXTRA_HEADERS.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode


DEFAULT_PROXY = "http://127.0.0.1:8080"
DEFAULT_SCHEME = "https"


def _latest_flow_file() -> Path:
    files = sorted(
        Path("/tmp").glob("mitm_flows_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("No /tmp/mitm_flows_*.json files found")
    return files[0]


def _parse_header(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("headers must be in 'Name: value' format")
    name, header_value = value.split(":", 1)
    name = name.strip()
    header_value = header_value.strip()
    if not name:
        raise argparse.ArgumentTypeError("header name cannot be empty")
    return name, header_value


def _headers_from_env_and_args(args) -> dict:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "capture-replay/1.0",
    }

    authorization = os.environ.get("REPLAY_AUTHORIZATION")
    if authorization:
        headers["Authorization"] = authorization

    cookie = os.environ.get("REPLAY_COOKIE")
    if cookie:
        headers["Cookie"] = cookie

    extra_headers = os.environ.get("REPLAY_EXTRA_HEADERS")
    if extra_headers:
        parsed = json.loads(extra_headers)
        if not isinstance(parsed, dict):
            raise ValueError("REPLAY_EXTRA_HEADERS must be a JSON object")
        headers.update({str(k): str(v) for k, v in parsed.items()})

    for name, value in args.header or []:
        headers[name] = value

    return headers


def _load_flows(path: Path) -> list:
    flows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            flows.append(json.loads(line))
    if not flows:
        raise ValueError(f"{path} contains no flow entries")
    return sorted(flows, key=lambda flow: int(flow.get("id", flow.get("sequence", 0)) or 0))


def _selected_flows(flows: list, args) -> list:
    selected_ids = None
    if args.ids:
        selected_ids = {
            int(part.strip())
            for part in args.ids.split(",")
            if part.strip()
        }

    selected = []
    for flow in flows:
        flow_id = int(flow.get("id", 0) or 0)
        if selected_ids is not None and flow_id not in selected_ids:
            continue
        if args.start_id is not None and flow_id < args.start_id:
            continue
        if args.end_id is not None and flow_id > args.end_id:
            continue
        selected.append(flow)
    return selected


def _url_for_flow(flow: dict, args) -> str:
    host = args.host or flow.get("host")
    if not host:
        raise ValueError(f"Flow {flow.get('id')} does not have a host")

    path = flow.get("path") or "/"
    query_string = flow.get("query_string") or ""
    if isinstance(query_string, dict):
        query_string = urlencode(query_string, doseq=True)

    url = f"{args.scheme}://{host}{path}"
    if query_string:
        url += f"?{query_string}"
    return url


def _request_payload_and_headers(flow: dict, headers: dict) -> tuple[bytes | None, dict]:
    request_headers = dict(headers)
    method = (flow.get("method") or "GET").upper()
    body = flow.get("request_body")
    data = None
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        if isinstance(body, (dict, list)) and body:
            data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str) and body:
            data = body.encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

    return data, request_headers


def _build_opener(args):
    context = ssl._create_unverified_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({
            "http": args.proxy,
            "https": args.proxy,
        }),
        urllib.request.HTTPSHandler(context=context),
    )


def _preview(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")[:500]


def replay(flows: list, headers: dict, args) -> list:
    results = []
    total = len(flows)
    opener = _build_opener(args) if args.execute else None
    for idx, flow in enumerate(flows, start=1):
        flow_id = flow.get("id")
        method = (flow.get("method") or "GET").upper()
        url = _url_for_flow(flow, args)
        print(f"[{idx}/{total}] id={flow_id} {method} {url}", flush=True)

        if not args.execute:
            results.append({
                "id": flow_id,
                "method": method,
                "url": url,
                "dry_run": True,
            })
            continue

        started = time.time()
        try:
            data, request_headers = _request_payload_and_headers(flow, headers)
            request = urllib.request.Request(
                url,
                data=data,
                headers=request_headers,
                method=method,
            )
            response = opener.open(request, timeout=args.timeout)
            elapsed_ms = int((time.time() - started) * 1000)
            preview = _preview(response.read(500))
            result = {
                "id": flow_id,
                "method": method,
                "url": url,
                "status": response.status,
                "elapsed_ms": elapsed_ms,
                "response_preview": preview,
            }
            print(f"    -> {response.status} in {elapsed_ms}ms", flush=True)
        except urllib.error.HTTPError as exc:
            elapsed_ms = int((time.time() - started) * 1000)
            result = {
                "id": flow_id,
                "method": method,
                "url": url,
                "status": exc.code,
                "elapsed_ms": elapsed_ms,
                "response_preview": _preview(exc.read(500)),
            }
            print(f"    -> {exc.code} in {elapsed_ms}ms", flush=True)
        except Exception as exc:
            result = {
                "id": flow_id,
                "method": method,
                "url": url,
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"    -> ERROR {result['error']}", flush=True)
            if args.stop_on_error:
                results.append(result)
                break

        results.append(result)
        if args.sleep > 0:
            time.sleep(args.sleep)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay captured flows through mitmproxy")
    parser.add_argument("--flows", type=Path, default=None, help="Captured flow JSON file. Defaults to latest /tmp/mitm_flows_*.json")
    parser.add_argument("--host", default=None, help="Override host from the flow file")
    parser.add_argument("--scheme", default=DEFAULT_SCHEME, choices=["http", "https"])
    parser.add_argument("--proxy", default=DEFAULT_PROXY)
    parser.add_argument("--header", action="append", type=_parse_header, help="Extra header, e.g. --header 'X-Tenant: abc'")
    parser.add_argument("--ids", default="", help="Comma-separated flow ids to replay")
    parser.add_argument("--start-id", type=int, default=None)
    parser.add_argument("--end-id", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=0.15, help="Delay between requests")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually send requests. Without this, only prints what would be replayed.")
    parser.add_argument("--results", type=Path, default=None, help="Where to write replay results JSON")
    args = parser.parse_args()

    flow_file = args.flows or _latest_flow_file()
    flows = _selected_flows(_load_flows(flow_file), args)
    headers = _headers_from_env_and_args(args)

    print(f"flow_file={flow_file}", flush=True)
    print(f"selected_flows={len(flows)} execute={args.execute} proxy={args.proxy}", flush=True)
    print("headers=" + ", ".join(sorted(headers.keys())), flush=True)

    results = replay(flows, headers, args)
    result_path = args.results or Path(f"/tmp/capture_replay_{int(time.time())}.json")
    with result_path.open("w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"results={result_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
