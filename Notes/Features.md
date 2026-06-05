# Invoice Processor — Features

## User-Facing Features

### 1. Drag-Drop PDF Upload

- **Description:** Web dashboard accepts multiple PDF invoices via drag-and-drop or file picker. Each PDF is queued for async processing.
- **Entry point:** `app/main.py:POST /upload`
- **Key files:** `app/main.py` (route), `app/templates/index.html` (UI), `app/pipeline.py` (processing)
- **Input/Output:**
  - Input: Multiple `.pdf` files (MIME type `application/pdf`)
  - Output: JSON `{"queued": [job_id1, job_id2, ...]}`, background tasks enqueued
  - Files stored in: `/app/data/uploads/{job_id}_{original_filename}`
- **Validations/Edge cases:**
  - Non-PDF files ignored (extension check `.pdf` only)
  - Empty files allowed (will fail later in extraction)
  - Concurrent uploads queued sequentially per job ID
  - OneDrive lock handled via Docker named volume (not filesystem)

---

### 2. Real-Time Job Status Polling

- **Description:** Dashboard polls `/api/jobs` endpoint to display live status of all invoices (pending, processing, done, error).
- **Entry point:** `app/main.py:GET /api/jobs`
- **Key files:** `app/main.py`, `app/db.py`, `app/templates/index.html` (HTMX polling)
- **Input/Output:**
  - Input: None (reads all jobs from SQLite)
  - Output: JSON array of job objects with all extracted fields + status
  - Polling interval: [UNCLEAR] (likely 1-2s in HTML; not specified)
- **Validations/Edge cases:**
  - Jobs in "error" state show `error_msg` field with exception details
  - Jobs in "processing" state don't have extracted fields yet
  - Jobs in "done" state have all fields populated
  - Completed jobs persist indefinitely (can be deleted manually)

---

### 3. Modal Field Review & Editing

- **Description:** Operator reviews extracted fields in a modal dialog. All REVIEW_FIELDS can be edited and saved back to database.
- **Entry point:** `app/main.py:POST /api/review/{job_id}`
- **Key files:** `app/main.py`, `app/db.py`, `app/templates/index.html`
- **Input/Output:**
  - Input: Form data with keys: `invoice_id`, `receiver`, `amount`, `currency`, `due_date`, `iban`, `bic`, `reference`
  - Output: JSON `{"ok": True}`, updates DB and sets `needs_review=NO` + clears `review_reasons`
  - Editable fields: `REVIEW_FIELDS = ["invoice_id", "receiver", "amount", "currency", "due_date", "iban", "bic", "reference"]`
- **Validations/Edge cases:**
  - No validation on edited values (trusts operator input)
  - Saving review auto-clears error flags (operator confirmation = approval)
  - Missing fields shown as empty strings in modal
  - Modal blocks export until flagged invoices are reviewed (needs_review=YES → export skipped)

---

### 4. PDF Viewer Embedded in Modal

- **Description:** When reviewing an invoice, operator can view the original PDF in a modal (optional feature).
- **Entry point:** `app/main.py:GET /api/pdf/{job_id}`
- **Key files:** `app/main.py`, `app/templates/index.html` (iframe)
- **Input/Output:**
  - Input: `job_id`
  - Output: PDF file stream (`application/pdf`)
  - File lookup: `UPLOAD_DIR / f"{job_id}_*"` (glob first match)
- **Validations/Edge cases:**
  - Returns 404 if PDF not found
  - Assumes browser has PDF viewer (default in all modern browsers)
  - No access control (any job_id is readable)

---

### 5. Dark Mode Toggle

- **Description:** Operator can toggle dark mode on dashboard; preference persisted to browser localStorage.
- **Entry point:** [UNCLEAR] — HTML button (no explicit API endpoint)
- **Key files:** `app/templates/index.html` (CSS + JS toggle)
- **Input/Output:**
  - Input: Click event on dark-mode button
  - Output: CSS class added/removed, localStorage key updated
- **Validations/Edge cases:**
  - Persisted as `theme=dark|light` in localStorage
  - No server-side preference storage (browser-local only)
  - Default theme: [UNCLEAR] (likely light)

