# Invoice Processor — Project Context

## Tech Stack

**Languages & Runtime**
- Python 3.11+ (backend logic)
- HTML5 + JavaScript (frontend dashboard)
- Bash/sh (Docker entrypoint)

**Core Libraries**
| Library | Version | Purpose |
|---------|---------|---------|
| [FastAPI](https://fastapi.tiangolo.com) | latest | REST API + web framework |
| [markitdown](https://github.com/microsoft/markitdown) | latest | PDF → Markdown text extraction |
| [pdfplumber](https://github.com/jamesturk/pdfplumber) | latest | PDF text/table extraction (fallback) |
| [SQLite3](https://www.sqlite.org/) | bundled | Job database |
| [Ollama](https://ollama.ai) | 0.3.0+ | Local LLM inference (optional) |
| [pyzbar](https://github.com/NaturalHistoryMuseum/pyzbar-x) | latest | Swiss QR-bill barcode decoding |
| [zxingcpp](https://github.com/zxing-cpp/zxing-cpp) | latest | QR fallback decoder |
| [pymupdf/fitz](https://pymupdf.io/) | latest | PDF page rendering to PNG |
| [Jinja2](https://jinja.palletsprojects.com/) | latest | Template rendering (Flask/FastAPI) |

**External Services (Optional)**
- Anthropic Claude API — LLM OCR fallback
- DeepSeek API — Alternative LLM provider

**Container & DevOps**
- Podman / Docker (OCI-compatible container runtime)
- Docker Compose / Podman Compose (multi-service orchestration)

**Development Tools** (from CLAUDE.md)
- RTK — Token-optimized CLI proxy
- distill — Semantic compression for CLI output
- pytest — Unit testing

---

## Folder Structure

```
invoice-processor/
├── app/
│   ├── main.py              — FastAPI server + routes
│   ├── pipeline.py          — PDF → Markdown → fields extraction orchestration
│   ├── extract.py           — Regex-based field extraction (invoice_id, amount, IBAN, BIC, etc.)
│   ├── llm.py               — Pluggable LLM client (ollama/claude/deepseek)
│   ├── xml_export.py        — ISO 20022 pain.001.001.03 XML generator
│   ├── qr_swiss.py          — Swiss QR-bill (QR-Rechnung) decoder
│   ├── db.py                — SQLite job database schema + queries
│   ├── md_clean.py          — Markdown cleanup + garbage detection
│   ├── tests.py             — Startup self-tests (DEV_MODE)
│   ├── test_ollama.py       — [UNCLEAR] Ollama integration test
│   ├── templates/
│   │   └── index.html       — Web dashboard (drag-drop, modal review, dark mode)
│   ├── static/
│   │   ├── css/
│   │   └── js/
│   └── __pycache__/
├── rules/
│   └── default.py           — Bank-specific regex patterns (extensible per CSV_RULE_SET)
├── data/
│   └── invoices.db          — SQLite DB (volume-mounted to avoid OneDrive lock conflicts)
├── Notes/
│   ├── PROJECT_CONTEXT.md   — This file
│   ├── DECISIONS.md
│   └── Features.md
├── README.md                — User-facing setup & architecture guide
├── start.sh                 — Docker entrypoint script
├── Dockerfile               — Container image definition
├── docker-compose.yml       — Service orchestration (app + data volume)
├── .env.example             — Template env vars
├── .gitignore
└── .git/
```

---

## Entry Points

**Web Server**
- **Primary**: `app/main.py` → FastAPI lifespan context
  - Startup: `db.init_db()`, optional `tests.run_startup_tests()` if DEV_MODE
  - Routes: `/` (dashboard), `/api/*` (REST endpoints), `/download/*` (exports)
  - Mounted statics: `/static` → `app/static/` (CSS, JS, assets)

**Background Processing**
- `app/main.py:_process_job()` — Async task handler
  - Calls `pipeline.run(pdf_path, force_llm)` → dict of extracted fields
  - Stores result in SQLite via `db.upsert_job()`

**CLI Entry** (Podman/Docker)
- `start.sh` → entry point in Dockerfile
  - Runs `tests.run_startup_tests()` if DEV_MODE=true
  - Starts `uvicorn main:app --host 0.0.0.0 --port 8000`

**Local Dev** (Without Docker)
```bash
python -m pytest app/tests.py -v          # Run tests
uvicorn app.main:app --reload --port 8000 # Run server
```

---

## Environment Variables

**LLM Configuration**
```env
LLM_PROVIDER=ollama|claude|deepseek    # Default: ollama
LLM_URL=http://host.docker.internal:11434  # Ollama/DeepSeek endpoint
LLM_MODEL=llama3.2                        # Model name per provider
ANTHROPIC_API_KEY=sk-ant-...             # Claude API key (if LLM_PROVIDER=claude)
DEEPSEEK_API_KEY=...                     # DeepSeek key (if LLM_PROVIDER=deepseek)
```

**Debtor / Sender Details** (for pain.001 XML)
```env
DEBTOR_NAME=Your Company AG
DEBTOR_IBAN=CH5604835012345678009
DEBTOR_BIC=BLKBCH22
```

**Data & Paths**
```env
UPLOAD_DIR=/app/data/uploads             # Where PDFs are stored
DB_PATH=/app/data/invoices.db            # SQLite database location
DEBUG_MD_DIR=/app/data/debug_md          # Optional: save extracted markdown for inspection
```

**Feature Flags**
```env
DEV_MODE=true|false                      # Enables startup tests + synthetic data fill buttons
CSV_RULE_SET=default|<bank_name>         # Bank-specific regex patterns (loads rules/<name>.py)
MDX_MIN_CHARS=80                         # Min printable chars in markdown before LLM fallback (default: 80)
```

---

## Naming Conventions

**Files & Modules**
- snake_case: `extract.py`, `md_clean.py`, `xml_export.py`
- Bank rule sets: `rules/<bank_name>.py` (e.g., `rules/ubs.py`, `rules/postfinance.py`)
- Startup tests: `tests.py` (legacy pytest + manual test orchestration)

**Database**
- Table: `jobs` (invoice processing jobs)
- Column naming: snake_case, lowercase (`invoice_id`, `due_date`, `needs_review`)
- Status enum: `pending|processing|done|error`
- Review flag: `needs_review` → `YES|NO` (strings, not booleans)

**API Endpoints**
- REST convention: `POST /api/upload`, `GET /api/jobs`, `POST /api/review/{id}`
- Download routes: `GET /download/{format}` (format: `xml`, `csv`)
- Web routes: `GET /` (index), `POST /process/{id}` (single), `POST /process-all` (batch)

**Regex Pattern Keys** (in `extract.py` + bank rule sets)
- `invoice_id`, `amount`, `currency`, `receiver`
- `iban`, `bic`, `due_date`, `reference`
- Legacy (always empty): `bankgiro`, `plusgiro`

**Field Confidence Statuses** (per field, stored in `field_statuses` JSON column)
- `SUCCESSFUL` — value found and passes sanity checks; QR-filled fields always SUCCESSFUL
- `SUSPICIOUS` — value extracted but looks wrong (image artifact, too short, too small, defaulted)
- `EMPTY` — value not found
- Both EMPTY and SUSPICIOUS fields are sent to AI on retry

**Suspicious detection rules** (in `extract.py`):
- `receiver`: contains HTML tags / `<!-- image -->` → cleared to empty, status=SUSPICIOUS
- `invoice_id`: len < 6, or alphanumeric ratio < 0.5 → SUSPICIOUS
- `amount`: float < 100.0 → SUSPICIOUS
- `iban`: body after country code has >4 alpha chars, or invalid MOD-97 → SUSPICIOUS
- `due_date`: defaulted to end-of-month → SUSPICIOUS
- QR-filled: overrides to SUCCESSFUL unconditionally

**Flag Names** (error tracking)
- ERROR flags (block payment): `amount_not_found`, `currency_not_found`, `receiver_not_found`, `due_date_not_found`, `no_payment_method`, `reference_not_found`, `iban_invalid_checksum`
- WARN flags (non-blocking): `invoice_id_not_found`, `due_date_defaulted`, `iban_missing_bic`
- QR-specific: `qr_scan_failed`, `qr_iban_missing`
- LLM-specific: `llm_fallback_failed(...)`

---

## Architectural Patterns

**Pipeline Pattern** (`app/pipeline.py:run()`)
```
PDF file
  ↓
[Step 1: markitdown]  (try OCR via markitdown, clean markdown)
  ↓
[Step 2: Garbage?]    (check if markdown has min chars, force_llm flag)
  ↓
[Step 2: LLM fallback] (if sparse OCR, call ollama/claude/deepseek)
  ↓
[Step 3: Extraction]  (regex patterns from extract.py)
  ↓
[Step 3b: QR scan]    (override regex fields with Swiss QR-bill data if found)
  ↓
Result dict → SQLite
```

**Pluggable LLM Providers** (`app/llm.py`)
- Registry pattern: `PROVIDERS = { "ollama": _call_ollama, "claude": _call_claude, "deepseek": _call_deepseek }`
- Selected via `LLM_PROVIDER` env var
- All providers have identical signature: `(image_b64: str) → str` (OCR text output)

**Bank-Specific Rules** (`app/extract.py` + `rules/`)
- Default patterns baked into `extract.py:DEFAULT_PATTERNS`
- Overridden by `rules/<bank_name>.py` if `CSV_RULE_SET` env var set
- Pattern sets are Python dicts with keys matching field names (`invoice_id`, `amount`, etc.)
- Allows custom rules without modifying core code

**REST + Background Tasks** (`app/main.py`)
- FastAPI with BackgroundTasks
- Upload triggers async `_process_job()` in background (non-blocking)
- Web dashboard polls `/api/jobs` via HTMX / fetch (real-time updates)

**SQLite with Context Manager** (`app/db.py`)
- `@contextmanager get_db()` ensures connection cleanup
- Upsert pattern: `upsert_job()` inserts or updates only supplied columns
- Row factory: `sqlite3.Row` → dict-like access for JSON serialization

**ISO 20022 pain.001** (`app/xml_export.py`)
- One PmtInf (payment info) block per currency
- Groups jobs by currency (CHF, EUR, GBP, USD, etc.)
- Service level codes: `SEPA` (EUR), `NURG` (Swiss domestic CHF), `SWIFT` (cross-border)
- Handles multi-currency payment batches atomically

**Test Harness** (`app/tests.py`)
- Startup self-tests (T1-T6) run in container init phase
- Validates IBAN checksums, BIC regex, extraction structure, XML well-formedness
- Blocks container startup if tests fail (safety check for DEV_MODE)

---

## Key Architectural Decisions

1. **markitdown over Tesseract** — No external OCR service deps, handles both scanned + native PDFs
2. **SQLite over PostgreSQL** — Lightweight, volume-mounted, avoids OneDrive lock conflicts
3. **Pluggable LLM** — Supports Ollama (free local), Claude (reliable), DeepSeek (cost-effective)
4. **Swiss QR-bill extraction** — Overrides regex when SPC QR detected (de facto standard in Switzerland)
5. **pain.001 ISO 20022** — Multi-currency, multi-bank compatible, imports directly into ebanking portals
6. **Modeless drag-drop UI** — Single-page app with modal review (no page reloads)
7. **Dev/Prod mode split** — DEV_MODE relaxes IBAN checksums + adds synthetic data fill (testing)

