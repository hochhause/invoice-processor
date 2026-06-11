# Invoice Processor — Project Context (feat/llm-first)

## Tech Stack

**Languages & Runtime**
- Python 3.11+ (backend logic)
- HTML5 + JavaScript (frontend dashboard + export screen)
- Bash/sh (Docker entrypoint)

**Core Libraries**
| Library | Version | Purpose |
|---------|---------|---------|
| FastAPI | latest | REST API + web framework |
| pyzbar | latest | Swiss QR-bill barcode decoding (primary) |
| zxingcpp | latest | QR fallback decoder |
| pymupdf/fitz | latest | PDF page rendering to PNG (for LLM vision) |
| anthropic | latest | Claude Haiku API client (structured extraction) |
| xmlschema | >=2.4.0 | pain.001.001.09 XSD validation (T10) — pure-Python, no libxml2 build |
| SQLite3 | bundled | Job database |
| Jinja2 | latest | Template rendering |

**Removed Libraries**
- markitdown — no longer needed (LLM replaces markdown OCR)
- pdfplumber — removed
- Ollama client — removed
- DeepSeek client — removed

**External Services**
- Anthropic Claude API (Haiku) — structured invoice field extraction

**Container & DevOps**
- Podman / Docker (OCI-compatible)
- Docker Compose / Podman Compose

---

## Folder Structure

```
invoice-processor/
├── app/
│   ├── main.py              — FastAPI server + routes; _accounts() → config.get_accounts(); download route passes accounts to build_pain001 (T8); _export_blockers() three-rule gate: incomplete | unresolvable_account | cross_border_incomplete (T9); /api/accounts-summary exposes config-driven ccy→account-ccy map for export sub-totals (T13) [[DECISIONS#Per-Account Debtor Model]] [[DECISIONS#Export Hard-Blocked on Incomplete Invoices]]
│   ├── auth.py              — Optional HTTP Basic auth (APP_PASSWORD; off when unset)
│   ├── config.py            — Per-bank/ccy account config (load_accounts, resolve_account) [[DECISIONS#Per-Account Debtor Model]]
│   ├── pipeline.py          — Two-function pipeline: run_qr() + run_llm()
│   ├── llm.py               — Claude client: extract_text_stage/extract_image_stage → (fields, usage); PROMPT extracts 13 keys incl. cdtr_* address fields (T6) [[DECISIONS#LLM Returns Structured JSON (not raw OCR text)]]
│   ├── cost.py              — Per-model token pricing + estimate_cost()
│   ├── xml_export.py        — ISO 20022 pain.001.001.09 generator: build_pain001(jobs, accounts, bank); per-ccy PmtInf debits config.resolve_account(bank, ccy) (DbtrAcct/Ccy=account ccy, InstdAmt/Ccy=payment ccy/FX); service level **currency-driven** (CHF→NURG, EUR→SEPA, else NURG+SWIFT — works for settings-added banks) [[DECISIONS#In-App Settings + Config-Driven Bank UI (branch: desktop)]]; ChrgBr at PmtInf level (SEPA→SLEV, SWIFT→SHAR); structured Cdtr/PstlAdr (_cdtr_address); validates vs SIX CH XSD; T9 blockers gate upstream [[DECISIONS#Per-Account Debtor Model]] [[DECISIONS#pain.001.001.09 Migration]]
│   ├── qr_swiss.py          — Swiss QR-bill (SPC) decoder (pyzbar optional; zxing-cpp always)
│   ├── paths.py             — Path resolution: container `/app/data` vs desktop app-data; resource_dir() for bundled assets [[DECISIONS#Desktop Packaging — PyInstaller onedir + app-data]]
│   ├── settings_store.py    — Desktop settings.env (load into environ before app imports; set_value persists + applies)
│   ├── db.py                — SQLite schema + queries (pain.001 fields + token usage); DB_PATH via paths.db_path()
│   ├── tests.py             — Startup self-tests (DEV_MODE; excluded from desktop bundle)
│   ├── templates/
│   │   ├── index.html       — Main dashboard (table + edit modal)
│   │   └── export.html      — Export screen (drag board + download)
│   ├── static/
│   │   ├── css/
│   │   └── js/
│   └── __pycache__/
├── data/
│   └── invoices.db          — SQLite DB (volume-mounted)
├── Notes/
│   ├── PROJECT_CONTEXT.md   — This file
│   ├── DECISIONS.md         — [[DECISIONS]]
│   ├── Features.md          — [[Features]]
│   └── Plan.md              — [[Plan]]
├── desktop/                 — Desktop packaging (branch: desktop) [[DECISIONS#Desktop Packaging — PyInstaller onedir + app-data]]
│   ├── launcher.py          — Entry point: load settings.env → uvicorn on 127.0.0.1:8743 → open browser
│   ├── InvoiceProcessor.spec — PyInstaller onedir spec (datas: templates/static/schemas; excludes pyzbar/tests)
│   ├── settings.env.template — Seeded to app-data on first run (bank config, models)
│   ├── requirements-desktop.txt — requirements.txt minus pyzbar/xmlschema + pyinstaller
│   └── README.md            — Build + recipient instructions
├── .github/workflows/desktop-build.yml — Windows+macOS PyInstaller builds (manual / desktop-v* tag)
├── README.md
├── start.sh
├── Dockerfile
├── compose.yaml
├── .env.example
└── .gitignore
```