---

### 6. Bulk Operations: Process All, Retry All, AI on All, Dev-Fill All, Clear All

- **Description:** Batch operations on all invoices without individual clicks.
- **Entry point:**
  - `POST /process-all` — reprocess all pending or error jobs
  - `POST /api/queue-llm-all` — force LLM OCR on all jobs needing review (without full re-scan)
  - `[DEV] Dev Fill All` — populate all jobs with synthetic test data (DEV_MODE only)
  - `DELETE /api/clear-all` — delete all jobs + uploads + debug markdown
- **Key files:** `app/main.py`, `app/db.py`, `app/pipeline.py`
- **Input/Output:**
  - Input: Bulk action button (no params)
  - Output: JSON with count of jobs queued/deleted
- **Validations/Edge cases:**
  - Process all: skips jobs that are already "processing"
  - LLM all: only processes jobs with `needs_review=YES` and no LLM method yet
  - Dev fill: only available if `DEV_MODE=true`
  - Clear all: deletes everything (no undo); removes debug markdown from DEBUG_MD_DIR if set

---

## System Features

### 7. PDF → Markdown Extraction (via markitdown)

- **Description:** Core OCR step converts PDF (scanned or native text) to plain markdown, preserving structure.
- **Entry point:** `app/pipeline.py:run()` → markitdown global instance
- **Key files:** `app/pipeline.py` (orchestration), `app/md_clean.py` (cleanup), markitdown library
- **Input/Output:**
  - Input: PDF file path
  - Output: Markdown string with extracted text, tables, headers
  - Calls: `_md.convert(pdf_path)` → `result.text_content`
- **Validations/Edge cases:**
  - Handles scanned PDFs (image-based) + native text PDFs
  - Extracts tables as markdown pipes
  - Cleans markdown via `md_clean.clean_markdown()` (removes noise)
  - If result < `MDX_MIN_CHARS` printable chars, triggers LLM fallback

---

### 8. Garbage Markdown Detection & LLM Fallback

- **Description:** If markdown is sparse or corrupted, automatically fall back to LLM vision OCR (ollama/claude/deepseek).
- **Entry point:** `app/pipeline.py:_is_garbage()` + LLM fallback logic
- **Key files:** `app/pipeline.py`, `app/llm.py`
- **Input/Output:**
  - Input: Raw markdown string
  - Output: Triggers LLM call, returns OCR text or logs error
  - Decision threshold: `MDX_MIN_CHARS = 80` (configurable)
- **Validations/Edge cases:**
  - Garbage detection strips control chars, pipes, hyphens, whitespace before counting
  - LLM fallback catches exceptions and logs to stderr
  - If both markitdown + LLM fail, returns empty markdown + error flag
  - `force_llm=True` skips markitdown entirely (for manual re-processing)

---

### 9. Pluggable LLM Vision OCR (Ollama / Claude / DeepSeek)

- **Description:** LLM vision converts PDF pages to PNG, sends to configured provider, returns OCR text.
- **Entry point:** `app/llm.py:ocr_pdf_via_llm(pdf_path)`
- **Key files:** `app/llm.py`, `app/pipeline.py` (calls via conditional)
- **Input/Output:**
  - Input: PDF file path, selected provider via `LLM_PROVIDER` env var
  - Output: Concatenated OCR text from all pages
  - Providers: `ollama` (local), `claude` (Anthropic), `deepseek` (third-party API)
  - Page rendering: pymupdf/fitz → PNG @ 150 DPI
- **Validations/Edge cases:**
  - If provider not recognized, raises ValueError
  - If LLM endpoint unreachable, returns error string + logs to stderr
  - API key missing for claude/deepseek → auth error
  - Large PDFs may timeout (120s per page)
  - Concatenates all page OCR sequentially; no parallel processing

---

### 10a. Field Confidence Status System

