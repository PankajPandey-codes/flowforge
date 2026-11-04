import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from flow_parser import parse_flows


def test_parse_flows_reads_ndjson_lines(tmp_path):
    flows_file = tmp_path / "flows.ndjson"
    flows_file.write_text(
        '{"id": 1, "path": "/a"}\n'
        '{"id": 2, "path": "/b"}\n'
    )

    result = parse_flows(str(flows_file))

    assert [f["path"] for f in result] == ["/a", "/b"]


def test_parse_flows_skips_incomplete_trailing_line(tmp_path):
    """A trailing line still being written by the addon must not fail the read."""
    flows_file = tmp_path / "flows.ndjson"
    flows_file.write_text(
        '{"id": 1, "path": "/a"}\n'
        '{"id": 2, "path": "/b"'  # no closing brace/newline — mid-write
    )

    result = parse_flows(str(flows_file))

    assert [f["path"] for f in result] == ["/a"]


def test_parse_flows_missing_file_returns_empty(tmp_path):
    assert parse_flows(str(tmp_path / "missing.ndjson")) == []
