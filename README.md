# Invoice Processor

Intelligent invoice extraction, validation, and export system for mass payment processing compatible with Swiss and European ebanking platforms (UBS, Raiffeisen, BLKB, BKB, etc.).

## What It Does

1. **PDF Upload & OCR** — Accepts PDF invoices and extracts text via `Docling` (local ML-based layout analysis + OCR)
2. **QR Code Extraction** — Decodes Swiss QR-bill (SPC) codes first to lock known payment fields
3. **Field Extraction** — Regex-based pattern matching for invoice ID, amount, currency, IBAN, BIC, due date, and reference
4. **Field Confidence** — Every extracted field is rated `SUCCESSFUL / SUSPICIOUS / EMPTY` (color-coded in review modal)
5. **AI Fallback** — Haiku re-reads the invoice markdown for any EMPTY or SUSPICIOUS field on demand
6. **Web Review Interface** — Modal-based editing; field borders colored green/amber/red by confidence
7. **Multi-Format Export**:
   - **ISO 20022 pain.001.001.03 XML** — compatible with BLKB, Raiffeisen, UBS, BKB, PostFinance
   - **CSV** — spreadsheet export with all payment fields

## Who Made It

**Lyfegen HealthTech AG** — Built for internal invoice mass-payment automation.

## Installation

### Prerequisites

- **Docker** or **Podman** (5.0+)
- **Memory**: ≥2GB RAM (Docling model cache)

### Quick Start

```bash
git clone <repo-url>
cd invoice-processor

cp .env.example .env
# Edit .env — fill in DEBTOR details and optionally ANTHROPIC_API_KEY

podman compose up --build -d
# Server: http://localhost:8080
```

### Environment Variables

```env
# ── Pain.001 Sender (required for XML export) ─────────────────────────────
DEBTOR_NAME=Your Company AG
DEBTOR_IBAN=CH56...
DEBTOR_BIC=BLKBCH22

# ── AI Field Fallback (optional) ──────────────────────────────────────────
# Enables "Retry with AI" button in review modal.
# Haiku re-reads invoice markdown for EMPTY/SUSPICIOUS fields.
ANTHROPIC_API_KEY=sk-ant-...
ENABLE_LLM_FALLBACK=true

# ── Development ────────────────────────────────────────────────────────────
DEV_MODE=true          # Startup tests + synthetic data fill buttons
CSV_RULE_SET=default   # Bank-specific regex override (rules/<name>.py)

# ── Debug (optional) ──────────────────────────────────────────────────────
DEBUG_MD_DIR=/app/data/debug-md   # Save Docling markdown to disk for inspection
# DEBUG_QR_DIR=/app/data/debug-qr # Save QR scan images for inspection
```

### Volume Configuration

Named volume avoids OneDrive sync conflicts:

```yaml
volumes:
  invoice_processor_data:   # Stores uploads/ + invoices.db
```

```bash
podman volume inspect invoice_processor_data  # Locate on disk
```

## Technical Architecture

### Data Pipeline

```
PDF Upload
    ↓
QR Code Extraction (qr_swiss.py)         → locks fields: IBAN, BIC, receiver, amount, currency, reference
    ↓
Docling (pipeline.py)                    → markdown text
    ↓
extract_fields (extract.py)              → fields + SUCCESSFUL/SUSPICIOUS/EMPTY status per field
    ↓
_merge_qr (pipeline.py)                  → QR fields forced to SUCCESSFUL
    ↓
SQLite DB (invoices.db)                  → field_statuses stored as JSON
    ↓
Web Review Modal                         → inputs colored by status; AI button if any EMPTY/SUSPICIOUS
    ↓
[Optional] Retry with AI (Haiku)         → re-reads markdown for EMPTY+SUSPICIOUS fields only
    ↓
Export: pain.001 XML or CSV
```

### Field Confidence Status

Every extracted field carries one of three statuses, stored in `field_statuses` (JSON column):

| Status | Color | Meaning |
|--------|-------|---------|
| `SUCCESSFUL` | 🟢 Green | Value found and passes sanity checks |
| `SUSPICIOUS` | 🟡 Amber | Value found but looks wrong |
| `EMPTY` | 🔴 Red | Value not found |

