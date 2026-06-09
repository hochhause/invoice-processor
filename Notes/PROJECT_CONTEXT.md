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
│   ├── main.py              — FastAPI server + routes (HTTP Basic auth gate)
│   ├── auth.py              — Optional HTTP Basic auth (APP_PASSWORD; off when unset)
│   ├── pipeline.py          — Two-function pipeline: run_qr() + run_llm()
│   ├── llm.py               — Claude client: extract_text_stage/extract_image_stage → (fields, usage)
│   ├── cost.py              — Per-model token pricing + estimate_cost()
│   ├── xml_export.py        — ISO 20022 pain.001 generator (per-bank filtering)
│   ├── qr_swiss.py          — Swiss QR-bill (SPC) decoder
│   ├── db.py                — SQLite schema + queries (pain.001 fields + token usage)
│   ├── tests.py             — Startup self-tests (DEV_MODE)
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
│   └── export_screen_poc.html — POC for export screen UI
├── README.md
├── start.sh
├── Dockerfile
├── docker-compose.yml
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
  created_at       TEXT DEFAULT (datetime('now')),
  updated_at       TEXT DEFAULT (datetime('now'))
);
```

> Note: `due_date` is **not** a column — it is parsed transiently for the pain.001
> `ReqdExctnDt` but not persisted. A separate `vendors` table holds receiver→IBAN/BIC.
> Token columns feed the cost tracker (see [[DECISIONS]]).

**Status enum:**
- `QR-processed` — QR scan succeeded
- `LLM-Pending` — awaiting LLM batch
- `LLM-Done` — LLM extraction complete + mandatory fields present
- `needs_review` — missing mandatory fields, needs human review
- `archived` — exported, hidden from main view
- `error` — extraction failed

**bank_target auto-routing (via `derive_bank_target()`):**
- CHF, SEK, EUR → `BKB`
- USD, CAD, GBP → `RAIFFEISEN`
- other → `MANUAL`

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
| GET | `/api/pdf/{id}` | Serve original PDF |
| POST | `/download/confirm` | 409 if not export-ready; else generate + zip both pain.001 files, archive |
| GET | `/download/csv` | Export CSV |
| DELETE | `/api/jobs/{id}` | Delete single job |
| DELETE | `/api/clear-all` | Wipe all non-archived jobs |
| GET | `/api/analytics` | Match-type counts **+ `cost` block** (per-model token totals + USD) |
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

**Debtor (for pain.001)**
```env
DEBTOR_NAME=Lyfegen HealthTech AG
DEBTOR_IBAN=CH5604835012345678009
DEBTOR_BIC=BLKBCH22
```

**Paths**
```env
UPLOAD_DIR=/app/data/uploads
DB_PATH=/app/data/invoices.db
DEBUG_QR_DIR=/app/data/debug_qr   # optional: save QR scan debug images
```

**Feature flags**
```env
DEV_MODE=true|false
```

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
