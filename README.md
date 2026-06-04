# Invoice Processor

Intelligent invoice extraction, validation, and export system for mass payment processing compatible with Swiss and European ebanking platforms (UBS, Raiffeisen, BLKB, BKB, etc.).

## What It Does

1. **PDF Upload & OCR** — Accepts PDF invoices and extracts text via `Docling` (local ML-based layout analysis + OCR)
2. **QR Code Extraction** — Decodes Swiss QR codes first (if present) to lock known payment fields
3. **Field Extraction** — Regex-based pattern matching for invoice ID, amount, currency, IBAN, BIC, due date, and reference
4. **Web Review Interface** — Modal-based editing and validation of extracted fields
5. **Multi-Format Export**:
   - **ISO 20022 pain.001.001.03 XML** — Pain format compatible with BLKB, Raiffeisen, UBS, BKB ebanking portals
   - **CSV** — Spreadsheet export with all payment fields
6. **Dev Autofill** — Currency-aware synthetic test data generation (CHF/EUR with valid IBANs and BICs)

## Who Made It

**Lyfegen HealthTech AG** — Built for internal invoice mass-payment automation.

## Installation

### Prerequisites

- **Docker** or **Podman** (5.0+)
- **Memory**: ≥2GB RAM (for Docling model cache)

### Quick Start

```bash
# Clone the repo
git clone <repo-url>
cd invoice-processor

# Create .env (or copy from .env.example)
cp .env.example .env

# Edit .env to configure:
# - DEBTOR company details (for pain.001 generation)
# - DEV_MODE (for startup tests + auto-fill)

# Build and run with Podman
podman compose up --build -d

# Server runs on http://localhost:8080
```

### Environment Variables

```env
# Pain.001 Sender (Debtor) — Your company details
DEBTOR_NAME=Your Company AG
DEBTOR_IBAN=CH56...  # Your company's IBAN
DEBTOR_BIC=BLKBCH22  # Your bank's BIC

# Development mode
DEV_MODE=true        # Enables startup tests + synthetic data fill buttons
```

### Volume Configuration

The application uses a named Docker/Podman volume to avoid OneDrive sync conflicts:

```yaml
volumes:
  invoice_processor_data:    # Auto-managed by Docker/Podman
    # Stores: uploads/ and invoices.db
```

To locate the volume on your system:

```bash
podman volume ls | grep invoice_processor_data
podman volume inspect invoice_processor_data
```

## Technical Architecture

### Data Pipeline

```
PDF Upload
    ↓
QR Code Extraction (qr_swiss.py)
    ↓
Docling (text extraction + layout analysis)
    ↓
extract_fields (regex patterns, skip fields locked by QR)
    ↓
SQLite DB (invoices.db)
    ↓
Web Review Modal (manual validation)
    ↓
Export: pain.001 XML or CSV
```

### Key Components

#### 1. **PDF → Markdown** (`app/pipeline.py`)

