# Desktop build

Packages the invoice processor as a double-clickable app: one folder, one
`InvoiceProcessor.exe` (or mac binary) that starts the server on
`127.0.0.1:8743` and opens the browser. No Python, no Docker, no terminal.

## How it works

- `launcher.py` — entry point. Loads `settings.env` from the user's app-data
  folder into the environment, starts uvicorn in-process, opens the browser
  when the server answers. Closing the console window quits the app. If an
  instance is already running it just re-opens the browser tab.
- `app/paths.py` — all writable data (`invoices.db`, `uploads/`, `settings.env`)
  lives in the per-user app-data folder, never next to the executable:
  - Windows: `%APPDATA%\InvoiceProcessor`
  - macOS: `~/Library/Application Support/InvoiceProcessor`
  - Container/server mode keeps `/app/data` — behaviour there is unchanged.
- `settings.env.template` — copied to app-data on first run; holds the bank
  account config (fill the IBANs once before shipping, or edit on the target
  machine).
- API key — never baked into the build. On first launch the dashboard shows a
  one-time prompt; the key is saved to `settings.env` and applies immediately.
- QR decoding uses zxing-cpp (bundles cleanly). pyzbar is excluded — it needs
  a system library PyInstaller can't ship, and `qr_swiss.py` treats it as
  optional.

## Build

PyInstaller does not cross-compile — build on the OS you ship to.
Easiest: GitHub Actions (`.github/workflows/desktop-build.yml`) — run the
`desktop-build` workflow manually or push a `desktop-v*` tag, then download
the artifact zip.

Local build:

```
pip install -r desktop/requirements-desktop.txt
pyinstaller desktop/InvoiceProcessor.spec --noconfirm
# result: dist/InvoiceProcessor/  → zip the folder and send it
```

Env knobs (mostly for testing): `INVOICE_PORT` (fixed port),
`INVOICE_NO_BROWSER=1` (don't open a browser), `INVOICE_DESKTOP=1`
(force desktop paths when running from source).

## What to tell the recipient

1. Unzip anywhere (Desktop is fine), open the folder, double-click
   `InvoiceProcessor.exe`.
2. **Windows SmartScreen**: click "More info" → "Run anyway" (first time only).
   **macOS Gatekeeper**: right-click the app → Open (first time only).
3. A black window appears (that's the app) and the browser opens by itself.
   Keep the black window open while working; close it to quit.
4. First launch asks for the API key — paste the one you were given. Done
   once, remembered afterwards.

Updates are manual: rebuild, send a new zip, they replace the folder. Their
data is safe — it lives in the app-data folder, not in the app folder.
