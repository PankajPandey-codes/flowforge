# FlowForge — Setup Guide

> **macOS only.** Chrome capture works without admin permissions by launching a
> dedicated browser window with per-process proxy flags.

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| macOS | 12 Monterey+ | Intel or Apple Silicon |
| Python | 3.9+ | Check: `python3 --version` |
| Google Chrome | Any recent | Must be installed at `/Applications/Google Chrome.app` |
| Free ports | 5050, 8080 | Tool will fail silently if these are in use |

---

## 1. Install Python dependencies

```bash
pip3 install flask mitmproxy anthropic
```

Verify `mitmdump` is on your PATH after install:

```bash
mitmdump --version
```

If not found, add pip's bin to your PATH:

```bash
export PATH="$HOME/Library/Python/3.x/bin:$PATH"   # adjust version number
```

---

## 2. Set your Anthropic API key (optional)

The API key enables LLM auto-mapping of captured flows to test cases.
**Without it the tool still works fully** — you just map manually in Stage 3.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

To make it permanent, add to `~/.zshrc`:

```bash
echo 'export ANTHROPIC_API_KEY="your-key-here"' >> ~/.zshrc
source ~/.zshrc
```

> **Security note:** Never commit your API key. The tool reads it from the
> environment only — it is never written to any file in this repo.

---

## 3. Run the tool

From the **repo root**:

```bash
python3 tools/flowforge/server.py
```

Opens automatically at **http://localhost:5050**.

---

## 4. Chrome capture

On **Start Capture**, the tool launches a dedicated Chrome window configured with:

- `--proxy-server=http://127.0.0.1:8080`
- `--proxy-bypass-list=localhost;127.0.0.1`
- `--ignore-certificate-errors`
- a temporary isolated `--user-data-dir` at `/tmp/flowforge_capture_chrome`

This means **no system proxy changes, no admin prompt, no CA certificate trust required**.

The window closes automatically when you click **Stop Capture**.

### Capture ignore rules

The capture addon skips requests listed in `tools/flowforge/ignore.txt` before
writing the flow file. Edit that file to reduce noisy captures before LLM
auto-mapping runs.

- `/assets` means substring match anywhere in the URL path.
- `/token` means substring match anywhere in the URL path.
- Matching is case-insensitive.

### First-time HTTPS capture (one-off)

If you see TLS errors on first run, visit `http://mitm.it` inside the capture
window and install the mitmproxy CA certificate. This is a one-time step per
machine and is stored only in the temporary Chrome profile.

---

## 5. Port conflicts

If port 5050 or 8080 is already in use:

```bash
# Find what's using them
lsof -i :5050
lsof -i :8080

# Kill the process if safe to do so
kill -9 <PID>
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mitmdump: command not found` | Run `pip3 install mitmproxy` and check PATH (step 1) |
| Chrome window doesn't open | Confirm Chrome is at `/Applications/Google Chrome.app` |
| 0 requests captured | Make sure you're doing the transaction in the dedicated capture window, not your main Chrome |
| Auto-mapping returns empty | `ANTHROPIC_API_KEY` not set — map manually or set the key |
| Port already in use | See step 5 above |
| `ModuleNotFoundError: flask` | Run `pip3 install flask mitmproxy anthropic` again |
