# Invoice Processor — Features

---

## 1. Drag-Drop PDF Upload + Immediate QR Scan

**Implemented in:** Step 2 (`app/pipeline.py`), Step 4 (`app/main.py`), Step 6 (`app/templates/index.html`)

**User flow:**
1. Drag PDF onto upload zone
2. `POST /api/upload` → sync QR scan via `qr_swiss.extract_from_pdf()`
3. Fields populated immediately (if QR found) or status set to `needs_llm`
4. Row appears in job table with `qr_done` or `needs_llm` status

**API:** `POST /api/upload` (no BackgroundTask; returns immediately)

---

## 2. Real-Time Job Status Table

**Implemented in:** Step 6 (`app/templates/index.html`), Step 4 (`app/main.py`)

**Display:**
- Columns: Filename, Receiver, Amount, Currency, Due Date, IBAN, Status, Bank Target, Actions
- Status badges (color-coded): `qr_done` (teal), `needs_llm` (amber), `llm_done` (blue), `needs_review` (red), `archived` (gray)
- HTMX polling every 2s to refresh (when jobs in progress)

**API:** `GET /api/jobs` (excludes `archived` by default; add `?include_archived=true` to show all)

---

## 3. Manual LLM Batch Trigger

**Implemented in:** Step 3 (`app/llm.py`), Step 4 (`app/main.py`)

**User flow:**
1. Operator clicks "Run AI Extraction" button (topbar, shown only if `needs_llm` jobs exist)
2. `POST /api/run-llm-batch` → BackgroundTask
3. For each job with `status=needs_llm` (or QR exceptions):
   - `llm.extract_fields(pdf_path)` → Claude Haiku vision API
   - Returns structured JSON dict
   - Merge with existing QR fields (QR wins on conflict)
   - Validate IBAN MOD-97
   - Status → `llm_done` or `needs_review`
4. Table updates via polling

**API:** `POST /api/run-llm-batch`
**Cost:** ~$0.005–$0.01 per invoice (Haiku vision)

---

## 4. Full-Page Edit Modal

**Implemented in:** Step 6 (`app/templates/index.html`)

**Layout:**
- Max-width: 980px, height: calc(100vh - 48px)
- Grid: 1fr 1.8fr (PDF iframe left, form right)
- Form fields: receiver, invoice_id, amount, currency, due_date, iban, bic, reference
- Bank assignment pills: BKB / Raiffeisen / Manual (click to assign)

**User flow:**
1. Click edit icon on a job row
2. Modal opens, PDF visible on left, editable fields on right
3. Make changes, click "Save & Next →" to advance through all unsorted invoices
4. Or click "Close" to discard

**API:** `POST /api/review/{id}` (save edited fields)

---

## 5. Export Screen — Bank Assignment & Download

**Implemented in:** Step 7 (`app/templates/export.html`), Step 4 (`app/main.py`)

**Layout:**
- Three columns (BKB, Raiffeisen, Manual) for drag-board
- Each column shows jobs, grouped by currency (with running amount totals)
- Header buttons: "Export →" (navigates here), back link

**User flow:**
1. Navigate to `/export`
2. Load all non-archived jobs (pre-sorted by `bank_target`)
3. Drag jobs between columns to reassign bank (calls `POST /api/assign-bank/{id}`)
4. Review amounts per currency per column
5. Click "Accept & Download":
   - If any non-archived invoice is `needs_review`/`error`/`LLM-Pending` or missing a
     mandatory pain.001 field (`receiver`, `iban`, `amount`, `currency`) → **popup lists
     the blocking invoices; export is refused (HTTP 409) until they are fixed.**
   - Otherwise → generates pain.001 files, archives sorted jobs, returns zip
6. Toast shows "✓ N invoices archived" then redirects to dashboard