Uses [Docling](https://github.com/DS4SD/docling) for ML-based document layout analysis and text extraction.

```
- Handles scanned PDFs (image-based) via built-in OCR
- Extracts tables, headers, structured text with layout awareness
- Returns clean markdown for regex pattern matching
- Runs locally (no external API required)
```

#### 2. **Field Extraction** (`app/extract.py`)

Regex-based pattern matching with bank-specific rule sets (configurable via `CSV_RULE_SET`):

- **Invoice ID**: "INVOICE NUMBER | INV-001", "invoice no: INV-001"
- **Amount**: Handles commas/periods as thousands/decimals
- **Currency**: Detects CHF/EUR/USD/GBP symbols and codes
- **IBAN**: Extracts country code + alphanumeric, validates MOD-97 checksum
- **BIC**: Validates 8 or 11-character SWIFT codes
- **Due Date**: Parses DD/MM/YYYY, DD.MM.YYYY, YYYY-MM-DD formats

**Validation**:
- IBAN MOD-97 checksum (ISO 13616) — no external dependencies
- BIC regex pattern enforcement (A-Z bank code + country + location)

#### 3. **Swiss QR Code Extraction** (`app/qr_swiss.py`)

Decodes ISO 20022 SPC (Structured Post Code) QR codes from invoices.

- Extracts: IBAN, receiver, amount, currency, reference (if present)
- Fields locked by QR are skipped in regex pattern matching (prevents contradictions)
- Logs `ocr_method = "docling+qr"` for invoices with QR

#### 4. **ISO 20022 pain.001 Export** (`app/xml_export.py`)

**Standard**: [ISO 20022 pain.001.001.03](https://www.iso20022.org/)

Generates XML compatible with Swiss/EU banking:

- **Domestic CHF** (SIC): `SvcLvl/Cd = NURG`
- **SEPA EUR** (euroSIC): `SvcLvl/Cd = SEPA`
- **Cross-border** (GBP, USD, etc.): `SvcLvl/Cd = NURG` + `LclInstrm/Cd = SWIFT`

Banks accepting pain.001 format:
- ✅ UBS
- ✅ Raiffeisen
- ✅ BLKB (Basellandschaftliche Kantonalbank)
- ✅ BKB (Basler Kantonalbank)
- ✅ PostFinance
- ✅ Most EU SEPA banks

#### 5. **Web Dashboard** (`app/main.py`, `app/templates/index.html`)

**Features**:
- Drag-drop PDF upload
- Real-time job status (processing/done/error)
- Modal-based field review and editing
- Dark mode toggle (persisted in localStorage)
- Bulk operations: retry all, run AI on all, dev-fill all, clear all
- Download buttons: pain.001 XML, CSV export

**API Endpoints**:
- `POST /api/upload` — File upload
- `GET /api/jobs` — List all invoices
- `POST /api/review/{id}` — Update invoice fields
- `DELETE /api/jobs/{id}` — Delete invoice
- `DELETE /api/clear-all` — Delete all
- `GET /download/xml` — Export pain.001
- `GET /download/csv` — Export CSV

#### 6. **Startup Tests** (`app/tests.py`)

Runs automatically in `DEV_MODE` on container startup:

- **T1**: IBAN MOD-97 checksum validation
- **T2**: BIC regex pattern validation
- **T3**: extract_fields structure validation
- **T4**: pain.001 XML well-formedness + element presence
- **T5**: Multi-currency grouping (CHF/EUR → separate PmtInf blocks)
- **T6**: Skip jobs missing IBAN/BIC (NbOfTxs=0)

Fails the container startup if tests don't pass (safety check).

### Tools & Standards

#### Extraction & Processing

| Tool | Purpose | Link |
|------|---------|------|
| **Docling** | PDF → Markdown (ML layout analysis + OCR) | [DS4SD/docling](https://github.com/DS4SD/docling) |
| **fastapi** | Web framework + REST API | [FastAPI](https://fastapi.tiangolo.com) |
| **pymupdf** | PDF page rendering for QR extraction | [PyMuPDF](https://pymupdf.readthedocs.io) |
| **pyzbar** | QR code decoding | [pyzbar](https://github.com/NaturalHistoryMuseum/pyzbar) |

#### Standards & Formats

| Standard | Purpose | Link |
|----------|---------|------|
| **ISO 20022 pain.001.001.03** | Payment initiation XML format | [ISO 20022 Registry](https://www.iso20022.org/) |
| **IBAN (ISO 13616)** | International bank account number + MOD-97 checksum | [Wikipedia IBAN](https://en.wikipedia.org/wiki/International_Bank_Account_Number) |
| **SWIFT/BIC** | Bank identifier code (8-11 chars) | [SWIFT](https://www.swift.com) |
| **SIC/euroSIC** | Swiss banking standards for domestic/SEPA | [SIX](https://www.six-group.com) |

#### Development Tools

| Tool | Purpose | Link |
|------|---------|------|
| **RTK** | Token-optimized CLI (saves 60-90% on dev operations) | [RTK](https://github.com/reachingforthejack/rtk) |
| **distill** | Semantic compression for command-line output | [distill](https://github.com/anthropics/distill) |
| **pytest** | Unit testing framework | [pytest](https://pytest.org) |
| **podman** | OCI container runtime (Docker-compatible) | [Podman](https://podman.io) |

### Database Schema

**invoices.db** (SQLite):

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  filename TEXT,
  status TEXT (processing|done|error),
  invoice_id TEXT,
  amount TEXT,
  currency TEXT,
  receiver TEXT,
  iban TEXT,
  bic TEXT,
  bankgiro TEXT,  -- Legacy (always "")
  plusgiro TEXT,  -- Legacy (always "")
  due_date TEXT,
  reference TEXT,
  needs_review TEXT (YES|NO),
  review_reasons TEXT,
  ocr_method TEXT (docling|docling+qr),
  error_msg TEXT,
  created_at TEXT,
  updated_at TEXT
);
```

## Development

### Running Locally (Without Docker)

```bash
# Install Python 3.11+
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Run startup tests
python -m pytest app/tests.py -v

# Run server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Adding Custom Bank Rules

Create `app/rules/<bank_name>.py`:

```python
PATTERNS = {
    "invoice_id": [r"Invoice #(\w+)"],
    "amount": [r"Total.*?([\d,\.]+)"],
    # Override defaults for this bank
}
```

Set `CSV_RULE_SET=<bank_name>` in `.env` to use.

### Building with Podman

```bash
# Build
podman compose build

# Run with startup tests
podman compose up -d

# View logs
podman compose logs -f app
```

## Troubleshooting

### Docling Model Download

On first run, Docling downloads ~500MB of models. This happens during container build (via the pre-bake RUN step in Dockerfile). Subsequent runs use cached models.

If you see model download warnings during runtime, this is normal — models are cached after first download.

### OneDrive Sync Issues

The app uses a named Docker/Podman volume (`invoice_processor_data`) to avoid conflicts with OneDrive:

- Uploads and database are stored in the volume, NOT in the project directory
- This prevents file-lock conflicts during sync

### Volume Permissions (Linux/Mac)

If you see permission errors on volume access:

```bash
# Run container with current user UID
podman compose exec app chown -R $(id -u):$(id -g) /app/data
```

## License

Proprietary — Lyfegen HealthTech AG

## Contact

For support or questions, contact: sasha.bieri@lyfegen.com
