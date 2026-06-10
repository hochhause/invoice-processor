"""
paths.py — central path resolution for the three deployment modes.

Modes (detected, no config needed):
  1. Container / legacy dev:  defaults to /app/data  (unchanged behaviour)
  2. Desktop (PyInstaller frozen, or INVOICE_DESKTOP=1): per-user app-data dir
       Windows:  %APPDATA%\\InvoiceProcessor
       macOS:    ~/Library/Application Support/InvoiceProcessor
       Linux:    $XDG_DATA_HOME/invoice-processor (~/.local/share fallback)
  3. Explicit env override: DB_PATH / UPLOAD_DIR always win when set.

Also exposes resource_dir() — the directory holding bundled read-only assets
(templates/, static/, schemas/). Inside a PyInstaller bundle that is
sys._MEIPASS; otherwise the app/ source directory. Never write there: a
packaged app may live in a read-only location.
"""
import os
import sys
from pathlib import Path

APP_NAME = "InvoiceProcessor"


def is_desktop() -> bool:
    """True when running as a packaged desktop app (or forced via env)."""
    return bool(getattr(sys, "frozen", False)) or \
        os.environ.get("INVOICE_DESKTOP", "").lower() in ("1", "true", "yes")


def resource_dir() -> Path:
    """Read-only bundled assets (templates/static/schemas)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def app_data_dir() -> Path:
    """Per-user writable data dir for desktop mode. Created on first call."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        d = base / APP_NAME
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        d = base / "invoice-processor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    """Writable data root: app-data on desktop, /app/data in the container."""
    if is_desktop():
        return app_data_dir()
    return Path("/app/data")


def db_path() -> str:
    return os.environ.get("DB_PATH") or str(data_dir() / "invoices.db")


def upload_dir() -> Path:
    return Path(os.environ.get("UPLOAD_DIR") or data_dir() / "uploads")