**Suspicious detection rules:**

| Field | Suspicious if... |
|-------|-----------------|
| `receiver` | Contains HTML tags / `<!-- image -->` (OCR artifact) |
| `invoice_id` | Length < 6, or alphanumeric ratio < 50% |
| `amount` | Numeric value < 100.00 |
| `iban` | Body after country code has >4 alpha chars, or MOD-97 fails |
| `due_date` | Defaulted to end-of-month (no date found in text) |

QR-bill fields are **always** `SUCCESSFUL` — they override any suspicious flags.

**AI retry**: "Retry with AI" button appears when any field is EMPTY or SUSPICIOUS. Haiku re-reads the markdown for those fields only. SUCCESSFUL fields are never re-sent to AI.

### Key Components

#### 1. PDF → Markdown (`app/pipeline.py`)

Uses [Docling](https://github.com/DS4SD/docling) for ML-based document layout analysis and OCR. Handles scanned + native PDFs. Returns clean markdown for regex matching.

#### 2. Field Extraction (`app/extract.py`)

Regex pattern matching with per-field confidence assessment:

- **Invoice ID**: `INVOICE NUMBER | INV-001`, `invoice no: INV-001`, `Rechnungsnummer`
- **Amount**: Handles Swiss comma/period decimal formats; normalized to `X.XX`
- **Currency**: CHF/EUR/USD/GBP codes and symbols (`€`, `£`, `$`)
- **IBAN**: Extracts + validates MOD-97 checksum (ISO 13616); no external deps
- **BIC**: 8 or 11-char SWIFT code pattern
- **Due Date**: DD/MM/YYYY, DD.MM.YYYY, YYYY-MM-DD, German month names (`5. Oktober 2025`)
- **Reference**: OCR-nr, Referenz, order number patterns

Bank-specific pattern overrides via `rules/<bank_name>.py` + `CSV_RULE_SET` env var.

#### 3. Swiss QR Code Extraction (`app/qr_swiss.py`)

Decodes ISO 20022 SPC QR codes (Swiss QR-bill / QR-Rechnung):

- Multi-DPI scan strategy: 150/300/400 DPI full page + bottom-half crop
- Libraries: `pyzbar` (primary) + `zxing-cpp` (fallback)
- QR-filled fields override regex results and are always `SUCCESSFUL`

#### 4. AI Field Fallback (`app/extract.py:llm_extract_field`, `app/config.py`)

Tier 1 fallback using Anthropic Haiku:

- Triggered manually via "Retry with AI" button in review modal
- Only processes fields with status `EMPTY` or `SUSPICIOUS`
- Requires: `ANTHROPIC_API_KEY` + `ENABLE_LLM_FALLBACK=true` in `.env`
- Health check runs at startup (`config.py:llm_available()`) — result cached
- After AI update: field status promoted to `SUCCESSFUL`

#### 5. ISO 20022 pain.001 Export (`app/xml_export.py`)

**Standard**: ISO 20022 pain.001.001.03

| Currency | Service Level | Instrument |
|----------|--------------|------------|
| CHF (domestic) | `NURG` | — |
| EUR (SEPA) | `SEPA` | — |
| Other | `NURG` | `SWIFT` |

Compatible banks: UBS, Raiffeisen, BLKB, BKB, PostFinance, most EU SEPA banks.

#### 6. Web Dashboard (`app/main.py`, `app/templates/index.html`)

- Drag-drop PDF upload with progress bar
- Real-time job status polling (2s interval while processing)
- Review modal: field inputs colored green/amber/red by confidence status
- AI button shown per-job when EMPTY or SUSPICIOUS fields exist
- Bulk ops: retry errors, clear all, dev-fill all
- Dark mode (persisted in localStorage)

**API Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/upload` | Upload PDF(s) |
| `GET` | `/api/jobs` | List all jobs (includes `field_statuses`) |
| `POST` | `/api/review/{id}` | Save reviewed fields |
| `POST` | `/api/retry-ai/{id}` | Re-extract EMPTY+SUSPICIOUS fields with Haiku |
| `GET` | `/api/llm-available` | Check if AI fallback is configured |
| `DELETE` | `/jobs/{id}` | Delete job + file |
| `DELETE` | `/api/clear-all` | Delete all jobs |
| `GET` | `/download/xml` | Export pain.001 XML |
| `GET` | `/download/csv` | Export CSV |

#### 7. Startup Tests (`app/tests.py`)

Runs in `DEV_MODE` on container startup; blocks start if any fail:

- **T1**: IBAN MOD-97 checksum (valid CH/DE, invalid fake)
- **T2**: BIC regex (8/11-char, various formats)
- **T3**: `extract_fields` output structure
- **T4**: pain.001 XML well-formedness
- **T5**: Multi-currency grouping (CHF/EUR → separate PmtInf)
- **T6**: Skip jobs with missing IBAN (NbOfTxs=0)

### Database Schema

**invoices.db** (SQLite, named volume):

```sql
CREATE TABLE jobs (
  id              TEXT PRIMARY KEY,
  filename        TEXT,
  status          TEXT,          -- pending|processing|done|error
  invoice_id      TEXT,
  amount          TEXT,
  currency        TEXT,
  receiver        TEXT,
  iban            TEXT,
  bic             TEXT,
  bankgiro        TEXT,          -- legacy, always ""
  plusgiro        TEXT,          -- legacy, always ""
  due_date        TEXT,
  reference       TEXT,
  needs_review    TEXT,          -- YES|NO
  review_reasons  TEXT,          -- semicolon-separated flag list
  ocr_method      TEXT,          -- docling|docling+qr|docling+llm
  field_statuses  TEXT,          -- JSON: {"invoice_id":"SUCCESSFUL","amount":"EMPTY",...}
  error_msg       TEXT,
  created_at      TEXT,
  updated_at      TEXT
);
```

### Tools & Standards

| Tool / Standard | Purpose |
|----------------|---------|
| [Docling](https://github.com/DS4SD/docling) | PDF → Markdown (ML OCR) |
| [FastAPI](https://fastapi.tiangolo.com) | REST API + web framework |
| [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) | Haiku AI field fallback |
| [PyMuPDF](https://pymupdf.readthedocs.io) | PDF page rendering for QR |
| [pyzbar](https://github.com/NaturalHistoryMuseum/pyzbar) | QR code decoding |
| ISO 20022 pain.001.001.03 | Payment initiation XML |
| IBAN ISO 13616 | MOD-97 checksum validation |

## Development

### Running Locally (Without Podman)

```bash
python -m venv venv
venv\Scripts\activate   # Windows

pip install -r requirements.txt

# Set env vars (no auto-.env loading outside container)
$env:ANTHROPIC_API_KEY="sk-ant-..."
$env:ENABLE_LLM_FALLBACK="true"
$env:DEV_MODE="true"
$env:DEBUG_MD_DIR="./data/debug-md"

uvicorn app.main:app --reload --port 8000
```

### Adding Custom Bank Rules

Create `rules/<bank_name>.py`:

```python
PATTERNS = {
    "invoice_id": [r"Invoice #(\w+)"],
    "amount": [r"Total.*?([\d,\.]+)"],
    # Only override keys that differ from defaults
}
```

Set `CSV_RULE_SET=<bank_name>` in `.env`.

### Container Operations

```bash
podman compose up --build -d   # Build + start
podman compose logs -f app     # Follow logs
podman compose restart app     # Restart (reload env vars)
podman volume inspect invoice_processor_data  # Find data dir
```

## Troubleshooting

### AI Button Not Appearing

1. Check `ANTHROPIC_API_KEY` starts with `sk-ant-` in `.env`
2. Check `ENABLE_LLM_FALLBACK=true` in `.env`
3. Check container logs for `[config] LLM health check passed ✓`
4. If missing: `anthropic` package not installed — rebuild with `podman compose up --build`

### Docling Model Download

First run downloads ~500MB of models during `podman compose up --build`. Subsequent runs use cached models. Download warnings at runtime are normal.

### OneDrive Sync Issues

Named volume (`invoice_processor_data`) stores uploads + DB outside the project directory — no sync conflicts.

## License

Proprietary — Lyfegen HealthTech AG

## Contact

sasha.bieri@lyfegen.com
