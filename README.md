# Invoice Processor

Intelligent invoice extraction, validation, and export system for mass payment processing compatible with Swiss and European ebanking platforms (BKB, Raiffeisen, UBS, BLKB, PostFinance, etc.).

## What It Does

1. **PDF Upload + Swiss QR Scan** — Accepts PDF invoices; instantly decodes Swiss QR-bill (QR-Rechnung / SPC) codes where present
2. **Two-Stage LLM Extraction** — For non-QR invoices: text-layer extraction first (fast/cheap), then image-rendering fallback for scanned PDFs — both using Claude Haiku
3. **Vendor IBAN Lookup** — Cross-references extracted receiver names against a local vendor database; autofills or flags IBAN mismatches
4. **Web Review Interface** — Modal-based editing with field validation; inline bank assignment
5. **Dual-Bank Export Screen** — Drag-and-drop invoices to BKB or Raiffeisen columns; gated export with blocker popup
6. **ISO 20022 pain.001 Export** — Bank-sorted XML files for direct upload to ebanking portals
7. **CSV Export** — Spreadsheet export with all payment fields
8. **Analytics Dashboard** — Breakdown of extraction methods (QR / text / image / hybrid)

## Who Made It

**Lyfegen HealthTech AG** — Built for internal invoice mass-payment automation.

---

## Installation

### Prerequisites

- **Docker** or **Podman** (5.0+)
- **Anthropic API key** — Claude Haiku is used for all LLM extraction

### Quick Start

```bash
# Clone the repo
git clone <repo-url>
cd invoice-processor

# Create .env from template
cp .env.example .env

# Edit .env — set ANTHROPIC_API_KEY and DEBTOR_* fields at minimum

# Build and run
podman compose up --build -d

# Server runs on http://localhost:8080
```

### Environment Variables

```env
# Required: Anthropic API key for Claude Haiku extraction
ANTHROPIC_API_KEY=sk-ant-...

# Optional: override Claude model (default: claude-haiku-4-5-20251001)
LLM_MODEL=claude-haiku-4-5-20251001
LLM_MODEL_TEXT=claude-haiku-4-5-20251001

# Pain.001 sender (debtor) — your company
DEBTOR_NAME=Your Company AG
DEBTOR_IBAN=CH56...
DEBTOR_BIC=BLKBCH22

# Development: enables startup self-tests on container boot
DEV_MODE=true

# Storage paths (defaults work inside container)
DB_PATH=/app/data/invoices.db
UPLOAD_DIR=/app/data/uploads

# Optional: save QR scan debug images
DEBUG_QR_DIR=/tmp/qr_debug
```

### Volume Configuration

The app uses a named Docker/Podman volume to avoid OneDrive sync conflicts:

```yaml
volumes:
  invoice_processor_data:    # Stores uploads/ and invoices.db
```

```bash
podman volume inspect invoice_processor_data
```

---

## Technical Architecture

### Data Pipeline

```
PDF Upload
    │
    ├─ Swiss QR-bill present? ──► QR-processed (done instantly)
    │      (qr_swiss.py: pyzbar/zxingcpp decode)
    │
    └─ No QR ──► LLM-Pending
                    │
                    ▼  (background batch — POST /api/run-llm-batch)
             Stage 1: Text layer (fitz → Claude Haiku)
                    │
             Vendor check: autofill IBAN from vendor DB
                    │
             Complete? ──► LLM-Done
                    │
             Incomplete? ──► Stage 2: Image render (fitz → PNG → Claude Haiku)
                    │
             Final vendor check
                    │
             All mandatory fields? ──► LLM-Done
                    │
             Missing fields? ──► needs_review
                    │
                    ▼
             Web Review Modal (manual editing)
                    │
                    ▼
             Export Screen (drag BKB ↔ Raiffeisen)
                    │
                    ▼
             pain.001 XML + archive
```

### Key Components

#### 1. Swiss QR Decoder (`app/qr_swiss.py`)

Scans each PDF page at multiple DPIs (150/300/400) and crop strategies:

