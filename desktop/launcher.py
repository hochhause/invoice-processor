"""
launcher.py — desktop entry point (PyInstaller target).

Double-click flow:
  1. Mark desktop mode + load <app-data>/settings.env into the environment
     (API key, bank account config) BEFORE app modules read os.environ.
  2. Start uvicorn in-process on 127.0.0.1.
  3. Open the default browser at the dashboard once the server answers.

Quit = close this window (console build) — uvicorn dies with the process.
If the port is already taken by a running instance, we just re-open the
browser tab instead of starting a second server.
"""
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8743  # uncommon on purpose — dev servers love 8000/8080

# Desktop mode must be set before app modules import (paths.py reads it).
os.environ.setdefault("INVOICE_DESKTOP", "1")

if not getattr(sys, "frozen", False):
    # Running from source: make the flat app/ modules importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import paths            # noqa: E402
import settings_store   # noqa: E402


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_our_server(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/settings/status", timeout=2) as r:
            return b"api_key_set" in r.read()
    except Exception:
        return False


def _pick_port() -> int:
    if not _port_in_use(DEFAULT_PORT):
        return DEFAULT_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_browser_when_ready(url: str, probe: str):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(probe, timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    webbrowser.open(url)


def main():
    # Frozen: PyInstaller datas put the template in resource_dir() (_MEIPASS).
    # Source: it sits next to this file in desktop/.
    if getattr(sys, "frozen", False):
        template = paths.resource_dir() / "settings.env.template"
    else:
        template = Path(__file__).resolve().parent / "settings.env.template"
    settings_store.ensure_template(template)
    settings_store.load_into_environ()

    port = int(os.environ.get("INVOICE_PORT", "0")) or DEFAULT_PORT
    if _port_in_use(port):
        if _is_our_server(port):
            print("Invoice Processor is already running — opening browser.")
            webbrowser.open(f"http://127.0.0.1:{port}")
            return
        port = _pick_port()

    url = f"http://127.0.0.1:{port}"
    print("=" * 56)
    print("  Invoice Processor")
    print(f"  Dashboard:  {url}")
    print(f"  Your data:  {paths.app_data_dir()}")
    print("  Keep this window open. Close it to quit the app.")
    print("=" * 56, flush=True)

    if os.environ.get("INVOICE_NO_BROWSER", "").lower() not in ("1", "true"):
        threading.Thread(
            target=_open_browser_when_ready,
            args=(url, f"{url}/api/settings/status"),
            daemon=True,
        ).start()

    import uvicorn
    from main import app  # imports after env is fully prepared
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
