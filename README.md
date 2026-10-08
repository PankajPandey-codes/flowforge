# FlowForge

Turn real browser traffic into ready-to-use API test fixtures — no admin rights, no manual JSON hand-typing.

FlowForge is a local, browser-driven capture tool for building data-driven API regression tests. You perform an action in a dedicated Chrome window, FlowForge records the HTTP traffic, optionally uses an LLM to map each captured request to the right test case, and generates the payload/expected-response JSON and manifest rows your test framework needs — with sensitive data redacted before anything is ever sent to an LLM.

## What it does

A single Flask app (`server.py`, port 5050) drives a 4-stage workflow in the browser:

1. **Capture** — Launches `mitmdump` on a local port plus an isolated, dedicated Chrome window configured with per-process proxy flags. No system proxy changes, no admin prompt, no CA trust changes system-wide. Traffic to/from your target host is recorded as newline-delimited JSON (one flow per line).
2. **Parse & Map** — Captured flows are matched against the test cases in an uploaded CSV. An LLM (via the Anthropic API, or a local `claude` CLI fallback) can auto-suggest which flow belongs to which test case/step.
3. **Redact-before-LLM** — Before any captured data is sent to an LLM for mapping, leaf values are masked (strings → `<redacted>`, numbers → `0`). Identifier-style keys (e.g. `id`, `accountNo`) are deliberately exempted so the LLM can still disambiguate between similar requests.
4. **Generate artifacts** — Writes out:
   - `apiInputs/<APIName>.csv`
   - `fileFromJson/payloadJSON/<APIName>Request.json`
   - `fileFromJson/expectedJSON/<APIName>Response.json`
   - an appended row in `apisToBeValidated/APIsToBeValidated_<X>.csv`

There's also a standalone CLI, `replay_flows.py`, for replaying a previously saved flow file through the proxy for debugging.

## Requirements

- **macOS only** (Chrome capture relies on macOS-specific per-process proxy launch flags)
- Python 3.9+
- Google Chrome installed at `/Applications/Google Chrome.app`
- Free local ports `5050` (Flask UI) and `8080` (mitmdump)
- `pip3 install flask mitmproxy anthropic` (the `anthropic` package is optional — the tool works fully without it, you just map flows to test cases manually in Stage 2 instead of getting LLM suggestions)

See [`SETUP.md`](./SETUP.md) for full setup, troubleshooting, and first-run HTTPS certificate steps.

## Where this expects to live

FlowForge assumes it's dropped two directories below the root of the test-framework project it's generating artifacts for — e.g. `<your-project>/tools/flowforge/` — because `server.py` resolves `PROJECT_ROOT` by walking up two directories from its own location. It then reads/writes the `apiInputs/`, `fileFromJson/`, and `apisToBeValidated/` folders relative to that resolved root.

It's built against a CSV-manifest-driven test framework convention (a manifest CSV lists which APIs to test; each API has an `apiInputs` CSV plus payload/expected JSON files). If your project doesn't follow that layout, you'll need to adapt `artifact_generator.py`.

## Quick start

```bash
pip3 install flask mitmproxy anthropic   # anthropic is optional
export ANTHROPIC_API_KEY="sk-ant-..."    # optional, enables LLM auto-mapping

python3 server.py
# Opens automatically at http://localhost:5050
```

## Security notes

- Captured flow files are written to `/tmp` with `0600` permissions and are never committed to this repo — they can contain real, unredacted request/response data from whatever host you point the capture at.
- Data is redacted (see Stage 3 above) before it is ever sent to an LLM.
- The Anthropic API key is read from the environment only (`ANTHROPIC_API_KEY`) — it's never written to or read from any file in this repo.

## Running the tests

```bash
python3 -m pytest
```

## Contributing

This is a personal project. Issues and PRs are welcome.