- Full page scan
- Bottom-third crop (Swiss QR slip is always in the bottom section)
- Decoders: **pyzbar** (primary) + **zxingcpp** (fallback)
- Parses SPC payload (SIX standard 2.2): IBAN, receiver, amount, currency, reference

#### 2. Two-Stage LLM Extraction (`app/llm.py`)

Uses **Claude Haiku** exclusively (no Ollama, no local models):

| Stage | Method | When |
|-------|--------|------|
| Text | fitz text layer → Haiku JSON | PDF has ≥80 chars of text |
| Image | fitz → PNG base64 → Haiku (prefill `{`) | Text stage incomplete or no text layer |

Both stages return the same JSON schema:
```json
{"invoice_id": "...", "receiver": "...", "amount": "1234.56",
 "currency": "CHF", "due_date": "2026-07-01", "iban": "CH56...",
 "bic": "BLKBCH22", "reference": "..."}
```

`match_type` values: `text_full` | `image_only` | `hybrid` | `failed`

#### 3. Vendor IBAN Lookup (`app/vendors.py`)

Case-insensitive exact match on `receiver_name`:

- Match → autofill IBAN/BIC from DB (`iban_source = "database"`)
- Match + IBAN present → confirm (`iban_source = "document"`) or flag mismatch (`iban_source = "document_mismatch"`)
- Operator saves review → `iban_source = "manual"`

#### 4. Bank Routing (`app/db.py: derive_bank_target`)

| Currency | Bank |
|----------|------|
| CHF, SEK, EUR | BKB |
| USD, CAD, GBP | RAIFFEISEN |
| Other | MANUAL |

#### 5. ISO 20022 pain.001 Export (`app/xml_export.py`)

Generates `pain.001.001.03` XML, one file per bank, grouped by currency:

| Currency | Bank | Service Level | Local Instrument |
|----------|------|--------------|-----------------|
| CHF | BKB | NURG | — |
| SEK | BKB | NURG | SWIFT |
| EUR | BKB | SEPA | — |
| USD/CAD/GBP | RAIFFEISEN | NURG | SWIFT |

- Uses `decimal.Decimal` for all monetary sums (no float drift)
- Handles European decimal comma (`"1234,50"` → `1234.50`)

#### 6. Web Dashboard (`app/main.py`, `app/templates/`)

Two screens:

**Dashboard** (`/`):
- Drag-drop upload (multi-PDF)
- Job table with status badges and IBAN source chips
- Search and sort
- Modal review editor with inline PDF viewer
- Run AI batch button

**Export Screen** (`/export`):
- Drag-and-drop between BKB / Raiffeisen / Unsorted columns
- Blocker popup: shows invoices that need review before export
- Download button: generates both pain.001 files as a ZIP + archives sorted jobs

#### 7. Startup Tests (`app/tests.py`, `DEV_MODE=true`)

| ID | Tests |
|----|-------|
| T1 | IBAN MOD-97 checksum (valid, invalid, CH fake, zero-width space) |
| T2 | BIC regex (8-char, 11-char, fake patterns) |
| T4 | pain.001 XML well-formedness + GrpHdr + NbOfTxs + creditor IBAN |
| T5 | Multi-currency → separate PmtInf blocks |
| T6 | No-IBAN jobs skipped; IBAN-only (no BIC) included |
| T7 | Bank routing: CHF→BKB, USD→RAIFFEISEN, XYZ→MANUAL |
| T8 | run_qr: valid SPC → fields; no-QR → LLM-Pending |
| T9 | IBAN validation edge cases |
| T10 | Amount parsing: EU comma, thousands separator |
| T11 | Vendor lookup: autofill, mismatch, unknown vendor |

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard page |
| `GET` | `/export` | Export screen |
| `POST` | `/api/upload` | Upload PDF(s), sync QR scan, persist |
| `GET` | `/api/jobs` | List jobs (`?include_archived=true`) |
| `POST` | `/api/review/{id}` | Save edited fields from review modal |
| `POST` | `/api/run-llm-batch` | Trigger background LLM extraction |
| `POST` | `/api/assign-bank/{id}` | Override bank_target |
| `GET` | `/api/pdf/{id}` | Serve original PDF |
| `GET` | `/api/analytics` | Extraction method breakdown |
| `GET` | `/api/vendors` | List vendors |
| `POST` | `/api/vendors` | Create vendor |
| `PUT` | `/api/vendors/{id}` | Update vendor |
| `DELETE` | `/api/vendors/{id}` | Delete vendor |
| `GET` | `/api/export-readiness` | Check for blockers |
| `POST` | `/download/confirm` | Generate ZIP (pain.001 ×2) + archive |
| `GET` | `/download/csv` | CSV export |
| `DELETE` | `/api/jobs/{id}` | Hard-delete job + file |
| `DELETE` | `/api/clear-all` | Wipe all non-archived jobs |