**Deleted from prior architecture:**
- `app/extract.py` — regex extraction (removed)
- `app/md_clean.py` — garbage detection (removed)
- `app/rules/` — bank-specific regex overrides (removed)

---

## Entry Points

**Web Server**
- `app/main.py` → FastAPI lifespan
  - Routes: `/` (dashboard), `/export` (export screen), `/api/*`, `/download/*`

**Background Processing**
- QR scan: sync on upload (fast, no background task needed)
- LLM batch: `POST /api/run-llm-batch` → BackgroundTask

**Local Dev**
```bash
uvicorn app.main:app --reload --port 8000
```

**Desktop (PyInstaller / from source)**
```bash
python desktop/launcher.py        # source; INVOICE_NO_BROWSER=1, INVOICE_PORT=n for testing
pyinstaller desktop/InvoiceProcessor.spec --noconfirm   # build → dist/InvoiceProcessor/
```
Desktop mode (`sys.frozen` or `INVOICE_DESKTOP=1`) moves all writable data to
`%APPDATA%\InvoiceProcessor` (Win) / `~/Library/Application Support/InvoiceProcessor` (mac);
config is loaded from `<app-data>/settings.env` before app imports. See [[DECISIONS#Desktop Packaging — PyInstaller onedir + app-data]].

**Startup Self-Tests (T11)**
```bash
cd app && DEV_MODE=true python -c "from tests import run_startup_tests; run_startup_tests()"
```
Requires env config (BKB_CURRENCIES, etc.); see [[DECISIONS#Per-Account Debtor Model (.env-driven)]]. Checks cover IBAN/BIC validation, pain.001 generation, multi-currency, vendor IBAN priority (space-insensitive), routing, QR mock, LLM JSON, cost tracking, config loader, the three **`.09` breaking deltas** asserted directly on emitted XML (`Tdelta`: namespace, BICFI on both agents, ReqdExctnDt/Dt), the **per-account FX mechanic** (`Tfx`: CHF+SEK share BKB-CHF IBAN, EUR own IBAN, account-ccy vs payment-ccy), and **XSD validation vs SIX CH schema** (`Txsd`). All pass when env is set. See [[DECISIONS#Test Suite (T11 DONE — Opus verify pass 2026-06-10)]].

---

## Pipeline Architecture

### Step 1 — On Upload (sync)
```
PDF file
  ↓
[QR Scan] qr_swiss.extract_from_pdf()
  ↓
  QR found → status=QR-processed, bank_target=auto
  QR not found → status=LLM-Pending
  ↓
upsert_job()
```

### Step 2 — Manual LLM Batch (async, operator-triggered)
```
POST /api/run-llm-batch
  ↓
fetch all jobs WHERE status=LLM-Pending
  + QR jobs missing bic (non-CHF) or due_date
  ↓
[LLM] llm.extract_fields(pdf_path)
  → Claude Haiku vision: PDF pages as PNG → structured JSON
  ↓
merge fields + validate IBAN MOD-97
  ↓
status=LLM-Done (or needs_review if mandatory fields empty)
bank_target=auto
  ↓
upsert_job()
```

### Step 3 — Export Screen
```
GET /export
  ↓
Load all non-archived jobs → pre-sorted by bank_target
  ↓
Operator drags/reassigns → POST /api/assign-bank/{id}
  ↓
POST /download/confirm
  → generate pain001_BKB.xml   (bank_target=BKB)
  → generate pain001_Raiffeisen.xml (bank_target=RAIFFEISEN)
  → flag sorted jobs: status=archived
  → return zip or sequential download
```

---

## Database Schema

```sql
CREATE TABLE jobs (
  id               TEXT PRIMARY KEY,
  filename         TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'LLM-Pending',
  receiver         TEXT DEFAULT '',
  iban             TEXT DEFAULT '',
  bic              TEXT DEFAULT '',
  amount           TEXT DEFAULT '',
  currency         TEXT DEFAULT '',
  reference        TEXT DEFAULT '',
  invoice_id       TEXT DEFAULT '',
  bank_target      TEXT DEFAULT '',
  iban_source      TEXT DEFAULT '',    -- document | document_mismatch | database | llm | manual
  iban_mismatch_db TEXT DEFAULT '',    -- vendor-DB IBAN when extracted IBAN disagrees
  match_type       TEXT DEFAULT '',    -- '' (qr) | text_full | hybrid | image_only | failed
  input_tokens     INTEGER DEFAULT 0,  -- LLM cost tracking
  output_tokens    INTEGER DEFAULT 0,
  llm_model        TEXT DEFAULT '',    -- model that produced this job (drives cost.estimate_cost)
  cdtr_street      TEXT DEFAULT '',    -- creditor address for cross-border pain.001 (T7) [[DECISIONS#pain.001.001.09 Migration]]
  cdtr_building_no TEXT DEFAULT '',
  cdtr_postcode    TEXT DEFAULT '',
  cdtr_town        TEXT DEFAULT '',
  cdtr_country     TEXT DEFAULT '',    -- ISO 3166-1 alpha-2 or ''
  created_at       TEXT DEFAULT (datetime('now')),
  updated_at       TEXT DEFAULT (datetime('now'))
);
```

> Note: `due_date` is **not** a column — it is parsed transiently for the pain.001
> `ReqdExctnDt` but not persisted. A separate `vendors` table holds receiver→IBAN/BIC.
> Token columns feed the cost tracker (see [[DECISIONS]]).
> `cdtr_*` columns added by T7 — supply the structured `Cdtr/PstlAdr` emitted by
> `_cdtr_address` in `xml_export.py`; existing rows default to `''` (silently no
> address emitted until re-extracted or edited). See [[DECISIONS#pain.001.001.09 Migration]].

**Status enum:**
- `QR-processed` — QR scan succeeded
- `LLM-Pending` — awaiting LLM batch
- `LLM-Done` — LLM extraction complete + mandatory fields present
- `needs_review` — missing mandatory fields, needs human review
- `archived` — exported, hidden from main view
- `error` — extraction failed

**bank_target auto-routing (via `derive_bank_target()` — config-driven since T2):**
- Looks up `config.get_accounts()["ccy_bank_index"]` — the ccy→bank map built from `{BANK}_CURRENCIES` env keys.
- Unknown ccy (not in any bank's CURRENCIES list) → `MANUAL`.
- Default mapping (when `.env` uses the standard config): CHF/SEK/EUR → `BKB`; USD/CAD/GBP → `RAIFFEISEN`.
- See [[DECISIONS#Per-Account Debtor Model]].

---

## API Routes

| Method | Path | Action |
|--------|------|--------|
| GET | `/` | Dashboard |
| GET | `/export` | Export screen |
| POST | `/api/upload` | Upload PDF + QR scan |
| GET | `/api/jobs` | List jobs (excludes archived by default) |
| POST | `/api/review/{id}` | Save edited fields |
| POST | `/api/run-llm-batch` | Trigger LLM on all LLM-Pending jobs |
| POST | `/api/assign-bank/{id}` | Override bank_target |
| GET | `/api/export-readiness` | `{ready, blockers[]}` — gate export, drive popup |
| GET | `/api/accounts-summary` | `{BANK:{default_ccy, resolve:{ccy:acct_ccy}}}` — which debtor acct each ccy debits; drives export per-account sub-totals (T13) |
| GET | `/api/pdf/{id}` | Serve original PDF |
| POST | `/download/confirm` | 409 if not export-ready; else generate + zip both pain.001 files, archive |
| GET | `/download/csv` | Export CSV |
| DELETE | `/api/jobs/{id}` | Delete single job |
| DELETE | `/api/clear-all` | Wipe all non-archived jobs |
| GET | `/api/analytics` | Match-type counts **+ `cost` block** (per-model token totals + USD) |
| GET | `/api/settings/status` | `{desktop, api_key_set}` — first-run probe (opens settings popup when unset) |
| GET | `/api/settings` | Full editable config: `{desktop, api_key_set, debtor_name, llm_model, banks:[{name, currencies, default_ccy, accounts:[{ccy,iban,bic}]}]}` |
| POST | `/api/settings` | Persist full config to `<app-data>/settings.env` (validates bank/ccy/IBAN-MOD97/BIC; removes stale bank keys; clears config cache → applies live); 403 outside desktop mode |
| GET/POST/PUT/DELETE | `/api/vendors[/{id}]` | Vendor IBAN/BIC CRUD |

> **Auth:** when `APP_PASSWORD` is set, every route above requires HTTP Basic
> (applied as an app-wide `Depends(require_auth)`). `/static/*` is intentionally open.

---

## Environment Variables

**LLM**
```env
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5-20251001        # image stage; set claude-sonnet-4-6 to switch to Sonnet
LLM_MODEL_TEXT=claude-haiku-4-5-20251001   # text stage (keep in sync with LLM_MODEL)
```
> Switching models is just a config change — cost.py reprices automatically per the
> `llm_model` stored on each job. Sonnet 4.6 is $3/$15 vs Haiku 4.5 $1/$5 per 1M tok.

**Auth (optional — for secure deployment)**
```env
APP_PASSWORD=          # set → every page + API route requires HTTP Basic; unset/empty → auth DISABLED
APP_USERNAME=admin     # optional, defaults to "admin"
```

**Debtor + per-account config (for pain.001) — see [[DECISIONS#Per-Account Debtor Model]]**
```env
DEBTOR_NAME=Lyfegen HealthTech AG

# BKB: CHF domestic + SEPA (SEK has no own acct → falls back to CHF default)
BKB_CURRENCIES=CHF,EUR,SEK
BKB_DEFAULT_CCY=CHF
BKB_CHF_IBAN=<CH IBAN>        # spaces allowed — config.load_accounts normalizes
BKB_EUR_IBAN=<CH IBAN>
BKB_BIC=BLKBCH22              # bank-level fallback; {BANK}_{CCY}_BIC overrides per account

# Raiffeisen: cross-border (SWIFT)
RAIFFEISEN_CURRENCIES=USD,CAD,GBP
RAIFFEISEN_DEFAULT_CCY=USD
RAIFFEISEN_USD_IBAN=<CH IBAN>
RAIFFEISEN_CAD_IBAN=<CH IBAN>
RAIFFEISEN_GBP_IBAN=<CH IBAN>
RAIFFEISEN_BIC=RAIFCH22
```
> IBANs are normalized (whitespace stripped, uppercased) at load. `{BANK}_BIC`
> fills any account missing a per-ccy BIC. The settings popup saves explicit
> per-ccy keys and removes the bank-level fallback key it supersedes.
> `DEBTOR_IBAN` / `DEBTOR_BIC` are superseded by the per-bank keys above — **T12 cleaned** them out of `.env.example` (only `DEBTOR_NAME` + the per-bank/ccy keys remain). This section is the source of truth the `.env.example` body mirrors.

**Paths**
```env
UPLOAD_DIR=/app/data/uploads
DB_PATH=/app/data/invoices.db
DEBUG_QR_DIR=/app/data/debug_qr   # optional: save QR scan debug images
```

**Feature flags**
```env
DEV_MODE=true|false      # desktop build excludes tests.py — leave false there
```

**Desktop mode (branch: desktop)**
```env
INVOICE_DESKTOP=1        # force desktop paths when running from source (frozen builds auto-detect)
INVOICE_PORT=8743        # fixed port (default 8743)
INVOICE_NO_BROWSER=1     # suppress auto-open (CI smoke tests)
```
> Desktop config lives in `<app-data>/settings.env` (same KEY=VALUE vocabulary as `.env`);
> real env vars always win. `DB_PATH`/`UPLOAD_DIR` defaults switch to app-data in desktop
> mode, stay `/app/data/*` in the container.

---

## Naming Conventions

**Job statuses:** `QR-processed | LLM-Pending | LLM-Done | needs_review | archived | error`
**Bank targets:** `BKB | RAIFFEISEN | MANUAL`
**Field keys:** `receiver, iban, bic, amount, currency, due_date, reference, invoice_id`

---

## Key Architectural Decisions

See [[DECISIONS]] for full rationale. Summary:

1. QR scan on upload (sync, free) → LLM batch manual trigger (cost control)
2. LLM returns structured JSON directly (no regex post-processing)
3. Claude Haiku only (no Ollama/DeepSeek)
4. DB stores only pain.001-relevant fields
5. Export screen as dedicated route with drag-board for bank assignment
6. `archived` status instead of delete (audit trail)
7. Unsorted invoices excluded from export (not blocking — operator confirms)
8. HTTP Basic auth (single shared `APP_PASSWORD`), disabled when env unset — see [[DECISIONS]]
9. Cost tracker from real API token usage; model-aware pricing → Sonnet switch is config-only
10. Testing phase: dev tunnel (`lyfegen-invoice-test`, Entra-tenant-gated) instead of deploy — see [[DECISIONS#Testing-Phase Access — Microsoft Dev Tunnels (branch: desktop, 2026-06-11)]]

---

## Testing-Phase Access (branch: desktop)

- `scripts/start-test-tunnel.ps1` — starts `dist\InvoiceProcessor\InvoiceProcessor.exe`
  (INVOICE_NO_BROWSER=1) + hosts dev tunnel `lyfegen-invoice-test` (port 8743);
  prereq one-time commands in the script header (`devtunnel create/port/access --tenant`).
- `docs/confluence/invoice-processor-user-guide.html` — tester-facing Confluence page
  (storage format); `docs/confluence/README.md` — how to publish + URL check.
- Tunnel URL: `https://x3m2th39-8743.euw.devtunnels.ms` (stable; stored on the persistent
  tunnel `lyfegen-invoice-test.euw` — changes only if the tunnel is deleted + recreated).
