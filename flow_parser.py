"""
flow_parser.py — Read the newline-delimited JSON flows file written by
mitm_addon.py.

The heavy lifting (filtering, body parsing, path normalisation) now happens
inside the addon that runs in mitmdump's own process.  This module is a thin
reader so the Flask server never needs to import the mitmproxy Python package.
"""

import json
import os


def parse_flows(flows_file: str) -> list:
    """
    Read captured flows from the NDJSON file written by mitm_addon.py.
    Returns [] if the file does not exist. A trailing line still being
    written by the addon is skipped rather than failing the whole read.
    """
    if not flows_file or not os.path.exists(flows_file):
        return []
    flows = []
    try:
        with open(flows_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    flows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"[flow_parser] ERROR reading {flows_file}: {e}", flush=True)
        return []
    return flows