---

## Database Schema

**invoices.db** (SQLite):

```sql
CREATE TABLE jobs (
    id               TEXT PRIMARY KEY,
    filename         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'LLM-Pending',
    -- Extracted payment fields
    receiver         TEXT DEFAULT '',
    iban             TEXT DEFAULT '',
    bic              TEXT DEFAULT '',
    amount           TEXT DEFAULT '',
    currency         TEXT DEFAULT '',
    reference        TEXT DEFAULT '',
    invoice_id       TEXT DEFAULT '',
    -- Routing & metadata
    bank_target      TEXT DEFAULT '',   -- BKB | RAIFFEISEN | MANUAL
    iban_source      TEXT DEFAULT '',   -- document | database | document_mismatch | llm | manual
    iban_mismatch_db TEXT DEFAULT '',   -- vendor DB IBAN when mismatch detected
    match_type       TEXT DEFAULT '',   -- text_full | image_only | hybrid | failed
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE vendors (
    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    receiver_name TEXT NOT NULL,
    iban          TEXT NOT NULL,
    bic           TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);
```

**Job status enum**: `QR-processed` | `LLM-Pending` | `LLM-Done` | `needs_review` | `archived` | `error`

---

## Dependencies

| Package | Purpose |
|---------|---------|
| **fastapi** | Web framework + REST API |
| **uvicorn** | ASGI server |
| **anthropic** | Claude Haiku API client |
| **pymupdf (fitz)** | PDF text extraction + image rendering |
| **pyzbar** | Primary QR/barcode decoder |
| **zxingcpp** | Fallback QR decoder |
| **Pillow** | Image processing for QR scan |
| **python-multipart** | Multipart file upload |
| **jinja2** | HTML templating |

---

## Development

### Running Locally (Without Docker)

```bash
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Mac/Linux

pip install -r requirements.txt

# Set env vars
set ANTHROPIC_API_KEY=sk-ant-...
set DEBTOR_NAME=Test Corp
set DEBTOR_IBAN=CH5604835012345678009
set DEBTOR_BIC=BLKBCH22
set DEV_MODE=true

# Run
uvicorn app.main:app --reload --port 8000
```

### Volume on Windows/OneDrive

The volume keeps data outside the synced project directory:

```bash
podman volume ls | findstr invoice_processor
podman volume inspect invoice_processor_data
```

---

## Troubleshooting

### LLM extraction returns null fields

- Check `ANTHROPIC_API_KEY` is set and valid
- Check container logs: `podman compose logs -f app`
- Force re-run: click "✦ Run AI" on the dashboard

### QR code not detected

- Invoice may not be a Swiss QR-Rechnung
- Enable debug mode: set `DEBUG_QR_DIR=/tmp/qr_debug` and inspect saved images
- QR code may be too small — the scanner tries 150/300/400 DPI automatically

### OneDrive Sync Issues

Uploads and database are stored in a named volume, not the project directory — no sync conflicts.

---

## License

Proprietary — Lyfegen HealthTech AG

## Contact

sasha.bieri@lyfegen.com