- **Description:** Every extracted field carries a confidence status: `SUCCESSFUL`, `SUSPICIOUS`, or `EMPTY`. Displayed as green/amber/red input borders in the review modal.
- **Entry point:** `app/extract.py:extract_fields()` → returns `field_statuses` dict; pipeline.py upgrades QR-filled fields to SUCCESSFUL
- **Key files:** `app/extract.py`, `app/pipeline.py`, `app/db.py` (column `field_statuses`), `app/static/style.css` (`.field-successful`, `.field-suspicious`, `.field-empty`)
- **Rules:**
  - QR-filled fields → always `SUCCESSFUL`
  - Regex-matched, passes sanity → `SUCCESSFUL`
  - `receiver` has HTML artifact → `SUSPICIOUS` (cleared from value)
  - `invoice_id` < 6 chars or mostly non-alphanumeric → `SUSPICIOUS`
  - `amount` < 100.0 → `SUSPICIOUS`
  - `iban` body has >4 letters or fails MOD-97 → `SUSPICIOUS`
  - `due_date` defaulted to end-of-month → `SUSPICIOUS`
  - Not found → `EMPTY`
- **AI trigger:** AI retry button visible when any field is EMPTY or SUSPICIOUS; sends both EMPTY+SUSPICIOUS to Haiku

---

### 10. Regex Field Extraction (IBAN, BIC, Amount, Currency, Dates, etc.)

- **Description:** Pattern-matched extraction of invoice fields from markdown text. Supports bank-specific rule sets.
- **Entry point:** `app/extract.py:extract_fields(md, filename)`
- **Key files:** `app/extract.py`, `app/rules/<bank_name>.py` (extensible)
- **Input/Output:**
  - Input: Markdown string, filename (for logging)
  - Output: Dict with keys: `invoice_id`, `receiver`, `amount`, `currency`, `due_date`, `iban`, `bic`, `bankgiro` (always ""), `plusgiro` (always ""), `reference`, `needs_review` (YES|NO), `review_reasons`, `flags`
- **Validations/Edge cases:**
  - Amount: Handles commas/periods as thousands/decimal separators; normalizes to `12.34` format
  - Currency: Matches 3-letter codes (CHF, EUR, USD, GBP, etc.) and symbols ($, €, £)
  - IBAN: Validates MOD-97 checksum; clears field if invalid (unless DEV_MODE)
  - BIC: Basic regex check (8 or 11 chars, specific pattern)
  - Due date: Supports DD.MM.YYYY, DD/MM/YYYY, YYYY-MM-DD, German month names ("5. Oktober 2025")
  - Receiver: Falls back to first non-empty, non-markdown line if pattern doesn't match
  - Reference: Strips whitespace after extraction
  - If mandatory field missing, adds error flag + sets `needs_review=YES`

---

### 11. IBAN MOD-97 Checksum Validation (ISO 13616)

- **Description:** Cryptographic validation of IBAN structure and checksum digits. No external library.
- **Entry point:** `app/extract.py:_validate_iban_checksum(iban)`
- **Key files:** `app/extract.py`
- **Input/Output:**
  - Input: IBAN string (with or without spaces)
  - Output: Boolean (valid checksum = True)
  - Algorithm: Rearrange (move first 4 chars to end) → convert letters to numbers (A=10, B=11, ..., Z=35) → check if `numeric % 97 == 1`
- **Validations/Edge cases:**
  - Rejects IBANs < 5 chars
  - Non-alphanumeric chars cause ValueError → returns False
  - Test cases: CH5604835012345678009 ✓, DE89370400440532013000 ✓, CH9900000000000000000 ✗

---

### 12. Swiss QR-Bill (SPC) Extraction

- **Description:** Scans PDF for Swiss Payments Code QR barcode, decodes SPC format, extracts IBAN/BIC/amount/reference/receiver. Overrides regex extraction when found.
- **Entry point:** `app/qr_swiss.py:extract_from_pdf(pdf_path)`
- **Key files:** `app/qr_swiss.py`, `app/pipeline.py` (calls after regex extraction)
- **Input/Output:**
  - Input: PDF file path
  - Output: Dict with keys `iban`, `bic`, `receiver`, `amount`, `currency`, `reference`, `flags` (or None if no QR found)
  - QR detection: pyzbar (primary) + zxingcpp (fallback)
  - Strategy: Full page scan @ 150/300/400 DPI, then bottom-half crop @ same DPIs (Swiss QR is always bottom)