**Export readiness gate:** Incomplete/review-pending invoices **hard-block** export
(payment-correctness). Unrouted-but-complete `MANUAL` invoices are excluded with
confirmation, not blocked. See [[DECISIONS#Export Hard-Blocked on Incomplete Invoices]].

**APIs:**
- `GET /api/jobs` (load jobs)
- `GET /api/export-readiness` (`{ready, blockers[]}` — gate the button + drive popup)
- `POST /api/assign-bank/{id}` (reassign bank)
- `POST /download/confirm` (409 + `blockers[]` if not ready; else generate, archive, zip)

---

## 6. pain.001 XML Export (per bank)

**Implemented in:** Step 5 (`app/xml_export.py`), Step 4 (`app/main.py`)

**Output:** Two separate pain.001 files
- `pain001_BKB.xml` — for BKB jobs (CHF NURG, SEK NURG+SWIFT, EUR SEPA)
- `pain001_Raiffeisen.xml` — for Raiffeisen jobs (USD/CAD/GBP NURG+SWIFT)

**Behavior:**
- Filters jobs by `bank_target` parameter
- Groups PmtInf blocks by currency (ISO 20022 requirement)
- Applies correct service level codes per bank + currency
- Raises HTTP 400 if no jobs match filter (no empty files)

**API:** `POST /download/confirm` (triggered from export screen)

---

## 7. CSV Export

**Implemented in:** Step 4 (`app/main.py`)

**Columns:** Filename, Receiver, IBAN, BIC, Amount, Currency, Due Date, Reference, Invoice ID, Bank Target, Status

**Behavior:**
- Excludes archived jobs (unless manually modified)
- Renders as `.csv` file

**API:** `GET /download/csv`

---

## 8. Job Lifecycle

**State machine:**
```
upload
  ├── QR found → status=qr_done
  │             (bank_target auto-assigned)
  │
  └── no QR   → status=needs_llm
                  ↓ (manual: POST /api/run-llm-batch)
                  ├── all mandatory fields → llm_done
                  └── missing field(s)   → needs_review
                      ↓ (edit modal)
                      → llm_done (if all filled)
                  ↓ (operator assigns bank via export screen)
                  ↓ (operator confirms export)
                → archived
```

**Deletion:** No hard delete. Use `DELETE /api/jobs/{id}` to remove individual jobs (soft: marks as `archived`). Use `DELETE /api/clear-all` to wipe non-archived jobs.

---

## 9. Cross-Feature Flows

### Happy Path: Swiss QR Invoice
1. Drag Swiss QR PDF → upload
2. Status: `qr_done`, fields auto-filled, bank: `BKB`
3. Navigate to `/export`
4. Drag to correct column (if needed)
5. Click "Accept & Download"
6. Zip with pain.001_BKB.xml downloads, job archived

### Happy Path: Non-QR Invoice (Manual Entry)
1. Drag non-QR PDF → upload
2. Status: `needs_llm`
3. Click "Run AI Extraction"
4. LLM extracts fields → `llm_done` or `needs_review`
5. If `needs_review`, click edit modal → fill missing fields → Save
6. Navigate to `/export`
7. Assign bank if needed (MANUAL → correct bank via drag)
8. Download, job archived

---

## 10. Password Protection (HTTP Basic Auth)

**Implemented in:** `app/auth.py`, `app/main.py`

**Behavior:**
- When `APP_PASSWORD` is set, every page + API route requires HTTP Basic credentials
  (username `APP_USERNAME`, default `admin`); the browser shows its native sign-in prompt.
- When `APP_PASSWORD` is unset/empty, auth is disabled (startup warning logged) — local
  dev stays open. Set the env var only on the deployed instance.
- `/static/*` assets stay public; constant-time credential compare.

**Config:** `APP_PASSWORD`, `APP_USERNAME` (see [[PROJECT_CONTEXT#Environment Variables]]).
Rationale: [[DECISIONS#HTTP Basic Authentication (optional, env-gated)]].

---

## 11. LLM Cost Tracker (Analytics)

**Implemented in:** `app/llm.py`, `app/pipeline.py`, `app/cost.py`, `app/main.py`, `app/templates/index.html`

**Behavior:**
- Token usage (`input_tokens`/`output_tokens`) from each Claude call is stored per job
  with the `llm_model` that produced it.
- The Analytics modal shows an **Estimated LLM Cost** section: total USD, total in/out
  tokens, and a per-model breakdown (`/api/analytics` → `cost` block).
- Cost is computed from real token counts via `cost.estimate_cost()`; switching
  `LLM_MODEL` to Sonnet reprices automatically (model-aware pricing table).

**Cost basis:** Haiku 4.5 $1/$5, Sonnet 4.5/4.6 $3/$15 per 1M tokens (in/out).
Rationale: [[DECISIONS#Cost Tracking from Real Token Usage (model-aware)]].

---

## Features NOT Implemented (Out of Scope)

- Rate limiting on `/api/run-llm-batch`
- Archived job audit viewer
- Email notifications
- Webhook integrations
