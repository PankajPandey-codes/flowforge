"""
proxy_manager.py — Start/stop mitmdump and launch a dedicated Chrome capture window.

No admin permissions are required. Instead of modifying macOS system proxy
settings, this module launches Chrome with per-process proxy flags so only the
capture window is routed through mitmdump.
"""

import json
import os
import subprocess
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON_SCRIPT = os.path.join(_HERE, "mitm_addon.py")
_IGNORE_FILE = os.path.join(_HERE, "ignore.txt")

SESSION_FILE  = "/tmp/mitm_session.json"
MITMDUMP_PORT = 8080
FLASK_PORT    = 5050
PAC_URL       = f"http://127.0.0.1:{FLASK_PORT}/proxy.pac"
CHROME_PROFILE_DIR = "/tmp/flowforge_capture_chrome"
CHROME_APP_NAME = "Google Chrome"


def _get_active_interfaces() -> list:
    """Return all active (non-disabled) network service names."""
    result = subprocess.run(
        ["networksetup", "-listallnetworkservices"],
        capture_output=True, text=True,
    )
    return [
        l.strip() for l in result.stdout.strip().splitlines()
        if l.strip()
        and not l.startswith("*")
        and "asterisk" not in l.lower()
        and "denotes" not in l.lower()
    ]


def _kill_existing_mitmdump():
    subprocess.run(["pkill", "-f", "mitmdump"], capture_output=True)
    time.sleep(0.5)


def _reset_capture_profile():
    """Remove any stale temp profile so each run starts clean."""
    subprocess.run(["rm", "-rf", CHROME_PROFILE_DIR], capture_output=True)


def _launch_capture_chrome(capture_host: str = "") -> bool:
    """
    Launch a dedicated Chrome instance that uses the mitm proxy only for this
    window. This avoids system-wide proxy changes and does not require admin.
    """
    _reset_capture_profile()

    start_url = f"https://{capture_host}" if capture_host else "about:blank"
    args = [
        "open",
        "-na",
        CHROME_APP_NAME,
        "--args",
        f"--user-data-dir={CHROME_PROFILE_DIR}",
        f"--proxy-server=http://127.0.0.1:{MITMDUMP_PORT}",
        "--proxy-bypass-list=localhost;127.0.0.1",
        "--ignore-certificate-errors",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        start_url,
    ]
    try:
        result = subprocess.run(args, capture_output=True, timeout=20)
        return result.returncode == 0
    except Exception:
        return False


def start_capture(flows_file: str, capture_host: str = "") -> dict:
    """
    Start mitmdump and launch a dedicated Chrome capture window.

    Chrome is started with per-process proxy flags so traffic from that window
    flows through mitmdump without touching macOS system proxy settings.
    """
    _kill_existing_mitmdump()

    env = os.environ.copy()
    env["MITM_FLOWS_JSON"] = flows_file
    env["CAPTURE_IGNORE_FILE"] = _IGNORE_FILE
    if capture_host:
        env["CAPTURE_HOST"] = capture_host

    mitmdump_proc = subprocess.Popen(
        ["mitmdump", "-s", _ADDON_SCRIPT, "--listen-port", str(MITMDUMP_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    time.sleep(1)
    # mitmdump exits almost immediately if MITMDUMP_PORT is already bound
    # (a documented, known failure mode) -- poll() is None while still
    # running, or the exit code once it's dead. Without this check, a dead
    # mitmdump was silently reported as a successful capture start as long
    # as Chrome opened, and the user only found out via a stuck 0 flow count.
    mitmdump_ok = mitmdump_proc.poll() is None
    chrome_ok = _launch_capture_chrome(capture_host=capture_host)

    session = {
        "flows_file":         flows_file,
        "interfaces":         [],
        "start_time":         time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mitmdump_pid":       mitmdump_proc.pid,
        "mitmdump_ok":        mitmdump_ok,
        "ignore_file":        _IGNORE_FILE,
        "proxy_ok":           chrome_ok and mitmdump_ok,
        "chrome_profile_dir": CHROME_PROFILE_DIR,
        "capture_mode":       "chrome-per-process-proxy",
    }
    with open(SESSION_FILE, "w") as f:
        json.dump(session, f, indent=2)

    return session


def _close_capture_chrome():
    """Kill the dedicated capture Chrome window (matched by its unique profile dir)."""
    subprocess.run(["pkill", "-f", CHROME_PROFILE_DIR], capture_output=True)


def stop_capture() -> dict:
    """Kill mitmdump and close the dedicated Chrome capture window."""
    try:
        with open(SESSION_FILE) as f:
            session = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        session = {}

    subprocess.run(["pkill", "-f", "mitmdump"], capture_output=True)
    _close_capture_chrome()
    time.sleep(1.5)

    session["stop_time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return session


def get_flow_count(flows_file: str) -> int:
    """Count flows written by the addon to the NDJSON file."""
    if not flows_file or not os.path.exists(flows_file):
        return 0
    try:
        with open(flows_file, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0