- **Validations/Edge cases:**
  - If QR found but decode fails, returns None (regex fallback used)
  - If SPC payload malformed, catches exception and logs to stderr
  - zxingcpp imported conditionally (if not available, pyzbar-only mode)
  - Bottom-half crop significantly improves detection on large pages

---

### 13. ISO 20022 pain.001.001.03 XML Export

- **Description:** Converts all "done" jobs with IBAN to ISO 20022 payment initiation XML, grouped by currency (CHF, EUR, cross-border).
- **Entry point:** `app/main.py:GET /download/xml` → `app/xml_export.py:build_pain001(jobs, debtor)`
- **Key files:** `app/xml_export.py`, `app/main.py`
- **Input/Output:**
  - Input: All jobs from DB + debtor company details (name, IBAN, BIC)
  - Output: UTF-8 XML file (`pain001.xml`), compatible with Swiss + EU ebanking portals
  - Debtor details from env vars: `DEBTOR_NAME`, `DEBTOR_IBAN`, `DEBTOR_BIC`
  - Filters: Only jobs with `status=done` and non-empty IBAN exported
- **Validations/Edge cases:**
  - Handles multi-currency batches (splits into separate PmtInf blocks)
  - Service level: CHF → "NURG", EUR → "SEPA", others → "NURG" + "SWIFT" instrument
  - If no payable jobs, returns empty but valid pain.001 structure
  - Amount formatted as `X.XX` (2 decimal places)
  - Control sum calculated for all transactions

---

### 14. CSV Export

- **Description:** Exports all completed jobs as CSV spreadsheet with all billing fields.
- **Entry point:** `app/main.py:GET /download/csv`
- **Key files:** `app/main.py`
- **Input/Output:**
  - Input: All jobs from DB
  - Output: CSV file (`invoices.csv`) with columns: `filename`, `invoice_id`, `receiver`, `iban`, `bic`, `amount`, `currency`, `due_date`, `reference`, `needs_review`, `review_reasons`, `ocr_method`
  - Only jobs with `status=done` included
- **Validations/Edge cases:**
  - CSV uses standard Python csv.DictWriter (dialect: excel)
  - Ignores extra DB columns not in fieldnames list
  - Empty fields rendered as empty strings

---

### 15. Job Lifecycle Management

- **Description:** Jobs transition through states: pending → processing → done/error. Can be reprocessed or deleted individually.
- **Entry point:** 
  - `POST /upload` → creates job (status=pending)
  - `POST /process/{id}` → reprocess single job
  - `DELETE /jobs/{id}` → delete job + files
  - Background task in `_process_job()` → updates status
- **Key files:** `app/main.py`, `app/db.py`, `app/pipeline.py`
- **Input/Output:**
  - Input: Job ID (UUID4 generated on upload)
  - Output: Job record in SQLite with timestamps (created_at, updated_at)
  - Lifecycle: pending → processing (async task) → done (on success) or error (on exception)
- **Validations/Edge cases:**
  - Job state is authoritative source of truth (checked by `/api/jobs` polling)
  - PDF file deleted when job deleted; debug markdown also removed if DEBUG_MD_DIR set
  - Reprocessing existing job re-runs pipeline (can trigger LLM if `force_llm=True`)
  - No job retention policy (manual cleanup required)

---

### 16. Startup Self-Tests (DEV_MODE)

- **Description:** When DEV_MODE=true, container runs 6 test suites before starting server. Failures block startup.
- **Entry point:** `app/main.py` lifespan → `tests.run_startup_tests()`
- **Key files:** `app/tests.py`, `start.sh`
- **Input/Output:**
  - Input: Test data embedded in tests.py
  - Output: Test results logged to stdout; exit code 0 (pass) or 1 (fail)
- **Validations/Edge cases:**
  - T1: IBAN checksum (valid CH/DE, invalid fake)
  - T2: BIC regex pattern (valid 8/11 char, fake patterns)
  - T3: extract_fields structure (invoice_id, amount, currency, IBAN, BIC, legacy fields)
  - T4: pain.001 XML well-formedness + required elements
  - T5: Multi-currency grouping (CHF/EUR separate PmtInf blocks)
  - T6: Skip jobs with missing IBAN (NbOfTxs=0)
  - Failures include detailed reason in output

---

### 17. Debug Markdown Storage (Optional)

- **Description:** If DEBUG_MD_DIR env var set, save extracted markdown to disk for troubleshooting OCR issues.
- **Entry point:** `app/pipeline.py:_save_debug_md()`, called after extraction
- **Key files:** `app/pipeline.py`, `app/main.py` (delete on clear-all)
- **Input/Output:**
  - Input: Job ID, original filename, extracted markdown
  - Output: File at `{DEBUG_MD_DIR}/{job_id}_{filename_stem}.md`
  - Cleanup: Deleted when job deleted or clear-all called
- **Validations/Edge cases:**
  - Optional feature (no-op if DEBUG_MD_DIR not set)
  - Creates directory if missing
  - Overwrites old debug markdown for same job

---

### 18. Bank-Specific Rule Set Selection

- **Description:** Allows operators to switch extraction patterns per bank without code changes via CSV_RULE_SET env var.
- **Entry point:** `app/extract.py:_load_rule_set()`
- **Key files:** `app/extract.py`, `app/rules/<bank_name>.py`
- **Input/Output:**
  - Input: CSV_RULE_SET env var (default: "default")
  - Output: Merged pattern dict (bank overrides + defaults)
  - Example: CSV_RULE_SET=ubs → loads `rules/ubs.py`, overrides specific regex patterns
- **Validations/Edge cases:**
  - Missing rule file → falls back to defaults (no error)
  - Partial overrides supported (only non-default keys needed in rule file)
  - Pattern file must define `PATTERNS = { ... }` dict

---

### 19. Dev-Mode Synthetic Data Fill

- **Description:** When DEV_MODE=true, button to auto-populate all jobs with realistic test invoices (currency-aware, valid IBANs/BICs).
- **Entry point:** [UNCLEAR] — HTML button in dashboard (no explicit API endpoint seen)
- **Key files:** `app/main.py` (likely POST /api/dev-fill-all), `app/templates/index.html`
- **Input/Output:**
  - Input: Click event (button click)
  - Output: JSON with count of jobs filled; updates all jobs with synthetic `receiver`, `iban`, `bic`, `amount`, `currency`, `due_date`, `reference`
- **Validations/Edge cases:**
  - Only enabled if DEV_MODE=true (security)
  - Generates valid MOD-97 IBANs
  - Currency-aware: CHF → BLKB BIC, EUR → PBNKCH BIC (example)
  - Overrides existing extracted data (destructive operation)

---

## Cross-Feature Interactions

### Upload → Process → Review → Export Flow

1. Operator uploads PDF via drag-drop
2. Background task runs `pipeline.run()` → extraction
3. If extraction has error flags, job gets `needs_review=YES`
4. Operator sees job in dashboard, clicks to open review modal
5. Operator edits fields if needed, saves
6. Job status updates to `done`, review flag cleared
7. Operator clicks "Download XML" → pain.001 export includes this job
8. Job persists in DB for audit trail

### LLM Fallback Trigger Chain

1. markitdown extracts markdown from PDF
2. `_is_garbage()` checks if markdown is sparse
3. If sparse, sets `force_llm=True`
4. LLM fallback called → sends PDF pages as PNG to configured provider
5. LLM returns OCR text → fed into extraction
6. `ocr_method` field records which provider was used

### QR Override Merge

1. Regex extraction completes → `fields = extract_fields(md, filename)`
2. QR scan attempted → `qr = qr_swiss.extract_from_pdf(pdf_path)`
3. If QR found, merge logic overwrites regex fields where QR has valid data
4. Merged flags tracked separately → audit trail shows both methods attempted
5. If QR scan failed, error logged but regex results still returned

